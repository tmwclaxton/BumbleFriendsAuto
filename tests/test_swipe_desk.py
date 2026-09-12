"""Swipe-desk prefs merge into the live swipe config."""

from __future__ import annotations

import unittest

from src.swipe_desk import apply_prefs_to_cfg, default_prefs, normalize_prefs


class PrefsTests(unittest.TestCase):
    def test_defaults(self):
        prefs = default_prefs()
        self.assertTrue(prefs["final_say"])
        self.assertIn("white", prefs["men_include"])
        self.assertIn("black", prefs["women_include"])

    def test_legacy_swipe_job_is_auto(self):
        cfg = apply_prefs_to_cfg({"filters": {}, "max_swipes": 30}, {"phone_id": "archie", "max_swipes": 12})
        self.assertFalse(cfg["final_say"])
        self.assertEqual(cfg["max_swipes"], 12)
        self.assertEqual(cfg["filters"]["swipe_vision"]["men_if_missing"], "pass")

    def test_desk_keeps_final_say(self):
        prefs = normalize_prefs({"phone_id": "all", "final_say": True, "men_include": ["hispanic"]})
        self.assertEqual(prefs["phone_id"], "toby")
        self.assertEqual(prefs["men_include"], ["hispanic"])
        cfg = apply_prefs_to_cfg({}, prefs)
        self.assertTrue(cfg["final_say"])
        self.assertEqual(cfg["filters"]["swipe_vision"]["women_include"], prefs["women_include"])

    def test_snapshot_includes_liked_you(self):
        from src.swipe_desk import snapshot

        snap = snapshot()
        self.assertIn("liked_you", snap)
        self.assertIn("items", snap["liked_you"])


if __name__ == "__main__":
    unittest.main()
