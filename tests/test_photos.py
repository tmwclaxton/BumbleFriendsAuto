"""Avatar slot rules: list crops must not clobber a stored face."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.photos import is_blank_face_image, next_photo_slot, photo_exists, photo_file


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

    def test_blank_placeholder_is_not_a_photo(self):
        from PIL import Image

        white = Image.new("RGB", (160, 160), (255, 255, 255))
        grey = Image.new("RGB", (160, 160), (234, 234, 234))
        face = Image.new("RGB", (160, 160), (40, 80, 60))
        face.putpixel((20, 20), (200, 40, 30))
        face.putpixel((80, 90), (30, 40, 180))
        self.assertTrue(is_blank_face_image(white))
        self.assertTrue(is_blank_face_image(grey))
        self.assertFalse(is_blank_face_image(face))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "toby" / "connor.jpg"
            dest.parent.mkdir()
            white.save(dest, "JPEG", quality=82)
            with patch("src.photos.avatars_dir", return_value=root):
                from src.phones import phone_scope

                with phone_scope("toby"):
                    self.assertFalse(photo_exists("Connor", "toby"))
                    self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
