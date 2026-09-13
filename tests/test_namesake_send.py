"""Namesake send: identify the open thread, never hit a phone."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.phones import phone_scope
from src.store import connect, namesake_same_person, replace_thread, upsert_chat
from src.sync_chats import (
    _list_row_key,
    _new_friend_already_saved,
    _save_name_for_thread,
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

    def test_empty_ghost_clone_merges_into_the_real_chat(self):
        chat = upsert_chat(self.conn, "Hannah", last_from="them", last_text="I have Exploding Kittens")
        replace_thread(
            self.conn,
            chat,
            [("them", "I have Exploding Kittens"), ("you", "bring it")],
        )
        upsert_chat(self.conn, "Hannah 3", last_from="", last_text="", preview="")
        self.assertTrue(namesake_same_person(self.conn, "Hannah", "Hannah 3"))

    def test_list_row_key_keeps_both_hannahs(self):
        live = _list_row_key({"name": "Hannah", "preview": "Yea it’s my fav!"})
        dead = _list_row_key({"name": "Hannah", "preview": "Match expired"})
        self.assertNotEqual(live, dead)


class EmptyNewFriendNamesakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "t.db")
        self.scope = phone_scope("toby")
        self.scope.__enter__()

    def tearDown(self):
        self.scope.__exit__(None, None, None)
        self.conn.close()
        self.tmp.cleanup()

    def test_empty_strip_does_not_reuse_past_hannah(self):
        hid = upsert_chat(self.conn, "Hannah", last_from="them", last_text="Can't do tomorrow")
        replace_thread(
            self.conn,
            hid,
            [("them", "Can't do tomorrow"), ("you", "no worries")],
        )
        self.conn.execute("UPDATE chats SET status = 'dismissed' WHERE person_id = ?", (hid,))
        self.conn.commit()
        self.assertFalse(_new_friend_already_saved(self.conn, "Hannah"))
        self.assertEqual(_save_name_for_thread(self.conn, "Hannah", []), "Hannah 2")

    def test_recapture_reuses_existing_new_friend_stub(self):
        hid = upsert_chat(self.conn, "Hannah", last_from="them", last_text="old chat")
        replace_thread(self.conn, hid, [("them", "old chat"), ("you", "ok")])
        upsert_chat(self.conn, "Hannah 2", message_until="2026-09-20T12:00:00+00:00")
        self.conn.commit()
        self.assertTrue(_new_friend_already_saved(self.conn, "Hannah 2"))
        self.assertEqual(_save_name_for_thread(self.conn, "Hannah", []), "Hannah 2")


if __name__ == "__main__":
    unittest.main()
