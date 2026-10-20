"""Frozen-profile validation on a separate archive using independent one-shot runs."""

import argparse
import bisect
import csv
import gzip
import hashlib
import json
import math
import sys
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.analyze_3weeks import epoch, frames, load_series
from scripts.validate_one_shot import assume_reset_inactive_before, heating_window
from zontcontrol.anomaly import HeatingSample, NoHeatDetector
from zontcontrol.detection_profile import SMART_2_HEATING_CONFIG
from zontcontrol.history import history_period
from zontcontrol.monitor import assess_history


MOSCOW = ZoneInfo("Europe/Moscow")


def iso(time):
    return datetime.fromtimestamp(time, timezone.utc).isoformat()


def local(time):
    return datetime.fromtimestamp(time, MOSCOW).strftime("%d.%m.%Y %H:%M")


def frame_samples(rows):
    return [HeatingSample(datetime.fromtimestamp(row["time"], timezone.utc), row["air"], row["air_target"],
                          row["coolant"], row["coolant_target"], row["worktime"],
                          bool(row["reset"]) if row["reset"] in (0, 1) else None,
                          row["selected_air_sensor"],
                          datetime.fromtimestamp(row["reset_changed_at"], timezone.utc)
                          if row["reset_changed_at"] is not None else None) for row in rows]


def assess_samples(samples, config, *, truncated=False):
    detector = NoHeatDetector(config, require_known_reset=True)
    for sample in samples:
        detector.update(sample)
    sample = samples[-1]
    values = (sample.air, sample.air_target, sample.coolant, sample.coolant_target, sample.worktime)
    usable = (sample.selected_air_sensor
              and all(type(value) in (int, float) and math.isfinite(value) for value in values)
              and 0 <= sample.worktime <= 60)
    grace = (sample.reset_changed_at is not None
             and (sample.time - sample.reset_changed_at).total_seconds() < config.reset_grace_minutes * 60)
    state, reason = detector.level, "current_pattern" if detector.level != "normal" else "no_current_pattern"
    if truncated:
        state, reason = "unknown", "truncated_history"
    elif sample.reset_active is None:
        state, reason = "unknown", "reset_state_unknown"
    elif sample.reset_active:
        state, reason = "suppressed", "intentional_shutdown"
    elif grace:
        state, reason = "suppressed", "restart_grace"
    elif not usable or len(detector.samples) < 5:
        state, reason = "unknown", "insufficient_or_invalid_telemetry"
    return state, reason, detector


def attach_stats(segment, rows, times, strong_heat):
    selected = rows[bisect.bisect_left(times, segment["first_time"]):bisect.bisect_right(times, segment["last_time"])]
    segment["first"] = iso(segment.pop("first_time"))
    segment["last"] = iso(segment.pop("last_time"))
    segment["first_moscow"] = local(epoch(segment["first"]))
    segment["last_moscow"] = local(epoch(segment["last"]))
    segment["duration_minutes"] = len(selected)
    segment["air_min"] = min(row["air"] for row in selected)
    segment["air_max"] = max(row["air"] for row in selected)
    segment["max_air_deficit"] = max(row["air_target"] - row["air"] for row in selected)
    segment["coolant_min"] = min(row["coolant"] for row in selected)
    segment["coolant_max"] = max(row["coolant"] for row in selected)
    segment["target_min"] = min(row["coolant_target"] for row in selected)
    segment["target_max"] = max(row["coolant_target"] for row in selected)
    segment["mean_worktime"] = sum(row["worktime"] for row in selected) / len(selected)
    index = bisect.bisect_right(strong_heat, epoch(segment["last"]))
    recovery = strong_heat[index] if index < len(strong_heat) else None
    segment["next_strong_response"] = iso(recovery) if recovery else None
    segment["minutes_until_next_strong_response"] = (recovery - epoch(segment["last"])) / 60 if recovery else None
    segment["recorded_temperatures_at_first"] = {key: selected[0][key]
                                                for key in ("air", "air_target", "coolant", "coolant_target", "worktime")}
    return segment


