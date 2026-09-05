"""Tests for automatic GPT Sol draft turn detection and validation."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.draft_llm import validate_draft
from src.store import (
    claim_auto_draft,
    complete_auto_draft,
    connect,
    enqueue_auto_draft_if_needed,
    fail_auto_draft,
    incoming_turn_fingerprint,
    list_pending_auto_drafts,
    replace_thread,
    retry_auto_draft,
    upsert_chat,
)


class FingerprintTests(unittest.TestCase):
    def test_trailing_them_turn(self):
        msgs = [
            ("you", "Hi, wee group?"),
            ("them", "Yeah sounds good"),
            ("them", "Where is it?"),
        ]
        self.assertEqual(
            incoming_turn_fingerprint(msgs),
            "yeah sounds good\nwhere is it?",
        )

    def test_no_fingerprint_when_you_last(self):
        msgs = [
            ("them", "Hi"),
            ("you", "Awesome whereabouts you based?"),
        ]
        self.assertIsNone(incoming_turn_fingerprint(msgs))

    def test_skips_chrome(self):
        msgs = [
            ("you", "Hi"),
            ("them", "Sure"),
            ("them", "24 hours left to message"),
        ]
        self.assertEqual(incoming_turn_fingerprint(msgs), "sure")


class AutoDraftQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _needs_reply(self, name: str, thread: list[tuple[str, str]]) -> int:
        pid = upsert_chat(
            self.conn,
            name,
            last_from=thread[-1][0],
            last_text=thread[-1][1],
            badge="Your turn" if thread[-1][0] == "them" else "",
        )
        replace_thread(self.conn, pid, thread)
        self.conn.commit()
        return pid

    def test_one_generation_per_incoming_turn(self):
        thread = [("you", "Hi mate wee group?"), ("them", "Yeah!")]
        pid = self._needs_reply("Alex", thread)
        row = self.conn.execute(
            "SELECT draft_status, draft_pending_fp FROM chats WHERE person_id=?",
            (pid,),
        ).fetchone()
        self.assertEqual(row["draft_status"], "pending")
        fp = row["draft_pending_fp"]

        # Recapture same turn — no re-queue while pending/running
        replace_thread(self.conn, pid, thread)
        self.conn.commit()
        row2 = self.conn.execute(
            "SELECT draft_status, draft_pending_fp FROM chats WHERE person_id=?",
            (pid,),
        ).fetchone()
        self.assertEqual(row2["draft_pending_fp"], fp)
        self.assertEqual(row2["draft_status"], "pending")

        self.assertTrue(claim_auto_draft(self.conn, pid, fp))
        self.assertTrue(
            complete_auto_draft(self.conn, "Alex", pending_fp=fp, text="Awesome whereabouts you based?")
        )
        row3 = self.conn.execute(
            "SELECT draft, draft_status, draft_turn_fp, draft_pending_fp FROM chats WHERE person_id=?",
            (pid,),
        ).fetchone()
        self.assertEqual(row3["draft"], "Awesome whereabouts you based?")
        self.assertEqual(row3["draft_status"], "done")
        self.assertEqual(row3["draft_turn_fp"], fp)
        self.assertIsNone(row3["draft_pending_fp"])

        # Same turn again after done — must not re-enqueue
        replace_thread(self.conn, pid, thread)
        self.conn.commit()
        row4 = self.conn.execute(
            "SELECT draft_status, draft_pending_fp FROM chats WHERE person_id=?",
            (pid,),
        ).fetchone()
        self.assertEqual(row4["draft_status"], "done")
        self.assertIsNone(row4["draft_pending_fp"])

    def test_new_turn_replaces_existing_draft(self):
        t1 = [("you", "Hi"), ("them", "Yes")]
        pid = self._needs_reply("Sam", t1)
        fp1 = self.conn.execute(
            "SELECT draft_pending_fp FROM chats WHERE person_id=?", (pid,)
        ).fetchone()["draft_pending_fp"]
        self.assertTrue(claim_auto_draft(self.conn, pid, fp1))
        self.assertTrue(complete_auto_draft(self.conn, "Sam", pending_fp=fp1, text="Whereabouts?"))

        t2 = [("you", "Hi"), ("them", "Yes"), ("you", "Whereabouts?"), ("them", "Battersea")]
        upsert_chat(self.conn, "Sam", last_from="them", last_text="Battersea", badge="Your turn")
        replace_thread(self.conn, pid, t2)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT draft, draft_status, draft_pending_fp FROM chats WHERE person_id=?",
            (pid,),
        ).fetchone()
        self.assertEqual(row["draft_status"], "pending")
        self.assertEqual(row["draft_pending_fp"], "battersea")
        # Old draft still there until worker finishes; then replaced
        self.assertEqual(row["draft"], "Whereabouts?")
        fp2 = row["draft_pending_fp"]
        self.assertTrue(claim_auto_draft(self.conn, pid, fp2))
        self.assertTrue(
            complete_auto_draft(
                self.conn,
                "Sam",
                pending_fp=fp2,
                text="Awesome tbf Battersea Sat 19th mini golf, you interested",
            )
        )
        final = self.conn.execute(
            "SELECT draft FROM chats WHERE person_id=?", (pid,)
        ).fetchone()["draft"]
        self.assertIn("Battersea", final)

    def test_stale_generation_rejected(self):
        thread = [("them", "Hello")]
        pid = self._needs_reply("Jo", thread)
        fp = self.conn.execute(
            "SELECT draft_pending_fp FROM chats WHERE person_id=?", (pid,)
        ).fetchone()["draft_pending_fp"]
        self.assertTrue(claim_auto_draft(self.conn, pid, fp))
        # Newer turn arrives while generating
        newer = [("them", "Hello"), ("them", "You there?")]
        upsert_chat(self.conn, "Jo", last_from="them", last_text="You there?", badge="Your turn")
        replace_thread(self.conn, pid, newer)
        self.conn.commit()
        self.assertFalse(
            complete_auto_draft(self.conn, "Jo", pending_fp=fp, text="stale reply")
        )

    def test_restart_recovery_lists_pending(self):
        thread = [("them", "Interested")]
        pid = self._needs_reply("Lee", thread)
        # Simulate process restart: pending row still in SQLite
        rows = list_pending_auto_drafts(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Lee")
        self.assertEqual(int(rows[0]["person_id"]), pid)

    def test_retry_after_give_up(self):
        thread = [("them", "Yes")]
        pid = self._needs_reply("Pat", thread)
        fp = self.conn.execute(
            "SELECT draft_pending_fp FROM chats WHERE person_id=?", (pid,)
        ).fetchone()["draft_pending_fp"]
        self.assertTrue(claim_auto_draft(self.conn, pid, fp))
        fail_auto_draft(
            self.conn,
            pid,
            pending_fp=fp,
            error="boom",
            attempts=5,
            next_attempt_at=None,
            give_up=True,
        )
        self.assertEqual(list_pending_auto_drafts(self.conn), [])
        self.assertTrue(retry_auto_draft(self.conn, "Pat"))
        rows = list_pending_auto_drafts(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["draft_status"], "pending")


class ValidateDraftTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(
            validate_draft("Awesome whereabouts you based?"),
            "Awesome whereabouts you based?",
        )

    def test_rejects_links(self):
        with self.assertRaises(ValueError):
            validate_draft("See https://docs.google.com/x")

    def test_rejects_sent_claim(self):
        with self.assertRaises(ValueError):
            validate_draft("I have sent you the details")

    def test_strips_fences(self):
        self.assertEqual(validate_draft('```\nSweett\n```'), "Sweett")


class ObsidianContextMockTests(unittest.TestCase):
    def test_generate_draft_uses_mock_providers(self):
        from src import draft_llm

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            upsert_chat(conn, "Ravi", last_from="them", last_text="Yes", badge="Your turn")
            replace_thread(
                conn,
                conn.execute("SELECT id FROM people WHERE name='Ravi'").fetchone()[0],
                [("you", "Hi wee group?"), ("them", "Yes")],
            )
            conn.commit()
            with mock.patch(
                "src.draft_llm.load_draft_context",
                return_value={
                    "events": "## London\n| upcoming | Saturday 19 September 2026 | Battersea | https://example.com | ok |",
                    "run_prompt": "Sound like Toby. Short.",
                    "person_note": "hub: london",
                    "person_note_path": "LGS/People/Ravi.md",
                },
            ), mock.patch(
                "src.draft_llm._chat_completion",
                return_value="Awesome! Whereabouts you based?",
            ):
                text = draft_llm.generate_draft(conn, "Ravi")
            self.assertEqual(text, "Awesome! Whereabouts you based?")
            conn.close()


if __name__ == "__main__":
    unittest.main()
