"""Calibrate on owner-labelled faults while excluding ordinary heating cycles."""

import argparse
import bisect
import hashlib
import json
import math
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.analyze_3weeks import epoch, frames, load_series
from scripts.backtest_profile import frame_samples, local
from scripts.validate_one_shot import assume_reset_inactive_before
from zontcontrol.anomaly import DetectionConfig, NoHeatDetector


ROOT = Path(__file__).resolve().parents[1]
TEMPERATURE_SPLITS = (30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, math.inf)
AIR_THRESHOLDS = (0, 0.3, 0.5)
DROP_THRESHOLDS = (0.5, 1, 2, 3)


def load_dataset():
    stores = None
    manifests = []
    sources = {}
    for directory in (ROOT / "reports/history-prior-2months", ROOT / "reports/history-3w"):
        manifest, series = load_series(directory, 20496, 8207, 8429, 8832)
        if any(chunk["time_truncated"] for chunk in manifest["chunks"]):
            raise ValueError("calibration requires complete archived responses")
        manifests.append(manifest)
        if stores is None:
            stores = {key: {} for key in series}
        for key, points in series.items():
            stores[key].update(points)
        for chunk in manifest["chunks"]:
            path = directory / chunk["file"]
            sources[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"start": manifests[0]["start"], "end": manifests[-1]["end"]}
    series = {key: sorted(points.items()) for key, points in stores.items()}
    series = assume_reset_inactive_before(series, epoch(manifest["start"]), epoch("2026-09-27T23:36:00+03:00"))
    rows = frames(manifest, series, 8207)
    times = [row["time"] for row in rows]
    samples = frame_samples(rows)
    old = json.loads((ROOT / "reports/profile-prior-2months.json").read_text())
    labels = []
    for i, group in enumerate((g for g in old["signal_groups"] if g["long_pattern"]), 1):
        first = old["confirmed_segments"][group["segments"][0] - 1]
        start = epoch(first["evidence_since_at_first"])
        source = "observed_conditions_before_owner_confirmed_segment"
        if first["first"].startswith("2026-08-28"):
            start = epoch("2026-08-28T07:43:00+03:00")
            source = "owner_explicit_start"
        elif first["first"].startswith("2026-07-23"):
            start = epoch("2026-07-23T18:34:00+03:00")
            source = "owner_confirmed_earlier_start"
        elif first["first"].startswith("2026-08-13"):
            start = epoch("2026-08-13T10:33:00+03:00")
            source = "owner_confirmed_earlier_start"
        labels.append({"id": f"П{i}", "start": start, "end": epoch(group["recovery"]),
                       "reference_confirmation": epoch(group["first"]), "start_source": source,
                       "owner_confirmed": True})
    recent = json.loads((ROOT / "reports/anomalies-3w.json").read_text())
    for i, incident in enumerate(recent["incidents"], 1):
        labels.append({"id": f"А{i}", "start": epoch(incident["start"]), "end": epoch(incident["recovery"]),
                       "reference_confirmation": epoch(incident["warning"]),
                       "start_source": "previous_observed_conditions", "owner_confirmed": True})
    labels.sort(key=lambda label: label["start"])
    positive = [-1] * len(rows)
    targets = [[] for _ in labels]
    for number, label in enumerate(labels):
        lo = bisect.bisect_left(times, label["start"])
        hi = bisect.bisect_left(times, label["end"])
        positive[lo:hi] = [number] * (hi - lo)
        targets[number] = list(range(lo, min(hi, lo + 121)))
    normal = set(i for i, t in enumerate(times) if i >= 44 and positive[i] == -1
                 and (t % 21600 == 0 or local(t).startswith("22.07.2026")))
    for group in (g for g in old["signal_groups"] if not g["long_pattern"]):
        lo = bisect.bisect_left(times, epoch(group["first"]) - 15 * 60)
        hi = bisect.bisect_right(times, epoch(group["recovery"]) + 15 * 60)
        normal.update(i for i in range(max(44, lo), hi) if positive[i] == -1)
    indices = sorted(normal | {i for target in targets for i in target})
    manifest["analysis_start"] = old["period"]["start"]
    return manifest, series, rows, samples, labels, positive, targets, indices, sources


