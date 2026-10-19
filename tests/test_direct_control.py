"""Commands validate and send directly, without ownership or serialization."""

from copy import deepcopy
from threading import Barrier, Thread
import unittest
from unittest.mock import Mock

from tests.test_inventory import inventory_device
from zontcontrol.controller.contracts import (
    CommandOutcomeUnknown, ControlKey, DeviceUnavailable, InvalidArguments,
)
from zontcontrol.controller.service import ControllerService


class DirectControlTests(unittest.TestCase):
    def setUp(self):
        self.raw = inventory_device()
        self.adapter = Mock()
        self.adapter.devices.side_effect = lambda device_id=None: [deepcopy(self.raw)]
        self.controller = ControllerService(self.adapter)
        self.command = self.controller.prepare_command(
            42, ControlKey(42, 'mode', 20), 'activate', {})

    def test_execute_validates_and_sends_without_a_session(self):
        result = self.controller.execute_command(self.command)
        self.assertTrue(result.api_accepted)
        self.adapter.send_command.assert_called_once()
        sent = self.adapter.send_command.call_args.args[0]
        self.assertEqual((sent.device_id, sent.element_id), (42, 20))
        self.adapter.update_metadata.assert_not_called()
        self.adapter.read_metadata.assert_not_called()

    def test_preview_is_revalidated_before_sending(self):
        self.raw['modes'][0]['can_be_applied'] = []
        with self.assertRaises(InvalidArguments):
            self.controller.execute_command(self.command)
        self.adapter.send_command.assert_not_called()

    def test_offline_device_rejects_command_before_send(self):
        self.raw['online'] = False
        with self.assertRaises(DeviceUnavailable):
            self.controller.execute_command(self.command)
        self.adapter.send_command.assert_not_called()

    def test_unknown_outcome_is_not_retried(self):
        self.adapter.send_command.side_effect = CommandOutcomeUnknown()
        with self.assertRaises(CommandOutcomeUnknown):
            self.controller.execute_command(self.command)
        self.adapter.send_command.assert_called_once()

    def test_two_commands_for_the_same_device_are_not_serialized(self):
        barrier = Barrier(2, timeout=2)
        self.adapter.send_command.side_effect = lambda command: barrier.wait()
        failures = []
        def execute():
            try:
                self.controller.execute_command(self.command)
            except BaseException as error:
                failures.append(error)
        threads = [Thread(target=execute) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.adapter.send_command.call_count, 2)
