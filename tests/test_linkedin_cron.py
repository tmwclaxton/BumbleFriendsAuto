import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.jobs.linkedin_cron import (
    _should_consume_slot,
    mark_feed_ran,
    mark_scan_ran,
    next_feed_due_at,
    next_scan_due_at,
    should_run,
    should_run_feed,
)
from src.linkedin_screen import looks_like_security_wall
from src.phone_queue import LINKEDIN_OCCUPY, job_channel, job_title


class LinkedInDayCronTests(unittest.TestCase):
    def test_scan_and_feed_are_registered_jobs(self):
        self.assertEqual(job_channel("linkedin_scan"), "linkedin")
        self.assertEqual(job_channel("linkedin_feed"), "linkedin")
        self.assertEqual(job_title({"kind": "linkedin_scan"}), "LinkedIn reply check")
        self.assertEqual(job_title({"kind": "linkedin_feed"}), "LinkedIn feed react")
        self.assertIn("linkedin_scan", LINKEDIN_OCCUPY)
        self.assertIn("linkedin_feed", LINKEDIN_OCCUPY)

    def test_plan_gaps_skip_until_due(self):
        rng = random.Random(1)
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            with (
                patch("src.jobs.linkedin_cron._state_path", side_effect=lambda name: Path(tmp) / name),
                patch("src.daily_schedule._plan_path", return_value=plan),
            ):
                now = 1_700_000_000.0
                scan_due = mark_scan_ran(now=now, rng=rng)
                feed_due = mark_feed_ran(now=now, rng=rng)
                self.assertGreater(scan_due, now)
                self.assertGreater(feed_due, now)
                self.assertFalse(should_run(now=now + 30))
                self.assertFalse(should_run_feed(now=now + 30))
                self.assertTrue(should_run(now=scan_due))
                self.assertTrue(should_run_feed(now=feed_due))
                self.assertGreater(next_scan_due_at(), 0)
                self.assertGreater(next_feed_due_at(), 0)

    def test_partial_queue_does_not_burn_the_slot(self):
        self.assertTrue(
            _should_consume_slot({"status": "queued", "queued": True, "jobs": [{"id": 1}], "skipped": []})
        )
        self.assertFalse(
            _should_consume_slot(
                {
                    "status": "queued",
                    "queued": True,
                    "jobs": [{"id": 1, "phone_id": "archie"}],
                    "skipped": [{"phone_id": "toby", "reason": "waiting for Instagram job"}],
                }
            )
        )
        self.assertFalse(_should_consume_slot({"status": "queued", "queued": False, "jobs": [], "skipped": []}))

    def test_security_wall_is_detected(self):
        self.assertTrue(looks_like_security_wall("We restricted your account after unusual activity"))
        self.assertTrue(looks_like_security_wall("Try again later — too many attempts"))
        self.assertFalse(looks_like_security_wall("Ada liked your comment"))


if __name__ == "__main__":
    unittest.main()
