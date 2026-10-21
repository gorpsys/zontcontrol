"""Connection markers are metadata, never device ownership or command retries."""

from datetime import datetime, timezone
import unittest
from unittest.mock import Mock

from zontcontrol.controller.zont import ZontAdapter
from zontcontrol.controller.contracts import CommandOutcomeUnknown


NOW = datetime(2026, 10, 21, 12, 34, 56, tzinfo=timezone.utc)


class ConnectionObservabilityTests(unittest.TestCase):
    def test_read_facade_has_metadata_side_effect_without_exposing_commands(self):
        from zontcontrol.application import create_read_controller
        from tests.test_inventory import inventory_device
        transport = Mock()
        transport.request.side_effect = [{'devices': [inventory_device()]}, {'ok': True}]
        controller = create_read_controller(environ={'ZONT_TOKEN': 'synthetic'},
            reader=lambda _: {'contact': 'reader@example.invalid', 'http_timeout_seconds': 5},
            transport_factory=lambda **_: transport)
        controller.list_controls(42)
        self.assertFalse(hasattr(controller, 'execute_command'))
        self.assertEqual([call.args[0].full_url for call in transport.request.call_args_list], [
            'https://my.zont.online/api/widget/v3/devices?device_ids=42',
            'https://my.zont.online/api/update_device'])

    def adapter(self, name='Дом'):
        adapter = ZontAdapter(Mock(), {}, clock=lambda: NOW)
        adapter._request = Mock(return_value={'devices': [{'id': 42, 'name': name}]})
        return adapter

    def test_scoped_read_marks_only_selected_device_once(self):
        adapter = self.adapter()
        adapter.devices()
        adapter._request.assert_called_once_with('https://my.zont.online/api/widget/v3/devices')
        adapter.devices(42)
        adapter.devices(42)
        writes = [call for call in adapter._request.call_args_list if call.kwargs.get('write')]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].args, ('https://my.zont.online/api/update_device',))
        self.assertEqual(writes[0].kwargs, {'body': {'device_id': 42,
            'name': 'Дом [zontcontrol connected 2026-10-21T12:34:56+00:00]'}, 'write': True})

    def test_new_client_replaces_marker_preserves_exact_base_and_other_suffixes(self):
        for base in ('Дом  ', 'A' * 70000, 'Дом [чужой маркер]'):
            name = base + ' [zontcontrol connected 2026-10-20T10:00:00+00:00]'
            adapter = self.adapter(name)
            adapter.devices(42)
            self.assertEqual(adapter._request.call_args.kwargs['body']['name'],
                base + ' [zontcontrol connected 2026-10-21T12:34:56+00:00]')

    def test_unknown_marker_write_is_not_retried_and_cannot_break_read(self):
        adapter = self.adapter()
        def request(url, **kwargs):
            if kwargs.get('write'):
                raise CommandOutcomeUnknown()
            return {'devices': [{'id': 42, 'name': 'Дом'}]}
        adapter._request.side_effect = request
        with self.assertLogs('zontcontrol', level='WARNING') as logs:
            self.assertEqual(adapter.devices(42), [{'id': 42, 'name': 'Дом'}])
        adapter.devices(42)
        self.assertEqual(sum(call.kwargs.get('write', False)
                             for call in adapter._request.call_args_list), 1)
        self.assertNotIn('Дом', ''.join(logs.output))

    def test_failed_read_does_not_consume_marker_attempt(self):
        adapter = self.adapter()
        adapter._request.side_effect = [ValueError('private'),
            {'devices': [{'id': 42, 'name': 'Дом'}]}, {'ok': True}]
        with self.assertRaises(ValueError):
            adapter.devices(42)
        adapter.devices(42)
        self.assertTrue(adapter._request.call_args.kwargs['write'])

    def test_marker_diagnostic_failure_does_not_break_read(self):
        from unittest.mock import patch
        adapter = self.adapter()
        adapter._request.side_effect = [{'devices': [{'id': 42, 'name': 'Дом'}]},
                                        RuntimeError('private')]
        with patch('zontcontrol.controller.zont.logger.log', side_effect=RuntimeError('private')):
            self.assertEqual(adapter.devices(42)[0]['id'], 42)
