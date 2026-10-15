"""Offline SVG contract; the renderer consumes DTOs rather than a controller."""

import importlib
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zontcontrol.controller.contracts import (
    DeviceInfo, HistoryPoint, HistoryResult, HistorySeries, InvalidArguments,
    Quality, SeriesKind, ValueDescriptor, ValueKey, ValueType,
)
from zontcontrol.algorithms.charts import (
    GraphSpec, SeriesSpec, chart_path, render_charts,
)


START = datetime(2026, 10, 15, tzinfo=timezone.utc)
END = START + timedelta(hours=1)
NS = {"s": "http://www.w3.org/2000/svg"}


def key(device=1, field="actual"):
    return ValueKey(device, "circuit", 7, field)


def descriptor(value_key, *, kind=SeriesKind.MEASUREMENT, name="Air", unit="°C"):
    return ValueDescriptor(value_key, name, unit, "archive", value_key.field,
                           ValueType.NUMBER, True, True, kind)


def row(value_key, values=(18, 19, 20), *, kind=SeriesKind.MEASUREMENT,
        qualities=None, reasons=()):
    qualities = qualities or (Quality.GOOD,) * len(values)
    times = (START + timedelta(minutes=30 * i) for i in range(len(values)))
    return HistorySeries(value_key, ValueType.NUMBER, "°C", "archive", kind,
                         tuple(HistoryPoint(t, v, q) for t, v, q in zip(times, values, qualities)),
                         reasons)


def history(*rows, device=1, **kwargs):
    return HistoryResult(device, START, END, END, tuple(rows), **kwargs)


