"""Replay independent 45-minute assessments against archived measurements."""

import argparse
import bisect
import csv
import hashlib
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.analyze_3weeks import epoch, frames, load_series
from zontcontrol.monitor import assess_history
from zontcontrol.detection_profile import SMART_2_HEATING_CONFIG


OFFSETS = (0, 1, 5, 6, 7, 10, 15, 30, 45)
MOSCOW = ZoneInfo("Europe/Moscow")
FEATURES = ("air_deficit", "coolant_mean", "target_mean", "worktime_mean", "coolant_range")
LABELS = {
    "normal": "Н", "warning": "А", "confirmed_pattern": "А+",
    "unknown": "?", "suppressed": "П",
}


def iso(time):
    return datetime.fromtimestamp(time, timezone.utc).isoformat()


def local(time):
    return datetime.fromtimestamp(time, MOSCOW).strftime("%d.%m %H:%M:%S")


def assume_reset_inactive_before(series, start, cutoff):
    """Apply an owner's historical fact without altering recorded archive files."""
    reset = [(time, value) for time, value in series["reset"] if time >= cutoff]
    if start < cutoff:
        reset.insert(0, (start, 0))
        # The supplied fact ends at the cutoff. Missing later recordings must
        # still remain unknown rather than inheriting the assumed off state.
        if not any(time == cutoff for time, value in reset):
            reset.append((cutoff, None))
    return {**series, "reset": sorted(reset)}


def manual_episodes(series):
    result = []
    previous = None
    active = None
    for time, value in series["reset"]:
        if value == 1 and previous == 0:
            active = {"start": time, "end": None}
            result.append(active)
        if value == 0 and previous == 1 and active is not None:
            active["end"] = time
            active = None
        previous = value
    return result


def history_window(series, indexes, end):
    start = end - 45 * 60

    def points(key):
        # Preserve the real timestamp of the last known initial value; never
        # turn an old temperature into a fresh measurement at the window edge.
        lo = bisect.bisect_left(indexes[key], start)
        hi = bisect.bisect_right(indexes[key], end)
        if lo:
            lo -= 1
        return [{"time": iso(t), "value": v} for t, v in series[key][lo:hi]]

    circuit = {"target_temp": "air_target", "setpoint_temp": "coolant_target",
               "worktime": "worktime", "target_sensor_id": "sensor",
               "mode_id": "mode", "status": "status"}
    reset = points("reset")
    return {
        "fetched_at": iso(end),
        "requested_period": {"start": iso(start), "end": iso(end)},
        "devices": [{
            "id": 700877, "time_truncated": False,
            "temperature_sensors": [
                {"id": 8207, "points": points("air")},
                {"id": 8429, "points": points("coolant")},
            ],
            "circuits": [{"id": 20496, "series": {
                name: points(key) for name, key in circuit.items()}}],
            "web_elements": [{"id": 8832, "series": {"state": reset}}] if reset else [],
        }],
    }


def working_candidates(rows, incidents, manual):
    times = [row["time"] for row in rows]
    excluded = [(epoch(item["start"]), epoch(item["recovery"])) for item in incidents]
    excluded += [(item["start"], item["end"] or times[-1]) for item in manual]
    candidates = {"night": [], "day": []}
    for anchor in rows:
        time = anchor["time"]
        if time % 900:
            continue
        lo, hi = time - 45 * 60, time + 45 * 60
        if lo < times[0] or hi > times[-1] or any(lo <= end and hi >= start for start, end in excluded):
            continue
        hour = datetime.fromtimestamp(time, MOSCOW).hour
        group = "night" if 0 <= hour < 6 else "day" if 9 <= hour < 21 else None
        if group is None or datetime.fromtimestamp(hi, MOSCOW).hour >= (6 if group == "night" else 21):
            continue
        window = rows[bisect.bisect_left(times, lo):bisect.bisect_right(times, hi)]
        if len(window) != 91:
            continue
        required = ("air", "coolant", "air_target", "coolant_target", "worktime")
        if any(row["reset"] != 0 or not row["selected_air_sensor"]
               or any(type(row[key]) not in (int, float) or not math.isfinite(row[key]) for key in required)
               for row in window):
            continue
        features = {
            "air_deficit": statistics.mean(row["air_target"] - row["air"] for row in window),
            "coolant_mean": statistics.mean(row["coolant"] for row in window),
            "target_mean": statistics.mean(min(row["coolant_target"], 85) for row in window),
            "worktime_mean": statistics.mean(row["worktime"] for row in window),
            "coolant_range": max(row["coolant"] for row in window) - min(row["coolant"] for row in window),
        }
        if features["worktime_mean"] < 5:
            continue
        # Working reference intervals have measured heating in BOTH halves,
        # selected without looking at any output of the detector under test.
        def has_response(part):
            return any(part[i]["coolant"] >= part[i - 1]["coolant"]
                       and part[i]["coolant"] - min(r["coolant"] for r in part[max(0, i - 5):i + 1]) >= 3
                       for i in range(1, len(part)))
        if not has_response(window[:46]) or not has_response(window[45:]):
            continue
        candidates[group].append({"start": time, "features": features})
    return candidates


