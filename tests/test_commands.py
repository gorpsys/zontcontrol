import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from zontcontrol.client import API_URL, ZontClient, ZontError
from zontcontrol.commands import PreparedCommand, prepare_command
from zontcontrol.control import main


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


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.client = ZontClient("owner@example.com", token="test-token")
        self.opener = Mock()
        self.client._opener = self.opener
        self.command = prepare_command("set-temp", 42, 10, target_temp=22)
        self.element = {
            "id": 10, "kind": "circuit", "state": {"min": 10, "max": 30},
            "actions": [{"method": "POST", "url": self.command.preview(API_URL)["url"]}],
        }
        self.device = {"id": 42, "online": True, "elements": [self.element]}
        self.client.list_controls = Mock(return_value={"devices": [self.device]})

    def test_default_preview_has_zero_network_calls(self):
        result = self.client.apply_command(self.command)
        self.assertTrue(result["dry_run"])
        self.opener.open.assert_not_called()
        self.client.list_controls.assert_not_called()
        self.assertNotIn("test-token", json.dumps(result))

    def test_explicit_execution_checks_inventory_and_sends_one_json_post(self):
        self.opener.open.return_value = io.BytesIO(b'{"ok": true}')
        result = self.client.apply_command(self.command, execute=True)
        self.client.list_controls.assert_called_once_with(42)
        self.opener.open.assert_called_once()
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, self.command.preview(API_URL)["url"])
        self.assertEqual(json.loads(request.data), {"target_temp": 22})
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("X-zont-token"), "test-token")
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 15)
        self.assertTrue(result["executed"])
        self.assertFalse(result["command"]["dry_run"])
        self.assertNotIn("test-token", json.dumps(result))

    def test_offline_unknown_or_unsupported_element_is_not_sent(self):
        cases = [
            {"id": 42, "online": False, "elements": [self.element]},
            {"id": 99, "online": True, "elements": [self.element]},
            {"id": 42, "online": True, "elements": []},
            {"id": 42, "online": True, "elements": [{**self.element, "actions": []}]},
            {"id": 42, "online": True, "elements": [{**self.element, "kind": "relays"}]},
            {"id": 42, "online": True, "elements": [{**self.element, "actions": [
                {"method": "POST", "url": "https://other.example.com"}]}]},
        ]
        for device in cases:
            with self.subTest(device=device):
                self.client.list_controls.return_value = {"devices": [device]}
                with self.assertRaises((ZontError, ValueError)):
                    self.client.apply_command(self.command, execute=True)
                self.opener.open.assert_not_called()

    def test_limits_checked_before_sending(self):
        for temperature in [9, 31]:
            with self.subTest(temperature=temperature), self.assertRaises(ValueError):
                self.client.apply_command(
                    prepare_command("set-temp", 42, 10, target_temp=temperature), execute=True)
        self.opener.open.assert_not_called()

    def test_unavailable_mode_is_not_sent(self):
        command = prepare_command("activate-mode", 42, 10)
        self.element.update(kind="mode", state={"can_be_applied": []},
                            actions=[{"method": "POST", "url": command.preview(API_URL)["url"]}])
        with self.assertRaises(ValueError):
            self.client.apply_command(command, execute=True)
        self.opener.open.assert_not_called()

    def test_failed_or_unconfirmed_command_is_never_retried(self):
        for error in [URLError("test-token"), HTTPError("url", 403, "test-token", {}, None)]:
            with self.subTest(error=type(error).__name__):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = error
                with self.assertRaises(ZontError) as caught:
                    self.client.apply_command(self.command, execute=True)
                self.opener.open.assert_called_once()
                self.assertIn("before any retry", str(caught.exception))
                self.assertNotIn("test-token", str(caught.exception))
        self.opener.open.reset_mock()
        self.opener.open.side_effect = None
        self.opener.open.return_value = io.BytesIO(b'{"ok": false, "error": "test-token"}')
        with self.assertRaises(ZontError):
            self.client.apply_command(self.command, execute=True)
        self.opener.open.assert_called_once()

    def test_response_device_is_sanitized_and_matches_requested_device(self):
        device = {"id": 42, "name": "Дом", "online": True,
                  "device_info": {"model": "SMART 2.0", "serial": "PRIVATE"}}
        self.opener.open.return_value = io.BytesIO(json.dumps({"ok": True, "device": device}).encode())
        result = self.client.apply_command(self.command, execute=True)
        self.assertEqual(result["device"]["id"], 42)
        self.assertNotIn("PRIVATE", json.dumps(result))
        device["id"] = 99
        self.opener.open.return_value = io.BytesIO(json.dumps({"ok": True, "device": device}).encode())
        with self.assertRaises(ZontError):
            self.client.apply_command(self.command, execute=True)


class ControlCliTests(unittest.TestCase):
    @patch("zontcontrol.control.ZontClient")
    def test_preview_without_token_or_client(self, client):
        cases = [
            (["set-temp", "--temperature", "22"], {"target_temp": 22.0}),
            (["activate-mode", "--duration", "30"], {"duration": 30}),
            (["set-switch", "--state", "off"], {"target_state": False}),
            (["press-button"], {}),
            (["set-regulator", "--value", "2.5"], {"target_value": 2.5}),
            (["run-scenario"], {}),
            (["set-guard", "--state", "on"], {"enable": True}),
        ]
        for argv, body in cases:
            with self.subTest(argv=argv), patch.dict("os.environ", {}, clear=True), \
                    patch("sys.argv", ["control", *argv, "--device-id", "42", "--element-id", "10"]), \
                    patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(main(), 0)
                result = json.loads(stdout.getvalue())
                self.assertTrue(result["dry_run"])
                self.assertEqual(result["body"], body)
        client.assert_not_called()

    @patch("zontcontrol.control.ZontClient")
    def test_execute_requires_token(self, client):
        with patch.dict("os.environ", {}, clear=True), \
                patch("sys.argv", ["control", "set-temp", "--device-id", "42", "--element-id", "10",
                                   "--temperature", "22", "--execute"]), \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(main(), 1)
            self.assertIn("ZONT_TOKEN", stderr.getvalue())
        client.assert_not_called()

    @patch("zontcontrol.control.ZontClient")
    def test_execute_flag_dispatches_only_to_mock_client(self, client):
        client.return_value.apply_command.return_value = {"ok": True, "executed": True}
        with patch.dict("os.environ", {"ZONT_TOKEN": "test-token", "ZONT_CONTACT": "owner@example.com"}), \
                patch("sys.argv", ["control", "set-switch", "--device-id", "42", "--element-id", "10",
                                   "--state", "on", "--execute"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(), 0)
        command = client.return_value.apply_command.call_args.args[0]
        self.assertEqual(command.body, {"target_state": True})
        self.assertEqual(client.return_value.apply_command.call_args.kwargs, {"execute": True})

    @patch("zontcontrol.control.ZontClient")
    def test_invalid_cli_number_fails_without_network(self, client):
        with patch("sys.argv", ["control", "set-temp", "--device-id", "42", "--element-id", "10",
                               "--temperature", "nan"]), \
                patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(), 1)
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
