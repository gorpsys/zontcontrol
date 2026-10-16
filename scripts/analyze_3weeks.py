"""Offline retrospective analysis of previously collected ZONT history."""

import argparse
import bisect
import csv
import gzip
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zontcontrol.anomaly import DetectionConfig, HeatingSample, NoHeatDetector


def epoch(value):
    return int(datetime.fromisoformat(value).timestamp())


def load_series(root, circuit_id, air_id, coolant_id, reset_id):
    manifest = json.loads((root / "manifest.json").read_text())
    stores = {key: {} for key in ("air", "coolant", "weather", "air_target", "coolant_target",
                                  "worktime", "status", "mode", "sensor", "reset")}
    for chunk in manifest["chunks"]:
        with gzip.open(root / chunk["file"], "rt") as stream:
            device = json.load(stream)["devices"][0]
        for sensor in device["temperature_sensors"]:
            key = {air_id: "air", coolant_id: "coolant", 8306: "weather"}.get(sensor["id"])
            if key:
                stores[key].update({epoch(p["time"]): p["value"] for p in sensor["points"]})
        for circuit in device["circuits"]:
            if circuit["id"] == circuit_id:
                for raw, key in {"target_temp": "air_target", "setpoint_temp": "coolant_target",
                                 "worktime": "worktime", "status": "status", "mode_id": "mode",
                                 "target_sensor_id": "sensor"}.items():
                    stores[key].update({epoch(p["time"]): p["value"] for p in circuit["series"][raw]})
        for control in device.get("web_elements", []):
            if control["id"] == reset_id:
                stores["reset"].update({epoch(p["time"]): p["value"] for p in control["series"]["state"]})
    return manifest, {key: sorted(values.items()) for key, values in stores.items()}