class ChartTests(unittest.TestCase):
    def render(self, *, graphs=None, descriptors=None, histories=None, devices=None, **kwargs):
        return render_charts(
            (GraphSpec("air", "Air actual / target", (SeriesSpec(key()),)),)
            if graphs is None else graphs,
            devices=(DeviceInfo(1, "Home", "SMART", True),) if devices is None else devices,
            descriptors=(descriptor(key()),) if descriptors is None else descriptors,
            histories=(history(row(key())),) if histories is None else histories,
            start=kwargs.pop("start", START), end=kwargs.pop("end", END), **kwargs,
        )

    def tree(self, artifact):
        return ET.fromstring(artifact.svg)

    def text(self, artifact):
        return " ".join(self.tree(artifact).itertext())

    def test_air_water_and_arbitrary_declared_pairs(self):
        actual = key()
        target = key(field="target")
        water = key(field="water_actual")
        calculated = key(field="calculated_water_target")
        arbitrary = ValueKey(1, "sensor", 15, "humidity")
        pairs = ((actual, target), (water, calculated), (arbitrary, actual))
        descriptors = tuple(descriptor(k, kind=(SeriesKind.STEP if k in (target, calculated)
                                               else SeriesKind.MEASUREMENT), name=k.field)
                            for k in (actual, target, water, calculated, arbitrary))
        rows = tuple(row(d.key, kind=d.history_kind) for d in descriptors)
        graphs = tuple(GraphSpec(f"pair_{i}", f"Pair {i}", tuple(SeriesSpec(k) for k in pair))
                       for i, pair in enumerate(pairs))
        result = self.render(graphs=graphs, descriptors=descriptors, histories=(history(*rows),))
        self.assertEqual(len(result), 3)
        for chart, pair in zip(result, pairs):
            self.assertEqual(chart.device_id, 1)
            self.assertIn("°C", self.text(chart))
            for k in pair:
                self.assertIn(k.field, self.text(chart))
        self.assertIn("calculated_water_target", self.text(result[1]))

    def test_measurements_are_only_real_timestamped_markers(self):
        chart, = self.render()
        root = self.tree(chart)
        marks = root.findall(".//s:circle[@data-time]", NS)
        self.assertEqual([m.get("data-time") for m in marks],
                         [(START + timedelta(minutes=m)).isoformat() for m in (0, 30, 60)])
        self.assertEqual([m.get("data-value") for m in marks], ["18", "19", "20"])
        self.assertEqual(root.findall(".//s:path", NS), [])
        self.assertIn("history_complete=true", self.text(chart))

    def test_target_steps_do_not_extend_before_or_after_records(self):
        k = key(field="target")
        points = (HistoryPoint(START + timedelta(minutes=10), 20),
                  HistoryPoint(START + timedelta(minutes=40), 22))
        target = replace(row(k, kind=SeriesKind.STEP), points=points)
        chart, = self.render(graphs=(GraphSpec("target", "Target", (SeriesSpec(k),)),),
                             descriptors=(descriptor(k, kind=SeriesKind.STEP),),
                             histories=(history(target),))
        paths = self.tree(chart).findall(".//s:path", NS)
        self.assertEqual(len(paths), 1)
        self.assertRegex(paths[0].get("d"), r"^M [\d.]+ [\d.]+ H [\d.]+ V [\d.]+$")
        self.assertEqual(paths[0].get("data-start"), points[0].measured_at.isoformat())
        self.assertEqual(paths[0].get("data-end"), points[1].measured_at.isoformat())
        self.assertIn("history_complete=false", self.text(chart))

    def test_nulls_and_non_good_quality_break_steps(self):
        k = key(field="target")
        for value, quality in ((None, Quality.GOOD), (21, Quality.UNKNOWN),
                               (21, Quality.STALE), (21, Quality.MISSING)):
            with self.subTest(value=value, quality=quality):
                target = row(k, (20, value, 22), kind=SeriesKind.STEP,
                             qualities=(Quality.GOOD, quality, Quality.GOOD))
                chart, = self.render(
                    graphs=(GraphSpec("target", "Target", (SeriesSpec(k),)),),
                    descriptors=(descriptor(k, kind=SeriesKind.STEP),),
                    histories=(history(target),))
                self.assertEqual(self.tree(chart).findall(".//s:path", NS), [])
                self.assertIn("history_complete=false", self.text(chart))
                self.assertIn(f"quality={quality.value}", self.text(chart))
                if value is None:
                    nulls = self.tree(chart).findall(".//*[@data-value='null']", NS)
                    self.assertEqual(len(nulls), 1)
                    self.assertEqual(nulls[0].get("data-time"), (START + timedelta(minutes=30)).isoformat())

    def test_incomplete_archive_does_not_claim_known_step_spans(self):
        k = key(field="target")
        for result in (history(row(k, kind=SeriesKind.STEP, reasons=("archive_gap",))),
                       history(row(k, kind=SeriesKind.STEP), incomplete_reasons=("part_missing",)),
                       history(row(k, kind=SeriesKind.STEP), time_truncated=True)):
            with self.subTest(result=result):
                chart, = self.render(
                    graphs=(GraphSpec("target", "Target", (SeriesSpec(k),)),),
                    descriptors=(descriptor(k, kind=SeriesKind.STEP),), histories=(result,))
                self.assertEqual(self.tree(chart).findall(".//s:path", NS), [])
                self.assertIn("history_complete=false", self.text(chart))
                self.assertIn("step spans omitted", self.text(chart))
                for reason in result.incomplete_reasons + result.series[0].incomplete_reasons:
                    self.assertIn(reason, self.text(chart))
                self.assertIn(f"time_truncated={str(result.time_truncated).lower()}", self.text(chart))

    def test_empty_missing_and_all_null_series_are_explicit(self):
        for result in (history(), history(row(key(), ())), history(row(key(), (None, None, None)))):
            with self.subTest(result=result):
                chart, = self.render(histories=(result,))
                self.assertIn("No numeric data", self.text(chart))
                self.assertIn("history_complete=false", self.text(chart))
                self.assertEqual(self.tree(chart).findall(".//s:circle[@data-time]", NS), [])
        chart, = self.render(histories=(history(),))
        self.assertIn("series missing", self.text(chart))
        self.assertIn("records: none", self.text(chart))

    def test_coverage_and_source_are_explicit_and_not_filled(self):
        partial = replace(row(key()), points=(HistoryPoint(START + timedelta(minutes=15), 19),))
        chart, = self.render(histories=(history(partial),))
        text = self.text(chart)
        self.assertIn("records: 2026-10-15T00:15:00+00:00 .. 2026-10-15T00:15:00+00:00", text)
        self.assertIn("archive", text)
        self.assertIn("history_complete=false", text)
        self.assertEqual(len(self.tree(chart).findall(".//s:circle[@data-time]", NS)), 1)

    def test_missing_selected_series_cannot_inherit_another_series_completeness(self):
        other = key(field="unselected")
        chart, = self.render(histories=(history(row(other)),))
        self.assertIn("history_complete=false", self.text(chart))
        self.assertIn("series missing", self.text(chart))
        self.assertIn("No numeric data", self.text(chart))

    def test_constant_series_has_one_correctly_placed_axis_value(self):
        chart, = self.render(histories=(history(row(key(), (20, 20, 20))),))
        root = self.tree(chart)
        ticks = root.findall(".//s:text[@data-axis-value]", NS)
        self.assertEqual(len(ticks), 1)
        self.assertEqual(ticks[0].get("data-axis-value"), "20")
        marker = root.find(".//s:circle[@data-time]", NS)
        self.assertEqual(ticks[0].get("y"), marker.get("cy"))

    def test_utc_bounds_and_display_timezone_are_distinct(self):
        offset = timezone(timedelta(hours=3))
        chart, = self.render(start=START.astimezone(offset), end=END.astimezone(offset),
                             display_timezone="Europe/Moscow")
        text = self.text(chart)
        self.assertIn(f"UTC: {START.isoformat()} .. {END.isoformat()}", text)
        self.assertIn("Europe/Moscow", text)
        self.assertIn("2026-10-15T03:00:00+03:00", text)
        self.assertEqual(self.tree(chart).find(".//s:circle[@data-time]", NS).get("data-time"),
                         START.isoformat())

    def test_same_element_id_on_different_devices_never_mix(self):
        k1, k2 = key(1), key(2)
        graphs = tuple(GraphSpec("air", "Air", (SeriesSpec(k),)) for k in (k1, k2))
        result = self.render(graphs=graphs,
                             devices=(DeviceInfo(1, "One", None, True), DeviceInfo(2, "Two", None, True)),
                             descriptors=(descriptor(k1), descriptor(k2)),
                             histories=(history(row(k1, (1, 2, 3))),
                                        history(row(k2, (51, 52, 53)), device=2)))
        self.assertEqual([c.device_id for c in result], [1, 2])
        self.assertNotEqual(result[0].filename, result[1].filename)
        for chart, expected in zip(result, (["1", "2", "3"], ["51", "52", "53"])):
            self.assertEqual([m.get("data-value") for m in self.tree(chart).findall(
                ".//s:circle[@data-time]", NS)], expected)

    def test_only_selected_devices_and_series_appear(self):
        k2 = key(2)
        chart, = self.render(
            graphs=(GraphSpec("air", "Air", (SeriesSpec(key()),)),
                    GraphSpec("secret", "UNSELECTED", (SeriesSpec(k2),))),
            descriptors=(descriptor(key()), descriptor(k2, name="UNSELECTED")),
            histories=(history(row(key())), history(row(k2, (50, 51, 52)), device=2)))
        self.assertNotIn("UNSELECTED", chart.svg)
        self.assertNotIn("50", [m.get("data-value") for m in self.tree(chart).findall(
            ".//s:circle[@data-time]", NS)])

    def test_labels_are_escaped_and_never_used_as_paths(self):
        label = '<script>alert("x")</script> & \'label\''
        chart, = self.render(graphs=(GraphSpec("safe", label, (SeriesSpec(key(), label),)),),
                             devices=(DeviceInfo(1, "../" + label, None, True),),
                             descriptors=(descriptor(key(), name=label, unit='°C & <unit>'),),
                             histories=(history(replace(row(key()), unit='°C & <unit>')),))
        self.assertIn(label, self.text(chart))
        self.assertNotIn("<script>", chart.svg)
        self.assertIn("&lt;script&gt;", chart.svg)
        self.assertEqual(chart.filename, "device-1-safe.svg")
        self.assertEqual(self.tree(chart).findall(".//s:script", NS), [])

    def test_invalid_xml_control_characters_are_replaced(self):
        chart, = self.render(descriptors=(descriptor(key(), name="Air\x00\ud800"),))
        self.assertIn("Air��", self.text(chart))

    def test_chart_path_is_contained_without_writing(self):
        chart, = self.render()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = chart_path(root, chart)
            self.assertEqual(path, root / chart.filename)
            self.assertFalse(path.exists())
            outside = root.parent / (root.name + "-outside.svg")
            path.symlink_to(outside)
            with self.assertRaises(InvalidArguments):
                chart_path(root, chart)

    def test_invalid_graphs_and_path_traversal_are_rejected(self):
        for graph_id in ("../escape", "/absolute", "a/b", "a\\b", "", ".", "..", "<x>"):
            with self.subTest(graph_id=graph_id), self.assertRaises(InvalidArguments):
                GraphSpec(graph_id, "Title", (SeriesSpec(key()),))
        for series in ((), (SeriesSpec(key()), SeriesSpec(key())),
                       (SeriesSpec(key()), SeriesSpec(key(2)))):
            with self.subTest(series=series), self.assertRaises(InvalidArguments):
                GraphSpec("air", "Title", series)
        with self.assertRaises(InvalidArguments):
            SeriesSpec(key(), " ")

    def test_intervals_and_timezones_are_validated(self):
        for kwargs in ({"start": START.replace(tzinfo=None)}, {"end": END.replace(tzinfo=None)},
                       {"end": START}, {"start": END}, {"display_timezone": "No/SuchZone"},
                       {"display_timezone": "../UTC"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(InvalidArguments):
                self.render(**kwargs)
        with self.assertRaises(InvalidArguments):
            self.render(histories=(replace(history(row(key())), start=START - timedelta(seconds=1)),))

    def test_unavailable_unknown_and_non_numeric_keys_are_rejected(self):
        unavailable = replace(descriptor(key()), history_available=False, history_kind=None)
        text_value = replace(descriptor(key()), value_type=ValueType.TEXT)
        for descriptors in ((), (unavailable,), (text_value,), (descriptor(key()), descriptor(key()))):
            with self.subTest(descriptors=descriptors), self.assertRaises(InvalidArguments):
                self.render(descriptors=descriptors)
        with self.assertRaises(InvalidArguments):
            self.render(graphs=(GraphSpec("wrong", "Wrong", (SeriesSpec(key(field="unknown")),)),))

    def test_data_must_match_declared_kind_type_and_unit(self):
        for mismatch in (replace(row(key()), kind=SeriesKind.STEP), replace(row(key()), unit="F"),
                         replace(row(key()), value_type=ValueType.INTEGER)):
            with self.subTest(mismatch=mismatch), self.assertRaises(InvalidArguments):
                self.render(histories=(history(mismatch),))
        with self.assertRaises(InvalidArguments):
            self.render(histories=())
        with self.assertRaises(InvalidArguments):
            self.render(histories=(history(row(key())), history(row(key()))))

    def test_shared_axis_requires_same_units(self):
        k2 = key(field="humidity")
        with self.assertRaises(InvalidArguments):
            self.render(graphs=(GraphSpec("mixed", "Mixed", (SeriesSpec(key()), SeriesSpec(k2))),),
                        descriptors=(descriptor(key()), descriptor(k2, unit="%")))

    def test_duplicate_graphs_and_devices_are_rejected(self):
        graph = GraphSpec("air", "Air", (SeriesSpec(key()),))
        with self.assertRaises(InvalidArguments):
            self.render(graphs=(graph, graph))
        with self.assertRaises(InvalidArguments):
            self.render(devices=(DeviceInfo(1, "One", None, True), DeviceInfo(1, "Other", None, True)))
        with self.assertRaises(InvalidArguments):
            self.render(devices=())

    def test_no_optional_imports_browser_or_network_even_in_fresh_process(self):
        # Guards are installed before import, so accidental import-time effects fail too.
        code = '''
import builtins, socket, webbrowser
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'matplotlib', 'numpy', 'PIL', 'selenium', 'playwright'}:
        raise AssertionError(name)
    return original(name, *args, **kwargs)
def forbidden(*args, **kwargs):
    raise AssertionError('external effect')
builtins.__import__ = guarded
socket.socket = forbidden
socket.create_connection = forbidden
webbrowser.open = forbidden
webbrowser.open_new = forbidden
webbrowser.open_new_tab = forbidden
import unittest
from tests.test_charts import ChartTests
case = ChartTests('test_air_water_and_arbitrary_declared_pairs')
result = unittest.TestResult()
case.run(result)
assert result.wasSuccessful(), (result.errors, result.failures)
'''
        result = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True,
                                cwd=Path(__file__).resolve().parents[1], timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        module = importlib.import_module("zontcontrol.algorithms.charts")
        self.assertFalse(hasattr(module, "Controller"))


if __name__ == "__main__":
    unittest.main()
