import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from zontcontrol.cli import main
from zontcontrol.client import ZontClient, ZontError


def response(devices):
    return io.BytesIO(json.dumps({"ok": True, "devices": devices}).encode())


def device(online=True, value=0):
    return {
        "id": 42, "name": "Дом", "online": online,
        "device_info": {"model": "SMART 2.0", "serial": "PRIVATE"},
        "sensors": [{"id": 7, "name": "Комната", "type": "temperature",
                     "status": "ok", "value": value, "unit": "°C"}],
    }


def circuit(kind="consumer", target=23.5, mode=20503):
    return {
        "id": 20496, "name": "Отопление", "status": "ok", "type": kind,
        "actual_temp": 23.7, "target_temp": target, "current_mode": mode,
        "active": True, "is_off": False, "in_summer_mode": False,
        "work_mode": "on_request", "min": 10, "max": 30, "step": 1,
    }


def full_response(states, device_id=42):
    return response([{"id": device_id, "io": {"z3k-state": states}}])


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = ZontClient("owner@example.com", token="test-token")
        self.opener = Mock()
        self.client._transport._opener = self.opener

    def test_token_request_filters_and_preserves_zero(self):
        self.opener.open.return_value = response([device()])
        snapshot = self.client.read_sensors(42)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        url = urlsplit(request.full_url)
        self.assertEqual(url.scheme + "://" + url.netloc + url.path,
                         "https://my.zont.online/api/widget/v3/devices")
        self.assertEqual(parse_qs(url.query),
                         {"fields": ["sensors,circuits,modes"], "device_ids": ["42"]})
        self.assertEqual(request.get_header("X-zont-token"), "test-token")
        self.assertEqual(request.get_header("X-zont-client"), "owner@example.com")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 15)
        self.assertEqual(snapshot["devices"][0]["sensors"][0]["value"], 0)
        self.assertNotIn("PRIVATE", json.dumps(snapshot))
        self.assertTrue(snapshot["fetched_at"].endswith("+00:00"))

    def test_offline_missing_value_and_triggered(self):
        data = device(online=False, value=None)
        data["sensors"][0].update(status="failure", triggered=False)
        self.opener.open.return_value = response([data])
        snapshot = self.client.read_sensors()
        self.assertFalse(snapshot["devices"][0]["online"])
        sensor = snapshot["devices"][0]["sensors"][0]
        self.assertIsNone(sensor["value"])
        self.assertEqual(sensor["status"], "failure")
        self.assertIs(sensor["triggered"], False)

    def test_errors_do_not_expose_server_body_or_secret(self):
        errors = [
            HTTPError("https://example.com", 403, "test-token", {}, None),
            HTTPError("https://example.com", 429, "test-token", {}, None),
            URLError("test-token"),
            TimeoutError("test-token"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.opener.open.side_effect = error
                with self.assertRaises(ZontError) as caught:
                    self.client.read_sensors()
                self.assertNotIn("test-token", str(caught.exception))

    def test_bad_responses_and_sensor_values(self):
        invalid = [b"<html>test-token</html>", b"[]", b'{"ok": false, "error": "test-token"}',
                   b'{"ok": true}', b'{"ok": true, "devices": [null]}']
        for raw in invalid:
            with self.subTest(raw=raw):
                self.opener.open.return_value = io.BytesIO(raw)
                with self.assertRaises(ZontError):
                    self.client.read_sensors()
        for value in [float("nan"), float("inf"), "12", True]:
            with self.subTest(value=value):
                self.opener.open.return_value = response([device(value=value)])
                with self.assertRaises(ZontError):
                    self.client.read_sensors()

    def test_missing_requested_device(self):
        self.opener.open.return_value = response([])
        with self.assertRaises(ZontError):
            self.client.read_sensors(42)

    def test_circuit_air_and_boiler_targets_and_mode_name(self):
        data = device()
        boiler = circuit(kind="boiler", target=64.6, mode=None)
        boiler.update(id=20494, name="Котел", actual_temp=52.0, min=15, max=85)
        boiler.pop("step")
        data["circuits"] = [circuit(), boiler]
        data["modes"] = [{"id": 20503, "name": "Расписание"}]
        self.opener.open.side_effect = [
            response([data]), full_response({"20496": {"setpoint_temp": 58.3}}),
        ]
        result = self.client.read_sensors()["devices"][0]
        heating, boiler = result["circuits"]
        self.assertEqual(heating["target_temp"], 23.5)
        self.assertEqual(heating["setpoint_temp"], 58.3)
        self.assertEqual(heating["actual_temp"], 23.7)
        self.assertEqual(heating["current_mode_name"], "Расписание")
        self.assertEqual(heating["work_mode"], "on_request")
        self.assertTrue(heating["active"])
        self.assertFalse(heating["is_off"])
        self.assertEqual(boiler["target_temp"], 64.6)
        self.assertEqual(boiler["unit"], "°C")
        self.assertIsNone(boiler["current_mode"])
        self.assertIsNone(boiler["current_mode_name"])
        self.assertIsNone(boiler["step"])
        self.assertIsNone(boiler["setpoint_temp"])
        self.assertEqual(result["sensors"][0]["value"], 0)

    def test_missing_targets_zero_and_unresolved_mode(self):
        for target in [None, 0]:
            with self.subTest(target=target):
                data = device()
                data["circuits"] = [circuit(target=target)]
                data["circuits"][0].update(actual_temp=None, active=False, is_off=True)
                self.opener.open.side_effect = [response([data]), full_response({})]
                result = self.client.read_sensors()["devices"][0]["circuits"][0]
                self.assertEqual(result["target_temp"], target)
                self.assertIsNone(result["actual_temp"])
                self.assertEqual(result["current_mode"], 20503)
                self.assertIsNone(result["current_mode_name"])
                self.assertTrue(result["is_off"])
                self.assertIsNone(result["setpoint_temp"])

    def test_device_without_heating_circuits(self):
        self.opener.open.return_value = response([device()])
        result = self.client.read_sensors()["devices"][0]
        self.assertEqual(result["circuits"], [])
        self.assertEqual(result["modes"], [])
        self.opener.open.assert_called_once()

    def test_full_api_setpoint_request_and_device_circuit_matching(self):
        data = device()
        data["circuits"] = [circuit()]
        full = [
            {"id": 99, "io": {"z3k-state": {"20496": {"setpoint_temp": 80}}}},
            {"id": 42, "io": {"z3k-state": {
                "20494": {"setpoint_temp": 75}, "20496": {"setpoint_temp": 15},
            }}, "password": "private-password"},
        ]
        self.opener.open.side_effect = [response([data]), response(full)]
        result = self.client.read_sensors(42)
        self.assertEqual(result["devices"][0]["circuits"][0]["setpoint_temp"], 15)
        self.assertNotIn("private-password", json.dumps(result))
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://my.zont.online/api/devices")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"load_io": True})
        self.assertEqual(request.get_header("X-zont-token"), "test-token")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 15)

    def test_missing_and_zero_coolant_setpoint(self):
        for value in [None, 0]:
            with self.subTest(value=value):
                data = device()
                data["circuits"] = [circuit()]
                self.opener.open.side_effect = [
                    response([data]), full_response({"20496": {"setpoint_temp": value}}),
                ]
                result = self.client.read_sensors()["devices"][0]["circuits"][0]
                self.assertEqual(result["setpoint_temp"], value)

    def test_invalid_full_api_setpoints_fail(self):
        for value in [True, "15", float("nan"), float("inf")]:
            with self.subTest(value=value):
                data = device()
                data["circuits"] = [circuit()]
                self.opener.open.side_effect = [
                    response([data]), full_response({"20496": {"setpoint_temp": value}}),
                ]
                with self.assertRaises(ZontError):
                    self.client.read_sensors()

    def test_full_api_failure_is_reported(self):
        data = device()
        data["circuits"] = [circuit()]
        self.opener.open.side_effect = [response([data]), URLError("test-token")]
        with self.assertRaises(ZontError) as caught:
            self.client.read_sensors()
        self.assertNotIn("test-token", str(caught.exception))

    def test_invalid_circuit_values_are_rejected(self):
        invalid = [("target_temp", float("nan")), ("actual_temp", float("inf")),
                   ("min", "10"), ("target_temp", True), ("active", "false"),
                   ("current_mode", True), ("status", []), ("id", "20496")]
        for key, value in invalid:
            with self.subTest(key=key, value=value):
                data = device()
                data["circuits"] = [circuit()]
                data["circuits"][0][key] = value
                self.opener.open.return_value = response([data])
                with self.assertRaises(ZontError):
                    self.client.read_sensors()

    def test_invalid_circuit_or_mode_containers_are_rejected(self):
        for key, value in [("circuits", None), ("circuits", [None]),
                           ("modes", {}), ("modes", [{"id": 1}])]:
            with self.subTest(key=key, value=value):
                data = device()
                data[key] = value
                self.opener.open.return_value = response([data])
                with self.assertRaises(ZontError):
                    self.client.read_sensors()

    def test_input_validation(self):
        for timeout in [0, -1, float("nan"), float("inf"), 121]:
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                ZontClient("owner@example.com", token="test-token", timeout=timeout)
        with self.assertRaises(ValueError):
            ZontClient("owner@example.com\r\nInjected: value", token="test-token")
        with self.assertRaises(ValueError):
            self.client.read_sensors(-1)


class CliTests(unittest.TestCase):
    @patch("zontcontrol.cli.ZontClient")
    def test_missing_or_blank_token_exits_without_prompts_or_client(self, client):
        for token in [None, "", "  "]:
            env = {} if token is None else {"ZONT_TOKEN": token}
            with self.subTest(token=token), \
                    patch.dict("os.environ", env, clear=True), \
                    patch("sys.argv", ["zontcontrol"]), \
                    patch("builtins.input") as prompt, \
                    patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                    patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(main(), 1)
                self.assertIn("ZONT_TOKEN", stderr.getvalue())
                self.assertIn("python3 -m zontcontrol.get_token", stderr.getvalue())
                self.assertIn("export ZONT_TOKEN=", stderr.getvalue())
                self.assertEqual(stdout.getvalue(), "")
                prompt.assert_not_called()
                client.assert_not_called()

    @patch("zontcontrol.cli.ZontClient")
    def test_vault_injected_token_does_not_require_prompt(self, client):
        client.return_value.read_sensors.return_value = {"devices": []}
        with patch.dict("os.environ", {"ZONT_TOKEN": "injected-token", "ZONT_CONTACT": "owner@example.com"}), \
                patch("sys.argv", ["zontcontrol"]), \
                patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                patch("builtins.input") as prompt:
            self.assertEqual(main(), 0)
            prompt.assert_not_called()
            self.assertEqual(json.loads(stdout.getvalue()), {"devices": []})
            self.assertNotIn("injected-token", stdout.getvalue())
        self.assertEqual(client.call_args.kwargs["token"], "injected-token")




if __name__ == "__main__":
    unittest.main()
