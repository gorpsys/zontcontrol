import io
import json
import unittest
from unittest.mock import Mock, patch

from zontcontrol.cli import main
from zontcontrol.client import ZontClient, ZontError


def inventory_device():
    return {
        "id": 42, "name": "Дом", "online": True,
        "device_info": {"model": "SMART 2.0", "serial": "PRIVATE"},
        "circuits": [{"id": 10, "name": "Отопление", "type": "consumer",
                      "actual_temp": 21.6, "target_temp": 21, "current_mode": 20,
                      "active": False, "is_off": False, "in_summer_mode": False,
                      "min": 10, "max": 30}],
        "modes": [{"id": 20, "name": "Расписание", "applied": [10], "can_be_applied": [10]}],
        "controls": {
            "buttons": [{"id": 30, "name": "Нажать"}],
            "toggle_buttons": [{"id": 31, "name": {"name": "Сброс котла",
                                "active_label": "отключен котёл", "inactive_label": "включен котёл"},
                                "active": False}],
            "regulators": [{"id": 32, "name": "Выход", "value": 2, "min": 0, "max": 10,
                            "step": 0.1, "unit": "В"}],
            "statuses": [{"id": 33, "name": "Статус", "active": True}],
            "selects": [{"id": 34, "name": "Новый тип"}],
        },
        "actuators": {"relays": [{"id": 40, "name": "Реле", "on": False}]},
        "scenarios": [{"id": 50, "name": "Сценарий"}],
        "guard_zones": [{"id": 60, "name": "Охрана", "state": "off", "alarm": False}],
        "sim_info": {"phone": "PRIVATE_PHONE"},
    }


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.client = ZontClient("owner@example.com", token="test-token")
        self.opener = Mock()
        self.client._opener = self.opener

    def catalog(self, device):
        self.opener.open.return_value = io.BytesIO(json.dumps({"ok": True, "devices": [device]}).encode())
        result = self.client.list_controls()
        return {item["id"]: item for item in result["devices"][0]["elements"]}

    def test_catalog_is_read_only_and_has_correct_command_parameters(self):
        items = self.catalog(inventory_device())
        self.opener.open.assert_called_once()
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.full_url, "https://my.zont.online/api/widget/v3/devices")
        self.assertEqual(items[10]["state"]["target_temp"], 21)
        self.assertEqual(items[10]["actions"][0]["url"],
                         "https://my.zont.online/api/widget/v3/devices/42/circuits/10/actions/target-temp")
        self.assertIn("target_temp", items[10]["actions"][0]["parameters"])
        self.assertEqual(items[20]["state"]["can_be_applied"], [10])
        self.assertEqual(items[20]["actions"][0]["scope"], "device")
        self.assertEqual(items[30]["actions"][0]["parameters"], {})
        self.assertEqual(items[31]["name"], "Сброс котла")
        self.assertEqual(items[31]["active_label"], "отключен котёл")
        self.assertEqual(items[31]["inactive_label"], "включен котёл")
        self.assertIn("target_state", items[31]["actions"][0]["parameters"])
        self.assertIn("target_value", items[32]["actions"][0]["parameters"])
        self.assertEqual(items[33]["actions"], [])
        self.assertEqual(items[34]["actions"], [])
        self.assertEqual(items[40]["actions"], [])
        self.assertIn("reason", items[40])
        self.assertIn("/scenarios/50/", items[50]["actions"][0]["url"])
        self.assertIn("enable", items[60]["actions"][0]["parameters"])
        self.assertNotIn("PRIVATE", json.dumps(items))

    def test_dependent_circuit_cannot_set_target(self):
        device = inventory_device()
        device["circuits"][0]["is_dependent"] = True
        items = self.catalog(device)
        self.assertEqual(items[10]["actions"], [])
        self.assertIn("Зависимый", items[10]["reason"])

    def test_missing_optional_categories(self):
        device = inventory_device()
        for key in ("controls", "actuators", "scenarios", "guard_zones"):
            device.pop(key)
        self.assertEqual(set(self.catalog(device)), {10, 20})

    def test_bad_containers_fail_without_raw_response(self):
        for field, value in [("controls", []), ("actuators", None), ("scenarios", {}),
                             ("controls", {"buttons": [{"id": 30, "name": None}]})]:
            with self.subTest(field=field):
                device = inventory_device()
                device[field] = value
                with self.assertRaises(ValueError):
                    self.catalog(device)

    def test_device_filter_and_missing_device(self):
        self.opener.open.return_value = io.BytesIO(b'{"ok": true, "devices": []}')
        with self.assertRaises(ZontError):
            self.client.list_controls(42)
        request = self.opener.open.call_args.args[0]
        self.assertTrue(request.full_url.endswith("?device_ids=42"))

    @patch("zontcontrol.cli.ZontClient")
    def test_cli_catalog_flag_does_not_read_sensors(self, client):
        client.return_value.list_controls.return_value = {"devices": []}
        with patch.dict("os.environ", {"ZONT_TOKEN": "test-token", "ZONT_CONTACT": "owner@example.com"}), \
                patch("sys.argv", ["zontcontrol", "--list-controls", "--device-id", "42"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(), 0)
        client.return_value.list_controls.assert_called_once_with(42)
        client.return_value.read_sensors.assert_not_called()


if __name__ == "__main__":
    unittest.main()