def select_working(candidates):
    selected = []
    details = {}
    for group, pool in candidates.items():
        if not pool:
            raise ValueError(f"no working candidates for {group}")
        medians = {key: statistics.median(item["features"][key] for item in pool) for key in FEATURES}
        scales = {}
        for key in FEATURES:
            quartiles = statistics.quantiles([item["features"][key] for item in pool], n=4, method="inclusive")
            scales[key] = max(quartiles[2] - quartiles[0], 1)
        for item in pool:
            item["distance"] = math.sqrt(sum(((item["features"][key] - medians[key]) / scales[key]) ** 2
                                             for key in FEATURES))
        bins = ((0, 2), (2, 4), (4, 6)) if group == "night" else ((9, 13), (13, 17), (17, 21))
        dates = set()
        typical = []
        bin_profiles = []
        for low, high in bins:
            bin_pool = [item for item in pool if low <= datetime.fromtimestamp(item["start"], MOSCOW).hour < high]
            center = {key: statistics.median(item["features"][key] for item in bin_pool) for key in FEATURES}
            eligible = [item for item in bin_pool if datetime.fromtimestamp(item["start"], MOSCOW).date() not in dates]
            distance = lambda item: math.sqrt(sum(((item["features"][key] - center[key]) / scales[key]) ** 2
                                                  for key in FEATURES))
            choice = min(eligible, key=lambda item: (distance(item), item["start"]))
            typical.append({**choice, "distance_to_time_bin": distance(choice)})
            bin_profiles.append({"hours": [low, high], "medians": center, "candidates": len(bin_pool)})
            dates.add(datetime.fromtimestamp(choice["start"], MOSCOW).date())
        unusual = []
        for item in sorted(pool, key=lambda item: (-item["distance"], item["start"])):
            date = datetime.fromtimestamp(item["start"], MOSCOW).date()
            if date in dates or any(abs(item["start"] - chosen["start"]) < 90 * 60 for chosen in unusual):
                continue
            unusual.append(item)
            dates.add(date)
            if len(unusual) == 3:
                break
        if len(unusual) != 3:
            raise ValueError(f"not enough distinct working dates for {group}")
        for kind, chosen in (("typical", typical), ("unusual", unusual)):
            for number, item in enumerate(chosen, 1):
                selected.append({"id": f"{'Н' if group == 'night' else 'Д'}{'Т' if kind == 'typical' else 'О'}{number}",
                                 "kind": f"{group}_{kind}", **item})
        details[group] = {"candidate_count": len(pool), "medians": medians, "scales_iqr": scales,
                          "typical_time_bins": bin_profiles}
    return selected, details


