"""Data-only SVG adapter for boiler_cli; no fetching, writing or browser effects.

The caller selects devices, supplies current descriptors and fetches HistoryResult
through its facade. Graphs name explicit keys, never infer air/water/target from
UI labels. Each graph belongs to one device and shares one unit on its y axis.
Measurements are markers at real timestamps, without interpolation. STEP rows
join consecutive good records only; incomplete archives cannot establish spans.
Nothing is extrapolated to requested bounds. Nulls retain their actual timestamps.
"""

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from zontcontrol.controller.contracts import (
    DeviceInfo, HistoryResult, InvalidArguments, Quality, SeriesKind,
    ValueDescriptor, ValueKey, ValueType,
)


_GRAPH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_COLORS = ("#1565c0", "#c62828", "#2e7d32", "#6a1b9a", "#a34600")
_LEFT, _RIGHT, _TOP, _BOTTOM = 90, 1040, 120, 400


def _require(condition):
    if not condition:
        raise InvalidArguments()


def _label(value):
    _require(isinstance(value, str) and bool(value.strip()))


def _graph_id(value):
    _require(isinstance(value, str) and _GRAPH_ID.fullmatch(value) is not None)


@dataclass(frozen=True, slots=True)
class SeriesSpec:
    key: ValueKey
    label: str | None = None

    def __post_init__(self):
        _require(isinstance(self.key, ValueKey))
        if self.label is not None:
            _label(self.label)


@dataclass(frozen=True, slots=True)
class GraphSpec:
    graph_id: str
    title: str
    series: tuple[SeriesSpec, ...]

    def __post_init__(self):
        _graph_id(self.graph_id)
        _label(self.title)
        _require(isinstance(self.series, tuple) and bool(self.series))
        _require(all(isinstance(item, SeriesSpec) for item in self.series))
        _require(len({item.key for item in self.series}) == len(self.series))
        _require(len({item.key.device_id for item in self.series}) == 1)

    @property
    def device_id(self):
        return self.series[0].key.device_id


@dataclass(frozen=True, slots=True)
class Chart:
    device_id: int
    graph_id: str
    svg: str

    def __post_init__(self):
        _require(type(self.device_id) is int and self.device_id > 0)
        _graph_id(self.graph_id)
        _require(isinstance(self.svg, str))

    @property
    def filename(self):
        """Labels never become paths; equal element IDs on devices stay distinct."""
        return f"device-{self.device_id}-{self.graph_id}.svg"


def chart_path(output_directory: str | Path, chart: Chart) -> Path:
    """Resolve a contained artifact path, without creating or writing anything.

    Refuse an existing symlink that points outside the selected directory.
    The eventual writer owns file creation/overwrite policy and should avoid
    following symlinks at open time too; this helper is not a filesystem lock.
    """
    _require(isinstance(chart, Chart) and isinstance(output_directory, (str, Path)))
    try:
        directory = Path(output_directory).resolve()
        destination = (directory / chart.filename).resolve()
        _require(destination.parent == directory)
    except (OSError, RuntimeError, ValueError):
        raise InvalidArguments() from None
    return destination


def _utc(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None
             and value.utcoffset() is not None)
    return value.astimezone(timezone.utc)


def _index(items, item_type, identity):
    _require(isinstance(items, tuple) and all(isinstance(item, item_type) for item in items))
    indexed = {identity(item): item for item in items}
    _require(len(indexed) == len(items))
    return indexed


