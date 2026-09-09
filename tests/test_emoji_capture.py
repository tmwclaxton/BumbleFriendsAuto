"""Bumble accessibility dumps emoji as .. — keep the real glyph."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from src.store import connect, keep_richer_body, merge_thread_keep_emoji, replace_thread, upsert_chat
from src.sync_chats import _bubble_text, extract_messages


class KeepRicherTests(unittest.TestCase):
    def test_keeps_stored_emoji(self):
        self.assertEqual(
            keep_richer_body("you interested ..", "you interested 👀"),
            "you interested 👀",
        )

    def test_prefers_new_when_words_differ(self):
        self.assertEqual(
            keep_richer_body("Sweet I'll look", "you interested 👀"),
            "Sweet I'll look",
        )

    def test_merge_thread(self):
        old = [("you", "you interested 👀"), ("them", "Nice yeah")]
        new = [("you", "you interested .."), ("them", "Nice yeah")]
        self.assertEqual(
            merge_thread_keep_emoji(old, new),
            [("you", "you interested 👀"), ("them", "Nice yeah")],
        )


class ReplaceKeepsEmojiTests(unittest.TestCase):
    def test_recapture_does_not_strip_emoji(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            pid = upsert_chat(conn, "Gianluca", last_from="you", last_text="go karting 👀")
            replace_thread(conn, pid, [("you", "go karting 👀")])
            replace_thread(conn, pid, [("you", "go karting ..")])
            row = conn.execute("SELECT body FROM messages WHERE person_id=?", (pid,)).fetchone()
            self.assertEqual(row["body"], "go karting 👀")
            chat = conn.execute("SELECT last_text FROM chats WHERE person_id=?", (pid,)).fetchone()
            self.assertEqual(chat["last_text"], "go karting 👀")
            conn.close()


class BubbleTextTests(unittest.TestCase):
    def test_uses_desc_when_text_is_dots(self):
        node = ET.fromstring(
            '<node text="you interested .." content-desc="you interested 👀" bounds="[0,200][100,300]"/>'
        )
        self.assertEqual(_bubble_text(node), "you interested 👀")

    def test_splices_child_emoji(self):
        xml = """<?xml version="1.0"?>
        <hierarchy>
          <node text="future one .." bounds="[40,400][900,520]">
            <node content-desc="👀" bounds="[820,430][880,490]"/>
          </node>
        </hierarchy>"""
        msgs = extract_messages(xml, 1080, 2400)
        self.assertTrue(any(m["text"] == "future one 👀" for m in msgs))
