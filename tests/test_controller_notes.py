"""Public notes metadata operations, with no network or physical commands."""

import io
import json
import unittest
from unittest.mock import Mock

from zontcontrol.controller.contracts import CommandOutcomeUnknown, InvalidArguments
from zontcontrol.controller.service import ControllerService, ReadControllerService
from zontcontrol.controller.transport import HttpTransport
from zontcontrol.controller.zont import ZontAdapter


class ControllerNotesTests(unittest.TestCase):
    def setUp(self):
        self.opener = Mock()
        self.adapter = ZontAdapter(HttpTransport(timeout=2, opener=self.opener), {})
        self.controller = ControllerService(self.adapter)

    def answer(self, **data):
        self.opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(
            json.dumps({'ok': True, **data}).encode())

    def test_read_notes_preserves_exact_text_on_explicit_offline_device(self):
        notes = 'первая строка\n"вторая" ☃'
        self.answer(devices=[{'id': 41, 'notes': 'other'},
                             {'id': 42, 'name': 'Original', 'online': False, 'notes': notes}])
        self.assertEqual(self.controller.read_notes(42), notes)
        request = self.opener.open.call_args_list[0].args[0]
        self.assertEqual(request.full_url, 'https://my.zont.online/api/devices')
        self.assertEqual(json.loads(request.data), {'load_io': True})
        self.assertEqual(self.opener.open.call_count, 2)
        marker = json.loads(self.opener.open.call_args_list[1].args[0].data)
        self.assertEqual(set(marker), {'device_id', 'name'})
        self.assertTrue(marker['name'].startswith('Original [zontcontrol connected '))
        self.controller.read_notes(42)
        self.assertEqual(self.opener.open.call_count, 3)

    def test_absent_or_null_notes_are_empty_and_other_types_rejected(self):
        for metadata in ({}, {'notes': None}, {'notes': ''}):
            with self.subTest(metadata=metadata):
                self.answer(devices=[{'id': 42, 'name': 'Original', **metadata}])
                self.assertEqual(self.controller.read_notes(42), '')
        for notes in (False, 1, [], {}):
            with self.subTest(notes=notes), self.assertRaises(InvalidArguments):
                self.answer(devices=[{'id': 42, 'name': 'Original', 'notes': notes}])
                self.controller.read_notes(42)

    def test_write_replaces_only_notes_once_and_allows_clearing(self):
        for notes in ('лог\nс новой строкой', ''):
            with self.subTest(notes=notes):
                self.opener.reset_mock()
                self.answer()
                self.assertIsNone(self.controller.write_notes(42, notes))
                request = self.opener.open.call_args.args[0]
                self.assertEqual(request.get_method(), 'POST')
                self.assertEqual(request.full_url, 'https://my.zont.online/api/update_device')
                self.assertEqual(json.loads(request.data), {'device_id': 42, 'notes': notes})
                self.opener.open.assert_called_once()

    def test_invalid_arguments_fail_before_http(self):
        for device_id in (True, 0, -1, 1.0, '42', None):
            for operation in (lambda: self.controller.read_notes(device_id),
                              lambda: self.controller.write_notes(device_id, 'text')):
                with self.subTest(device_id=device_id), self.assertRaises(InvalidArguments):
                    operation()
        for notes in (None, 1, False, [], {}):
            with self.subTest(notes=notes), self.assertRaises(InvalidArguments):
                self.controller.write_notes(42, notes)
        self.opener.open.assert_not_called()

    def test_unknown_write_is_not_retried(self):
        self.opener.open.side_effect = ConnectionResetError('private upstream text')
        with self.assertRaises(CommandOutcomeUnknown) as caught:
            self.controller.write_notes(42, 'log')
        self.assertNotIn('private', str(caught.exception))
        self.opener.open.assert_called_once()

    def test_read_facade_has_notes_read_but_no_write(self):
        facade = ReadControllerService(self.adapter)
        self.answer(devices=[{'id': 42, 'name': 'Original', 'notes': 'text'}])
        self.assertEqual(facade.read_notes(42), 'text')
        self.assertFalse(hasattr(facade, 'write_notes'))

    def test_read_does_not_accept_foreign_metadata_from_adapter(self):
        adapter = Mock()
        for device_id in (41, True, 42.0, None):
            adapter.read_notes.return_value = {'id': device_id, 'notes': 'foreign'}
            with self.subTest(device_id=device_id), self.assertRaises(InvalidArguments):
                ControllerService(adapter).read_notes(42)
