"""Unit tests for gender-aware swipe vision decisions."""

from __future__ import annotations

import unittest

from src.swipe_vision import decide_swipe, gender_from_texts


class GenderTextTests(unittest.TestCase):
    def test_pronouns(self):
        self.assertEqual(gender_from_texts(["He/Him", "London"]), "male")
        self.assertEqual(gender_from_texts(["She/Her"]), "female")
        self.assertIsNone(gender_from_texts(["They/Them"]))


class DecideSwipeTests(unittest.TestCase):
    def test_man_white_like(self):
        ok, reason = decide_swipe(
            texts=["He/Him"],
            vision={"gender": "male", "ethnicity": "white", "crazy": "no"},
        )
        self.assertTrue(ok)
        self.assertIn("allowed", reason)

    def test_man_south_asian_pass(self):
        ok, reason = decide_swipe(
            texts=[],
            vision={"gender": "male", "ethnicity": "south_asian", "crazy": "no"},
        )
        self.assertFalse(ok)
        self.assertIn("excluded", reason)

    def test_man_black_pass(self):
        ok, _ = decide_swipe(
            texts=[],
            vision={"gender": "male", "ethnicity": "black", "crazy": "no"},
        )
        self.assertFalse(ok)

    def test_man_east_asian_like(self):
        ok, _ = decide_swipe(
            texts=[],
            vision={"gender": "male", "ethnicity": "east_asian", "crazy": "no"},
        )
        self.assertTrue(ok)

    def test_woman_any_race_like(self):
        ok, reason = decide_swipe(
            texts=["She/Her"],
            vision={"gender": "female", "ethnicity": "black", "crazy": "no"},
        )
        self.assertTrue(ok)
        self.assertIn("woman", reason)

    def test_woman_crazy_pass(self):
        ok, reason = decide_swipe(
            texts=[],
            vision={"gender": "female", "ethnicity": "white", "crazy": "yes"},
        )
        self.assertFalse(ok)
        self.assertIn("crazy", reason)

    def test_chip_overrides_vision_ethnicity(self):
        ok, reason = decide_swipe(
            texts=["Ethnicity", "South Asian", "He/Him"],
            vision={"gender": "male", "ethnicity": "white", "crazy": "no"},
        )
        self.assertFalse(ok)
        self.assertIn("south_asian", reason)


if __name__ == "__main__":
    unittest.main()
