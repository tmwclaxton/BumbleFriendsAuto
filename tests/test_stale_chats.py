import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.store import (
    activity_is_stale,
    chat_activity_stale,
    connect,
    parse_list_when,
    replace_thread,
    upsert_chat,
)
from src.sync_chats import _fast_verdict, _list_rows


class ListWhenTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc)

    def test_relative_clocks(self):
        self.assertFalse(activity_is_stale(parse_list_when("2h", now=self.now), now=self.now))
        self.assertFalse(activity_is_stale(parse_list_when("6d", now=self.now), now=self.now))
        self.assertTrue(activity_is_stale(parse_list_when("3w", now=self.now), now=self.now))
        self.assertTrue(activity_is_stale(parse_list_when("2 weeks ago", now=self.now), now=self.now))
        self.assertTrue(activity_is_stale(parse_list_when("20 Aug", now=self.now), now=self.now))
        self.assertIsNone(parse_list_when("24 hours left to message", now=self.now))

    def test_list_row_picks_up_clock(self):
        xml = """
        <hierarchy>
          <node resource-id="com.bumblebff.app:id/connectionItem" bounds="[0,200][1080,360]">
            <node resource-id="com.bumblebff.app:id/personName" text="Kiki"/>
            <node resource-id="com.bumblebff.app:id/connectionItem_message" text="hey"/>
            <node resource-id="com.bumblebff.app:id/connectionItem_time" text="3w"/>
          </node>
        </hierarchy>
        """
        rows = _list_rows(xml, height=2400, width=1080)
        self.assertEqual(rows[0]["name"], "Kiki")
        self.assertEqual(rows[0]["list_time"], "3w")


class StaleVerdictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_fast_skips_quiet_row_even_if_preview_differs(self):
        upsert_chat(self.conn, "Kiki", last_from="them", last_text="old hello")
        action, target, _ = _fast_verdict(
            self.conn,
            {"name": "Kiki", "preview": "new hello", "badge": "", "list_time": "3w"},
        )
        self.assertEqual(action, "skip")
        self.assertEqual(target, "Kiki")

    def test_stored_last_active_skips_without_clock(self):
        upsert_chat(self.conn, "Marwan", last_from="them", last_text="yo", list_time="3w")
        self.assertTrue(chat_activity_stale(self.conn, "Marwan"))
        action, _, _ = _fast_verdict(
            self.conn,
            {"name": "Marwan", "preview": "different", "badge": "", "list_time": ""},
        )
        self.assertEqual(action, "skip")

    def test_replace_thread_marks_fresh_activity(self):
        pid = upsert_chat(self.conn, "Tinie", last_from="them", last_text="old", list_time="3w")
        replace_thread(self.conn, pid, [("them", "just now hi")])
        row = self.conn.execute(
            "SELECT last_active_at FROM chats WHERE person_id=?", (pid,)
        ).fetchone()
        self.assertTrue(row["last_active_at"])
        self.assertFalse(chat_activity_stale(self.conn, "Tinie"))


if __name__ == "__main__":
    unittest.main()
