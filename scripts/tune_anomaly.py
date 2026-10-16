"""Search confirmation thresholds using independent, past-only replay windows."""

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.analyze_3weeks import epoch, frames, load_series
from scripts.validate_one_shot import assume_reset_inactive_before, history_window, local
from zontcontrol.anomaly import DetectionConfig, HeatingSample, NoHeatDetector
from zontcontrol.monitor import assess_history, history_samples


def load_cases(root, reference):
    manifest, series = load_series(root, 20496, 8207, 8429, 8832)
    series = assume_reset_inactive_before(series, epoch(manifest["start"]),
                                          epoch(reference["reset_assumption"]["before_exclusive"]))
    indexes = {key: [t for t, v in points] for key, points in series.items()}
    incidents = json.loads((root.parent / "anomalies-3w.json").read_text())["incidents"]
    bounds = [(epoch(e["start"]), epoch(e["recovery"])) for e in incidents]
    cases = []
    for episode in reference["episodes"]:
        offsets = range(46) if episode["kind"] == "anomaly" else reference["offsets_minutes"]
        for offset in offsets:
            end = episode["start"] + offset * 60
            packet = history_window(series, indexes, end)
            assessment = assess_history(packet, circuit_id=20496)
            samples = list(history_samples(packet["devices"][0], datetime.fromtimestamp(end - 2700, timezone.utc),
                                           datetime.fromtimestamp(end, timezone.utc),
                                           circuit_id=20496, air_id=8207, coolant_id=8429))
            overlap = any(start <= end < finish for start, finish in bounds)
            cases.append({"id": episode["id"], "kind": episode["kind"], "offset": offset,
                          "time": end, "packet": packet, "samples": samples,
                          "eligible": assessment["state_at_period_end"] not in ("unknown", "suppressed"),
                          "overlap": overlap})
    return manifest, series, cases


def trace_case(case, config):
    detector = NoHeatDetector(replace(config, warning_minutes=1, confirm_minutes=200), require_known_reset=True)
    trace = []
    previous = None
    for sample in case["samples"]:
        detector.update(sample)
        if detector.evidence_since is None:
            trace = []
            previous = None
            continue
        if detector.evidence_since != previous:
            trace = []
            previous = detector.evidence_since
        absence = (sample.time - max(detector.last_pulse, detector.evidence_since)).total_seconds() / 60
        drop = detector.baseline - sample.coolant
        if absence >= 1 and not (drop < 0.5 and sample.coolant > sample.air + 10):
            trace.append((absence, sample.air_target - sample.air, drop))
    return trace if case["eligible"] else []


def search(cases):
    best = {"strict": None, "state_labels": None}
    totals = {"evaluated": 0, "strict_feasible": 0, "state_labels_feasible": 0}
    for supply_gap in (3, 5, 8, 10):
        for pulse_gain in (0.5, 1, 2, 3):
            base = DetectionConfig(supply_gap=supply_gap, pulse_gain=pulse_gain)
            traces = [trace_case(case, base) for case in cases]
            for air in (0, 0.1, 0.3, 0.5, 0.7, 1, 2, 10):
                for drop in (0.5, 1, 1.5, 2, 3, 5, 10):
                    # For a candidate to be confirmed in the current segment,
                    # at least one earlier eligible point must pass its rule.
                    maximum = [max((t for t, a, d in trace if a >= air or d >= drop), default=-1)
                               for trace in traces]
                    for confirm in range(1, 21):
                        totals["evaluated"] += 1
                        detected = [value >= confirm for value in maximum]
                        latencies = []
                        for episode in ("А1", "А2", "А3", "А4"):
                            found = [case["offset"] for case, yes in zip(cases, detected) if case["id"] == episode and yes]
                            latencies.append(min(found) if found else 999)
                        if max(latencies) == 999:
                            continue
                        for mode in best:
                            negatives = [i for i, case in enumerate(cases)
                                         if case["kind"] != "anomaly" and (mode == "strict" or not case["overlap"])]
                            if any(detected[i] for i in negatives):
                                continue
                            totals[mode + "_feasible"] += 1
                            # First minimize worst delay, then total delay;
                            # prefer fewer missed later target points and
                            # parameters closer to the existing physical rules.
                            misses = sum(not yes for case, yes in zip(cases, detected)
                                         if case["kind"] == "anomaly" and case["offset"] >= max(latencies))
                            score = (max(latencies), sum(latencies), misses,
                                     abs(pulse_gain - 3), abs(supply_gap - 8), -confirm,
                                     abs(drop - 3), abs(air - 0.5))
                            if best[mode] is None or score < tuple(best[mode]["score"]):
                                config = replace(base, warning_minutes=min(6, confirm), confirm_minutes=confirm,
                                                 confirm_air_deficit=air, confirm_coolant_drop=drop)
                                best[mode] = {"config": asdict(config), "latencies_minutes": latencies,
                                              "score": score, "negative_cases": len(negatives)}
    return best, totals