def evaluate(episodes, series, rows):
    indexes = {key: [t for t, v in points] for key, points in series.items()}
    row_times = [row["time"] for row in rows]
    checks = []
    for episode in episodes:
        for offset in OFFSETS:
            end = episode["start"] + offset * 60
            history = history_window(series, indexes, end)
            # A fresh detector is constructed by assess_history on EVERY call.
            result = assess_history(history, circuit_id=20496)
            assert not result["automatic_control"]
            for device in history["devices"]:
                for sensor in device["temperature_sensors"]:
                    assert all(epoch(point["time"]) <= end for point in sensor["points"])
                for item in device["circuits"] + device["web_elements"]:
                    assert all(epoch(point["time"]) <= end for points in item["series"].values() for point in points)
            row = rows[bisect.bisect_right(row_times, end) - 1]
            checks.append({
                "episode_id": episode["id"], "kind": episode["kind"], "start": iso(episode["start"]),
                "offset_minutes": offset, "time": iso(end), "local_time": local(end),
                "anomaly": result["anomaly"], "state": result["state_at_period_end"],
                "reason": result["decision_reason"], "reset_active": result["reset_active_at_period_end"],
                "air": row["air"], "air_target": row["air_target"],
                "coolant": row["coolant"], "coolant_target": row["coolant_target"],
                "worktime": row["worktime"], "valid_samples": result["valid_temperature_samples"],
                "events": result["events"],
            })
    return checks


