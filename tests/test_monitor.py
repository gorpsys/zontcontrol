import unittest
import io
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from zontcontrol.monitor import assess_history, history_samples
from zontcontrol.cli import main


START = datetime(2026, 10, 1, tzinfo=timezone.utc)


def point(minute, value):
    return {"time": (START + timedelta(minutes=minute)).isoformat(), "value": value}


def packet():
    return {
        "fetched_at": (START + timedelta(minutes=45)).isoformat(),
        "requested_period": {"start": START.isoformat(), "end": (START + timedelta(minutes=45)).isoformat()},
        "devices": [{"id": 42, "time_truncated": False,
            "temperature_sensors": [
                {"id": 8207, "points": [point(i, 23.5) for i in range(46)]},
                {"id": 8429, "points": [point(i, 40 - .2 * i) for i in range(46)]},
            ],
            "circuits": [{"id": 20496, "series": {
                "target_temp": [point(0, 23.5)], "setpoint_temp": [point(0, 60)],
                "worktime": [point(0, 60)], "target_sensor_id": [point(0, 8207)],
            }}], "web_elements": [{"id": 8832, "series": {"state": [point(0, 0)]}}],
        }],
    }


class MonitorTests(unittest.TestCase):
    def test_read_only_assessment_returns_current_pattern(self):
        result = assess_history(packet(), circuit_id=20496)
        self.assertEqual(result["state_at_period_end"], "confirmed_pattern")
        self.assertIs(result["anomaly"], True)
        self.assertFalse(result["automatic_control"])
        self.assertEqual([event["type"] for event in result["events"]], ["confirmed_pattern"])

    def test_missing_telemetry_is_unknown_and_is_not_relabelled_fresh(self):
        data = packet()
        for sensor in data["devices"][0]["temperature_sensors"]:
            sensor["points"] = sensor["points"][:40]
        result = assess_history(data, circuit_id=20496)
        self.assertEqual(result["state_at_period_end"], "unknown")
        self.assertIsNone(result["anomaly"])
        self.assertEqual(result["latest_valid_sample"], (START + timedelta(minutes=39)).isoformat())

    def test_truncation_and_wrong_control_sensor_are_unknown(self):
        for flag in ("truncated", "sensor"):
            with self.subTest(flag=flag):
                data = packet()
                if flag == "truncated":
                    data["devices"][0]["time_truncated"] = True
                else:
                    data["devices"][0]["circuits"][0]["series"]["target_sensor_id"] = [point(0, 999)]
                self.assertEqual(assess_history(data, circuit_id=20496)["state_at_period_end"], "unknown")

    def test_reset_between_minute_samples_is_visible(self):
        data = packet()
        event_time = START + timedelta(minutes=42, seconds=20)
        data["devices"][0]["web_elements"][0]["series"]["state"] += [
            {"time": event_time.isoformat(), "value": 1},
            {"time": (event_time + timedelta(seconds=10)).isoformat(), "value": 0},
        ]
        result = assess_history(data, circuit_id=20496)
        self.assertEqual(result["state_at_period_end"], "suppressed")
        self.assertIs(result["anomaly"], False)

    def test_old_incident_does_not_override_current_decision(self):
        data = packet()
        data["devices"][0]["circuits"][0]["series"]["worktime"].append(point(40, 0))
        result = assess_history(data, circuit_id=20496)
        self.assertTrue(any(event["type"] == "confirmed_pattern" for event in result["events"]))
        self.assertTrue(result["incident_pending"])
        self.assertEqual(result["state_at_period_end"], "normal")
        self.assertIs(result["anomaly"], False)

    def test_missing_or_invalid_reset_state_prevents_a_definite_decision(self):
        for points in ([], [point(0, None)], [point(0, "0")], [point(46, 0)]):
            with self.subTest(points=points):
                data = packet()
                data["devices"][0]["web_elements"][0]["series"]["state"] = points
                result = assess_history(data, circuit_id=20496)
                self.assertEqual(result["decision_reason"], "reset_state_unknown")
                self.assertIsNone(result["anomaly"])

    def test_intentional_shutdown_overrides_temperature_pattern(self):
        data = packet()
        data["devices"][0]["web_elements"][0]["series"]["state"].append(point(44, 1))
        for sensor in data["devices"][0]["temperature_sensors"]:
            sensor["points"] = sensor["points"][:35]
        result = assess_history(data, circuit_id=20496)
        self.assertEqual(result["decision_reason"], "intentional_shutdown")
        self.assertIs(result["anomaly"], False)
        self.assertIs(result["reset_active_at_period_end"], True)

    def test_time_with_unknown_reset_does_not_accumulate_evidence(self):
        data = packet()
        data["devices"][0]["web_elements"][0]["series"]["state"] = [point(40, 0)]
        result = assess_history(data, circuit_id=20496)
        self.assertIs(result["anomaly"], False)
        self.assertEqual(result["events"], [])

    def test_reset_after_last_grid_minute_is_used_and_future_reset_is_ignored(self):
        for offset, expected in ((20, False), (50, True)):
            with self.subTest(offset=offset):
                data = packet()
                data["requested_period"]["end"] = (START + timedelta(minutes=45, seconds=30)).isoformat()
                data["devices"][0]["web_elements"][0]["series"]["state"].append({
                    "time": (START + timedelta(minutes=45, seconds=offset)).isoformat(), "value": 1})
                result = assess_history(data, circuit_id=20496)
                self.assertIs(result["anomaly"], expected)

    def test_short_window_or_invalid_current_worktime_is_unknown(self):
        for case in ("short", "worktime"):
            with self.subTest(case=case):
                data = packet()
                if case == "short":
                    data["requested_period"]["start"] = (START + timedelta(minutes=42)).isoformat()
                else:
                    data["devices"][0]["circuits"][0]["series"]["worktime"].append(point(45, 80))
                result = assess_history(data, circuit_id=20496)
                self.assertIsNone(result["anomaly"])

    def test_identical_independent_calls_return_identical_decisions(self):
        self.assertEqual(assess_history(packet(), circuit_id=20496),
                         assess_history(packet(), circuit_id=20496))

    def test_no_future_state_is_applied_to_past_measurements(self):
        data = packet()
        data["devices"][0]["circuits"][0]["series"]["target_temp"].append(point(30, 25))
        samples = list(history_samples(data["devices"][0], START, START + timedelta(minutes=45),
                                       circuit_id=20496, air_id=8207, coolant_id=8429))
        self.assertEqual(samples[28].air_target, 23.5)
        self.assertEqual(samples[29].air_target, 25)

    @patch("zontcontrol.cli.ZontClient")
    def test_detection_cli_reads_only_history_including_control_events(self, client):
        client.return_value.read_history.return_value = packet()
        with patch.dict("os.environ", {"ZONT_TOKEN": "test-token", "ZONT_CONTACT": "owner@example.com"}), \
                patch("sys.argv", ["zontcontrol", "--detect-anomalies", "--device-id", "42", "--circuit-id", "20496",
                                   "--from", START.isoformat(), "--to", (START + timedelta(minutes=45)).isoformat()]), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(), 0)
        client.return_value.read_history.assert_called_once_with(
            START, START + timedelta(minutes=45), device_id=42, circuit_id=20496, include_controls=True)
        client.return_value.apply_command.assert_not_called()
        client.return_value.read_sensors.assert_not_called()


if __name__ == "__main__":
    unittest.main()
