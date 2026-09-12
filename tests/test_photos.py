"""Avatar slot rules: list crops must not clobber a stored face."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.photos import next_photo_slot, photo_exists, photo_file


class NextSlotTests(unittest.TestCase):
    def test_first_empty_alias_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "toby").mkdir()
            (root / "toby" / "hannah.jpg").write_bytes(b"x" * 100)
            with patch("src.photos.avatars_dir", return_value=root):
                with patch("src.photos.current_phone_id", create=True):
                    from src.phones import phone_scope

                    with phone_scope("toby"):
                        self.assertTrue(photo_exists("Hannah", "toby"))
                        self.assertEqual(
                            next_photo_slot("Hannah", ["Hannah", "Hannah 2"]),
                            "Hannah 2",
                        )


class PhotoFileTests(unittest.TestCase):
    def test_slug(self):
        self.assertTrue(str(photo_file("Hannah", "toby")).endswith("hannah.jpg"))


if __name__ == "__main__":
    unittest.main()
