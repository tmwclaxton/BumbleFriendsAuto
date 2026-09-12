import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.linkedin_draft import load_prompt, save_prompt
from src.linkedin_profile import profile_message_fp, should_harvest
from src.linkedin_store import ensure_schema, get_person, replace_thread, upsert_chat
from src.phones import phone_scope
from src.store import connect


class _JsonRequest:
    method = "POST"

    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class LinkedInDraftTests(unittest.TestCase):
    def test_prompt_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "linkedin_prompt.json"
            with patch("src.linkedin_draft.PROMPT_PATH", path):
                saved = save_prompt("system x", "user {name}", calendly="https://calendly.com/tmwclaxton/30min")
                self.assertEqual(load_prompt(), saved)

    def test_schema_and_queue_endpoint(self):
        from src.server import api_li_draft_generate

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "friends.db"
            conn = connect(path)
            ensure_schema(conn)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(li_people)")}
            self.assertIn("profile_fp", cols)
            with phone_scope("archie"):
                pid = upsert_chat(conn, "Ada Lovelace", phone_id="archie")
                conn.execute(
                    "INSERT INTO li_messages(person_id, side, body, captured_at) VALUES (?, 'them', 'Hello', '2026-09-12T00:00:00Z')",
                    (pid,),
                )
                conn.commit()
            conn.close()
            with patch("src.server.load_config", return_value={"db_path": str(path)}):
                response = asyncio.run(
                    api_li_draft_generate(
                        _JsonRequest({"name": "Ada Lovelace", "phone_id": "archie", "text": "keep it short"})
                    )
                )
            self.assertEqual(response.status_code, 200)
            conn = connect(path)
            with phone_scope("archie"):
                row = get_person(conn, "Ada Lovelace", "archie")
            self.assertEqual(row["draft_status"], "queued")
            self.assertTrue(row["draft_pending_fp"])
            conn.close()

    def test_profile_harvest_skips_inmail_and_same_fingerprint(self):
        inmail = {
            "name": "Patricia Mae Fregil",
            "preview": "InMail • Grow Your Hiring Pipeline",
            "last_text": "",
            "spam": "",
            "profile_captured_at": "",
            "profile_fp": "",
            "last_from": "them",
        }
        self.assertEqual(should_harvest(inmail, [("them", inmail["preview"])])[1], "inmail skipped")
        pairs = [("you", "Hello"), ("them", "Interested")]
        normal = {
            "name": "Ada Lovelace",
            "preview": "Interested",
            "last_text": "Interested",
            "spam": "ok",
            "profile_captured_at": "2026-09-12T00:00:00Z",
            "profile_fp": profile_message_fp(pairs),
            "last_from": "them",
        }
        self.assertEqual(should_harvest(normal, pairs)[1], "already captured for this inbound")
        self.assertTrue(should_harvest(normal, pairs + [("them", "Can we talk?")])[0])

    def test_force_cannot_classify_outbound_first_thread(self):
        from src.linkedin_spam import classify_person

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "friends.db"
            conn = connect(path)
            ensure_schema(conn)
            with phone_scope("toby"):
                pid = upsert_chat(conn, "Outbound Person", phone_id="toby")
                replace_thread(conn, pid, [("you", "Our initial pitch"), ("them", "Thanks")])
                conn.commit()
            conn.close()
            result = classify_person(
                "Outbound Person",
                "toby",
                cfg={"db_path": str(path)},
                force=True,
            )
            self.assertEqual(result["reason"], "not inbound from stranger")
            conn = connect(path)
            with phone_scope("toby"):
                row = get_person(conn, "Outbound Person", "toby")
            self.assertEqual(row["spam"], "")
            conn.close()


if __name__ == "__main__":
    unittest.main()
