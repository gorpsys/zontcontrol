import unittest

from zontcontrol.controller.transport import API_URL
from zontcontrol.commands import PreparedCommand, prepare_command


class CommandTests(unittest.TestCase):
    def test_documented_paths_and_payloads(self):
        cases = [
            ("set-temp", {"target_temp": 22.5}, "circuits/10/actions/target-temp"),
            ("activate-mode", {}, "modes/10/actions/activate"),
            ("activate-mode", {"duration": 30}, "modes/10/actions/activate"),
            ("press-button", {}, "controls/10/actions/trigger"),
            ("set-switch", {"target_state": False}, "controls/10/actions/trigger"),
            ("set-regulator", {"target_value": 2.5}, "controls/10/actions/set-voltage"),
            ("run-scenario", {}, "scenarios/10/actions/activate"),
            ("set-guard", {"enable": True}, "guard-zones/10/actions/activate"),
        ]
        for action, body, path in cases:
            with self.subTest(action=action):
                preview = prepare_command(action, 42, 10, **body).preview(API_URL)
                self.assertTrue(preview["dry_run"])
                self.assertEqual(preview["method"], "POST")
                self.assertEqual(preview["url"], f"{API_URL}/devices/42/{path}")
                self.assertEqual(preview["body"], body)
                self.assertNotIn("headers", preview)

    def test_invalid_values_and_arbitrary_commands_are_rejected(self):
        cases = [
            ("set-temp", {"target_temp": float("nan")}),
            ("set-temp", {"target_temp": float("inf")}),
            ("set-temp", {"target_temp": True}), ("set-temp", {"target_temp": "22"}),
            ("set-temp", {}), ("set-switch", {"target_state": 1}),
            ("set-guard", {"enable": "true"}), ("activate-mode", {"duration": 0}),
            ("activate-mode", {"duration": True}), ("activate-mode", {"circuit_id": 10}),
            ("press-button", {"target_state": True}), ("set-relay", {"on": True}),
            ("../../../arbitrary", {}),
        ]
        for action, body in cases:
            with self.subTest(action=action, body=body), self.assertRaises(ValueError):
                prepare_command(action, 42, 10, **body)
        for device_id, element_id in [(0, 10), (42, -1), (True, 10), (42, "10")]:
            with self.subTest(device_id=device_id), self.assertRaises(ValueError):
                prepare_command("press-button", device_id, element_id)

    def test_prepared_payload_cannot_be_changed_by_modifying_body(self):
        command = prepare_command("set-temp", 42, 10, target_temp=22)
        command.body["target_temp"] = 80
        self.assertEqual(command.body["target_temp"], 22)
        with self.assertRaises(ValueError):
            PreparedCommand("set-temp", 42, 10, (("target_temp", 22), ("target_temp", 80)))