def verify(cases, result):
    if result is None:
        return None
    config = DetectionConfig(**result["config"])
    results = []
    for case in cases:
        actual = assess_history(case["packet"], circuit_id=20496, config=config)
        trace = trace_case(case, config)
        expected = any(t >= config.confirm_minutes and (a >= config.confirm_air_deficit or d >= config.confirm_coolant_drop)
                       for t, a, d in trace)
        assert expected == (actual["state_at_period_end"] == "confirmed_pattern"), (case["id"], case["offset"])
        results.append({"id": case["id"], "kind": case["kind"], "offset": case["offset"],
                        "time": local(case["time"]), "state": actual["state_at_period_end"],
                        "overlap": case["overlap"]})
    return {**result, "checks": results}


def scan_archive(manifest, series, cases, config, incidents):
    rows = frames(manifest, series, 8207)
    samples = [HeatingSample(datetime.fromtimestamp(row["time"], timezone.utc), row["air"],
                             row["air_target"], row["coolant"], row["coolant_target"], row["worktime"],
                             bool(row["reset"]) if row["reset"] in (0, 1) else None,
                             row["selected_air_sensor"],
                             datetime.fromtimestamp(row["reset_changed_at"], timezone.utc)
                             if row["reset_changed_at"] else None) for row in rows]
    bounds = [(epoch(item["start"]), epoch(item["recovery"])) for item in incidents]
    archived = []
    for i in range(44, len(samples)):
        time = rows[i]["time"]
        case = {"samples": samples[i - 44:i + 1], "eligible": True}
        trace = trace_case(case, config)
        outside = not any(start <= time < finish for start, finish in bounds)
        archived.append((time, outside, trace))
    selected = []
    for time, outside, trace in archived:
        if outside and any(t >= config.confirm_minutes
                           and (a >= config.confirm_air_deficit or d >= config.confirm_coolant_drop)
                           for t, a, d in trace):
            selected.append(time)
    best = None
    evaluated = 0
    feasible = 0
    frontier = []
    reference_traces = [trace_case(case, config) for case in cases]
    for air in (0, 0.1, 0.3, 0.5, 0.7, 1, 2, 10):
        for drop in (0.5, 1, 1.5, 2, 3, 5, 10):
            worst_negative = max((t for time, outside, trace in archived if outside
                                  for t, a, d in trace if a >= air or d >= drop), default=-1)
            frontier.append({"air_deficit": air, "coolant_drop": drop,
                             "maximum_negative_absence_minutes": worst_negative})
            maximum = [max((t for t, a, d in trace if a >= air or d >= drop), default=-1)
                       for trace in reference_traces]
            for confirm in range(1, 21):
                evaluated += 1
                if confirm <= worst_negative:
                    continue
                detected = [value >= confirm for value in maximum]
                if any(yes and case["kind"] != "anomaly" and not case["overlap"]
                       for case, yes in zip(cases, detected)):
                    continue
                latencies = [min((case["offset"] for case, yes in zip(cases, detected)
                                  if case["id"] == name and yes), default=999)
                             for name in ("А1", "А2", "А3", "А4")]
                if max(latencies) == 999:
                    continue
                feasible += 1
                misses = sum(not yes for case, yes in zip(cases, detected)
                             if case["kind"] == "anomaly" and case["offset"] >= max(latencies))
                score = (max(latencies), sum(latencies), misses, -confirm,
                         abs(drop - 3), abs(air - 0.5))
                if best is None or score < tuple(best["score"]):
                    candidate = replace(config, warning_minutes=min(6, confirm), confirm_minutes=confirm,
                                        confirm_air_deficit=air, confirm_coolant_drop=drop)
                    best = {"config": asdict(candidate), "latencies_minutes": latencies, "score": score,
                            "maximum_qualifying_negative_minutes": worst_negative}
    return {"minutes_checked": len(archived), "negative_minutes": sum(outside for _, outside, _ in archived),
            "initial_candidate_outside_confirmed_minutes": len(selected),
            "initial_candidate_outside_times": [local(time) for time in selected],
            "confirmation_candidates": evaluated, "feasible": feasible, "frontier": frontier,
            "best": verify(cases, best)}