def frames(manifest, series, air_id):
    indexes = {key: [time for time, value in points] for key, points in series.items()}
    start = epoch(manifest["start"])
    end = epoch(manifest["end"])
    rows = []
    reset_changes = []
    previous = None
    for time, value in series["reset"]:
        if previous is not None and value != previous:
            reset_changes.append(time)
        previous = value
    for time in range((start // 60 + 1) * 60, end + 1, 60):
        row = {"time": time}
        for key, points in series.items():
            index = bisect.bisect_right(indexes[key], time) - 1
            value = points[index][1] if index >= 0 else None
            if key in ("air", "coolant") and index >= 0 and time - points[index][0] > 90:
                value = None
            row[key] = value
        row["selected_air_sensor"] = row["sensor"] == air_id
        reset_index = bisect.bisect_right(reset_changes, time) - 1
        row["reset_changed_at"] = reset_changes[reset_index] if reset_index >= 0 else None
        rows.append(row)
    return rows


def replay(rows, warning=6, confirm=20, supply_gap=8):
    detector = NoHeatDetector(DetectionConfig(warning_minutes=warning, confirm_minutes=confirm, supply_gap=supply_gap))
    episodes = []
    active = None
    for row in rows:
        sample = HeatingSample(datetime.fromtimestamp(row["time"], timezone.utc), row["air"],
                               row["air_target"], row["coolant"], row["coolant_target"], row["worktime"],
                               bool(row["reset"]) if row["reset"] in (0, 1) else None, row["selected_air_sensor"],
                               datetime.fromtimestamp(row["reset_changed_at"], timezone.utc)
                               if row["reset_changed_at"] is not None else None)
        event = detector.update(sample)
        if not event:
            continue
        if event["type"] == "warning":
            active = {"start": event["evidence_since"], "warning": event["time"],
                      "confirmation": None, "end": None, "end_reason": None,
                      "air_at_warning": event["air"], "air_target_at_warning": event["air_target"],
                      "coolant_at_warning": event["coolant"], "supply_target_at_warning": event["coolant_target"]}
            episodes.append(active)
        if event["type"] == "confirmed_pattern" and active is not None:
            active["confirmation"] = event["time"]
        if event["type"] == "ended" and active is not None:
            active["end"] = event["time"]
            active["end_reason"] = event["reason"]
            active = None
    for episode in episodes:
        end = epoch(episode["end"]) if episode["end"] else rows[-1]["time"]
        start = epoch(episode["start"])
        selected = [row for row in rows if start <= row["time"] <= end]
        episode["duration_minutes"] = round((end - start) / 60, 1)
        episode["air_min"] = min((row["air"] for row in selected if row["air"] is not None), default=None)
        episode["coolant_min"] = min((row["coolant"] for row in selected if row["coolant"] is not None), default=None)
        episode["max_air_deficit"] = max((row["air_target"] - row["air"] for row in selected
                                          if row["air_target"] is not None and row["air"] is not None), default=None)
        episode["ongoing"] = episode["end"] is None
    return episodes


def group_incidents(episodes, rows):
    incidents = []
    times = [row["time"] for row in rows]
    for episode in episodes:
        if incidents and (incidents[-1]["recovery"] is None or epoch(episode["start"]) <= epoch(incidents[-1]["recovery"])):
            incidents[-1]["segments"].append(episode)
            continue
        if episode["confirmation"] is None:
            continue
        recovery = None
        beginning = bisect.bisect_left(times, epoch(episode["end"] or episode["warning"]))
        for index in range(beginning, len(rows)):
            row = rows[index]
            window = rows[max(0, index - 5):index]
            previous = [point["coolant"] for point in window if point["coolant"] is not None]
            if row["coolant"] is not None and previous and row["coolant"] - min(previous) >= 5:
                recovery = datetime.fromtimestamp(row["time"], timezone.utc).isoformat()
                break
        finish = epoch(recovery) if recovery else rows[-1]["time"]
        selected = rows[bisect.bisect_left(times, epoch(episode["start"])):bisect.bisect_right(times, finish)]
        late_deficit = next((point["time"] for point in selected
                             if point["air"] is not None and point["air_target"] is not None
                             and point["air_target"] - point["air"] > 0.5), None)
        incidents.append({
            "start": episode["start"], "warning": episode["warning"],
            "confirmation": episode["confirmation"], "recovery": recovery,
            "duration_minutes": round((finish - epoch(episode["start"])) / 60, 1),
            "air_min": min((point["air"] for point in selected if point["air"] is not None), default=None),
            "coolant_min": min((point["coolant"] for point in selected if point["coolant"] is not None), default=None),
            "max_air_deficit": max((point["air_target"] - point["air"] for point in selected
                                    if point["air_target"] is not None and point["air"] is not None), default=None),
            "lead_before_air_deficit_over_0_5_minutes": round((late_deficit - epoch(episode["warning"])) / 60, 1)
                if late_deficit is not None else None,
            "segments": [episode],
        })
    return incidents


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("reports/history-3w"))
    parser.add_argument("--output", type=Path, default=Path("reports/anomalies-3w.json"))
    args = parser.parse_args()
    manifest, series = load_series(args.input, 20496, 8207, 8429, 8832)
    rows = frames(manifest, series, 8207)
    episodes = replay(rows)
    incidents = group_incidents(episodes, rows)
    report = {"period": {"start": manifest["start"], "end": manifest["end"]},
              "samples": len(rows), "chunks": len(manifest["chunks"]),
              "truncated_chunks": sum(c["time_truncated"] for c in manifest["chunks"]),
              "missing_air_samples": sum(r["air"] is None for r in rows),
              "missing_coolant_samples": sum(r["coolant"] is None for r in rows),
              "episodes": episodes, "incidents": incidents,
              "sensitivity": {str(minutes): {"warnings": len(found),
                    "confirmed_patterns": sum(e["confirmation"] is not None for e in found)}
                  for minutes in (5, 6, 7, 8, 10, 12, 15)
                  for found in [replay(rows, warning=minutes, confirm=20)]}}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    csv_path = args.output.with_suffix(".csv")
    if episodes:
        with csv_path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(episodes[0]))
            writer.writeheader()
            writer.writerows(episodes)
    print(json.dumps({key: value for key, value in report.items() if key not in ("episodes", "incidents")}, ensure_ascii=False))
    for episode in episodes:
        localized = {key: datetime.fromisoformat(episode[key]).astimezone(ZoneInfo("Europe/Moscow")).isoformat()
                     if episode[key] else None for key in ("start", "warning", "confirmation", "end")}
        print(json.dumps({**episode, **localized}, ensure_ascii=False))


if __name__ == "__main__":
    main()
