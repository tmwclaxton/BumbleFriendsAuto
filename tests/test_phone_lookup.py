"""Find inbox people by a phone number in their messages."""

from __future__ import annotations

import sqlite3
import unittest

from src.store import extract_phones, find_person_by_phone_digits, phone_match_keys


class PhoneLookupTests(unittest.TestCase):
    def test_uk_formats_share_keys(self):
        self.assertTrue(phone_match_keys("07376771187") & phone_match_keys("+44 7376 771187"))

    def test_finds_person_from_thread_number(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, phone_id TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, person_id INTEGER, side TEXT, body TEXT);
            INSERT INTO people VALUES (1, 'Pete', 'toby');
            INSERT INTO messages VALUES (1, 1, 'them', '+44 7376 771187 here it is mate');
            """
        )
        row = find_person_by_phone_digits(conn, "07376771187")
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "Pete")
        self.assertEqual(extract_phones("+44 7376 771187 here"), ["07376771187"])
