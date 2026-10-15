"""Validate the installed profile by independent 45-point archive replays."""

import bisect
import csv
import gzip
import hashlib
import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.analyze_3weeks import epoch
from scripts.backtest_profile import assess_samples, iso, local
from scripts.recalibrate_archive import ROOT, load_dataset
from scripts.validate_one_shot import history_window
from zontcontrol.detection_profile import SMART_2_HEATING_CONFIG
from zontcontrol.monitor import assess_history


def main():
    manifest, series, rows, samples, labels, positive, targets, selected, sources = load_dataset()
    config = SMART_2_HEATING_CONFIG
    start, end = epoch(manifest["analysis_start"]), epoch(manifest["end"])
    counts, outside, reasons, july22 = Counter(), Counter(), Counter(), Counter()
    states, firsts, checks = {}, [None] * len(labels), set()
    times = [row["time"] for row in rows]
    previous = None
    output = ROOT / "reports/recalibration-validation-83days.json"
    with gzip.open(output.with_name(output.stem + "-minutes.csv.gz"), "wt", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("time", "state", "reason", "label", "air", "air_target", "coolant", "coolant_target", "worktime", "reset"))
        for i, row in enumerate(rows):
            time = row["time"]
            if not start < time <= end:
                continue
            state, reason, detector = assess_samples(samples[i - 44:i + 1], config)
            states[time] = state
            counts[state] += 1
            reasons[reason] += 1
            label = positive[i]
            if label < 0:
                outside[state] += 1
            elif state == "confirmed_pattern" and firsts[label] is None:
                firsts[label] = time
            if local(time).startswith("22.07.2026"):
                july22[state] += 1
            if state != previous:
                checks.update((time - 60, time))
            previous = state
            if time % 21600 == 0:
                checks.add(time)
            writer.writerow((iso(time), state, reason, labels[label]["id"] if label >= 0 else "",
                             *(row[key] for key in ("air", "air_target", "coolant", "coolant_target", "worktime", "reset"))))
            if time % 604800 == 0:
                print(f"Validated {sum(counts.values())} independent windows through {local(time)}", flush=True)
    offsets = (0, 1, 5, 6, 7, 10, 15, 30, 45)
    for label in labels:
        checks.update(label["start"] + offset * 60 for offset in offsets)
    indexes = {key: [time for time, value in points] for key, points in series.items()}
    verified = 0
    for time in sorted(checks):
        if time not in states:
            continue
        packet = history_window(series, indexes, time)
        actual = assess_history(packet, circuit_id=20496, config=config)
        assert actual["state_at_period_end"] == states[time], (iso(time), states[time], actual)
        verified += 1
    failures = []
    for label, first in zip(labels, firsts):
        failures.append({**label, "start_moscow": local(label["start"]), "end_moscow": local(label["end"]),
                         "first_confirmation": iso(first) if first else None,
                         "first_confirmation_moscow": local(first) if first else None,
                         "delay_minutes": (first - label["start"]) / 60 if first else None,
                         "offset_states": {str(offset): states.get(label["start"] + offset * 60, "outside_period")
                                           for offset in offsets}})
    old = json.loads((ROOT / "reports/profile-prior-2months.json").read_text())
    shorts = []
    for group in old["signal_groups"]:
        if group["long_pattern"]:
            continue
        low, high = epoch(group["first"]) - 900, epoch(group["recovery"]) + 900
        decisions = Counter(state for time, state in states.items() if low <= time <= high and positive[bisect.bisect_left(times, time)] < 0)
        owner = local(epoch(group["first"])).startswith(("22.07.2026", "15.08.2026"))
        shorts.append({"first_moscow": local(epoch(group["first"])), "recovery_moscow": local(epoch(group["recovery"])),
                       "classification": "normal_cycle_or_recovery", "provenance": "owner" if owner else "temperature_pattern_inference",
                       "review_window_states": dict(decisions)})
    report = {"period": {"start": iso(start), "end": iso(end), "start_moscow": local(start), "end_moscow": local(end)},
              "config": asdict(config), "minute_count": sum(counts.values()), "minute_decisions": dict(counts),
              "outside_known_failures": dict(outside), "decision_reasons": dict(reasons), "july22": dict(july22),
              "failures": failures, "short_cycles": shorts, "client_crosschecks": verified,
              "implementation_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                         for name in ("zontcontrol/anomaly.py", "zontcontrol/monitor.py", "zontcontrol/detection_profile.py")},
              "source_sha256": sources,
              "method": "Каждая минута: новый детектор, только предыдущие 45 минут. До 27.09 23:36 Москва сброс OFF. "
                        "Вне известных отказов — отрицательные ограничения, а не полный аппаратный журнал. "
                        "Профиль подобран на этом архиве; это не независимая будущая выборка. "
                        "Даты начала по наблюдаемым признакам могут быть позже физического отказа. "
                        "Задержка публикации истории облаком не учитывается."}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    assert report["minute_count"] == 119520
    assert all(first is not None for first in firsts)
    assert outside["confirmed_pattern"] == 0, dict(outside)
    calibration = json.loads((ROOT / "reports/recalibration-83days.json").read_text())
    assert asdict(config) == calibration["selected"]["config"]
    assert firsts == calibration["selected"]["first_confirmations"]
    lines = ["# Повторная калибровка ZONT", "", report["method"], "",
             f"Период (Москва): {local(start)} — {local(end)}. {sum(counts.values())} независимых решений.",
             f"Состояния: {dict(counts)}. Вне известных отказов: {dict(outside)}.",
             f"22 июля: {dict(july22)}. Сравнений с клиентом assess_history: {verified}.", "",
             "Порог воды 4 °C; worktime >=40 с/мин за 5 точек; отклик +3 °C от минимума за 5 минут "
             "или +1 °C относительно начала окна на восходящей фазе. Подтверждение через 13 минут "
             "при воде <=60 °C, через 14 минут выше 60 °C; воздух <= цели ИЛИ вода остыла >=3 °C. "
             "Стабильная горячая вода исключается; сброс подавляет решение и даёт 5 минут на запуск.", "",
             "| Эпизод | Начало, Москва | Первое подтверждение | Минут | Источник начала |", "|---|---|---|---:|---|"]
    for failure in failures:
        lines.append(f"| {failure['id']} | {failure['start_moscow']} | {failure['first_confirmation_moscow']} | {failure['delay_minutes']:g} | {failure['start_source']} |")
    lines += ["", "Нулевая задержка 15 августа означает, что предыдущие 45 минут уже содержали признаки. "
              "Она не означает мгновенного обнаружения физического отказа.", "",
              "| Эпизод | +0 | +1 | +5 | +6 | +7 | +10 | +15 | +30 | +45 |", "|---|---|---|---|---|---|---|---|---|---|"]
    for failure in failures:
        lines.append("| " + failure["id"] + " | " + " | ".join(failure["offset_states"][str(offset)] for offset in offsets) + " |")
    lines += ["", "Короткие сигналы старого профиля: классификация как обычные циклы/восстановление. "
              "Проверено по 15 минут до первого старого сигнала и после измеренного возврата нагрева.", "",
              "| Старый сигнал | Возврат нагрева | Основание | confirmed | warning | unknown |", "|---|---|---|---:|---:|---:|"]
    for short in shorts:
        decisions = short["review_window_states"]
        lines.append(f"| {short['first_moscow']} | {short['recovery_moscow']} | {short['provenance']} | {decisions.get('confirmed_pattern', 0)} | {decisions.get('warning', 0)} | {decisions.get('unknown', 0)} |")
    lines += ["", "Основания классификации коротких участков:", "",
              "- 22 июля: владелец подтвердил норму; повторяющиеся пики воды около 80–86 °C и провалы около 69–72 °C. "
              "Участки 12, 20 и 22 сентября имеют сходные горячие циклы с последующим нагревом.",
              "- 12 августа 11:19: регулярные низкотемпературные циклы достигают расчётной цели около 40 °C "
              "каждые 3–7 минут; воздух около 21,7 °C при цели 22 °C стабилен.",
              "- 19 августа 10:12: повторные пики воды 80–86 °C между провалами 40–55 °C; воздух растёт с 22 до 23 °C.",
              "- 15 августа 14:02: владелец подтвердил нормальное восстановление; после нагрева в 13:53 "
              "вода остывает примерно до 30 °C и вновь нагревается в 14:06, воздух растёт 21,4–21,9 °C.", "",
              "Три предупреждения вне известных границ (Москва): 13.08 10:13, 28.08 07:32, 12.09 08:16. "
              "Они не достигли confirmed_pattern, однако дают anomaly=true по текущему контракту клиента."]
    output.with_suffix(".md").write_text("\n".join(lines) + "\n")
    # Correct the reporting-only warmup count of an existing calibration run.
    calibration["minute_count"] = report["minute_count"]
    calibration["selection_objective"] = "minimize worst detection delay over ten faults, then total delay; zero confirmations outside labels; searched grid only"
    (ROOT / "reports/recalibration-83days.json").write_text(json.dumps(calibration, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"counts": dict(counts), "outside": dict(outside), "july22": dict(july22),
                      "crosschecks": verified, "delays": [f["delay_minutes"] for f in failures]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
