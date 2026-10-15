import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.collect_history import collect
from zontcontrol.client import ZontError


class CollectionTests(unittest.TestCase):
    @patch("scripts.collect_history.time.sleep")
    def test_rate_limited_read_is_retried_and_saved_result_is_reused(self, sleep):
        start = datetime(2026, 7, 22, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        data = {"devices": [{"time_truncated": False, "temperature_sensors": [], "circuits": []}]}
        client = Mock()
        client.read_history.side_effect = [ZontError("API rate limit exceeded; try again later"), data]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first = collect(client, start, end, output, 700877, 20496)
            second = collect(client, start, end, output, 700877, 20496)
            self.assertEqual(first, second)
            self.assertEqual(len(list(output.glob("*.json.gz"))), 1)
        self.assertEqual(client.read_history.call_count, 2)
        client.apply_command.assert_not_called()

    @patch("scripts.collect_history.time.sleep")
    def test_non_retryable_error_does_not_create_a_successful_checkpoint(self, sleep):
        start = datetime(2026, 7, 22, tzinfo=timezone.utc)
        client = Mock()
        client.read_history.side_effect = ZontError("API access denied")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ZontError):
                collect(client, start, start + timedelta(days=1), Path(directory), 700877, 20496)
            self.assertEqual(list(Path(directory).iterdir()), [])
        client.read_history.assert_called_once()
        client.apply_command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
