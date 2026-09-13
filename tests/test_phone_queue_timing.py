import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import src.phone_queue as queue


class QueueTimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(queue, "ROOT", self.root),
            patch.object(queue, "_banks", {}),
            patch.object(queue, "_job_seq", 0),
            patch.object(queue, "_cancel_ids", set()),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_success_lifecycle_records_all_timing(self):
        job = queue.enqueue("fast_scan", phone_id="toby")
        start = datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc)
        queue.mark_job_started(job["id"], now=start)
        queue.mark_job_finished(
            job["id"], ok=True, message="ok", now=start + timedelta(seconds=75)
        )
        done = queue.get_job(job["id"])
        self.assertEqual(done["status"], "completed")
        self.assertIsNotNone(done["queued_at"])
        self.assertEqual(done["duration_seconds"], 75.0)
        self.assertEqual(len(queue._load_history()), 1)

    def test_error_and_cancel_timing(self):
        failed = queue.enqueue("linkedin_scan", phone_id="toby")
        start = datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc)
        queue.mark_job_started(failed["id"], now=start)
        queue.mark_job_finished(
            failed["id"], ok=False, message="boom", now=start + timedelta(seconds=8)
        )
        self.assertEqual(queue.get_job(failed["id"])["duration_seconds"], 8.0)
        self.assertEqual(queue.get_job(failed["id"])["error"], "boom")

        waiting = queue.enqueue("refresh", name="A", phone_id="toby")
        self.assertTrue(queue.cancel_job(waiting["id"]))
        cancelled = queue.get_job(waiting["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNotNone(cancelled["finished_at"])
        self.assertEqual(cancelled["duration_seconds"], 0.0)

    def test_default_and_rolling_phone_average(self):
        self.assertEqual(queue.average_duration_seconds("fast_scan", "toby"), 600.0)
        for job_id, phone_id, duration in ((1, "toby", 120), (2, "toby", 180), (3, "archie", 240)):
            queue._record_history(
                {
                    "id": job_id,
                    "kind": "fast_scan",
                    "phone_id": phone_id,
                    "status": "completed",
                    "started_at": "2026-09-13T08:00:00Z",
                    "duration_seconds": duration,
                }
            )
        self.assertEqual(queue.average_duration_seconds("fast_scan", "toby"), 150.0)
        self.assertEqual(queue.average_duration_seconds("fast_scan", "archie"), 240.0)
        self.assertEqual(queue.average_duration_seconds("unknown", "toby"), 600.0)

    def test_zero_and_tiny_runtimes_fall_back_to_ten_minutes(self):
        queue._record_history(
            {
                "id": 9,
                "kind": "linkedin_scan",
                "phone_id": "toby",
                "status": "cancelled",
                "started_at": "2026-09-13T08:00:00Z",
                "duration_seconds": 0,
            }
        )
        queue._record_history(
            {
                "id": 10,
                "kind": "linkedin_feed",
                "phone_id": "toby",
                "status": "completed",
                "started_at": "2026-09-13T08:10:00Z",
                "duration_seconds": 12,
            }
        )
        self.assertEqual(queue.average_duration_seconds("linkedin_scan", "toby"), 600.0)
        self.assertEqual(queue.average_duration_seconds("linkedin_feed", "toby"), 600.0)

    def test_queued_projections_are_serial_per_phone(self):
        now = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)
        jobs = [
            {"id": 1, "kind": "a", "phone_id": "toby", "status": "queued"},
            {"id": 2, "kind": "b", "phone_id": "toby", "status": "queued"},
            {"id": 3, "kind": "c", "phone_id": "archie", "status": "queued"},
        ]
        with patch.object(queue, "average_duration_seconds", return_value=600.0):
            projected = queue.project_job_times(jobs, now=now)
        self.assertEqual(projected[0]["projected_start_at"], "2026-09-13T10:00:00Z")
        self.assertEqual(projected[1]["projected_start_at"], "2026-09-13T10:10:00Z")
        self.assertEqual(projected[2]["projected_start_at"], "2026-09-13T10:00:00Z")

    def test_running_end_is_start_plus_expected_and_completed_stays_actual(self):
        now = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)
        jobs = [
            {
                "id": 1,
                "kind": "a",
                "phone_id": "toby",
                "status": "running",
                "started_at": "2026-09-13T09:58:00Z",
            },
            {
                "id": 2,
                "kind": "a",
                "phone_id": "archie",
                "status": "completed",
                "started_at": "2026-09-13T09:00:00Z",
                "finished_at": "2026-09-13T09:03:00Z",
                "duration_seconds": 180.0,
            },
        ]
        with patch.object(queue, "average_duration_seconds", return_value=600.0):
            projected = queue.project_job_times(jobs, now=now)
        self.assertEqual(projected[0]["projected_end_at"], "2026-09-13T10:08:00Z")
        self.assertEqual(projected[1]["projected_end_at"], "2026-09-13T09:03:00Z")
        self.assertEqual(projected[1]["duration_seconds"], 180.0)

    def test_old_persisted_record_is_restored(self):
        path = self.root / "old.json"
        path.write_text(
            json.dumps([{"id": 7, "kind": "refresh", "status": "running"}]),
            encoding="utf-8",
        )
        restored = queue._load_file(path, "toby")
        self.assertEqual(restored[0]["status"], "queued")
        self.assertIsNotNone(restored[0]["queued_at"])
        self.assertIsNone(restored[0]["duration_seconds"])


if __name__ == "__main__":
    unittest.main()