def select_from_frontiers(cases, profiles):
    winner = None
    winner_score = None
    for profile in profiles:
        base = DetectionConfig(**profile["best"]["config"]) if profile["best"] else DetectionConfig(**profile["physical_profile"])
        traces = [trace_case(case, base) for case in cases]
        for point in profile["frontier"]:
            air, drop = point["air_deficit"], point["coolant_drop"]
            maximum = [max((t for t, a, d in trace if a >= air or d >= drop), default=-1) for trace in traces]
            for confirm in range(1, 21):
                if confirm <= point["maximum_negative_absence_minutes"]:
                    continue
                detected = [value >= confirm for value in maximum]
                if any(yes and case["kind"] != "anomaly" and not case["overlap"]
                       for case, yes in zip(cases, detected)):
                    continue
                latencies = [min((case["offset"] for case, yes in zip(cases, detected)
                                  if case["id"] == name and yes), default=999)
                             for name in ("А1", "А2", "А3", "А4")]
                if max(latencies) == 999:
                    continue
                misses = sum(not yes for case, yes in zip(cases, detected)
                             if case["kind"] == "anomaly" and case["offset"] >= max(latencies))
                score = (max(latencies), sum(latencies), misses, abs(base.pulse_gain - 3),
                         abs(base.supply_gap - 8), -confirm, abs(drop - 3), abs(air - 0.5))
                if winner_score is None or score < winner_score:
                    config = replace(base, warning_minutes=min(6, confirm), confirm_minutes=confirm,
                                     confirm_air_deficit=air, confirm_coolant_drop=drop)
                    winner_score = score
                    winner = {"config": asdict(config), "latencies_minutes": latencies, "score": score,
                              "maximum_qualifying_negative_minutes": point["maximum_negative_absence_minutes"]}
    return verify(cases, winner)