def render_charts(graphs: tuple[GraphSpec, ...], *, devices: tuple[DeviceInfo, ...],
                  descriptors: tuple[ValueDescriptor, ...], histories: tuple[HistoryResult, ...],
                  start: datetime, end: datetime, display_timezone: str = "UTC") -> tuple[Chart, ...]:
    """Validate and render declared graphs for exactly the selected ``devices``.

    ``devices`` is the explicit selection, not an account-wide device list.
    Graphs and data for other devices are ignored. Each selected graph requires
    known numeric, history-capable descriptors, equal units, and one result with
    the requested interval. Missing rows/empty histories are drawn as missing,
    not as zero or normal. Duplicate identities and conflicting DTOs are errors.
    Times accept aware datetimes and normalize to UTC; display timezone is IANA.
    """
    start, end = _utc(start), _utc(end)
    _require(start < end)
    _require(isinstance(display_timezone, str))
    try:
        zone = ZoneInfo(display_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise InvalidArguments() from None
    selected = _index(devices, DeviceInfo, lambda device: device.device_id)
    _require(bool(selected))
    _require(isinstance(graphs, tuple) and all(isinstance(graph, GraphSpec) for graph in graphs))
    chosen = tuple(graph for graph in graphs if graph.device_id in selected)
    _index(chosen, GraphSpec, lambda graph: (graph.device_id, graph.graph_id))
    _require(isinstance(descriptors, tuple)
             and all(isinstance(item, ValueDescriptor) for item in descriptors))
    _require(isinstance(histories, tuple)
             and all(isinstance(item, HistoryResult) for item in histories))
    available = _index(tuple(item for item in descriptors if item.key.device_id in selected),
                       ValueDescriptor, lambda item: item.key)
    results = _index(tuple(item for item in histories if item.device_id in selected),
                     HistoryResult, lambda item: item.device_id)
    # Validate every chosen graph before rendering any artifact.
    for graph in chosen:
        _require(graph.device_id in results)
        result = results[graph.device_id]
        _require(result.start == start and result.end == end)
        rows = {row.key: row for row in result.series}
        units = set()
        for item in graph.series:
            _require(item.key in available)
            description = available[item.key]
            _require(description.history_available
                     and description.value_type in (ValueType.NUMBER, ValueType.INTEGER))
            units.add(description.unit)
            if item.key in rows:
                row = rows[item.key]
                _require(row.unit == description.unit and row.value_type is description.value_type
                         and row.kind is description.history_kind)
        _require(len(units) == 1)
    return tuple(Chart(graph.device_id, graph.graph_id,
                       _render(graph, selected[graph.device_id], available,
                               results[graph.device_id], start, end, zone))
                 for graph in chosen)


def _xml_text(value):
    # Escaping <>& alone does not make control characters/surrogates valid XML.
    return "".join(char if (char in "\t\n\r" or 0x20 <= ord(char) <= 0xD7FF
                          or 0xE000 <= ord(char) <= 0xFFFD
                          or 0x10000 <= ord(char) <= 0x10FFFF) else "�" for char in str(value))


def _element(parent, tag, text=None, **attributes):
    node = ET.SubElement(parent, tag, {key.replace("_", "-"): _xml_text(value)
                                     for key, value in attributes.items()})
    if text is not None:
        node.text = _xml_text(text)
    return node


def _text(parent, x, y, value, **attributes):
    return _element(parent, "text", value, x=x, y=y, **attributes)


def _key_text(key):
    return f"{key.device_id}/{key.kind}/{key.element_id}/{key.field}"


def _span(points):
    return (f"{points[0].measured_at.isoformat()} .. {points[-1].measured_at.isoformat()}"
            if points else "none")


def _render(graph, device, available, result, start, end, zone):
    rows = {row.key: row for row in result.series}
    values = [point.value for item in graph.series if item.key in rows
              for point in rows[item.key].points if point.value is not None]
    low, high = (min(values), max(values)) if values else (0, 1)
    # Coordinates only: no new measurements or history points are constructed.
    # Scale before subtracting to keep opposite large finite values finite.
    scale = max(abs(low), abs(high), 1)
    scaled_low, scaled_high = low / scale, high / scale
    if low == high:
        scaled_low -= 0.5
        scaled_high += 0.5

    def x(timestamp):
        return f"{_LEFT + (timestamp - start).total_seconds() / (end - start).total_seconds() * (_RIGHT - _LEFT):.2f}"

    def y(value):
        return f"{_BOTTOM - (value / scale - scaled_low) / (scaled_high - scaled_low) * (_BOTTOM - _TOP):.2f}"

    height = 500 + 120 * len(graph.series)
    root = ET.Element("svg", {"xmlns": "http://www.w3.org/2000/svg", "viewBox": f"0 0 1100 {height}",
                              "width": "1100", "height": str(height), "role": "img",
                              "aria-label": _xml_text(f"{device.name}: {graph.title}")})
    _element(root, "title", f"{device.name} [{device.device_id}]: {graph.title}")
    _element(root, "desc", "Measurements are real timestamped markers. Targets use known step spans. "
             "Nulls and non-good quality break spans; no extrapolation or interpolated measurements.")
    _element(root, "metadata", json.dumps({"device_id": device.device_id, "graph_id": graph.graph_id,
             "start": start.isoformat(), "end": end.isoformat(), "display_timezone": zone.key},
             ensure_ascii=True))
    _element(root, "rect", x=0, y=0, width=1100, height=height, fill="white")
    body = _element(root, "g", font_family="sans-serif", font_size=13, fill="#202020")
    _text(body, 20, 28, f"{device.name} [{device.device_id}] — {graph.title}", font_size=20)
    _text(body, 20, 53, f"UTC: {start.isoformat()} .. {end.isoformat()}")
    _text(body, 20, 76, f"Display ({zone.key}): {start.astimezone(zone).isoformat()} .. "
          f"{end.astimezone(zone).isoformat()}")
    complete = result.complete and all(item.key in rows for item in graph.series)
    _text(body, 20, 99, f"history_complete={str(complete).lower()}; "
          f"time_truncated={str(result.time_truncated).lower()}; "
          f"reasons: {', '.join(result.incomplete_reasons) or 'none'}")
    _element(body, "line", x1=_LEFT, x2=_LEFT, y1=_TOP, y2=_BOTTOM, stroke="#555")
    _element(body, "line", x1=_LEFT, x2=_RIGHT, y1=_BOTTOM, y2=_BOTTOM, stroke="#555")
    unit = available[graph.series[0].key].unit
    _text(body, 20, _TOP - 7, unit or "unit unspecified")
    if values:
        for value in ((low,) if low == high else (low, high)):
            _text(body, 20, y(value), f"{value:g}", data_axis_value=value)
    else:
        _text(body, 400, 250, "No numeric data")
    for fraction in (0, 0.25, 0.5, 0.75, 1):
        instant = start + (end - start) * fraction
        position = x(instant)
        _element(body, "line", x1=position, x2=position, y1=_BOTTOM, y2=_BOTTOM + 5, stroke="#555")
        _text(body, position, _BOTTOM + 22, instant.astimezone(zone).strftime("%H:%M:%S"), text_anchor="middle")
    _text(body, 20, 452, "Markers: real records; hollow = non-good quality; × = null (no y value).")
    for index, item in enumerate(graph.series):
        description = available[item.key]
        row = rows.get(item.key)
        color = _COLORS[index % len(_COLORS)]
        group = _element(body, "g", data_key=_key_text(item.key))
        points = row.points if row is not None else ()
        base = 487 + index * 120
        _text(group, 20, base, f"{item.label or description.name} [{unit or 'unit unspecified'}] — "
              f"{_key_text(item.key)}; {description.history_kind.value}", fill=color)
        _text(group, 20, base + 21, f"source: {row.source if row else description.source}; records: {_span(points)}")
        reasons = row.incomplete_reasons if row is not None else ("series missing",)
        uncertain = bool(reasons or result.incomplete_reasons or result.time_truncated)
        _text(group, 20, base + 42, f"reasons: {', '.join(reasons) or 'none'}" +
              ("; step spans omitted" if uncertain and description.history_kind is SeriesKind.STEP else ""))
        counts = ", ".join(f"{quality.value}={sum(point.quality is quality for point in points)}"
                           for quality in Quality)
        _text(group, 20, base + 63, f"quality: {counts}; null={sum(point.value is None for point in points)}")
        good = tuple(point for point in points if point.value is not None and point.quality is Quality.GOOD)
        _text(group, 20, base + 84, f"numeric good: {_span(good)}")
        if row is not None and row.kind is SeriesKind.STEP and not uncertain:
            for left, right in zip(points, points[1:]):
                if (left.value is not None and right.value is not None
                        and left.quality is Quality.GOOD and right.quality is Quality.GOOD):
                    _element(group, "path", d=f"M {x(left.measured_at)} {y(left.value)} "
                             f"H {x(right.measured_at)} V {y(right.value)}", fill="none", stroke=color,
                             data_start=left.measured_at.isoformat(), data_end=right.measured_at.isoformat())
        for point in points:
            attributes = {"data_time": point.measured_at.isoformat(),
                          "data_value": "null" if point.value is None else str(point.value),
                          "data_quality": point.quality.value}
            if point.value is None:
                marker = _text(group, x(point.measured_at), _BOTTOM - 5, "×", fill=color,
                               text_anchor="middle", **attributes)
            else:
                marker = _element(group, "circle", cx=x(point.measured_at), cy=y(point.value), r=4,
                                  fill=color if point.quality is Quality.GOOD else "white",
                                  stroke=color, **attributes)
            _element(marker, "title", f"{point.measured_at.isoformat()}; "
                     f"display={point.measured_at.astimezone(zone).isoformat()}; "
                     f"value={attributes['data_value']}; quality={point.quality.value}")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
