import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import src.jobs.fast_scan_cron
import src.jobs.linkedin_backfill_cron
import src.jobs.linkedin_session_cron
import src.linkedin_session
import src.phone_queue
from src.daily_schedule import (
    ACCEPTED_CHANNELS,
    clamp_hinge_swipe_due,
    consume_fast_scan_slot,
    consume_plan_slot,
    space_lane,
    today_schedule,
)


LONDON = ZoneInfo("Europe/London")


class DailyScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "data").mkdir()
        self.plan = self.root / "data" / "daily_phone_plan.json"
        self.patches = [
            patch("src.daily_schedule._plan_path", return_value=self.plan),
            patch("src.jobs.fast_scan_cron._state_path", return_value=self.root / "data" / "fast_scan_next.txt"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _typical(self, fast_due):
        session_dues = {
            "toby": datetime(2026, 7, 2, 13, 4, tzinfo=LONDON).timestamp(),
            "archie": datetime(2026, 7, 2, 15, 8, tzinfo=LONDON).timestamp(),
        }
        backfill_dues = {
            "toby": datetime(2026, 7, 2, 21, 11, tzinfo=LONDON).timestamp(),
            "archie": datetime(2026, 7, 2, 22, 2, tzinfo=LONDON).timestamp(),
        }
        stack = ExitStack()
        stack.enter_context(patch("src.daily_schedule._configured_phone_ids", return_value=["toby", "archie"]))
        stack.enter_context(patch("src.daily_schedule.expand_phone_ids", return_value=["toby", "archie"]))
        stack.enter_context(patch("src.jobs.fast_scan_cron.next_due_at", return_value=fast_due))
        stack.enter_context(patch("src.linkedin_session.load_prefs", return_value={"daily_auto": True, "phone_id": "all"}))
        stack.enter_context(patch("src.jobs.linkedin_session_cron.due_map", return_value=session_dues))
        stack.enter_context(patch("src.jobs.linkedin_backfill_cron.due_map", return_value=backfill_dues))
        stack.enter_context(patch("src.phone_queue.average_duration_seconds", return_value=600.0))
        return stack

    def test_london_day_uses_intended_jobs_not_cron_ticks(self):
        now = datetime(2026, 7, 2, 9, 12, tzinfo=LONDON)
        fast_due = datetime(2026, 7, 2, 10, 17, tzinfo=LONDON).timestamp()
        with self._typical(fast_due):
            result = today_schedule(now=now, initialize_due=False)

        self.assertEqual(result["date"], "2026-07-02")
        self.assertEqual(result["timezone"], "Europe/London")
        self.assertEqual(result["channels"], list(ACCEPTED_CHANNELS))
        self.assertIn("hinge", result["channels"])
        hinge = [row for row in result["items"] if row["kind"] == "hinge_scan"]
        self.assertGreaterEqual(len(hinge), 4)
        self.assertTrue(all(row["phone_id"] == "toby" for row in hinge))
        fast = [row for row in result["items"] if row["kind"] == "fast_scan"]
        self.assertGreaterEqual(len(fast), 4)
        self.assertTrue(
            any("10:17:00+01:00" in str(row.get("intended_at") or row["scheduled_at"]) for row in fast)
        )
        self.assertGreater(len(result["items"]), 12)
        self.assertTrue(all(row["expected_duration_seconds"] == 600.0 for row in result["items"]))
        self.assertTrue(all(row["projected_end_at"] > row["projected_start_at"] for row in result["items"]))

    def test_typical_crons_fill_more_than_a_couple_blocks_per_phone(self):
        now = datetime(2026, 7, 2, 9, 12, tzinfo=LONDON)
        fast_due = datetime(2026, 7, 2, 10, 17, tzinfo=LONDON).timestamp()
        with self._typical(fast_due):
            result = today_schedule(now=now, initialize_due=False)

        for pid in ("toby", "archie"):
            kinds = [row["kind"] for row in result["items"] if row["phone_id"] == pid]
            self.assertGreater(len(kinds), 2, kinds)
            self.assertIn("recapture_all", kinds)
            self.assertGreater(kinds.count("fast_scan"), 2, kinds)
            self.assertGreater(kinds.count("linkedin_scan"), 6, kinds)
            self.assertGreaterEqual(kinds.count("linkedin_feed"), 3, kinds)
            self.assertIn("linkedin_session", kinds)
            self.assertIn("linkedin_backfill", kinds)
            if pid == "toby":
                self.assertGreaterEqual(kinds.count("hinge_scan"), 4, kinds)
                self.assertEqual(kinds.count("hinge_swipe"), 1, kinds)
                self.assertEqual(kinds.count("instagram_feed"), 2, kinds)
            else:
                self.assertEqual(kinds.count("hinge_scan"), 0, kinds)
                self.assertEqual(kinds.count("hinge_swipe"), 0, kinds)
                self.assertEqual(kinds.count("instagram_feed"), 0, kinds)
            rows = [row for row in result["items"] if row["phone_id"] == pid]
            by_lane = {}
            for row in rows:
                by_lane.setdefault(space_lane(row), []).append(row)
            for lane_rows in by_lane.values():
                lane_rows.sort(key=lambda row: row["scheduled_at"])
                prev_end = None
                for row in lane_rows:
                    start = datetime.fromisoformat(row["scheduled_at"])
                    end = datetime.fromisoformat(row["projected_end_at"])
                    self.assertGreater(end, start, row)
                    self.assertGreaterEqual(row["expected_duration_seconds"], 600.0, row)
                    if prev_end is not None:
                        self.assertGreaterEqual(start, prev_end, row)
                    prev_end = end
            swipe = [row for row in rows if row["kind"] == "hinge_swipe"]
            for row in swipe:
                hour = datetime.fromisoformat(row["scheduled_at"]).hour
                self.assertGreaterEqual(hour, 4, row)
                self.assertLess(hour, 7, row)

    def test_plan_persists_and_is_consumed(self):
        now = datetime(2026, 7, 2, 10, 17, tzinfo=LONDON)
        fast_due = now.timestamp()
        with self._typical(fast_due):
            first = today_schedule(now=now, initialize_due=False)
            second = today_schedule(now=now, initialize_due=False)
        self.assertEqual(
            [row["scheduled_at"] for row in first["items"] if row["kind"] == "fast_scan"],
            [row["scheduled_at"] for row in second["items"] if row["kind"] == "fast_scan"],
        )
        nxt = consume_fast_scan_slot(now=now)
        self.assertGreater(nxt, fast_due)
        raw = json.loads(self.plan.read_text(encoding="utf-8"))
        slots = raw["2026-07-02"]["fast_scan"]
        self.assertTrue(any(slot.get("consumed") for slot in slots))
        raw = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertGreater(len(raw["2026-07-02"]["linkedin_scan"]), 6)
        self.assertGreaterEqual(len(raw["2026-07-02"]["linkedin_feed"]), 3)

    def test_queue_accepts_linkedin_feed_and_hinge_kinds(self):
        from src.phone_queue import LINKEDIN_OCCUPY, job_channel, job_title

        self.assertEqual(job_channel("linkedin_feed"), "linkedin")
        self.assertEqual(job_title({"kind": "linkedin_feed"}), "LinkedIn feed react")
        self.assertEqual(job_title({"kind": "linkedin_scan"}), "LinkedIn reply check")
        self.assertIn("linkedin_feed", LINKEDIN_OCCUPY)
        self.assertEqual(job_channel("hinge_scan"), "hinge")
        self.assertEqual(job_channel("hinge_swipe"), "hinge")
        self.assertEqual(job_channel("instagram_prune"), "instagram")
        self.assertEqual(job_channel("instagram_feed"), "instagram")
        self.assertEqual(job_title({"kind": "instagram_feed"}), "Instagram following likes")
        self.assertEqual(job_channel("whatsapp_group"), "whatsapp")
        self.assertEqual(job_channel("fast_scan"), "bumble")
        now = datetime(2026, 7, 2, 9, 12, tzinfo=LONDON)
        fast_due = datetime(2026, 7, 2, 10, 17, tzinfo=LONDON).timestamp()
        with self._typical(fast_due):
            result = today_schedule(now=now, initialize_due=False)
        self.assertIn("hinge", result["channels"])
        self.assertTrue(any(row["kind"] == "hinge_scan" and row["phone_id"] == "toby" for row in result["items"]))
        self.assertFalse(any(row["kind"] == "hinge_scan" and row["phone_id"] == "archie" for row in result["items"]))

    def test_consume_marks_only_the_due_slot_not_every_overdue_one(self):
        now = datetime(2026, 7, 2, 12, 0, tzinfo=LONDON)
        self.plan.write_text(
            json.dumps(
                {
                    "2026-07-02": {
                        "timezone": "Europe/London",
                        "linkedin_scan": [
                            {"at": datetime(2026, 7, 2, 8, 0, tzinfo=LONDON).timestamp(), "consumed": False},
                            {"at": datetime(2026, 7, 2, 9, 30, tzinfo=LONDON).timestamp(), "consumed": False},
                            {"at": datetime(2026, 7, 2, 15, 0, tzinfo=LONDON).timestamp(), "consumed": False},
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        nxt = consume_plan_slot("linkedin_scan", now=now)
        slots = json.loads(self.plan.read_text(encoding="utf-8"))["2026-07-02"]["linkedin_scan"]
        self.assertTrue(slots[0]["consumed"])
        self.assertFalse(slots[1]["consumed"])
        self.assertFalse(slots[2]["consumed"])
        self.assertEqual(nxt, slots[1]["at"])

    def test_hinge_swipe_stays_early_morning(self):
        now = datetime(2026, 9, 13, 16, 10, tzinfo=LONDON)
        morning = datetime(2026, 9, 14, 4, 45, tzinfo=LONDON).timestamp()
        self.assertEqual(clamp_hinge_swipe_due(morning, now=now, rng=__import__("random").Random(1)), morning)
        afternoon = datetime(2026, 9, 14, 14, 2, tzinfo=LONDON).timestamp()
        nxt = clamp_hinge_swipe_due(afternoon, now=now, rng=__import__("random").Random(1))
        due = datetime.fromtimestamp(nxt, LONDON)
        self.assertGreaterEqual(due.hour, 4)
        self.assertLess(due.hour, 7)
        from src.jobs.hinge_cron import should_swipe, swipe_hour_ok

        self.assertTrue(swipe_hour_ok(morning))
        self.assertFalse(swipe_hour_ok(afternoon))
        self.assertFalse(should_swipe(afternoon))


if __name__ == "__main__":
    unittest.main()
