import unittest

from src.whatsapp import listed_groups, parse_people


class WhatsAppParseTest(unittest.TestCase):
    def test_parse_people_keeps_named_numbers(self) -> None:
        people = parse_people(
            [
                {"name": "Toby", "phone": "07837370669"},
                {"name": "Archie", "number": "+44 7398 727993"},
                {"name": "Name only"},
                {"name": ""},
            ]
        )
        self.assertEqual(
            people,
            [
                {"name": "Toby", "phone": "07837370669"},
                {"name": "Archie", "phone": "+44 7398 727993"},
                {"name": "Name only", "phone": ""},
            ],
        )

    def test_listed_groups_include_pixel_lgs_chats(self) -> None:
        names = listed_groups()
        self.assertIn("Let's Go Social (Bucks)", names)
        self.assertTrue(any("London" in name for name in names))
        self.assertIn("Toby + Archie", names)


if __name__ == "__main__":
    unittest.main()
