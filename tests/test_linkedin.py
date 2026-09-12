import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.linkedin_screen import (
    find_messaging_entry,
    find_nav_point,
    looks_like_feed,
    looks_like_messaging,
    looks_like_profile,
    parse_messaging_list,
    parse_open_thread,
    should_open_row,
)
from src.phone_queue import cron_skip_reason
from src.phones import phone_scope
from src.store import connect, list_people, upsert_chat


FIXTURES = Path(__file__).parent / "fixtures"


class LinkedInParseTests(unittest.TestCase):
    def test_messaging_list_and_unread(self):
        xml = (FIXTURES / "linkedin_messaging.xml").read_text(encoding="utf-8")
        hits = parse_messaging_list(xml)
        names = [h.name for h in hits]
        self.assertIn("Ada Lovelace", names)
        grace = next(h for h in hits if h.name == "Grace Hopper")
        self.assertTrue(grace.unread)
        self.assertTrue(should_open_row(grace, "old preview"))
        ada = next(h for h in hits if h.name == "Ada Lovelace")
        self.assertFalse(should_open_row(ada, ada.preview))
        self.assertTrue(should_open_row(ada, "different preview"))
        self.assertEqual(find_nav_point(xml, "Messaging")[0], 780)

    def test_open_thread(self):
        xml = (FIXTURES / "linkedin_thread.xml").read_text(encoding="utf-8")
        name, msgs = parse_open_thread(xml)
        self.assertEqual(name, "Grace Hopper")
        self.assertEqual(msgs[0], ("them", "Can you send the deck?"))
        self.assertEqual(msgs[-1][0], "you")

    def test_pixel_thread_uses_toolbar_title(self):
        xml = (FIXTURES / "linkedin_pixel_thread.xml").read_text(encoding="utf-8")
        name, msgs = parse_open_thread(xml)
        self.assertEqual(name, "David Parry")
        self.assertTrue(any("thanks for connecting" in m[1].lower() for m in msgs))
        self.assertFalse(any(m[1] in {"Active now", "TODAY", "Thanks David", "Okay"} for m in msgs))

    def test_galaxy_inbox_rows(self):
        xml = (FIXTURES / "linkedin_galaxy_messaging.xml").read_text(encoding="utf-8")
        self.assertTrue(looks_like_messaging(xml))
        hits = parse_messaging_list(xml)
        names = [h.name for h in hits]
        self.assertIn("Toby Claxton", names)
        self.assertIn("Patricia Mae Fregil", names)
        self.assertNotIn("Button", names)
        self.assertNotIn("Message", names)
        toby = next(h for h in hits if h.name == "Toby Claxton")
        self.assertTrue(toby.unread)
        self.assertEqual(toby.preview, "oh dear")

    def test_galaxy_feed_has_header_inbox(self):
        xml = (FIXTURES / "linkedin_galaxy_feed.xml").read_text(encoding="utf-8")
        self.assertTrue(looks_like_feed(xml))
        point = find_messaging_entry(xml)
        self.assertIsNotNone(point)
        self.assertGreater(point[0], 1200)

    def test_pixel_profile_is_not_inbox(self):
        xml = (FIXTURES / "linkedin_pixel_profile.xml").read_text(encoding="utf-8")
        self.assertTrue(looks_like_profile(xml))
        self.assertFalse(looks_like_messaging(xml))
        self.assertEqual(parse_messaging_list(xml), [])


class ChannelStoreTests(unittest.TestCase):
    def test_reconnect_keeps_linkedin_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "friends.db"
            conn = connect(path)
            try:
                with phone_scope("toby"):
                    upsert_chat(conn, "Ada", preview="li", last_text="li", channel="linkedin")
                    conn.commit()
            finally:
                conn.close()
            conn = connect(path)
            try:
                with phone_scope("toby"):
                    rows = list_people(conn, channel="linkedin")
                    bff = list_people(conn, channel="bumble")
                self.assertEqual([r["name"] for r in rows], ["Ada"])
                self.assertEqual(bff, [])
            finally:
                conn.close()

    def test_same_name_different_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "friends.db")
            try:
                with phone_scope("toby"):
                    upsert_chat(conn, "Ada", preview="bff", last_text="bff", channel="bumble")
                    upsert_chat(conn, "Ada", preview="li", last_text="li", channel="linkedin")
                    conn.commit()
                    bff = list_people(conn, channel="bumble")
                    li = list_people(conn, channel="linkedin")
                self.assertEqual([r["name"] for r in bff], ["Ada"])
                self.assertEqual([r["preview"] for r in bff], ["bff"])
                self.assertEqual([r["name"] for r in li], ["Ada"])
                self.assertEqual([r["preview"] for r in li], ["li"])
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM people WHERE IFNULL(channel,'bumble')='linkedin'").fetchone()[0],
                    0,
                )
                self.assertEqual(conn.execute("SELECT count(*) FROM li_people").fetchone()[0], 1)
            finally:
                conn.close()


class LinkedInCronSkipTests(unittest.TestCase):
    def test_skips_when_other_channel_occupies(self):
        busy = [{"phone_id": "toby", "kind": "fast_scan", "status": "running"}]
        with patch("src.phone_queue.queue_snapshot", return_value=busy):
            self.assertEqual(cron_skip_reason("toby", "linkedin"), "waiting for Bumble job")
            self.assertIsNone(cron_skip_reason("archie", "linkedin"))
        li = [{"phone_id": "archie", "kind": "linkedin_scan", "status": "queued"}]
        with patch("src.phone_queue.queue_snapshot", return_value=li):
            self.assertEqual(cron_skip_reason("archie", "bumble"), "waiting for LinkedIn job")
            self.assertIsNone(cron_skip_reason("toby", "bumble"))


class LinkedInCronGapTests(unittest.TestCase):
    def test_four_hour_gap(self):
        from src.jobs.linkedin_cron import mark_ran, should_run

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last.txt"
            with patch("src.jobs.linkedin_cron._state_path", return_value=path):
                now = 1_700_000_000.0
                self.assertTrue(should_run(now=now))
                mark_ran(now=now)
                self.assertFalse(should_run(now=now + 60))
                self.assertTrue(should_run(now=now + 4 * 60 * 60))


if __name__ == "__main__":
    unittest.main()