def run(manifest, series, start, end, output):
    config = SMART_2_HEATING_CONFIG
    rows = frames(manifest, series, 8207)
    times = [row["time"] for row in rows]
    samples = frame_samples(rows)
    truncated = [(epoch(chunk["start"]), epoch(chunk["end"]))
                 for chunk in manifest["chunks"] if chunk["time_truncated"]]
    counts, reasons = Counter(), Counter()
    segments = []
    active = None
    checks_to_verify = set()
    totals_by_day = {}
    strong_heat = []
    for i in range(5, len(rows)):
        window = rows[i - 5:i + 1]
        if all(row["coolant"] is not None for row in window):
            value = window[-1]["coolant"]
            if value >= window[-2]["coolant"] and value - min(row["coolant"] for row in window) >= 5:
                strong_heat.append(rows[i]["time"])
    minute_path = output.with_name(output.stem + "-minutes.csv.gz")
    with gzip.open(minute_path, "wt", encoding="utf-8", newline="") as stream:
        fields = ("time", "state", "reason", "air", "air_target", "coolant", "coolant_target", "worktime",
                  "reset", "evidence_since", "absence_minutes")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for i, row in enumerate(rows):
            time = row["time"]
            if not start < time <= end:
                continue
            if i < 44:
                state, reason, detector = "unknown", "missing_initial_window", None
            else:
                incomplete = any(time >= low and time - 2700 <= high for low, high in truncated)
                state, reason, detector = assess_samples(samples[i - 44:i + 1], config, truncated=incomplete)
            counts[state] += 1
            reasons[reason] += 1
            day = datetime.fromtimestamp(time, MOSCOW).strftime("%Y-%m-%d")
            totals_by_day.setdefault(day, Counter())[state] += 1
            evidence = detector.evidence_since if detector else None
            absence = ((samples[i].time - max(detector.last_pulse, evidence)).total_seconds() / 60
                       if evidence and detector.last_pulse else None)
            writer.writerow({"time": iso(time), "state": state, "reason": reason,
                             **{key: row[key] for key in ("air", "air_target", "coolant", "coolant_target", "worktime", "reset")},
                             "evidence_since": evidence.isoformat() if evidence else None, "absence_minutes": absence})
            if state == "confirmed_pattern":
                if active is None:
                    active = {"first_time": time, "last_time": time, "end_reason": None,
                              "evidence_since_at_first": evidence.isoformat() if evidence else None}
                    segments.append(active)
                    checks_to_verify.add(time)
                else:
                    active["last_time"] = time
            elif active is not None:
                active["end_reason"] = reason
                checks_to_verify.update((active["last_time"], time))
                active = None
            if time % 86400 == 0:
                print(f"Checked through {local(time)}; {sum(counts.values())} one-shot runs", flush=True)
    segments = [attach_stats(segment, rows, times, strong_heat) for segment in segments]
    # Compare different input policies at every
    # confirmed boundary and regularly across the rest of the period.
    checks_to_verify.update(time for time in times if start < time <= end and time % 21600 == 0)
    indexes = {key: [time for time, value in points] for key, points in series.items()}
    boundary_compared = 0
    input_policy_differences = []
    for time in sorted(checks_to_verify):
        i = bisect.bisect_left(times, time)
        if i < 44:
            continue
        incomplete = any(time >= low and time - 2700 <= high for low, high in truncated)
        expected, _, _ = assess_samples(samples[i - 44:i + 1], config, truncated=incomplete)
        history = replace(heating_window(series, indexes, time), time_truncated=incomplete)
        actual = assess_history(history, config=config)
        if expected != actual["state_at_period_end"]:
            input_policy_differences.append({
                "time": iso(time), "frames_state": expected,
                "normalized_history_state": actual["state_at_period_end"]})
        boundary_compared += 1
    report = {
        "period": {"start": iso(start), "end": iso(end), "start_moscow": local(start), "end_moscow": local(end)},
        "collected_period": {"start": manifest["start"], "end": manifest["end"]},
        "detection_config": asdict(config), "parameters_retuned": False,
        "chunks": len(manifest["chunks"]), "truncated_chunks": len(truncated),
        "minute_decisions": dict(counts), "decision_reasons": dict(reasons),
        "confirmed_segments": segments, "confirmed_segment_count": len(segments),
        "segment_length_counts": dict(Counter("1-5" if s["duration_minutes"] <= 5 else "6-30"
                                              if s["duration_minutes"] <= 30 else ">30" for s in segments)),
        "daily_decisions": {day: dict(value) for day, value in totals_by_day.items()},
        "input_policy_comparisons": boundary_compared,
        "input_policy_differences": input_policy_differences,
        "minute_input_policy": "legacy_frames",
        "implementation_sha256": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
                                  for name in ("zontcontrol/anomaly.py", "zontcontrol/monitor.py", "zontcontrol/detection_profile.py")},
        "method": "Минутные решения и сегменты используют прежние frames и новый детектор на предыдущих 45 минутах. "
                  "Данные после момента решения не используются. Параметры фиксированы до загрузки нового периода. "
                  "Сегмент — последовательные минуты confirmed_pattern; разрыв завершает сегмент, "
                  "но не доказывает восстановление. Возврат нагрева: последующий рост воды >=5 °C за <=5 минут. "
                  "Сегменты являются сигналами алгоритма, а не подтверждёнными аппаратными неисправностями. "
                  "Метки истинных отказов для этого периода не предоставлены. Задержка публикации облака не восстанавливается.",
    }
    return report