def write_reports(path, report):
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    flat = [{key: value for key, value in row.items() if key != "events"} for row in report["checks"]]
    with path.with_suffix(".csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(flat[0]))
        writer.writeheader()
        writer.writerows(flat)
    lines = ["Разовые проверки отопления за 21 сутки", "", report["method"], "",
             "Н — нет текущих признаков; А — аномалия (warning); А+ — confirmed_pattern;",
             "П — намеренное отключение/пауза; ? — недостаточно данных.", "",
             "| ID | Начало, Москва | Отключение, с | " + " | ".join(f"+{m}" for m in OFFSETS) + " |",
             "|---|---|---|" + "---|" * len(OFFSETS)]
    for episode in report["episodes"]:
        checks = [row for row in report["checks"] if row["episode_id"] == episode["id"]]
        duration = episode.get("duration_seconds", "—")
        lines.append(f"| {episode['id']} | {local(episode['start'])} | {duration} | "
                     + " | ".join(LABELS[row["state"]] for row in checks) + " |")
    lines += ["", "Рабочие интервалы и признаки отбора", "",
              "| ID | Начало, Москва | Воздух: дефицит, °C | Вода: средняя, °C | Цель воды: средняя, °C | worktime: среднее, с/мин | Размах воды, °C | Расстояние от типичного |",
              "|---|---|---|---|---|---|---|---|"]
    for episode in report["episodes"]:
        if "features" in episode:
            lines.append(f"| {episode['id']} | {local(episode['start'])} | "
                         + " | ".join(f"{episode['features'][key]:.2f}" for key in FEATURES)
                         + f" | {episode['distance']:.2f} |")
    lines += ["", "Причины решений", ""]
    for row in report["checks"]:
        lines.append(f"{row['episode_id']} +{row['offset_minutes']}: {row['local_time']} — "
                     f"{row['state']}, {row['reason']}; A={row['air']}/{row['air_target']}, "
                     f"W={row['coolant']}/{row['coolant_target']}, reset={row['reset_active']}")
    path.with_suffix(".md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("reports/history-3w"))
    parser.add_argument("--output", type=Path, default=Path("reports/one-shot-validation-3w.json"))
    parser.add_argument("--reset-inactive-before", help="Owner-confirmed inactive reset before an RFC 3339 timestamp")
    parser.add_argument("--reuse-selection", type=Path, help="Reassess the same episodes from an existing validation report")
    args = parser.parse_args()
    manifest, series = load_series(args.input, 20496, 8207, 8429, 8832)
    if any(chunk["time_truncated"] for chunk in manifest["chunks"]):
        raise ValueError("validation requires complete archived chunks")
    assumption = None
    if args.reset_inactive_before:
        cutoff = datetime.fromisoformat(args.reset_inactive_before)
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("reset cutoff must include a timezone")
        series = assume_reset_inactive_before(series, epoch(manifest["start"]), int(cutoff.timestamp()))
        assumption = {"source": "owner", "reset_id": 8832, "state": 0,
                      "before_exclusive": cutoff.isoformat(),
                      "reason": "Переключателя до этого времени не существовало; сброс был выключен."}
    previous = json.loads((args.input.parent / "anomalies-3w.json").read_text())
    incidents = previous["incidents"]
    rows = frames(manifest, series, 8207)
    manual = manual_episodes(series)
    reuse = json.loads(args.reuse_selection.read_text()) if args.reuse_selection else None
    if reuse is not None:
        if reuse["period"] != {"start": manifest["start"], "end": manifest["end"]}:
            raise ValueError("reused selection must cover the same archive period")
        working = [episode for episode in reuse["episodes"] if episode["kind"] not in ("anomaly", "manual")]
        selection = reuse["selection"]
    else:
        working, selection = select_working(working_candidates(rows, incidents, manual))
    episodes = [{"id": f"А{i}", "kind": "anomaly", "start": epoch(item["start"])}
                for i, item in enumerate(incidents, 1)]
    episodes += [{"id": f"Р{i:02}", "kind": "manual", "start": item["start"], "end": item["end"],
                  "duration_seconds": item["end"] - item["start"] if item["end"] else None}
                 for i, item in enumerate(manual, 1)]
    episodes += working
    checks = evaluate(episodes, series, rows)
    report = {
        "period": {"start": manifest["start"], "end": manifest["end"]},
        "reset_assumption": assumption, "selection_reused": reuse is not None,
        "offsets_minutes": OFFSETS,
        "detection_config": asdict(SMART_2_HEATING_CONFIG),
        "method": "Каждая точка — независимый assess_history на предшествующих 45 минутах. "
                  "Будущие данные не передаются детектору. Начала аномалий — ранее найденные начала "
                  "наблюдаемых условий, не известные моменты аппаратного отказа. Ручной эпизод — "
                  "каждое реальное включение переключателя 8832, без объединения импульсов. "
                  "Рабочие окна: 45 минут до и после точки, пригодные данные, известный выключенный сброс, "
                  "наблюдаемый нагрев в обеих половинах; без пересечения найденных аномалий и сбросов. "
                  "Типичные — ближайшие к медианному профилю своего интервала времени суток "
                  "в трёх интервалах на разных датах. "
                  "Отличающиеся — три наиболее далёких профиля на других разных датах. "
                  "Расстояние: евклидово по пяти признакам, масштабированным на IQR (минимум 1). "
                  "Отбор рабочих окон не использует результаты проверяемого алгоритма. "
                  "Сохраняется последний известный начальный отсчёт каждого ряда с его реальным временем. "
                  "Задержка облачной публикации по архиву не восстанавливается.",
        "selection": selection, "episodes": episodes, "checks": checks,
        "source_sha256": {chunk["file"]: hashlib.sha256((args.input / chunk["file"]).read_bytes()).hexdigest()
                          for chunk in manifest["chunks"]},
        "implementation_sha256": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
                                  for name in ("zontcontrol/anomaly.py", "zontcontrol/monitor.py", "zontcontrol/detection_profile.py")},
    }
    if assumption:
        report["method"] += (f" По уточнению владельца до {local(int(cutoff.timestamp()))} "
                             "по Москве сброс всегда выключен: переключателя ещё не существовало. "
                             "После этой границы используются фактические записи. "
                             "Исходные архивные файлы не изменены.")
    if reuse is not None:
        report["method"] += " Рабочие участки сохранены из предыдущего прогона для сопоставимости результатов."
        old = {(row["episode_id"], row["offset_minutes"]): row for row in reuse["checks"]}
        report["decision_changes"] = [{"episode_id": row["episode_id"], "offset_minutes": row["offset_minutes"],
                                        "previous_state": old[(row["episode_id"], row["offset_minutes"])]["state"],
                                        "current_state": row["state"]}
                                       for row in checks if row["state"] != old[(row["episode_id"], row["offset_minutes"])]["state"]]
    write_reports(args.output, report)
    print(json.dumps({"episodes": len(episodes), "manual": len(manual), "working": len(working),
                      "checks": len(checks), "files": [str(args.output.with_suffix(ext)) for ext in (".json", ".csv", ".md")]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
