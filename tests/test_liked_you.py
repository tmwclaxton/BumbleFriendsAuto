"""Liked You list parsing, screen class, and desk toggles."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.liked_you import find_liked_you_action, parse_liked_you_list, run_scan
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

    def test_add_does_not_duplicate_same_person(self):
        liked_you_begin_scan(phone_id="toby")
        liked_you_add({"id": "james|26", "name": "James", "proposed": "like", "decision": "like", "status": "pending"})
        liked_you_add({"id": "james", "name": "James", "proposed": "pass", "decision": "pass", "status": "pending"})
        items = snapshot()["liked_you"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["decision"], "pass")


class DedupParseTests(unittest.TestCase):
    def test_nested_checkout_same_person_once(self):
        xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node class="android.view.View" clickable="true" bounds="[40,400][1040,1800]">
    <node content-desc="Check out James’s profile" bounds="[40,400][1040,1800]"/>
    <node content-desc="Check out James's profile" bounds="[80,500][400,700]"/>
    <node text="James, 26" bounds="[74,1688][312,1757]"/>
  </node>
  <node class="android.view.View" clickable="true" bounds="[40,1870][1040,2190]">
    <node content-desc="Check out Brandon’s profile" bounds="[40,1870][1040,2190]"/>
  </node>
</hierarchy>"""
        hits = parse_liked_you_list(xml)
        names = [h.name for h in hits]
        self.assertEqual(names.count("James"), 1)
        self.assertEqual(len(hits), 2)


class ScanOnceTests(unittest.TestCase):
    @unittest.skipUnless(LIST_XML.is_file() and CARD_XML.is_file(), "no Liked You dumps")
    def test_scan_does_not_reopen_same_featured_card(self):
        list_xml = LIST_XML.read_text(encoding="utf-8")
        card_xml = CARD_XML.read_text(encoding="utf-8")
        list_state = classify("com.bumblebff.app", list_xml)
        card_state = classify("com.bumblebff.app", card_xml)
        phase = {"open": False, "taps": 0}
        hits = parse_liked_you_list(list_xml)

        def fake_read(_device, _package):
            if phase["open"]:
                return card_state, card_xml
            return list_state, list_xml

        def fake_tap(_device, _x, _y):
            phase["taps"] += 1
            phase["open"] = True

        def fake_dismiss(_device, _package):
            phase["open"] = False

        device = MagicMock()
        device.info = {"displayWidth": 1080, "displayHeight": 2400}
        cfg = {"package": "com.bumblebff.app", "phone_id": "toby"}
        with (
            patch("src.liked_you._unlock", return_value=(device, "")),
            patch("src.liked_you._read", side_effect=fake_read),
            patch("src.liked_you.tap", side_effect=fake_tap),
            patch("src.liked_you._dismiss_preview", side_effect=fake_dismiss),
            patch("src.liked_you.evaluate_card", return_value=(True, "ok", {"vision": {"name": "James"}})),
            patch("src.liked_you.wait_idle"),
            patch("src.phone_queue.check_cancel"),
        ):
            ok, msg = run_scan(cfg)
        self.assertTrue(ok)
        items = snapshot()["liked_you"]["items"]
        names = [i["name"] for i in items if i.get("name") and i["name"] != "?"]
        self.assertEqual(len(names), len(set(n.casefold() for n in names)))
        self.assertEqual(phase["taps"], len(hits))
        self.assertLessEqual(phase["taps"], 2)


if __name__ == "__main__":
    unittest.main()
