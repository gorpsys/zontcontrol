import unittest
from unittest.mock import Mock

from zontcontrol.inventory import control_descriptors, value_descriptors
from zontcontrol.controller.contracts import InvalidArguments
from zontcontrol.controller.service import ControllerService


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
    def service(self, raw):
        adapter = Mock()
        adapter.devices.return_value = [raw]
        return ControllerService(adapter), adapter

    def test_catalog_is_read_only_and_exposes_typed_actions_labels_and_limits(self):
        service, adapter = self.service(inventory_device())
        items = {row.key.element_id: row for row in service.list_controls(42)}
        self.assertEqual(items[10].actions, ('target-temp',))
        self.assertEqual((items[10].minimum, items[10].maximum), (10, 30))
        self.assertEqual(items[20].actions, ('activate',))
        self.assertEqual(items[30].actions, ('trigger',))
        self.assertEqual(items[31].name, 'Сброс котла')
        self.assertEqual(items[31].labels, ((True, 'отключен котёл'), (False, 'включен котёл')))
        self.assertEqual(items[32].actions, ('set-voltage',))
        self.assertEqual((items[32].minimum, items[32].maximum, items[32].step), (0, 10, .1))
        for element_id in (33, 34, 40):
            self.assertEqual(items[element_id].actions, ())
            self.assertTrue(items[element_id].unavailable_reasons)
        self.assertEqual(items[50].actions, ('activate',))
        self.assertEqual(items[60].actions, ('activate',))
        self.assertNotIn('PRIVATE', repr(items))
        adapter.send_command.assert_not_called()

    def test_dependent_circuit_has_no_target_action(self):
        raw = inventory_device()
        raw['circuits'][0]['is_dependent'] = True
        service, _ = self.service(raw)
        row = service.list_controls(42)[0]
        self.assertEqual(row.actions, ())
        self.assertTrue(row.unavailable_reasons)

    def test_missing_optional_categories_are_empty(self):
        raw = inventory_device()
        for key in ('controls', 'actuators', 'scenarios', 'guard_zones'):
            raw.pop(key)
        service, _ = self.service(raw)
        self.assertEqual({row.key.element_id for row in service.list_controls(42)}, {10, 20})

    def test_bad_containers_names_and_numeric_limits_are_safe_errors(self):
        for field, value in [('controls', []), ('actuators', None), ('scenarios', {}),
                             ('controls', {'buttons': [{'id': 30, 'name': None}]}),
                             ('circuits', None), ('circuits', [None]), ('modes', {}),
                             ('modes', [{'id': 1}])]:
            raw = inventory_device()
            raw[field] = value
            service, _ = self.service(raw)
            with self.subTest(field=field, value=value), self.assertRaises(InvalidArguments):
                service.list_controls(42)
        for field, value in [('min', '10'), ('max', float('nan')), ('step', True)]:
            raw = inventory_device()
            raw['circuits'][0][field] = value
            service, _ = self.service(raw)
            with self.subTest(field=field), self.assertRaises(InvalidArguments):
                service.list_controls(42)

    def test_value_and_control_identities_are_unique_per_kind(self):
        raw = inventory_device()
        raw['circuits'].append(dict(raw['circuits'][0]))
        with self.assertRaises(InvalidArguments):
            value_descriptors(raw)
        with self.assertRaises(InvalidArguments):
            control_descriptors(raw)
        raw['circuits'].pop()
        raw['controls']['buttons'][0]['id'] = 31
        rows = control_descriptors(raw)
        self.assertEqual(len([row for row in rows if row.key.element_id == 31]), 2)
