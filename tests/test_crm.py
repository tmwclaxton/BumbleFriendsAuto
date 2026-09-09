"""CRM pipeline client."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.crm import _crm_payload, create_lead
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


if __name__ == "__main__":
    unittest.main()
