"""CRM field extraction from Bumble threads."""

from __future__ import annotations

import unittest

from datetime import date

from src.crm_extract import (
    crm_ready_to_save,
    crm_should_update,
    extract_crm_fields,
    extract_hometown,
    extract_instagram_them,
)


class HometownTests(unittest.TestCase):
    def test_does_not_swallow_a_following_number(self):
        self.assertEqual(
            extract_hometown("I'm from High Wycombe\n07837000001"),
            "High Wycombe",
        )

    def test_same_line_number_and_later_insta_stay_out_of_hometown(self):
        self.assertEqual(
            extract_hometown("I'm in Battersea 07400999000\ninsta is maya.bff"),
            "Battersea",
        )

    def test_from_area(self):
        self.assertEqual(
            extract_hometown("I’m from Milton Keynes area but I drive."),
            "Milton Keynes",
        )

    def test_in_county(self):
        self.assertEqual(extract_hometown("I'm in Hertfordshire but am willing to travel"), "Hertfordshire")


class InstagramTests(unittest.TestCase):
    def test_loose_insta(self):
        self.assertIn(
            "gian103_",
            extract_instagram_them("if you wanted to add me on insta it’s gian103_"),
        )


class CrmFieldsTests(unittest.TestCase):
    def test_gianluca_style(self):
        thread = [
            {"side": "you", "body": "Whereabouts you based?"},
            {"side": "them", "body": "I’m from Milton Keynes area but I drive. How about you mate"},
            {"side": "you", "body": "We're planning an escape room and board games here on Saturday 26th September, you interested"},
            {"side": "them", "body": "Nice yeah I’d be up for that thank you mate. What’s your Instagram?"},
            {"side": "them", "body": "07853699997"},
            {"side": "them", "body": "add me on insta it’s gian103_"},
        ]
        groups = [
            {"id": 2, "name": "LGS London", "slug": "lgs-london", "town": "Battersea"},
            {"id": 1, "name": "LGS High Wycombe", "slug": "lgs-high-wycombe", "town": "High Wycombe"},
        ]
        data = extract_crm_fields(
            thread,
            inbox_name="Gianluca",
            display_name="Gianluca",
            ethnicity="white",
            groups=groups,
            today=date(2026, 9, 9),
        )
        self.assertEqual(data["suggested_contact_name"], "Gianluca LGS")
        self.assertEqual(data["phone"], "07853699997")
        self.assertEqual(data["instagram"], "gian103_")
        self.assertEqual(data["hometown"], "Milton Keynes")
        self.assertEqual(data["hub"], "bucks")
        self.assertEqual(data["home_lgs_group_id"], 1)
        self.assertTrue(data["interested_event"].lower().startswith("we're planning"))
        self.assertIn("escape room", data["interested_event"].lower())
        self.assertNotIn("wycombe mate", data["interested_event"].lower())
        self.assertIn("Travels / drives", data["suggested_notes"])
        self.assertEqual(data["region"], "Milton Keynes")
        self.assertEqual(data["closest_lgs_group_id"], 1)
        self.assertEqual(data["preferred_contact_method"], "whatsapp")
        self.assertTrue(data["consent_to_contact"])
        self.assertIn("board games", data["interests_skills"])
        self.assertEqual(data["next_follow_up_at"], "2026-09-26")
        self.assertIn("escape room", data["tags"])

    def test_strips_bio_title(self):
        data = extract_crm_fields(
            [],
            inbox_name="Joshua",
            display_name="Joshua • Is there a reason why you are organising",
        )
        self.assertEqual(data["suggested_contact_name"], "Joshua LGS")

    def test_strips_middle_dot_bio(self):
        data = extract_crm_fields(
            [],
            inbox_name="Pete",
            display_name="Pete · I’m in Bicester in Oxfordshire so I’m not…",
        )
        self.assertEqual(data["suggested_contact_name"], "Pete LGS")

    def test_live_in_reading(self):
        self.assertEqual(
            extract_hometown("It’s real cool. I live in Reading so I’d probably be able to do Wycombe"),
            "Reading",
        )


class CrmReadyTests(unittest.TestCase):
    def test_needs_name_phone_and_place(self):
        self.assertFalse(crm_ready_to_save({"suggested_contact_name": "Sam LGS", "phone": "07111"}))
        self.assertTrue(
            crm_ready_to_save(
                {"suggested_contact_name": "Sam LGS", "phone": "07111", "hometown": "Reading"}
            )
        )
        self.assertTrue(
            crm_ready_to_save({"name": "Sam LGS", "phone": "07111", "hub": "bucks"})
        )

    def test_update_when_new_fact_arrives(self):
        existing = {"phone": "07111222333", "hometown": "Reading"}
        self.assertFalse(
            crm_should_update(existing, {"phone": "07111222333", "hometown": "Reading"})
        )
        self.assertTrue(
            crm_should_update(
                existing,
                {"phone": "07111222333", "hometown": "Reading", "instagram": "samx"},
            )
        )
