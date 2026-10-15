"""Read a ZONT history interval, summarize it, and optionally plot it."""

import argparse
import bisect
import json
import math
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zontcontrol.client import ZontClient
from zontcontrol.history import cli_period


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


def plot(device, circuit, air, coolant, start, end, output):
    # Keep matplotlib configuration entirely in /tmp.
    os.environ["MPLCONFIGDIR"] = "/tmp/zont-history-matplotlib"
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as dates
    import matplotlib.pyplot as plt

    series = circuit["series"]
    fig, axes = plt.subplots(3, 2, figsize=(16, 11), sharex="col", constrained_layout=True)
    colors = {"actual": "#147b89", "target": "#d75a35"}

    def draw(axis, points, label, *, step=False, color=None):
        if not points:
            return
        times, values = [], []
        previous = None
        for point in points:
            time = moment(point)
            if not step and previous is not None and (time - previous).total_seconds() > 120:
                times.append(time)
                values.append(math.nan)
            times.append(time)
            values.append(math.nan if point["value"] is None else point["value"])
            previous = time
        if step:
            axis.step(times + [end], values + [values[-1]], where="post", label=label, color=color, linewidth=1.6)
        else:
            axis.plot(times, values, label=label, color=color, linewidth=1.4)

    for column in (0, 1):
        draw(axes[0, column], air["points"], "Фактическая температура воздуха", color=colors["actual"])
        draw(axes[0, column], series["target_temp"], "target_temp контура", step=True, color=colors["target"])
        draw(axes[1, column], coolant["points"], "Фактическая температура теплоносителя", color=colors["actual"])
        draw(axes[1, column], series["setpoint_temp"], "Расчётная уставка теплоносителя", step=True, color=colors["target"])
        axes[0, column].set_ylabel("°C")
        axes[1, column].set_ylabel("°C")
    axes[0, 0].set_title("Воздух — последние сутки")
    axes[1, 0].set_title("Теплоноситель — последние сутки")
    axes[0, 1].set_title("Воздух — последние два часа")
    axes[1, 1].set_title("Теплоноситель — последние два часа")
    draw(axes[2, 0], series["worktime"], "worktime контура", color="#69519d")
    axes[2, 0].set_ylabel("Секунды")
    axes[2, 0].set_title("Работа контура по ZONT — последние сутки")
    draw(axes[2, 1], series["worktime"], "worktime", color="#69519d")
    axes[2, 1].set_ylabel("Секунды")
    axes[2, 1].set_title("Время работы контура — последние два часа")
    for row in range(3):
        for column in (0, 1):
            axis = axes[row, column]
            axis.set_xlim(start if column == 0 else max(start, end - timedelta(hours=2)), end)
            axis.grid(alpha=0.2)
            axis.legend(loc="upper left", fontsize=8)
            axis.xaxis.set_major_locator(dates.AutoDateLocator(minticks=5, maxticks=8))
            axis.xaxis.set_major_formatter(dates.DateFormatter("%d.%m\n%H:%M", tz=MOSCOW))
    fig.suptitle(
        f"ZONT SMART 2.0 — {circuit['name']}\n"
        f"{start.astimezone(MOSCOW):%d.%m.%Y %H:%M} — {end.astimezone(MOSCOW):%d.%m.%Y %H:%M}, Москва",
        fontsize=14,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=160)
    fig.savefig(output.with_suffix(".svg"))
    plt.close(fig)


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
    client = ZontClient(os.environ["ZONT_CONTACT"], token=os.environ["ZONT_TOKEN"])
    data = client.read_history(start, end, device_id=args.device_id, circuit_id=args.circuit_id)
    device = data["devices"][0]
    circuit = next(c for c in device["circuits"] if c["id"] == args.circuit_id)
    air = next(s for s in device["temperature_sensors"] if s["id"] == args.air_sensor_id)
    coolant = next(s for s in device["temperature_sensors"] if s["id"] == args.coolant_sensor_id)
    summary = {"period": data["requested_period"], **summarize(device, circuit, air, coolant)}
    if args.plot:
        plot(device, circuit, air, coolant, start, end, args.plot)
        summary["plot"] = str(args.plot.resolve())
    if args.quiet:
        print(json.dumps({key: summary[key] for key in ("period", "plot") if key in summary},
                         ensure_ascii=False))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
