"""CRM pipeline client."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import tempfile
from pathlib import Path

from src.crm import _crm_payload, create_lead, sync_inbox_to_crm
from src.store import connect, enqueue_crm_sync_if_needed, list_pending_crm_syncs, replace_thread, upsert_chat
from src.crm_llm import merge_crm_fields


class CrmSettingsTests(unittest.TestCase):
    def test_missing_token(self):
        env = {"LGS_PIPELINE_TOKEN": "", "LGS_API_URL": "http://127.0.0.1:8099"}
        with patch.dict("os.environ", env, clear=False):
            with patch("src.crm.load_config", return_value={"lgs": {"api_url": "", "pipeline_token": ""}}):
                result = create_lead({"name": "Sam", "phone": "07123456789"})
        self.assertFalse(result["ok"])
        self.assertIn("LGS_PIPELINE_TOKEN", result["error"])

    def test_payload_maps_new_crm_columns(self):
        body = _crm_payload({
            "name": "Sam LGS",
            "phone": "07123456789",
            "hometown": "High Wycombe",
            "tags": "escape room, drives",
            "preferred_contact_method": "whatsapp",
            "consent_to_contact": True,
            "closest_lgs_group_id": 1,
            "source": "bumble",
        })
        self.assertEqual(body["region"], "High Wycombe")
        self.assertEqual(body["closest_lgs_group_id"], 1)
        self.assertEqual(body["source"], "bumble")
        self.assertEqual(body["tags"], ["escape room", "drives"])


class CrmMergeTests(unittest.TestCase):
    def test_rejects_invented_phone(self):
        messages = [{"side": "them", "body": "I'm in London. 07111222333"}]
        base = {
            "phone": "07111222333",
            "phone_candidates": ["07111222333"],
            "hometown": "London",
            "region": "London",
            "tags": [],
        }
        out = merge_crm_fields(
            base,
            {"phone": "07999999999", "region": "Paris", "email": "fake@example.com"},
            messages=messages,
        )
        self.assertEqual(out["phone"], "07111222333")
        self.assertEqual(out["hometown"], "London")
        self.assertFalse(out.get("email"))

    def test_keeps_regex_instagram_over_truncated_ai(self):
        messages = [{"side": "them", "body": "add me on insta it’s gian103_"}]
        out = merge_crm_fields(
            {"instagram": "gian103_", "phone": "", "phone_candidates": [], "tags": []},
            {"instagram": "gian103"},
            messages=messages,
        )
        self.assertEqual(out["instagram"], "gian103_")


class CrmAutoSyncTests(unittest.TestCase):
    def test_skips_create_until_ready(self):
        draft = {
            "ok": True,
            "suggested_contact_name": "Sam LGS",
            "phone": "",
            "hometown": "Reading",
            "phone_id": "toby",
        }
        with patch("src.crm.crm_draft", return_value=draft):
            result = sync_inbox_to_crm("Sam", phone_id="toby")
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "skip")

    def test_creates_when_ready(self):
        draft = {
            "ok": True,
            "suggested_contact_name": "Sam LGS",
            "phone": "07111222333",
            "hometown": "Reading",
            "phone_id": "toby",
        }
        with patch("src.crm.crm_draft", return_value=draft):
            with patch("src.crm.create_lead", return_value={"ok": True, "created": True}) as create:
                result = sync_inbox_to_crm("Sam", phone_id="toby")
        self.assertEqual(result["action"], "create")
        create.assert_called_once()

    def test_updates_when_existing_lead_is_missing_a_fact(self):
        draft = {
            "ok": True,
            "lgs_lead_id": 9,
            "existing_lead": {"id": 9, "phone": "07111222333", "hometown": "Reading"},
            "suggested_contact_name": "Sam LGS",
            "phone": "07111222333",
            "hometown": "Reading",
            "instagram": "samx",
            "phone_id": "toby",
        }
        with patch("src.crm.crm_draft", return_value=draft):
            with patch("src.crm.update_lead", return_value={"ok": True}) as update:
                result = sync_inbox_to_crm("Sam", phone_id="toby")
        self.assertEqual(result["action"], "update")
        update.assert_called_once()


class CrmQueueTests(unittest.TestCase):
    def test_new_them_message_queues_sync_once(self):
        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "t.db")
        try:
            pid = upsert_chat(conn, "Sam", last_from="them", last_text="hi")
            self.assertTrue(
                enqueue_crm_sync_if_needed(conn, pid, [("them", "I'm in Reading 07111222333")])
            )
            self.assertEqual(len(list_pending_crm_syncs(conn)), 1)
            self.assertFalse(
                enqueue_crm_sync_if_needed(conn, pid, [("them", "I'm in Reading 07111222333")])
            )
            replace_thread(
                conn,
                pid,
                [("them", "I'm in Reading 07111222333"), ("them", "add me on insta samx")],
            )
            self.assertEqual(len(list_pending_crm_syncs(conn)), 1)
        finally:
            conn.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
