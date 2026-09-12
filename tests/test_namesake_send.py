"""Namesake send: identify the open thread, never hit a phone."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.store import connect, namesake_same_person, replace_thread, upsert_chat
from src.sync_chats import (
    _list_row_key,
    namesake_identity_from_screen,
    namesake_screen_scores,
)


class NamesakeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "t.db")
        ru = upsert_chat(self.conn, "Ru", last_from="them", last_text="I'm around Slough this week")
        ru2 = upsert_chat(self.conn, "Ru 2", last_from="them", last_text="Yeah Bristol side")
        replace_thread(
            self.conn,
            ru,
            [("them", "I'm around Slough this week"), ("you", "nice one")],
        )
        replace_thread(
            self.conn,
            ru2,
            [("them", "Yeah Bristol side"), ("you", "cool")],
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_unique_visible_bubble_picks_ru(self):
        who = namesake_identity_from_screen(
            self.conn,
            "Ru",
            {"i'm around slough this week"},
        )
        self.assertEqual(who, "Ru")

    def test_scores_prefer_distinctive_overlap(self):
        scores = namesake_screen_scores(
            self.conn,
            "Ru",
            {"i'm around slough this week", "nice one"},
        )
        self.assertGreater(scores["Ru"], scores["Ru 2"])
        self.assertEqual(namesake_identity_from_screen(self.conn, "Ru", {"i'm around slough this week"}), "Ru")

    def test_shared_only_lines_do_not_pick_a_winner(self):
        # Both threads have openers stripped from evidence; empty visible → unsure.
        self.assertIsNone(namesake_identity_from_screen(self.conn, "Ru", set(), ""))

    def test_unique_visible_bubble_picks_ru_2(self):
        who = namesake_identity_from_screen(
            self.conn,
            "Ru 2",
            {"yeah bristol side"},
        )
        self.assertEqual(who, "Ru 2")

    def test_xml_dump_counts_when_bubbles_missing(self):
        xml = "<hierarchy><node text=\"I'm around Slough this week\"/></hierarchy>"
        who = namesake_identity_from_screen(self.conn, "Ru", set(), xml)
        self.assertEqual(who, "Ru")

    def test_both_names_on_screen_is_unsure(self):
        who = namesake_identity_from_screen(
            self.conn,
            "Ru",
            {"i'm around slough this week", "yeah bristol side"},
        )
        self.assertIsNone(who)

    def test_empty_screen_is_unsure(self):
        self.assertIsNone(namesake_identity_from_screen(self.conn, "Ru", set(), ""))


class ExpiredNamesakeSplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_expired_stub_is_not_the_past_chat(self):
        chat = upsert_chat(self.conn, "Hannah", last_from="them", last_text="I have Exploding Kittens")
        replace_thread(
            self.conn,
            chat,
            [("them", "I have Exploding Kittens"), ("you", "bring it")],
        )
        upsert_chat(self.conn, "Hannah 2", last_from="", last_text="expired", preview="Match expired")
        self.assertFalse(namesake_same_person(self.conn, "Hannah", "Hannah 2"))

    def test_list_row_key_keeps_both_hannahs(self):
        live = _list_row_key({"name": "Hannah", "preview": "Yea it’s my fav!"})
        dead = _list_row_key({"name": "Hannah", "preview": "Match expired"})
        self.assertNotEqual(live, dead)


if __name__ == "__main__":
    unittest.main()
