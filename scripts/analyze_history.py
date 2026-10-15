"""Read a ZONT history interval, summarize it, and optionally plot it."""

import argparse
import bisect
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zontcontrol.application import create_read_controller
from zontcontrol.history import cli_period
from scripts.history_archive import read_archive


MOSCOW = ZoneInfo("Europe/Moscow")


def moment(point):
    return datetime.fromisoformat(point["time"])


def local_time(point):
    return moment(point).astimezone(MOSCOW).isoformat()


def changes(points):
    result = []
    previous = object()
    for point in points:
        if point["value"] != previous:
            result.append({"time": local_time(point), "value": point["value"]})
            previous = point["value"]
    return result


def series_stats(points):
    valid = [point for point in points if point["value"] is not None]
    gaps = [(moment(b) - moment(a)).total_seconds() for a, b in zip(points, points[1:])]
    values = [point["value"] for point in valid]
    return {
        "points": len(points), "nulls": len(points) - len(valid),
        "first": local_time(points[0]) if points else None,
        "last": local_time(points[-1]) if points else None,
        "min": min(values) if values else None, "max": max(values) if values else None,
        "mean": round(statistics.mean(values), 3) if values else None,
        "largest_gap_seconds": max(gaps) if gaps else None,
        "gaps_over_120_seconds": sum(gap > 120 for gap in gaps),
    }


def step_value(points, time):
    index = bisect.bisect_right([moment(point) for point in points], time) - 1
    return points[index]["value"] if index >= 0 else None


def summarize(device, circuit, air, coolant):
    series = circuit["series"]
    mode_names = {mode["id"]: mode["name"] for mode in device["modes"]}
    transitions = changes(series["mode_id"])
    for transition in transitions:
        value = transition["value"]
        transition["name"] = "Ручной" if value == 0 else mode_names.get(value)
    rates = []
    for before, after in zip(coolant["points"], coolant["points"][1:]):
        seconds = (moment(after) - moment(before)).total_seconds()
        if before["value"] is not None and after["value"] is not None and 0 < seconds <= 120:
            rates.append({"time": local_time(after), "from": before["value"],
                          "to": after["value"], "seconds": seconds,
                          "degrees_per_minute": round((after["value"] - before["value"]) * 60 / seconds, 2)})
    errors = []
    for point in air["points"]:
        target = step_value(series["target_temp"], moment(point))
        sensor_id = step_value(series["target_sensor_id"], moment(point))
        if point["value"] is not None and target is not None and sensor_id == air["id"]:
            errors.append(point["value"] - target)
    return {
        "time_truncated": device["time_truncated"],
        "air": series_stats(air["points"]), "coolant": series_stats(coolant["points"]),
        "target": series_stats(series["target_temp"]),
        "setpoint": series_stats(series["setpoint_temp"]),
        "mode_changes": transitions, "target_changes": changes(series["target_temp"]),
        "target_sensor_changes": changes(series["target_sensor_id"]),
        "largest_coolant_slopes": sorted(rates, key=lambda item: abs(item["degrees_per_minute"]), reverse=True)[:8],
        "air_error_when_selected_for_control": {
            "samples": len(errors), "mean": round(statistics.mean(errors), 3) if errors else None,
            "min": min(errors) if errors else None, "max": max(errors) if errors else None,
            "samples_above_0_5_degrees": sum(error > 0.5 for error in errors),
            "samples_below_minus_0_5_degrees": sum(error < -0.5 for error in errors),
        },
        "worktime": series_stats(series["worktime"]),
    }


def plot(controller, device_id, circuit_id, air_id, coolant_id, start, end, output):
    """Produce standalone SVGs with the application's stdlib renderer."""
    from zontcontrol.algorithms.charts import GraphSpec, SeriesSpec, render_charts
    from zontcontrol.controller.contracts import ValueKey

    graphs = (
        GraphSpec('air', 'Воздух и цель', (
            SeriesSpec(ValueKey(device_id, 'sensor', air_id, 'value')),
            SeriesSpec(ValueKey(device_id, 'circuit', circuit_id, 'target_temp')))),
        GraphSpec('coolant', 'Теплоноситель и уставка', (
            SeriesSpec(ValueKey(device_id, 'sensor', coolant_id, 'value')),
            SeriesSpec(ValueKey(device_id, 'circuit', circuit_id, 'setpoint_temp')))),
        GraphSpec('worktime', 'Запрос нагрева', (
            SeriesSpec(ValueKey(device_id, 'circuit', circuit_id, 'worktime')),)),
    )
    devices = tuple(row for row in controller.list_devices() if row.device_id == device_id)
    descriptors = controller.describe_values(device_id)
    keys = tuple(item.key for graph in graphs for item in graph.series)
    result = controller.read_history(device_id, keys, start, end)
    charts = render_charts(graphs, devices=devices, descriptors=descriptors,
                           histories=(result,), start=start, end=end,
                           display_timezone='Europe/Moscow')
    output.parent.mkdir(parents=True, exist_ok=True)
    paths = []
    for chart in charts:
        path = output.with_name(f'{output.stem}-{chart.graph_id}.svg')
        path.write_text(chart.svg, encoding='utf-8')
        paths.append(str(path.resolve()))
    return paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device-id", type=int, default=700877)
    parser.add_argument("--circuit-id", type=int, default=20496)
    parser.add_argument("--air-sensor-id", type=int, default=8207)
    parser.add_argument("--coolant-sensor-id", type=int, default=8429)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")
    parser.add_argument("--plot", type=Path)
    parser.add_argument("--quiet", action="store_true", help="Печатать только период и пути графиков")
    args = parser.parse_args()
    start, end = cli_period(args.start, args.end, None if args.start else args.hours)
    client = create_read_controller()
    data = read_archive(client, args.device_id, args.circuit_id, start, end)
    device = data["devices"][0]
    circuit = next(c for c in device["circuits"] if c["id"] == args.circuit_id)
    air = next(s for s in device["temperature_sensors"] if s["id"] == args.air_sensor_id)
    coolant = next(s for s in device["temperature_sensors"] if s["id"] == args.coolant_sensor_id)
    summary = {"period": data["requested_period"], **summarize(device, circuit, air, coolant)}
    if args.plot:
        summary["plot"] = plot(client, args.device_id, args.circuit_id, args.air_sensor_id,
                               args.coolant_sensor_id, start, end, args.plot)
    if args.quiet:
        print(json.dumps({key: summary[key] for key in ("period", "plot") if key in summary},
                         ensure_ascii=False))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