def write_report(path, report):
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    segments = report["confirmed_segments"]
    if segments:
        with path.with_name(path.stem + "-segments.csv").open("w", encoding="utf-8", newline="") as stream:
            flat = [{key: value for key, value in item.items() if key != "recorded_temperatures_at_first"} for item in segments]
            writer = csv.DictWriter(stream, fieldnames=list(flat[0]))
            writer.writeheader()
            writer.writerows(flat)
    lines = ["Проверка фиксированного профиля на предшествующих двух месяцах", "",
             f"Период: {report['period']['start_moscow']} — {report['period']['end_moscow']} (Москва).", "",
             report["method"], "", f"Решения по минутам: {report['minute_decisions']}.",
             f"Подтверждённых сегментов: {report['confirmed_segment_count']}.", ""]
    if 'input_policy_comparisons' in report:
        lines += [f"Сравнений frames с нормализованной историей: {report['input_policy_comparisons']}; "
                  f"различий: {len(report['input_policy_differences'])}.", ""]
    else:
        # --summarize-only can still render a stored report of the old replay.
        lines += [f"Сравнений в историческом отчёте: {report['client_boundary_crosschecks']}.", ""]
    if "signal_groups" in report:
        lines += [f"До измеренного возврата нагрева объединено в {len(report['signal_groups'])} групп: "
                  f"{report['long_group_count']} длительных и {report['other_group_count']} остальных.",
                  f"Кандидатов на короткие циклические срабатывания: {report['short_cycle_candidate_count']}. "
                  "Они требуют проверки по графикам; число срабатываний не равно числу аппаратных отказов.", "",
                  "Длительные температурные эпизоды (кандидаты, аппаратная причина не установлена):", "",
                  "| Первое подтверждение, Москва | Измеренный возврат нагрева, Москва | Минут до нагрева | Минимум воздуха, °C | Минимум воды, °C | Максимальный недогрев воздуха, °C |",
                  "|---|---|---|---|---|---|"]
        for group in report["signal_groups"]:
            if group["long_pattern"]:
                lines.append(f"| {group['first_moscow']} | {group['recovery_moscow']} | "
                             f"{group['minutes_until_heat_returned']} | {group['air_min']:.1f} | "
                             f"{group['coolant_min']:.1f} | {group['max_air_deficit']:.1f} |")
        lines += ["", f"Причины неопределённых решений: {report['unknown_breakdown']}.", ""]
        if report.get("normal_cycle_example"):
            example = report["normal_cycle_example"]
            lines += [f"Пример короткого сигнала: {example['signal_time_moscow']}, "
                      f"вода {example['coolant_at_signal']:.1f} °C при цели ZONT {example['target_at_signal']:.1f} °C; "
                      f"в {example['heat_time_moscow']} вода уже {example['coolant_at_heat']:.1f} °C. "
                      "Такой сигнал похож на обычный цикл охлаждения/нагрева.", ""]
    lines += ["Все подтверждённые сегменты:", "",
             "| № | Первое подтверждение, Москва | Последнее подтверждение, Москва | Минут | Минимум воздуха, °C | Максимальный недогрев воздуха, °C | Вода: минимум—максимум, °C | Цель воды: минимум—максимум, °C | Нагрев после последнего сигнала, мин |",
             "|---|---|---|---|---|---|---|---|---|"]
    for i, segment in enumerate(segments, 1):
        recovery = segment["minutes_until_next_strong_response"]
        lines.append(f"| {i} | {segment['first_moscow']} | {segment['last_moscow']} | {segment['duration_minutes']} | "
                     f"{segment['air_min']:.1f} | {segment['max_air_deficit']:.1f} | "
                     f"{segment['coolant_min']:.1f}—{segment['coolant_max']:.1f} | "
                     f"{segment['target_min']:.1f}—{segment['target_max']:.1f} | {recovery if recovery is not None else 'нет данных'} |")
    path.with_suffix(".md").write_text("\n".join(lines) + "\n")


