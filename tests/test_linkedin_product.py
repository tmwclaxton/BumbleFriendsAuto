import tempfile
import unittest
from pathlib import Path

from src.linkedin_product import (
    heuristic_product,
    message_product_fp,
    needs_product_check,
    parse_product_reply,
    pick_primary,
)
from src.phones import phone_scope
from src.store import connect


class LinkedInProductTests(unittest.TestCase):
    def test_parse_product_reply(self):
        self.assertEqual(
            parse_product_reply('{"product":"grantgunner","reason":"we pitched"}'),
            ("grantgunner", "we pitched"),
        )
        self.assertEqual(parse_product_reply('{"product":"Grant Gunner","reason":"x"}')[0], "grantgunner")
        self.assertEqual(parse_product_reply('{"product":"","reason":"unrelated"}')[0], "")
        self.assertEqual(parse_product_reply("snitch looks relevant")[0], "snitch")
        self.assertEqual(parse_product_reply("ok thanks")[0], "")

    def test_heuristic_names_only(self):
        self.assertEqual(
            heuristic_product([("you", "Hi Rebecca, Founder of GrantGunner")])[0],
            "grantgunner",
        )
        self.assertEqual(
            heuristic_product([("you", "archie@canvassr.org — door-to-door campaigns")])[0],
            "canvassr",
        )
        self.assertEqual(
            heuristic_product([("you", "Try Snitch at snitchsocial.net")])[0],
            "snitch",
        )
        self.assertIsNone(heuristic_product([("you", "Hi, thanks for connecting.")]))
        self.assertIsNone(heuristic_product([("them", "We apply for grants every year.")]))

    def test_pick_primary_prefers_most_discussed(self):
        messages = [
            ("you", "Hi, Founder of GrantGunner"),
            ("them", "Canvassr looks closer to what we do on the doors"),
            ("them", "Yes — canvassing teams, not grant writing"),
        ]
        self.assertEqual(pick_primary(messages)[0], "canvassr")

    def test_product_fp_changes_with_new_message(self):
        opened = [("you", "Hi, founder of GrantGunner")]
        fp = message_product_fp(opened)
        self.assertTrue(needs_product_check("", opened))
        self.assertFalse(needs_product_check(fp, opened))
        self.assertTrue(needs_product_check(fp, opened + [("them", "Tell me more about Snitch")]))
        self.assertFalse(needs_product_check("", []))

    def test_store_product_column(self):
        from src.linkedin_store import ensure_schema, get_person, set_product
        from src.linkedin_store import upsert_chat as li_upsert

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "friends.db")
            try:
                ensure_schema(conn)
                cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(li_chats)")}
                self.assertIn("product", cols)
                self.assertIn("product_reason", cols)
                self.assertIn("product_fp", cols)
                with phone_scope("toby"):
                    li_upsert(conn, "Ada Lovelace", last_from="you", last_text="Hi")
                    ok = set_product(
                        conn,
                        "Ada Lovelace",
                        "snitch",
                        "competitor intel",
                        phone_id="toby",
                        fingerprint="abc123",
                    )
                    conn.commit()
                    person = get_person(conn, "Ada Lovelace", "toby")
                self.assertTrue(ok)
                self.assertEqual(person["product"], "snitch")
                self.assertEqual(person["product_reason"], "competitor intel")
                self.assertEqual(person["product_fp"], "abc123")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