def trace_window(samples, config):
    # Confirmation itself does not alter evidence. Use a distant confirmation
    # threshold to extract the current segment; ordinary response clears it.
    detector = NoHeatDetector(replace(config, warning_minutes=1, confirm_minutes=200,
                                     hot_coolant_above=None, hot_confirm_minutes=None), require_known_reset=True)
    trace = []
    previous = None
    for sample in samples:
        detector.update(sample)
        if detector.evidence_since is None:
            trace, previous = [], None
            continue
        if detector.evidence_since != previous:
            trace, previous = [], detector.evidence_since
        absence = (sample.time - max(detector.last_pulse, detector.evidence_since)).total_seconds() / 60
        drop = detector.baseline - sample.coolant
        if absence >= 1 and not (drop < 0.5 and sample.coolant > sample.air + 10):
            trace.append((absence, sample.coolant, sample.air_target - sample.air, drop))
    return trace


class FastTraces:
    """Exact causal traces for overlapping 45-minute windows.

    Demand and response checks need at most six past samples. After the
    sixth sample, a fresh replay and a full-history replay have identical
    clear/no-response conditions. Only the fifth sample at the left edge
    needs a separate replay when an old evidence segment crosses the edge.
    """

    def __init__(self, samples, config):
        self.samples = samples
        self.config = replace(config, warning_minutes=1, confirm_minutes=200,
                              hot_coolant_above=None, hot_confirm_minutes=None)
        self.starts = []
        detector = NoHeatDetector(self.config, require_known_reset=True)
        for sample in samples:
            detector.update(sample)
            start = (-1 if detector.evidence_since is None else
                     int((detector.evidence_since - samples[0].time).total_seconds() / 60))
            self.starts.append(start)

    def window(self, i):
        start = self.starts[i]
        if i < 44 or start < 0:
            return []
        left = i - 44
        if start <= left + 5:
            boundary = NoHeatDetector(self.config, require_known_reset=True)
            for sample in self.samples[left:left + 5]:
                boundary.update(sample)
            start = left + 4 if boundary.evidence_since is not None else left + 5
        baseline = self.samples[start].coolant
        trace = []
        for k in range(start + 1, i + 1):
            sample = self.samples[k]
            drop = baseline - sample.coolant
            if not (drop < 0.5 and sample.coolant > sample.air + 10):
                trace.append((k - start, sample.coolant, sample.air_target - sample.air, drop))
        return trace


def extract(samples, indices, config, positive, targets):
    branches = len(AIR_THRESHOLDS) + len(DROP_THRESHOLDS)
    normal_bins = [[-1] * len(TEMPERATURE_SPLITS) for _ in range(branches)]
    positive_traces = {}
    target_set = {i for target in targets for i in target}
    fast = FastTraces(samples, config)
    for position, i in enumerate(indices):
        trace = fast.window(i)
        if i % 997 == 0 or i in target_set:
            assert trace == trace_window(samples[i - 44:i + 1], config), i
        if positive[i] == -1:
            for duration, water, air, drop in trace:
                bin_id = bisect.bisect_left(TEMPERATURE_SPLITS, water)
                for j, threshold in enumerate(AIR_THRESHOLDS):
                    if air >= threshold:
                        normal_bins[j][bin_id] = max(normal_bins[j][bin_id], duration)
                for j, threshold in enumerate(DROP_THRESHOLDS, len(AIR_THRESHOLDS)):
                    if drop >= threshold:
                        normal_bins[j][bin_id] = max(normal_bins[j][bin_id], duration)
        if i in target_set:
            positive_traces[i] = trace
    cold = [[max(bins[:j + 1]) for j in range(len(TEMPERATURE_SPLITS))] for bins in normal_bins]
    hot = [[max(bins[j + 1:], default=-1) for j in range(len(TEMPERATURE_SPLITS))] for bins in normal_bins]
    return cold, hot, positive_traces


