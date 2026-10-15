import unittest
from datetime import datetime, timedelta, timezone

from zontcontrol.anomaly import DetectionConfig, HeatingSample, NoHeatDetector
from zontcontrol.detection_profile import SMART_2_HEATING_CONFIG


START = datetime(2026, 10, 1, tzinfo=timezone.utc)


def sample(minute, coolant=40, air=23.5, target=23.5, supply=60, work=60, reset=False, **kwargs):
    return HeatingSample(START + timedelta(minutes=minute), air, target, coolant, supply, work,
                         reset, kwargs.get("selected", True), kwargs.get("reset_changed_at"))


class DetectorTests(unittest.TestCase):
    def test_cooling_under_demand_warns_before_air_cools_and_confirms_later(self):
        detector = NoHeatDetector()
        events = []
        for minute in range(45):
            event = detector.update(sample(minute, coolant=40 - minute * 0.2))
            if event:
                events.append(event)
        self.assertEqual([event["type"] for event in events], ["warning", "confirmed_pattern"])
        self.assertEqual(events[0]["time"], (START + timedelta(minutes=10)).isoformat())
        self.assertEqual(events[0]["air"], events[0]["air_target"])

    def test_normal_five_minute_cycles_do_not_warn(self):
        detector = NoHeatDetector()
        cycle = [80, 65, 55, 45, 40]
        events = [detector.update(sample(minute, coolant=cycle[minute % 5], supply=90)) for minute in range(120)]
        self.assertFalse(any(event for event in events))

    def test_boiler_limit_and_steady_hot_supply_are_not_failures(self):
        for water in (86, 60):
            with self.subTest(water=water):
                detector = NoHeatDetector()
                events = [detector.update(sample(minute, coolant=water, supply=90)) for minute in range(90)]
                self.assertFalse(any(events))

    def test_slow_positive_warming_is_accepted(self):
        detector = NoHeatDetector()
        events = [detector.update(sample(minute, coolant=20 + minute * .5, air=20, target=25, supply=70))
                  for minute in range(80)]
        self.assertFalse(any(events))

    def test_short_requests_are_not_anomalies(self):
        detector = NoHeatDetector()
        events = [detector.update(sample(minute, coolant=25, air=20, target=23,
                                        work=60 if minute % 15 < 5 else 0)) for minute in range(90)]
        self.assertFalse(any(events))

    def test_subminute_reset_event_suppresses_restart_grace(self):
        detector = NoHeatDetector()
        events = []
        for minute in range(20):
            transition = START + timedelta(minutes=8, seconds=30) if minute >= 9 else None
            event = detector.update(sample(minute, coolant=25, air=20, target=23,
                                           reset_changed_at=transition))
            if event:
                events.append(event)
        self.assertFalse(any(events))

    def test_telemetry_gaps_and_missing_values_restart_evidence(self):
        detector = NoHeatDetector()
        for minute in range(8):
            detector.update(sample(minute, coolant=25, air=20, target=23))
        self.assertIsNone(detector.update(sample(20, coolant=None)))
        for minute in range(21, 30):
            self.assertIsNone(detector.update(sample(minute, coolant=25, air=20, target=23)))

    def test_recovery_requires_observed_temperature_response(self):
        detector = NoHeatDetector()
        for minute in range(25):
            detector.update(sample(minute, coolant=25, air=20, target=23))
        event = detector.update(sample(25, coolant=35, air=20, target=23))
        self.assertEqual(event["type"], "ended")
        self.assertEqual(event["reason"], "heating_response_returned")
        self.assertTrue(event["heat_returned"])
        self.assertFalse(detector.incident_pending)

    def test_manual_reset_suppresses_but_does_not_claim_repair(self):
        detector = NoHeatDetector()
        for minute in range(25):
            detector.update(sample(minute, coolant=25, air=20, target=23))
        event = detector.update(sample(25, coolant=25, air=20, target=23, reset=True))
        self.assertEqual(event["reason"], "reset_or_restart_grace")
        self.assertFalse(event["heat_returned"])
        self.assertTrue(detector.incident_pending)

    def test_chronology_and_config_validation(self):
        detector = NoHeatDetector()
        detector.update(sample(1))
        with self.assertRaises(ValueError):
            detector.update(sample(1))
        for options in ({"supply_gap": -1}, {"boiler_limit": float("nan")},
                        {"minimum_worktime": 61}, {"warning_minutes": 30, "confirm_minutes": 20},
                        {"confirm_air_deficit": -0.1}, {"confirm_coolant_drop": float("nan")}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                DetectionConfig(**options)

    def test_confirmation_thresholds_are_configurable_and_do_not_bypass_reset(self):
        config = DetectionConfig(warning_minutes=3, confirm_minutes=3,
                                 confirm_air_deficit=0.5, confirm_coolant_drop=0.5)
        detector = NoHeatDetector(config)
        event = None
        for minute in range(8):
            event = detector.update(sample(minute, coolant=40 - minute * 0.2))
        self.assertEqual(event["type"], "confirmed_pattern")
        event = detector.update(sample(8, coolant=25, air=20, target=23, reset=True))
        self.assertEqual(event["reason"], "reset_or_restart_grace")
        self.assertEqual(detector.level, "normal")

    def test_early_confirmation_can_precede_room_cooling(self):
        config = DetectionConfig(warning_minutes=4, confirm_minutes=4,
                                 confirm_air_deficit=0, confirm_coolant_drop=3)
        detector = NoHeatDetector(config)
        events = []
        for minute in range(14):
            event = detector.update(sample(minute, coolant=40 - minute * 0.2))
            if event:
                events.append(event)
        self.assertEqual([event["type"] for event in events], ["confirmed_pattern"])
        self.assertEqual(events[0]["minutes_without_response"], 4)
        self.assertEqual(events[0]["air"], events[0]["air_target"])

    def test_early_profile_still_accepts_normal_cycles_and_steady_hot_water(self):
        config = DetectionConfig(warning_minutes=4, confirm_minutes=4,
                                 confirm_air_deficit=0, confirm_coolant_drop=3)
        for cycle in ([80, 65, 55, 45, 40], [86], [60]):
            with self.subTest(cycle=cycle):
                detector = NoHeatDetector(config)
                events = [detector.update(sample(minute, coolant=cycle[minute % len(cycle)], supply=90))
                          for minute in range(90)]
                self.assertFalse(any(events))

    def test_future_samples_do_not_change_past_detection(self):
        def run(future_water):
            detector = NoHeatDetector()
            past = [detector.update(sample(minute, coolant=40 - .2 * minute)) for minute in range(15)]
            for minute in range(15, 40):
                detector.update(sample(minute, coolant=future_water))
            return past
        self.assertEqual(run(25), run(80))

    def test_calibrated_profile_waits_longer_for_hot_water(self):
        for initial_water, confirmation_minute in ((40, 17), (80, 18)):
            with self.subTest(initial_water=initial_water):
                detector = NoHeatDetector(SMART_2_HEATING_CONFIG, require_known_reset=True)
                confirmations = []
                for minute in range(20):
                    event = detector.update(sample(minute, coolant=initial_water - minute * .1, supply=90))
                    if event and event["type"] == "confirmed_pattern":
                        confirmations.append(minute)
                self.assertEqual(confirmations, [confirmation_minute])

    def test_calibrated_profile_accepts_long_hot_cycles(self):
        detector = NoHeatDetector(SMART_2_HEATING_CONFIG, require_known_reset=True)
        cycle = [86] + [80 - index * .7 for index in range(14)]
        events = [detector.update(sample(minute, coolant=cycle[minute % len(cycle)], supply=90))
                  for minute in range(150)]
        self.assertFalse(any(event and event["type"] == "confirmed_pattern" for event in events))

    def test_hot_confirmation_options_require_a_valid_pair(self):
        for options in ({"hot_coolant_above": 60}, {"hot_confirm_minutes": 25},
                        {"hot_coolant_above": float("nan"), "hot_confirm_minutes": 25},
                        {"hot_coolant_above": 60, "hot_confirm_minutes": 19},
                        {"hot_coolant_above": 60, "hot_confirm_minutes": 20.5}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                DetectionConfig(**options)


if __name__ == "__main__":
    unittest.main()
