"""Liked You list parsing, screen class, and desk toggles."""

from __future__ import annotations

import unittest
from pathlib import Path

from src.liked_you import find_liked_you_action, parse_liked_you_list
from src.screen import ScreenKind, classify
from src.swipe_desk import liked_you_add, liked_you_begin_scan, liked_you_toggle, snapshot

ROOT = Path(__file__).resolve().parents[1]
LIST_XML = ROOT / "tests" / "fixtures" / "liked_you_list.xml"
CARD_XML = ROOT / "tests" / "fixtures" / "liked_you_card.xml"


class ParseTests(unittest.TestCase):
    @unittest.skipUnless(LIST_XML.is_file(), "no Liked You list dump")
    def test_parse_pixel_list(self):
        xml = LIST_XML.read_text(encoding="utf-8")
        hits = parse_liked_you_list(xml)
        names = [h.name for h in hits]
        self.assertIn("James", names)
        self.assertTrue(any(h.name == "Brandon" or "brandon" in h.key for h in hits))
        james = next(h for h in hits if h.name == "James")
        self.assertEqual(james.age, 26)
        self.assertGreater(james.y, 0)

    @unittest.skipUnless(LIST_XML.is_file(), "no Liked You list dump")
    def test_classify_list(self):
        xml = LIST_XML.read_text(encoding="utf-8")
        state = classify("com.bumblebff.app", xml)
        self.assertEqual(state.kind, ScreenKind.LIKED_YOU)

    @unittest.skipUnless(CARD_XML.is_file(), "no Liked You card dump")
    def test_classify_preview(self):
        xml = CARD_XML.read_text(encoding="utf-8")
        state = classify("com.bumblebff.app", xml)
        self.assertEqual(state.kind, ScreenKind.LIKED_YOU_CARD)
        like = find_liked_you_action(xml, like=True)
        pass_pt = find_liked_you_action(xml, like=False)
        self.assertIsNotNone(like)
        self.assertIsNotNone(pass_pt)
        self.assertGreater(like[0], pass_pt[0])


class DeskToggleTests(unittest.TestCase):
    def test_toggle_pending(self):
        liked_you_begin_scan(phone_id="toby")
        liked_you_add(
            {
                "id": "james-26",
                "name": "James",
                "proposed": "like",
                "decision": "like",
                "status": "pending",
            }
        )
        self.assertTrue(liked_you_toggle("james-26", "pass"))
        items = snapshot()["liked_you"]["items"]
        self.assertEqual(items[0]["decision"], "pass")
        self.assertFalse(liked_you_toggle("james-26", "nope"))


if __name__ == "__main__":
    unittest.main()
