"""The notes history stores original log text, newest invocation first."""

import json
import unittest

from zontcontrol.cloud import _rotated_notes


class NotesRotationTests(unittest.TestCase):
    def test_zero_through_four_runs_keep_newest_three_exact_logs(self):
        notes = ''
        logs = ['первый\n', 'second "line"\n\n', '☃ third\n', 'fourth\n']
        for index, log in enumerate(logs):
            notes = _rotated_notes(notes, log)
            self.assertEqual(json.loads(notes), list(reversed(logs[:index + 1]))[:3])

    def test_rotation_drops_everything_older_than_two_previous_runs(self):
        self.assertEqual(json.loads(_rotated_notes(json.dumps(['a', 'b', 'c', 'd']), 'new')),
                         ['new', 'a', 'b'])

    def test_arbitrary_log_contents_are_not_parsed_or_filtered(self):
        log = 'ordinary text\n{"new_event":null}\n[not json]\n'
        self.assertEqual(json.loads(_rotated_notes('[]', log)), [log])

    def test_foreign_or_invalid_notes_are_not_silently_overwritten(self):
        for notes in ('foreign text', '{}', 'null', '"text"', '[null]', '[42]', '[{}]'):
            with self.subTest(notes=notes), self.assertRaises(ValueError):
                _rotated_notes(notes, 'new')