def verify_full_selected(manifest, series, result, incidents):
    config = DetectionConfig(**result["config"])
    rows = frames(manifest, series, 8207)
    samples = [HeatingSample(datetime.fromtimestamp(row["time"], timezone.utc), row["air"],
                             row["air_target"], row["coolant"], row["coolant_target"], row["worktime"],
                             bool(row["reset"]) if row["reset"] in (0, 1) else None,
                             row["selected_air_sensor"],
                             datetime.fromtimestamp(row["reset_changed_at"], timezone.utc)
                             if row["reset_changed_at"] else None) for row in rows]
    bounds = [(epoch(item["start"]), epoch(item["recovery"])) for item in incidents]
    outside = []
    confirmed_inside = 0
    checked = 0
    for i in range(44, len(samples)):
        detector = NoHeatDetector(config, require_known_reset=True)
        for sample in samples[i - 44:i + 1]:
            detector.update(sample)
        checked += 1
        if detector.level != "confirmed_pattern":
            continue
        if any(start <= rows[i]["time"] < end for start, end in bounds):
            confirmed_inside += 1
        else:
            outside.append(local(rows[i]["time"]))
    assert not outside, outside[:10]
    return {"independent_detector_runs": checked, "outside_confirmed_minutes": len(outside),
            "inside_confirmed_minutes": confirmed_inside, "outside_times": outside}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("reports/history-3w"))
    parser.add_argument("--reference", type=Path, default=Path("reports/one-shot-validation-3w.json"))
    parser.add_argument("--output", type=Path, default=Path("reports/anomaly-tuning-3w.json"))
    parser.add_argument("--scan-only", action="store_true", help="Expand an existing candidate to every archived minute")
    parser.add_argument("--scan-all", action="store_true", help="Expand every physical profile to every archived minute")
    parser.add_argument("--select-from-frontiers", action="store_true", help="Choose from already computed complete archive profiles")
    args = parser.parse_args()
    reference = json.loads(args.reference.read_text())
    manifest, series, cases = load_cases(args.input, reference)
    if args.select_from_frontiers:
        report = json.loads(args.output.read_text())
        profiles = report["full_archive_profiles"]
        if len(profiles) != 16:
            raise ValueError("wait for all 16 archive profiles before selecting")
        report["selected"] = select_from_frontiers(cases, profiles)
        incidents = json.loads((args.input.parent / "anomalies-3w.json").read_text())["incidents"]
        report["selected_full_verification"] = verify_full_selected(manifest, series, report["selected"], incidents)
        report["selection_objective"] = "Минимум максимальной задержки, затем суммы задержек и пропусков; при равенстве параметры ближе к прежнему профилю."
        report["label_policy"] = "Точки после ручного сброса положительны, если нагрев не восстановился внутри известного эпизода. Включённый сброс и переходная пауза подавляют обнаружение."
        report["reset_assumption"] = reference["reset_assumption"]
        report["implementation_sha256"] = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
                                            for name in ("zontcontrol/anomaly.py", "zontcontrol/monitor.py", "zontcontrol/detection_profile.py")}
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({key: value for key, value in report["selected"].items() if key != "checks"}, ensure_ascii=False))
        print(json.dumps(report["selected_full_verification"], ensure_ascii=False))
        return
    if args.scan_only or args.scan_all:
        report = json.loads(args.output.read_text())
        config = DetectionConfig(**report["state_labels"]["config"])
        incidents = json.loads((args.input.parent / "anomalies-3w.json").read_text())["incidents"]
        if args.scan_all:
            profiles = []
            winner = None
            winner_score = None
            for supply_gap in (8, 3, 5, 10):
                for pulse_gain in (3, 0.5, 1, 2):
                    base = replace(config, supply_gap=supply_gap, pulse_gain=pulse_gain)
                    scan = scan_archive(manifest, series, cases, base, incidents)
                    scan["physical_profile"] = {"supply_gap": supply_gap, "pulse_gain": pulse_gain}
                    profiles.append(scan)
                    if scan["best"]:
                        candidate = scan["best"]
                        score = (*candidate["score"][:3], abs(pulse_gain - 3), abs(supply_gap - 8),
                                 *candidate["score"][3:])
                        if winner_score is None or score < winner_score:
                            winner, winner_score = candidate, score
                    report["full_archive_profiles"] = profiles
                    report["selected"] = winner
                    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                    print(json.dumps({"profile": scan["physical_profile"],
                                      "best_delays": scan["best"]["latencies_minutes"] if scan["best"] else None,
                                      "completed": len(profiles)}, ensure_ascii=False), flush=True)
            print(json.dumps({key: value for key, value in winner.items() if key != "checks"}, ensure_ascii=False)
                  if winner else "No feasible profile")
            return
        scan = scan_archive(manifest, series, cases, config, incidents)
        report["full_archive"] = scan
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({key: value for key, value in scan.items() if key not in ("initial_candidate_outside_times", "best")},
                         ensure_ascii=False))
        print(json.dumps({key: value for key, value in scan["best"].items() if key != "checks"}, ensure_ascii=False)
              if scan["best"] else "No feasible configuration for this physical profile")
        return
    best, totals = search(cases)
    report = {"period": reference["period"], "search": totals,
              "strict": verify(cases, best["strict"]), "state_labels": verify(cases, best["state_labels"]),
              "overlapping_manual_checks": [{"id": case["id"], "offset": case["offset"], "time": local(case["time"])}
                                             for case in cases if case["kind"] == "manual" and case["overlap"]]}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"search": totals, "best": best, "cases": len(cases),
                      "overlapping_manual_checks": len(report["overlapping_manual_checks"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
