"""Script archives keep DTO timestamps/unknown and never start algorithms."""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.history_archive import read_archive
from scripts import analyze_history, collect_history
from tests.test_controller_reads import device, packet, START, END
from zontcontrol.controller.service import ControllerService


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.adapter = Mock()
        raw = device()
        raw['sensors'].append({**raw['sensors'][0], 'id': 8, 'name': 'Вода'})
        self.adapter.devices.return_value = [raw]
        self.adapter.load_history.return_value = packet()
        self.service = ControllerService(self.adapter, clock=lambda: END)

    def test_dto_projection_keeps_null_times_and_selected_circuit(self):
        archive = read_archive(self.service, 42, 10, START, END)
        raw, = archive['devices']
        self.assertEqual([c['id'] for c in raw['circuits']], [10])
        self.assertEqual(raw['temperature_sensors'][0]['points'][1],
            {'time': (START + timedelta(minutes=1)).isoformat(), 'value': None})
        self.assertEqual(archive['requested_period']['end'], END.isoformat())
        self.adapter.send_command.assert_not_called()
        self.adapter.update_metadata.assert_not_called()

    def test_control_events_keep_subminute_times_and_boolean_meaning(self):
        self.adapter.load_history.return_value = {'time_truncated': True,
            'z3k_web_element': {'31': {'state': [[int(START.timestamp()), 0], [-35, 1], [-7, 0]]}}}
        archive = read_archive(self.service, 42, 10, START, END)
        raw, = archive['devices']
        self.assertTrue(raw['time_truncated'])
        self.assertEqual(raw['web_elements'][0]['series']['state'][-1],
            {'time': (START + timedelta(seconds=42)).isoformat(), 'value': 0})
        self.assertTrue(self.adapter.load_history.call_args.kwargs['include_controls'])

    def test_plot_uses_stdlib_svg_renderer(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = analyze_history.plot(self.service, 42, 10, 7, 8, START, END,
                                          Path(directory) / 'history.svg')
            self.assertEqual(len(paths), 3)
            for path in paths:
                self.assertIn('<svg', Path(path).read_text())
        self.adapter.send_command.assert_not_called()

    def test_script_help_needs_no_controller_or_secret(self):
        for script in (analyze_history, collect_history):
            with self.subTest(script=script.__name__), patch.object(script, 'create_read_controller') as build, \
                    patch('sys.argv', [script.__name__, '--help']), patch('sys.stdout'):
                with self.assertRaises(SystemExit) as caught:
                    script.main()
                self.assertEqual(caught.exception.code, 0)
                build.assert_not_called()
