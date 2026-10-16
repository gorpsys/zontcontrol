"""Generate standalone plots and a compact CSV for the cached three-week analysis."""

import csv
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ["MPLCONFIGDIR"] = "/tmp/zont-history-matplotlib"
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from analyze_3weeks import epoch, frames, load_series


ROOT = Path(__file__).resolve().parents[1]
MOSCOW = ZoneInfo("Europe/Moscow")


def plot_line(axis, rows, key, label, color, step=False):
    times = [datetime.fromtimestamp(row["time"], timezone.utc) for row in rows]
    values = [row[key] if row[key] is not None else math.nan for row in rows]
    if step:
        axis.step(times, values, where="post", label=label, color=color, linewidth=1.2)
    else:
        axis.plot(times, values, label=label, color=color, linewidth=1.1)


def style(axis, hourly=False):
    axis.grid(alpha=.2)
    axis.legend(fontsize=8, loc="upper left")
    axis.set_ylabel("°C")
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m\n%H:%M", tz=MOSCOW))
    axis.xaxis.set_major_locator(mdates.HourLocator(interval=1) if hourly else mdates.AutoDateLocator(minticks=4, maxticks=8))


def main():
    report = json.loads((ROOT / "reports/anomalies-3w.json").read_text())
    manifest, series = load_series(ROOT / "reports/history-3w", 20496, 8207, 8429, 8832)
    rows = frames(manifest, series, 8207)
    incidents = report["incidents"]
    fig, axes = plt.subplots(3, 1, figsize=(17, 10), sharex=True, constrained_layout=True)
    plot_line(axes[0], rows, "air", "Воздух: факт", "#147b89")
    plot_line(axes[0], rows, "air_target", "Воздух: цель", "#d75a35", True)
    plot_line(axes[1], rows, "coolant", "Теплоноситель: факт", "#147b89")
    plot_line(axes[1], rows, "coolant_target", "Теплоноситель: расчётная уставка", "#d75a35", True)
    plot_line(axes[2], rows, "weather", "Погода из интернета", "#69519d")
    for index, incident in enumerate(incidents, 1):
        begin = datetime.fromisoformat(incident["start"])
        end = datetime.fromisoformat(incident["recovery"] or manifest["end"])
        for axis in axes:
            axis.axvspan(begin, end, color="#d75a35", alpha=.12)
        axes[0].annotate(str(index), (begin, 25.5), color="#9c3219", fontsize=12)
    for axis in axes:
        style(axis)
    axes[0].set_title("Температура воздуха и заданная температура")
    axes[1].set_title("Температура теплоносителя и запрос ZONT")
    axes[2].set_title("Уличная температура — для контекста")
    fig.suptitle("21 сутки: четыре группы эпизодов без ожидаемого теплового отклика\nВремя — Москва; затенены наблюдаемые интервалы, а не доказанные причины отказа", fontsize=14)
    fig.savefig(ROOT / "reports/heating-3w-overview.png", dpi=150)
    fig.savefig(ROOT / "reports/heating-3w-overview.svg")
    plt.close(fig)

    fig, axes = plt.subplots(len(incidents), 2, figsize=(16, 3.3 * len(incidents)), constrained_layout=True)
    for index, incident in enumerate(incidents):
        begin = epoch(incident["start"]) - 3600
        end = epoch(incident["recovery"] or manifest["end"]) + 1800
        selected = [row for row in rows if begin <= row["time"] <= end]
        left, right = axes[index]
        plot_line(left, selected, "air", "Воздух: факт", "#147b89")
        plot_line(left, selected, "air_target", "Воздух: цель", "#d75a35", True)
        plot_line(right, selected, "coolant", "Теплоноситель: факт", "#147b89")
        plot_line(right, selected, "coolant_target", "Запрос ZONT", "#d75a35", True)
        warning = datetime.fromisoformat(incident["warning"])
        for axis in (left, right):
            axis.axvline(warning, color="#368a46", linestyle="--", label="Первый сигнал детектора")
            axis.axvspan(datetime.fromisoformat(incident["start"]),
                         datetime.fromisoformat(incident["recovery"] or manifest["end"]), color="#d75a35", alpha=.08)
            style(axis, hourly=True)
        left.set_title(f"{index+1}. {warning.astimezone(MOSCOW):%d.%m.%Y}; первый сигнал {warning.astimezone(MOSCOW):%H:%M}")
        right.set_title("Высокая уставка без достаточного температурного отклика")
    fig.suptitle("Четыре группы: зелёная линия — срабатывание при ретропроверке только по прошлым данным", fontsize=13)
    fig.savefig(ROOT / "reports/heating-3w-incidents.png", dpi=150)
    fig.savefig(ROOT / "reports/heating-3w-incidents.svg")
    plt.close(fig)

    fields = ["number", "start_moscow", "warning_moscow", "confirmation_moscow", "first_heat_response_moscow",
              "duration_minutes", "min_air_C", "min_coolant_C", "max_air_deficit_C", "warning_segments"]
    with (ROOT / "reports/anomalies-3w-incidents.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for index, incident in enumerate(incidents, 1):
            localized = {field: datetime.fromisoformat(incident[key]).astimezone(MOSCOW).isoformat()
                         if incident[key] else None
                         for field, key in [("start_moscow", "start"), ("warning_moscow", "warning"),
                                            ("confirmation_moscow", "confirmation"), ("first_heat_response_moscow", "recovery")]}
            writer.writerow({"number": index, **localized, "duration_minutes": incident["duration_minutes"],
                             "min_air_C": incident["air_min"], "min_coolant_C": incident["coolant_min"],
                             "max_air_deficit_C": round(incident["max_air_deficit"], 1), "warning_segments": len(incident["segments"])})
    print("Created reports/heating-3w-overview.png, heating-3w-incidents.png and anomalies-3w-incidents.csv")


if __name__ == "__main__":
    main()
