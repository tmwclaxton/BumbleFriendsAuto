import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.ethnicity_vision import _people_to_guess, schedule_guess
from src.store import connect, set_ethnicity, upsert_chat


class EthnicityVisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "t.db")
        upsert_chat(self.conn, "Max")
        upsert_chat(self.conn, "Pete")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_backfill_skips_manual_and_people_without_photos(self) -> None:
        self.assertTrue(set_ethnicity(self.conn, "Pete", "white", source="manual"))
        with patch("src.ethnicity_vision.photo_exists", side_effect=lambda name, phone=None: name == "Max"):
            names = [n for n, _pid in _people_to_guess(self.conn)]
        self.assertEqual(names, ["Max"])

    def test_vision_tag_is_not_guessed_again_unless_forced(self) -> None:
        self.assertTrue(set_ethnicity(self.conn, "Max", "black", source="vision"))
        with patch("src.ethnicity_vision.photo_exists", return_value=True):
            names = [n for n, _pid in _people_to_guess(self.conn)]
            self.assertNotIn("Max", names)
            forced = [n for n, _pid in _people_to_guess(self.conn, name="Max", force=True)]
        self.assertEqual(forced, ["Max"])

    def test_schedule_without_key_does_not_raise(self) -> None:
        with patch("src.ethnicity_vision.api_key", return_value=""):
            out = schedule_guess(name="Max")
        self.assertFalse(out.get("ok"))
        self.assertIn("NANOGPT", out.get("error") or "")


if __name__ == "__main__":
    unittest.main()
