"""Internal API operations map typed transport outcomes to controller errors."""

import io
import json
import socket
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from zontcontrol.commands import prepare_command
from zontcontrol.controller.auth import token_headers
from zontcontrol.controller.contracts import (
    AuthenticationError, CommandOutcomeUnknown, DeviceUnavailable, InvalidArguments, RateLimited,
)
from zontcontrol.controller.transport import HttpTransport
from zontcontrol.controller.zont import ZontAdapter


class ZontAdapterTests(unittest.TestCase):
    def setUp(self):
        self.opener = Mock()
        self.adapter = ZontAdapter(HttpTransport(timeout=2, opener=self.opener),
                                   token_headers("contact", "test-token"))

    def answer(self, **data):
        self.opener.open.return_value = io.BytesIO(json.dumps({"ok": True, **data}).encode())

    def test_widget_read_filters_by_explicit_device(self):
        self.answer(devices=[{"id": 42, "name": "Дом"}])
        self.assertEqual(self.adapter.devices(42, fields="sensors,circuits,modes")[0]["id"], 42)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query),
                         {"device_ids": ["42"], "fields": ["sensors,circuits,modes"]})
        self.assertEqual(request.get_header("X-zont-token"), "test-token")

    def test_full_metadata_read_selects_device_and_does_not_turn_null_into_empty(self):
        self.answer(devices=[{"id": 1, "name": "Другой", "notes": "private"},
                             {"id": 42, "name": "Дом", "notes": None, "password": "private"}])
        self.assertEqual(self.adapter.read_metadata(42), {"name": "Дом", "notes": None})
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://my.zont.online/api/devices")
        self.assertEqual(json.loads(request.data), {"load_io": False})
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 2)

    def test_metadata_update_contains_one_field_and_device_id(self):
        for field in ("notes", "name"):
            self.opener.open.reset_mock()
            self.answer()
            self.adapter.update_metadata(42, field, "Дом [zc:тест]")
            request = self.opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://my.zont.online/api/update_device")
            self.assertEqual(json.loads(request.data), {"device_id": 42, field: "Дом [zc:тест]"})
            self.opener.open.assert_called_once()

    def test_metadata_field_read_preserves_exact_value_and_rejects_other_fields(self):
        self.answer(devices=[{"id": 42, "name": " Дом  ", "notes": None}])
        self.assertEqual(self.adapter.read_metadata_field(42, "name"), " Дом  ")
        self.answer(devices=[{"id": 42, "name": " Дом  ", "notes": None}])
        self.assertIsNone(self.adapter.read_metadata_field(42, "notes"))
        self.opener.open.reset_mock()
        with self.assertRaises(InvalidArguments):
            self.adapter.read_metadata_field(42, "heating")
        self.opener.open.assert_not_called()

    def test_invalid_metadata_never_reaches_network(self):
        for args in ((0, "notes", ""), (True, "notes", ""), (42, "other", ""),
                     (42, "notes", None), (42, "name", {})):
            with self.subTest(args=args), self.assertRaises(InvalidArguments):
                self.adapter.update_metadata(*args)
        self.opener.open.assert_not_called()

    def test_timeout_after_application_is_unknown_and_not_retried(self):
        state = {"notes": "old"}

        def apply_then_timeout(request, **kwargs):
            state.update(json.loads(request.data))
            raise TimeoutError("secret")

        self.opener.open.side_effect = apply_then_timeout
        with self.assertRaises(CommandOutcomeUnknown):
            self.adapter.update_metadata(42, "notes", "new")
        self.assertEqual(state["notes"], "new")
        self.opener.open.assert_called_once()

    def test_read_and_write_failures_map_by_outcome_not_error_text(self):
        cases = [(HTTPError("secret", 401, "secret", {}, None), AuthenticationError),
                 (HTTPError("secret", 403, "secret", {}, None), AuthenticationError),
                 (HTTPError("secret", 429, "secret", {}, None), RateLimited),
                 (HTTPError("secret", 400, "secret", {}, None), InvalidArguments),
                 (URLError(socket.gaierror("secret")), DeviceUnavailable)]
        for write in (False, True):
            for error, expected in cases:
                with self.subTest(write=write, expected=expected):
                    self.opener.open.reset_mock()
                    self.opener.open.side_effect = error
                    with self.assertRaises(expected) as caught:
                        if write:
                            self.adapter.update_metadata(42, "notes", "new")
                        else:
                            self.adapter.read_metadata(42)
                    self.assertNotIn("secret", str(caught.exception))
                    self.opener.open.assert_called_once()

    def test_malformed_write_response_is_unknown_but_ok_false_is_known(self):
        for raw, expected in ((b"secret", CommandOutcomeUnknown),
                              (b"[]", CommandOutcomeUnknown), (b"{}", CommandOutcomeUnknown),
                              (b'{"ok":false,"error":"secret"}', InvalidArguments)):
            self.opener.open.reset_mock()
            self.opener.open.return_value = io.BytesIO(raw)
            with self.subTest(raw=raw), self.assertRaises(expected):
                self.adapter.update_metadata(42, "name", "new")
            self.opener.open.assert_called_once()

    def test_missing_duplicate_or_invalid_device_response_is_rejected(self):
        for devices, expected in (([], DeviceUnavailable), ([{"id": 41}], DeviceUnavailable),
                                  ([{"id": 42}, {"id": 42}], InvalidArguments),
                                  ([None], InvalidArguments), ([{"id": True}], InvalidArguments)):
            self.answer(devices=devices)
            with self.subTest(devices=devices), self.assertRaises(expected):
                self.adapter.devices(42)

    def test_full_io_and_history_reads_are_not_writes(self):
        self.answer(devices=[{"id": 42, "io": {}}])
        self.assertEqual(self.adapter.full_device(42), {"id": 42, "io": {}})
        request = self.opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"load_io": True})
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        end = start + timedelta(minutes=45)
        self.answer(responses=[{"device_id": 42, "ok": True, "data": {}}])
        self.adapter.load_history(42, start, end, include_controls=True)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://my.zont.online/api/load_data")
        body = json.loads(request.data)["requests"][0]
        self.assertEqual(body["device_id"], 42)
        self.assertEqual(body["mintime"], int(start.timestamp()))
        self.assertIn("z3k_web_element", body["data_types"])
        self.opener.open.side_effect = TimeoutError("secret")
        with self.assertRaises(DeviceUnavailable):
            self.adapter.load_history(42, start, end)

    def test_history_query_encloses_fractional_interval_without_changing_measurements(self):
        start = datetime(2026, 10, 1, microsecond=100000, tzinfo=timezone.utc)
        end = start + timedelta(microseconds=800000)
        self.answer(responses=[{'device_id': 42, 'ok': True}])
        self.adapter.load_history(42, start, end)
        body = json.loads(self.opener.open.call_args.args[0].data)['requests'][0]
        self.assertEqual(body['mintime'], int(start.timestamp()))
        self.assertEqual(body['maxtime'], int(start.timestamp()) + 1)

    def test_control_send_success_is_api_acknowledgement_only(self):
        command = prepare_command("set-switch", 42, 10, target_state=False)
        self.answer()
        self.assertEqual(self.adapter.send_command(command), {"ok": True})
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, command.preview(
            "https://my.zont.online/api/widget/v3")["url"])
        self.assertEqual(json.loads(request.data), {"target_state": False})
        self.opener.open.assert_called_once()

    def test_control_send_never_retries_unknown_or_known_failure(self):
        command = prepare_command("set-switch", 42, 10, target_state=False)
        for raw, expected in ((b"secret", CommandOutcomeUnknown),
                              (b'{"ok":false}', InvalidArguments),
                              (b'{"ok":true,"device":{"id":99}}', CommandOutcomeUnknown),
                              (b'{"ok":true,"device":null}', CommandOutcomeUnknown)):
            self.opener.open.reset_mock()
            self.opener.open.return_value = io.BytesIO(raw)
            with self.subTest(raw=raw), self.assertRaises(expected):
                self.adapter.send_command(command)
            self.opener.open.assert_called_once()
        self.opener.open.reset_mock()
        with self.assertRaises(InvalidArguments):
            self.adapter.send_command({"url": "https://other.example"})
        self.opener.open.assert_not_called()

    def test_control_timeout_after_application_is_unknown(self):
        state = {}

        def apply_then_disconnect(request, **kwargs):
            state.update(json.loads(request.data))
            raise ConnectionResetError("secret")

        self.opener.open.side_effect = apply_then_disconnect
        with self.assertRaises(CommandOutcomeUnknown):
            self.adapter.send_command(prepare_command("set-switch", 42, 10, target_state=False))
        self.assertEqual(state, {"target_state": False})
        self.opener.open.assert_called_once()

    def test_full_metadata_and_history_validate_response_shapes(self):
        for notes in (False, 1, {}):
            self.answer(devices=[{"id": 42, "name": "Дом", "notes": notes}])
            with self.subTest(notes=notes), self.assertRaises(InvalidArguments):
                self.adapter.read_metadata(42)
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        for responses in (None, [], [None], [{"device_id": 99, "ok": True}],
                          [{"device_id": 42, "ok": False}],
                          [{"device_id": 42, "ok": True}] * 2):
            self.answer(responses=responses)
            with self.subTest(responses=responses), self.assertRaises(InvalidArguments):
                self.adapter.load_history(42, start, start + timedelta(minutes=45))
        self.opener.open.reset_mock()
        for operation in (lambda: self.adapter.full_device(True),
                          lambda: self.adapter.full_device(42, load_io=1),
                          lambda: self.adapter.devices(0),
                          lambda: self.adapter.load_history(42, start, start),
                          lambda: self.adapter.load_history(42, start, start, include_controls=1)):
            with self.assertRaises(InvalidArguments):
                operation()
        self.opener.open.assert_not_called()
