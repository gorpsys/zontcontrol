"""API v1 consumer scenarios, independent of HTTP and credentials."""

import dataclasses
import inspect
import math
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from zontcontrol.controller import contracts as api
from tests.fakes.clock import FakeClock
from tests.fakes.zont import FakeZontServer


UTC = timezone.utc
START = datetime(2026, 10, 15, tzinfo=UTC)
END = START + timedelta(minutes=45)


def key(device_id=1):
    return api.ValueKey(device_id, "sensor", 7, "temperature")


def reading(device_id=1, **overrides):
    fields = dict(key=key(device_id), value=21.5, value_type=api.ValueType.NUMBER,
                  unit="°C", source="sensor", measured_at=None, received_at=END,
                  quality=api.Quality.GOOD)
    return api.ValueReading(**(fields | overrides))


def series(device_id=1, **overrides):
    fields = dict(key=key(device_id), value_type=api.ValueType.NUMBER, unit="°C",
                  source="archive", kind=api.SeriesKind.MEASUREMENT,
                  points=(api.HistoryPoint(START, 20.0), api.HistoryPoint(END, 21.5)))
    return api.HistorySeries(**(fields | overrides))


class ControllerContractTests(unittest.TestCase):
    def test_two_devices_share_element_ids_without_aliasing(self):
        values = {key(1): reading(1), key(2): reading(2, value=18.0)}
        self.assertEqual(len(values), 2)
        self.assertEqual(values[key(2)].value, 18.0)
        self.assertNotEqual(api.ControlKey(1, "switch", 7), api.ControlKey(2, "switch", 7))
        devices = (api.DeviceInfo(1, "Home", "SMART 2.0", True),
                   api.DeviceInfo(2, "Cabin", "SMART 2.0", False))
        self.assertEqual([device.device_id for device in devices], [1, 2])

    def test_unknown_measurement_time_stays_unknown_and_snapshots_are_not_atomic(self):
        value = reading()
        snapshot = api.Snapshot(1, END, (value,))
        self.assertIsNone(value.measured_at)
        self.assertEqual(value.received_at, END)
        self.assertFalse(snapshot.is_atomic)
        self.assertEqual(snapshot.values[0].quality, api.Quality.GOOD)

    def test_dtos_and_nested_data_are_immutable(self):
        command = api.ValidatedCommand(api.ControlKey(1, "switch", 7), "set-switch",
                                       (("target_state", True),))
        for dto, field in ((command, "action"), (reading(), "value"),
                           (series(), "points"), (api.Snapshot(1, END, (reading(),)), "values")):
            with self.subTest(dto=type(dto).__name__), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(dto, field, None)
        for make in (lambda: api.Snapshot(1, END, [reading()]),
                     lambda: series(points=[api.HistoryPoint(START, 20)]),
                     lambda: api.ValidatedCommand(command.control_key, "set-switch",
                                                   (("target_state", {"mutable": True}),))):
            with self.assertRaises(api.InvalidArguments):
                make()

    def test_invalid_addresses_and_value_types_are_rejected(self):
        for bad in (True, 0, -1, "1", 1.0, None):
            for make in (lambda: key(bad), lambda: api.ControlKey(1, "switch", bad),
                         lambda: api.DeviceInfo(bad, "Home", None, True)):
                with self.subTest(bad=bad), self.assertRaises(api.InvalidArguments):
                    make()
        for value in ({"token": "private"}, [], math.nan, math.inf, True):
            with self.subTest(value=type(value).__name__), self.assertRaises(api.InvalidArguments):
                reading(value=value)
        for value_type, good, bad in ((api.ValueType.BOOLEAN, False, 0),
                                      (api.ValueType.INTEGER, 7, 7.5),
                                      (api.ValueType.TEXT, "active", 1)):
            self.assertEqual(reading(value=good, value_type=value_type).value, good)
            with self.assertRaises(api.InvalidArguments):
                reading(value=bad, value_type=value_type)
        self.assertIsNone(reading(value=None, quality=api.Quality.UNKNOWN).value)
        for make in (lambda: api.ValueKey(1, "", 7, "temperature"),
                     lambda: api.ValueKey(1, "sensor", 7, " "),
                     lambda: reading(value_type="number"),
                     lambda: reading(quality="good")):
            with self.assertRaises(api.InvalidArguments):
                make()

    def test_utc_timestamps_are_real_and_not_replaced(self):
        local = START.astimezone(timezone(timedelta(hours=3)))
        self.assertEqual(reading(measured_at=local).measured_at.utcoffset(), timedelta(0))
        self.assertEqual(reading(measured_at=local).measured_at, START)
        for bad in (datetime(2026, 10, 15), "2026-10-15", None):
            with self.assertRaises(api.InvalidArguments):
                reading(received_at=bad)
        with self.assertRaises(api.InvalidArguments):
            reading(measured_at=END + timedelta(seconds=1))

    def test_mixed_device_data_and_duplicate_keys_are_rejected(self):
        for values in ((reading(2),), (reading(), reading())):
            with self.assertRaises(api.InvalidArguments):
                api.Snapshot(1, END, values)
        for rows in ((series(2),), (series(), series())):
            with self.assertRaises(api.InvalidArguments):
                api.HistoryResult(1, START, END, END, rows)

    def test_history_preserves_null_truncation_gaps_and_actual_coverage(self):
        middle = START + timedelta(minutes=20)
        row = series(points=(api.HistoryPoint(middle, None, api.Quality.MISSING),
                             api.HistoryPoint(END, 21.5)), incomplete_reasons=("archive_gap",))
        result = api.HistoryResult(1, START, END, END, (row,), time_truncated=True,
                                   incomplete_reasons=("server_window",))
        self.assertIsNone(result.series[0].points[0].value)
        self.assertEqual(row.coverage_start, middle)
        self.assertEqual(row.coverage_end, END)
        self.assertFalse(result.complete)
        self.assertTrue(result.time_truncated)
        self.assertEqual(result.incomplete_reasons, ("server_window",))
        self.assertTrue(api.HistoryResult(1, START, END, END, (series(),)).complete)
        self.assertFalse(api.HistoryResult(1, START, END, END, ()).complete)
        self.assertFalse(api.HistoryResult(1, START, END, END, (series(points=()),)).complete)

    def test_invalid_history_intervals_points_and_types_are_rejected(self):
        for start, end in ((END, START), (START, START), (START.replace(tzinfo=None), END)):
            with self.assertRaises(api.InvalidArguments):
                api.HistoryResult(1, start, end, END, (series(),))
        for points in ((api.HistoryPoint(END, 1), api.HistoryPoint(START, 2)),
                       (api.HistoryPoint(START, True),),
                       (api.HistoryPoint(START - timedelta(seconds=1), 1),)):
            with self.assertRaises(api.InvalidArguments):
                api.HistoryResult(1, START, END, END, (series(points=points),))

    def test_current_does_not_imply_history_support(self):
        descriptor = api.ValueDescriptor(key(), "Water target", "°C", "calculated",
                                         "Calculated water target", api.ValueType.NUMBER,
                                         current_available=True, history_available=False)
        self.assertTrue(descriptor.current_available)
        self.assertFalse(descriptor.history_available)
        self.assertIsNone(descriptor.history_kind)
        with self.assertRaises(api.InvalidArguments):
            dataclasses.replace(descriptor, history_available=True)
        archived = dataclasses.replace(descriptor, history_available=True,
                                       history_kind=api.SeriesKind.STEP)
        self.assertEqual(archived.history_kind, api.SeriesKind.STEP)

    def test_controls_and_preview_expose_capabilities_without_http_details(self):
        control = api.ControlDescriptor(api.ControlKey(1, "switch", 7), "Reset",
                                        ("set-switch",), labels=((False, "Off"), (True, "On")),
                                        hidden=True)
        command = api.ValidatedCommand(control.key, control.actions[0], (("target_state", True),))
        self.assertEqual(command.device_id, 1)
        self.assertTrue(control.hidden)
        self.assertEqual(dict(control.labels)[False], "Off")
        self.assertNotIn("url", dataclasses.asdict(command))
        for make in (lambda: dataclasses.replace(command, arguments=(("x", 1), ("x", 2))),
                     lambda: dataclasses.replace(control, minimum=10, maximum=0),
                     lambda: dataclasses.replace(control, step=0),
                     lambda: dataclasses.replace(control, actions=["set-switch"])):
            with self.assertRaises(api.InvalidArguments):
                make()

    def test_session_deadline_is_local_and_lease_is_explicit(self):
        clock = FakeClock(start=100)
        session = api.ControlSession(1, uuid4(), clock.now() + 60)
        clock.advance(20)
        self.assertEqual(session.expires_at, 160)
        self.assertEqual(clock.now(), 120)
        parameters = inspect.signature(api.Controller.try_acquire_control).parameters
        self.assertEqual(parameters["lease_seconds"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(parameters["lease_seconds"].default, inspect.Parameter.empty)
        self.assertEqual(api.API_VERSION, 1)
        for bad in (math.nan, math.inf, -1, True):
            with self.assertRaises(api.InvalidArguments):
                api.ControlSession(1, uuid4(), bad)

    def test_command_success_does_not_claim_physical_recovery(self):
        command = api.ValidatedCommand(api.ControlKey(1, "switch", 7), "set-switch",
                                       (("target_state", False),))
        result = api.CommandResult(command, END)
        self.assertTrue(result.api_accepted)
        self.assertIsNone(result.observed_state)
        result = dataclasses.replace(result, observed_state=api.Snapshot(1, END, (reading(),)))
        self.assertEqual(result.observed_state.values[0].value, 21.5)
        with self.assertRaises(api.InvalidArguments):
            dataclasses.replace(result, observed_state=api.Snapshot(2, END, (reading(2),)))

    def test_safe_errors_do_not_accept_credentials_or_remote_response(self):
        names = ("AuthenticationError", "DeviceUnavailable", "UnsupportedAction", "InvalidArguments",
                 "IncompleteHistory", "RateLimited", "CommandOutcomeUnknown", "DeviceControlBusy",
                 "ControlOwnershipRequired")
        for name in names:
            error_type = getattr(api, name)
            error = error_type()
            self.assertIsInstance(error, api.ControllerError)
            with self.assertRaises(TypeError):
                error_type("token=private; arbitrary remote body")
            self.assertNotIn("private", str(error))
        self.assertNotIsInstance(api.DeviceUnavailable(), api.CommandOutcomeUnknown)
        self.assertNotIsInstance(api.UnsupportedAction(), api.CommandOutcomeUnknown)

    def test_result_schema_has_no_credentials_or_transport_payload(self):
        result = api.Snapshot(1, END, (reading(),))
        data = dataclasses.asdict(result)
        self.assertEqual(set(data), {"device_id", "received_at", "values", "is_atomic"})
        self.assertEqual(set(data["values"][0]), {"key", "value", "value_type", "unit", "source",
                                               "measured_at", "received_at", "quality"})
        with self.assertRaises(TypeError):
            api.Snapshot(1, END, (reading(),), token="private")
        self.assertEqual(set(name for name in vars(api.Controller) if not name.startswith("_")), {
            "list_devices", "describe_values", "read_current", "read_history", "list_controls",
            "prepare_command", "try_acquire_control", "release_control", "execute_command",
        })

    def test_offline_import_has_no_transport_cli_or_environment_dependency(self):
        script = """
import os, socket, sys
from unittest.mock import patch
with patch.dict(os.environ, {}, clear=True), patch.object(socket, 'socket', side_effect=AssertionError('network')):
    import zontcontrol.controller.contracts as api
    assert api.API_VERSION == 1
assert not any(name in sys.modules for name in (
    'zontcontrol.client', 'zontcontrol.cli', 'zontcontrol.controller.transport'))
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fake_distinguishes_pre_send_rejection_and_lost_response_after_effect(self):
        clock = FakeClock(start=10, rate=1.1)
        server = FakeZontServer({1: {"name": "Home", "notes": "", "reset": False},
                                 2: {"name": "Cabin", "notes": "", "reset": False}}, clock=clock)
        server.fail_before_send = True
        with self.assertRaises(api.DeviceUnavailable):
            server.send_update(1, {"reset": True})
        self.assertEqual(server.requests, ())
        server.fail_before_send = False
        rejected = server.send_update(1, {"reset": True})
        server.reject(rejected, api.UnsupportedAction())
        with self.assertRaises(api.UnsupportedAction):
            server.respond(rejected)
        self.assertFalse(server.read_device(1)["reset"])
        changes = {"reset": True}
        pending = server.send_update(1, changes)
        changes["reset"] = False
        clock.advance(2)
        self.assertAlmostEqual(clock.now(), 12.2)
        server.apply(pending)
        with self.assertRaises(api.CommandOutcomeUnknown):
            server.respond(pending, lose_response=True)
        self.assertTrue(server.read_device(1)["reset"])
        self.assertFalse(server.read_device(2)["reset"])
        off = server.send_update(1, {"reset": False})
        server.apply(off)
        self.assertTrue(server.respond(off))
        self.assertFalse(server.read_device(1)["reset"])
        self.assertEqual(len(server.requests), 3)
        # Consumer edits to returned data do not mutate the virtual device.
        server.read_device(1)["name"] = "changed outside server"
        self.assertEqual(server.read_device(1)["name"], "Home")


if __name__ == "__main__":
    unittest.main()
