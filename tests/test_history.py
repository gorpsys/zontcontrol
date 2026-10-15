import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from zontcontrol.client import ZontClient, ZontError
from zontcontrol.cli import main
from zontcontrol.history import cli_period, decode_delta_array


START = datetime.fromtimestamp(1000, timezone.utc)
END = datetime.fromtimestamp(1200, timezone.utc)


def device():
    return {
        "id": 42, "name": "Дом", "online": True, "device_info": {"model": "SMART 2.0"},
        "sensors": [{"id": 7, "name": "Воздух", "type": "temperature", "status": "ok"}],
        "circuits": [{"id": 10, "name": "Отопление", "type": "consumer", "active": False,
                      "is_off": False, "in_summer_mode": False}],
        "modes": [{"id": 20, "name": "Расписание"}],
    }


def response():
    return {
        "device_id": 42, "ok": True, "time_truncated": False,
        "z3k_temperature": {"7": [[1000, 21], [-60, 21.5], [-60, None]],
                            "99": [[1000, 0]]},
        "z3k_heating_circuit": {
            "10": {"target_temp": [[1000, 22]], "setpoint_temp": [[1000, 15], [-60, 45]],
                   "mode_id": [[1000, 20], [-100, 0]], "worktime": [[1000, 0]]},
            "11": {"target_temp": [[1000, 15]]},
        },
    }


def encoded(data):
    return io.BytesIO(json.dumps(data).encode())


class DeltaArrayTests(unittest.TestCase):
    def test_negative_delta_is_forward_time_and_absolute_reset_is_supported(self):
        points = decode_delta_array([[1000, 21], [-60, 22], [1150, 20]], START, END)
        times = [datetime.fromisoformat(point["time"]).timestamp() for point in points]
        self.assertEqual(times, [1000, 1060, 1150])
        self.assertEqual([point["value"] for point in points], [21, 22, 20])

    def test_window_nulls_and_zero_are_preserved_without_interpolation(self):
        points = decode_delta_array([[900, 20], [-100, None], [-50, 0], [1300, 22]], START, END)
        self.assertEqual([point["value"] for point in points], [None, 0])

    def test_invalid_points_and_values_fail(self):
        for rows in [[[-60, 21]], [[0, 21]], [[1000.5, 21]], [[1000, True]],
                     [[1000, float("nan")]], [[1000, "21"]], [[1000]], None]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                decode_delta_array(rows, START, END)
        with self.assertRaises(ValueError):
            decode_delta_array([[1000, 1.5]], START, END, integer=True)

    def test_period_timezone_and_relative_hours(self):
        start, end = cli_period("2026-10-12T10:00:00+03:00", "2026-10-12T11:00:00+03:00", None)
        self.assertEqual(start.hour, 7)
        self.assertEqual(end - start, timedelta(hours=1))
        start, end = cli_period(None, "2026-10-12T11:00:00Z", 24)
        self.assertEqual(end - start, timedelta(hours=24))
        for args in [(None, None, 0), (None, None, float("nan")),
                     ("2026-10-12T10:00:00", None, None),
                     ("2026-10-12T10:00:00Z", None, 24),
                     ("2026-10-12T11:00:00Z", "2026-10-12T10:00:00Z", None)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                cli_period(*args)


class HistoryClientTests(unittest.TestCase):
    def setUp(self):
        self.client = ZontClient("owner@example.com", token="test-token")
        self.opener = Mock()
        self.client._opener = self.opener

    def load(self, packet=None, **kwargs):
        packet = response() if packet is None else packet
        self.opener.open.side_effect = [encoded({"ok": True, "devices": [device()]}),
                                       encoded({"ok": True, "responses": [packet]})]
        return self.client.read_history(START, END, **kwargs)

    def test_history_request_normalization_names_and_circuit_filter(self):
        result = self.load(device_id=42, circuit_id=10)
        self.assertEqual(self.opener.open.call_count, 2)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://my.zont.online/api/load_data")
        body = json.loads(request.data)
        self.assertEqual(body["requests"][0], {
            "device_id": 42, "data_types": ["z3k_temperature", "z3k_heating_circuit"],
            "mintime": 1000, "maxtime": 1200,
        })
        self.assertEqual(request.get_header("X-zont-token"), "test-token")
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 15)
        data = result["devices"][0]
        self.assertEqual(data["temperature_sensors"][0]["name"], "Воздух")
        self.assertIsNone(data["temperature_sensors"][1]["name"])
        self.assertEqual(len(data["circuits"]), 1)
        circuit = data["circuits"][0]
        self.assertEqual(circuit["series"]["setpoint_temp"][1]["value"], 45)
        self.assertEqual(circuit["series"]["mode_id"][1]["value"], 0)
        self.assertEqual(circuit["series"]["status"], [])
        self.assertNotIn("test-token", json.dumps(result))

    def test_truncation_flag_is_exposed(self):
        packet = response()
        packet["time_truncated"] = True
        self.assertTrue(self.load(packet)["devices"][0]["time_truncated"])

    def test_optional_control_history_is_requested_and_decoded(self):
        packet = response()
        packet["z3k_web_element"] = {"8832": {"state": [[1000, 0], [-30, 1], [-7, 0]]}}
        result = self.load(packet, include_controls=True)
        request = self.opener.open.call_args.args[0]
        self.assertIn("z3k_web_element", json.loads(request.data)["requests"][0]["data_types"])
        points = result["devices"][0]["web_elements"][0]["series"]["state"]
        self.assertEqual([point["value"] for point in points], [0, 1, 0])
        self.assertEqual(datetime.fromisoformat(points[-1]["time"]).timestamp(), 1037)

    def test_invalid_or_failed_device_packet_is_not_silently_ignored(self):
        for changes in [{"ok": False}, {"device_id": 99}, {"time_truncated": "false"},
                        {"z3k_temperature": []}, {"z3k_heating_circuit": {"10": []}}]:
            with self.subTest(changes=changes), self.assertRaises((ZontError, ValueError)):
                self.load({**response(), **changes})

    def test_invalid_period_fails_before_network(self):
        with self.assertRaises(ValueError):
            self.client.read_history(START.replace(tzinfo=None), END)
        self.opener.open.assert_not_called()

    @patch("zontcontrol.cli.ZontClient")
    def test_cli_history_dispatches_period_and_filter(self, client):
        client.return_value.read_history.return_value = {"devices": []}
        with patch.dict("os.environ", {"ZONT_TOKEN": "test-token", "ZONT_CONTACT": "owner@example.com"}), \
                patch("sys.argv", ["zontcontrol", "--history", "--hours", "24", "--device-id", "42", "--circuit-id", "10"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(), 0)
        call = client.return_value.read_history.call_args
        self.assertEqual(call.args[1] - call.args[0], timedelta(hours=24))
        self.assertEqual(call.kwargs, {"device_id": 42, "circuit_id": 10})
        client.return_value.read_sensors.assert_not_called()


if __name__ == "__main__":
    unittest.main()