def choose(config, cold, hot, positive_traces, targets, rows, labels):
    best = None
    for a, air_threshold in enumerate(AIR_THRESHOLDS):
        for d, drop_threshold in enumerate(DROP_THRESHOLDS, len(AIR_THRESHOLDS)):
            for j, split in enumerate(TEMPERATURE_SPLITS):
                cold_bound, hot_bound = max(cold[a][j], cold[d][j]), max(hot[a][j], hot[d][j])
                cold_minutes = max(1, int(cold_bound) + 1)
                hot_minutes = max(cold_minutes, int(hot_bound) + 1)
                if hot_minutes > 40:
                    continue
                firsts = []
                for target in targets:
                    found = next((i for i in target if any(duration >= (cold_minutes if water <= split else hot_minutes)
                                                          and (air >= air_threshold or drop >= drop_threshold)
                                                          for duration, water, air, drop in positive_traces.get(i, []))), None)
                    firsts.append(found)
                if any(i is None for i in firsts):
                    continue
                delays = [(rows[i]["time"] - label["start"]) / 60 for i, label in zip(firsts, labels)]
                adaptive = cold_minutes != hot_minutes and math.isfinite(split)
                candidate = replace(config, warning_minutes=cold_minutes, confirm_minutes=cold_minutes,
                                    confirm_air_deficit=air_threshold, confirm_coolant_drop=drop_threshold,
                                    hot_coolant_above=split if adaptive else None,
                                    hot_confirm_minutes=hot_minutes if adaptive else None)
                score = (max(delays), sum(delays), int(adaptive), abs(config.pulse_gain - 3), abs(config.supply_gap - 8),
                         abs(drop_threshold - 3), abs(air_threshold), -cold_minutes, -hot_minutes,
                         abs(split - 60) if adaptive else 0)
                if best is None or score < tuple(best["score"]):
                    best = {"config": asdict(candidate), "score": score, "delays_minutes": delays,
                            "first_confirmations": [rows[i]["time"] for i in firsts],
                            "maximum_normal_cold_minutes": cold_bound, "maximum_normal_hot_minutes": hot_bound}
    return best


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("reports/recalibration-83days.json"))
    args = parser.parse_args()
    manifest, series, rows, samples, labels, positive, targets, selected, sources = load_dataset()
    report = {"period": manifest, "labels": labels, "source_sha256": sources,
              "selection_minutes": len(selected),
              "minute_count": sum(epoch(manifest["analysis_start"]) < row["time"] <= epoch(manifest["end"])
                                  for row in rows),
              "normal_label_policy": "22 июля подтверждено владельцем как нормальная работа. Аналогичные короткие "
                                     "циклы и остальные минуты вне десяти подтверждённых эпизодов используются "
                                     "как отрицательные ограничения; это не полный журнал аппаратных состояний.",
              "profiles": [], "full_profiles": [], "selected": None}
    for gap in (3, 4, 5, 6, 8, 10):
        for pulse in (0.5, 1, 2, 3):
            config = DetectionConfig(supply_gap=gap, pulse_gain=pulse)
            cold, hot, positives = extract(samples, selected, config, positive, targets)
            best = choose(config, cold, hot, positives, targets, rows, labels)
            report["profiles"].append({"supply_gap": gap, "pulse_gain": pulse, "optimistic": best})
            print(json.dumps({"stage": "screen", "gap": gap, "pulse": pulse,
                              "score": best["score"][:2] if best else None}, ensure_ascii=False), flush=True)
    profiles = sorted((p for p in report["profiles"] if p["optimistic"]), key=lambda p: p["optimistic"]["score"])
    for profile in profiles:
        if report["selected"] and tuple(profile["optimistic"]["score"]) >= tuple(report["selected"]["score"]):
            break
        config = DetectionConfig(supply_gap=profile["supply_gap"], pulse_gain=profile["pulse_gain"])
        cold, hot, positives = extract(samples, range(44, len(samples)), config, positive, targets)
        best = choose(config, cold, hot, positives, targets, rows, labels)
        report["full_profiles"].append({"supply_gap": config.supply_gap, "pulse_gain": config.pulse_gain,
                                        "best": best, "cold_bounds": cold, "hot_bounds": hot})
        if best and (report["selected"] is None or tuple(best["score"]) < tuple(report["selected"]["score"])):
            report["selected"] = best
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        print(json.dumps({"stage": "full", "gap": config.supply_gap, "pulse": config.pulse_gain,
                          "best": best}, ensure_ascii=False), flush=True)
    if report["selected"] is None:
        raise ValueError("no feasible confirmation profile within the causal 45-minute window")
    print(json.dumps({"selected": report["selected"], "full_profiles": len(report["full_profiles"]),
                      "labels": [{**label, "start_moscow": local(label["start"])} for label in labels]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