def enrich_report(report, manifest, series, output):
    """Describe signals after replay; grouping does not affect any decisions."""
    rows = frames(manifest, series, 8207)
    times = [row["time"] for row in rows]
    groups = []
    for number, segment in enumerate(report["confirmed_segments"], 1):
        first, last = epoch(segment["first"]), epoch(segment["last"])
        recovery = epoch(segment["next_strong_response"]) if segment["next_strong_response"] else None
        if groups and groups[-1]["recovery_time"] is not None and first <= groups[-1]["recovery_time"]:
            group = groups[-1]
            group["segments"].append(number)
            group["last_time"], group["recovery_time"] = last, recovery
        else:
            group = {"first_time": first, "last_time": last, "recovery_time": recovery, "segments": [number]}
            groups.append(group)
    for group in groups:
        first, last, recovery = group.pop("first_time"), group.pop("last_time"), group.pop("recovery_time")
        selected = rows[bisect.bisect_left(times, first):bisect.bisect_right(times, recovery or last)]
        valid = [row for row in selected if all(row[key] is not None for key in ("air", "air_target", "coolant"))]
        group.update({"first": iso(first), "last": iso(last), "recovery": iso(recovery) if recovery else None,
                      "first_moscow": local(first), "last_moscow": local(last),
                      "recovery_moscow": local(recovery) if recovery else None,
                      "minutes_until_heat_returned": (recovery - first) / 60 if recovery else None,
                      "confirmed_minutes": sum(report["confirmed_segments"][i - 1]["duration_minutes"] for i in group["segments"]),
                      "long_pattern": any(report["confirmed_segments"][i - 1]["duration_minutes"] > 30 for i in group["segments"]),
                      "air_min": min(row["air"] for row in valid),
                      "max_air_deficit": max(row["air_target"] - row["air"] for row in valid),
                      "coolant_min": min(row["coolant"] for row in valid)})
    report["signal_groups"] = groups
    report["long_group_count"] = sum(group["long_pattern"] for group in groups)
    report["other_group_count"] = sum(not group["long_pattern"] for group in groups)
    short_cycles = [group for group in groups if not group["long_pattern"] and group["confirmed_minutes"] <= 6
                    and group["recovery"] is not None and epoch(group["recovery"]) - epoch(group["last"]) <= 120]
    report["short_cycle_candidate_count"] = len(short_cycles)
    example_group = next((group for group in short_cycles if group["coolant_min"] >= 50), None)
    if example_group:
        signal = rows[bisect.bisect_left(times, epoch(example_group["last"]))]
        heat = rows[bisect.bisect_left(times, epoch(example_group["recovery"]))]
        report["normal_cycle_example"] = {"signal_time_moscow": local(signal["time"]),
                                           "coolant_at_signal": signal["coolant"],
                                           "target_at_signal": signal["coolant_target"],
                                           "heat_time_moscow": local(heat["time"]),
                                           "coolant_at_heat": heat["coolant"]}
    unknown_reasons = Counter()
    unknown_runs = []
    active = None
    minute_path = output.with_name(output.stem + "-minutes.csv.gz")
    with gzip.open(minute_path, "rt", encoding="utf-8") as stream:
        for decision in csv.DictReader(stream):
            if decision["state"] != "unknown":
                active = None
                continue
            time = epoch(decision["time"])
            row = rows[bisect.bisect_left(times, time)]
            if row["air"] is None or row["coolant"] is None:
                unknown_reasons["missing_or_stale_temperatures"] += 1
            elif not row["selected_air_sensor"]:
                unknown_reasons["different_or_unknown_regulation_sensor"] += 1
            elif row["worktime"] is not None and not 0 <= row["worktime"] <= 60:
                unknown_reasons["worktime_outside_supported_range"] += 1
            elif any(row[key] is None for key in ("air_target", "coolant_target", "worktime")):
                unknown_reasons["missing_circuit_state"] += 1
            else:
                unknown_reasons["insufficient_window_after_invalid_data"] += 1
            if active is None:
                active = {"first": decision["time"], "last": decision["time"], "minutes": 1}
                unknown_runs.append(active)
            else:
                active["last"] = decision["time"]
                active["minutes"] += 1
    report["unknown_breakdown"] = dict(unknown_reasons)
    report["unknown_intervals"] = unknown_runs
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("reports/history-prior-2months"))
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--reset-inactive-before", required=True)
    parser.add_argument("--output", type=Path, default=Path("reports/profile-prior-2months.json"))
    parser.add_argument("--summarize-only", action="store_true", help="Describe an existing frozen replay without repeating it")
    args = parser.parse_args()
    start, end = history_period(datetime.fromisoformat(args.start), datetime.fromisoformat(args.end))
    manifest, series = load_series(args.input, 20496, 8207, 8429, 8832)
    if epoch(manifest["start"]) > int(start.timestamp()) - 2760 or epoch(manifest["end"]) < int(end.timestamp()):
        raise ValueError("collect the full requested period plus 46 minutes of preceding history")
    if not manifest["chunks"] or epoch(manifest["chunks"][-1]["end"]) < int(end.timestamp()):
        raise ValueError("archive collection is not complete")
    series = assume_reset_inactive_before(series, epoch(manifest["start"]), epoch(args.reset_inactive_before))
    if args.summarize_only:
        report = enrich_report(json.loads(args.output.read_text()), manifest, series, args.output)
        write_report(args.output, report)
        print(json.dumps({key: report[key] for key in ("long_group_count", "other_group_count", "unknown_breakdown")}, ensure_ascii=False))
        return
    report = run(manifest, series, int(start.timestamp()), int(end.timestamp()), args.output)
    report["source_sha256"] = {chunk["file"]: hashlib.sha256((args.input / chunk["file"]).read_bytes()).hexdigest()
                                for chunk in manifest["chunks"]}
    report["reset_assumption"] = {"source": "owner", "state": 0, "before_exclusive": args.reset_inactive_before,
                                  "reason": "Переключателя тогда не существовало."}
    report = enrich_report(report, manifest, series, args.output)
    write_report(args.output, report)
    print(json.dumps({key: report[key] for key in ("period", "minute_decisions", "confirmed_segment_count",
                                                  "segment_length_counts", "input_policy_comparisons",
                                                  "input_policy_differences")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
