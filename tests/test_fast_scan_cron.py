import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.jobs.fast_scan_cron import mark_ran, next_due_at, should_run


class FastScanJitterTests(unittest.TestCase):
    def test_first_run_is_due(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("src.jobs.fast_scan_cron._state_path", return_value=Path(tmp) / "next.txt"):
                self.assertEqual(next_due_at(), 0.0)
                self.assertTrue(should_run(now=1_700_000_000))

    def test_skips_until_random_gap(self):
        rng = random.Random(0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "next.txt"
            with patch("src.jobs.fast_scan_cron._state_path", return_value=path):
                now = 1_700_000_000.0
                due = mark_ran(now=now, rng=rng)
                self.assertGreaterEqual(due - now, 30 * 60)
                self.assertLessEqual(due - now, 90 * 60)
                self.assertFalse(should_run(now=now + 60))
                self.assertTrue(should_run(now=due))


if __name__ == "__main__":
    unittest.main()
