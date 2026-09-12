"""Invented Bumble threads → CRM create/update decisions. Never hits production."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src.crm import _lead_payload_from_draft, process_due_crm_syncs, sync_inbox_to_crm
from src.crm_extract import crm_ready_to_save, crm_should_update, extract_crm_fields
from src.crm_llm import merge_crm_fields
from src.store import (
    connect,
    enqueue_crm_sync_if_needed,
    list_pending_crm_syncs,
    replace_thread,
    set_lgs_lead_id,
    upsert_chat,
)

GROUPS = [
    {"id": 1, "name": "LGS High Wycombe", "slug": "lgs-high-wycombe", "town": "High Wycombe"},
    {"id": 2, "name": "LGS London", "slug": "lgs-london", "town": "Battersea"},
    {"id": 3, "name": "LGS Glos", "slug": "lgs-glos", "town": "Gloucester"},
]
TODAY = date(2026, 9, 12)


def draft(name: str, thread: list[dict], **kwargs) -> dict:
    data = extract_crm_fields(
        thread,
        inbox_name=name,
        display_name=kwargs.get("display_name", name),
        profile_location=kwargs.get("profile_location", ""),
        profile_age=kwargs.get("profile_age"),
        ethnicity=kwargs.get("ethnicity", ""),
        phone_id=kwargs.get("phone_id", "toby"),
        groups=GROUPS,
        today=TODAY,
    )
    data["ok"] = True
    data["phone_id"] = kwargs.get("phone_id", "toby")
    return data


class FakeCRM:
    def __init__(self):
        self.created: list[dict] = []
        self.updated: list[tuple[int, dict]] = []
        self.next_id = 100

    def create(self, payload):
        self.next_id += 1
        card = {"id": self.next_id, **payload}
        self.created.append(card)
        return {"ok": True, "created": True, "contact": card}

    def update(self, lead_id, payload):
        self.updated.append((int(lead_id), payload))
        return {"ok": True, "contact": {"id": lead_id, **payload}}


class ScenarioExtractTests(unittest.TestCase):
    def test_full_yes_plus_number_is_ready_for_bucks(self):
        fields = draft(
            "Gianluca",
            [
                {"side": "you", "body": "Whereabouts you based?"},
                {"side": "them", "body": "I’m from Milton Keynes area but I drive"},
                {"side": "you", "body": "We're planning an escape room Saturday 26th September"},
                {"side": "them", "body": "Yeah I’d be up for that. 07853699997"},
                {"side": "them", "body": "add me on insta it’s gian103_"},
            ],
        )
        self.assertTrue(crm_ready_to_save(fields))
        self.assertEqual(fields["phone"], "07853699997")
        self.assertEqual(fields["hometown"], "Milton Keynes")
        self.assertEqual(fields["hub"], "bucks")
        self.assertEqual(fields["home_lgs_group_id"], 1)
        self.assertEqual(fields["instagram"], "gian103_")

    def test_chatty_without_number_is_not_ready(self):
        fields = draft(
            "Alex",
            [
                {"side": "them", "body": "I'm in Battersea, love hiking and padel"},
                {"side": "them", "body": "yeah that Saturday sounds class"},
            ],
        )
        self.assertEqual(fields["hometown"], "Battersea")
        self.assertFalse(crm_ready_to_save(fields))

    def test_number_without_place_is_not_ready(self):
        fields = draft("Lee", [{"side": "them", "body": "my number is 07911112222 add me"}])
        self.assertEqual(fields["phone"], "07911112222")
        self.assertFalse(crm_ready_to_save(fields))

    def test_profile_town_counts_as_rough_location(self):
        fields = draft(
            "Priya",
            [{"side": "them", "body": "whatsapp me 07700900123"}],
            profile_location="High Wycombe",
        )
        self.assertTrue(crm_ready_to_save(fields))
        self.assertEqual(fields["hometown"], "High Wycombe")
        self.assertEqual(fields["hub"], "bucks")

    def test_battersea_maps_to_london_group(self):
        fields = draft(
            "Maya",
            [{"side": "them", "body": "I'm based in Battersea. 07400111222"}],
        )
        self.assertEqual(fields["hub"], "london")
        self.assertEqual(fields["home_lgs_group_id"], 2)

    def test_gloucester_maps_to_glos(self):
        fields = draft(
            "Owen",
            [{"side": "them", "body": "I live in Gloucester. 07500111222"}],
        )
        self.assertEqual(fields["hub"], "glos")
        self.assertEqual(fields["home_lgs_group_id"], 3)

    def test_plus44_number(self):
        fields = draft(
            "Nia",
            [{"side": "them", "body": "I'm in Reading, +44 7700 900111"}],
        )
        self.assertTrue(crm_ready_to_save(fields))
        self.assertTrue(fields["phone"].replace(" ", "").endswith("7700900111") or "7700" in fields["phone"])

    def test_bio_junk_stripped_from_name(self):
        fields = draft(
            "Joshua",
            [{"side": "them", "body": "I'm in Oxford 07123456789"}],
            display_name="Joshua • Is there a reason why you are organising",
        )
        self.assertEqual(fields["suggested_contact_name"], "Joshua LGS")

    def test_wycombe_beats_battersea_mention_in_our_invite(self):
        fields = draft(
            "Josh",
            [
                {"side": "you", "body": "We do Battersea and High Wycombe"},
                {"side": "them", "body": "I'm from High Wycombe 07822001100"},
            ],
        )
        self.assertEqual(fields["hometown"], "High Wycombe")
        self.assertEqual(fields["hub"], "bucks")
        self.assertEqual(fields["home_lgs_group_id"], 1)

    def test_age_and_email_extracted(self):
        fields = draft(
            "Tinie",
            [
                {
                    "side": "them",
                    "body": "I'm 27 and based in Aylesbury. tinie@example.com 07988001122",
                }
            ],
        )
        self.assertEqual(fields["age"], 27)
        self.assertEqual(fields["email"], "tinie@example.com")
        self.assertEqual(fields["hometown"], "Aylesbury")
        self.assertTrue(crm_ready_to_save(fields))

    def test_merge_drops_invented_ai_facts(self):
        thread = [{"side": "them", "body": "I'm in London. 07111222333"}]
        base = draft("Sam", thread)
        out = merge_crm_fields(
            base,
            {
                "phone": "07999999999",
                "region": "Paris",
                "email": "not-in-thread@x.com",
                "instagram": "totallyfakehandle",
                "age": 41,
            },
            messages=thread,
        )
        self.assertEqual(out["phone"], "07111222333")
        self.assertEqual(out["hometown"], "London")
        self.assertFalse(out.get("email"))
        self.assertFalse(out.get("instagram"))


class ScenarioSyncTests(unittest.TestCase):
    def _sync(self, name: str, thread: list[dict], crm: FakeCRM, **kwargs):
        fields = draft(name, thread, **kwargs)
        fields.update(kwargs.get("extra") or {})
        with patch("src.crm.crm_draft", return_value=fields):
            with patch("src.crm.create_lead", side_effect=crm.create):
                with patch("src.crm.update_lead", side_effect=crm.update):
                    with patch("src.crm._request", side_effect=AssertionError("must not hit LGS API")):
                        return sync_inbox_to_crm(name, phone_id="toby"), fields

    def test_ready_thread_creates_once_then_skips(self):
        crm = FakeCRM()
        thread = [
            {"side": "them", "body": "I'm from High Wycombe."},
            {"side": "them", "body": "07837000001"},
        ]
        first, fields = self._sync("Tobyish", thread, crm)
        self.assertEqual(first["action"], "create")
        self.assertEqual(len(crm.created), 1)
        payload = _lead_payload_from_draft(fields, inbox_name="Tobyish", phone_id="toby")
        self.assertEqual(payload["hometown"], "High Wycombe")
        self.assertTrue(payload["phone"])

        fields["ok"] = True
        fields["lgs_lead_id"] = crm.created[0]["id"]
        fields["existing_lead"] = {
            "id": crm.created[0]["id"],
            "phone": fields["phone"],
            "hometown": fields["hometown"],
            "closest_lgs_group_id": fields.get("closest_lgs_group_id"),
            "instagram": fields.get("instagram") or "",
            "interested_event": fields.get("interested_event") or "",
        }
        with patch("src.crm.crm_draft", return_value=fields):
            with patch("src.crm.update_lead", side_effect=crm.update):
                again = sync_inbox_to_crm("Tobyish")
        self.assertEqual(again["action"], "skip")
        self.assertEqual(len(crm.updated), 0)

    def test_later_instagram_updates_existing_lead(self):
        crm = FakeCRM()
        thread = [
            {"side": "them", "body": "I'm in Battersea 07400999000"},
            {"side": "them", "body": "insta is maya.bff"},
        ]
        fields = draft("Maya", thread)
        fields["ok"] = True
        fields["lgs_lead_id"] = 44
        fields["existing_lead"] = {
            "id": 44,
            "phone": "07400999000",
            "hometown": "Battersea",
        }
        self.assertTrue(crm_should_update(fields["existing_lead"], fields))
        with patch("src.crm.crm_draft", return_value=fields):
            with patch("src.crm.create_lead", side_effect=crm.create):
                with patch("src.crm.update_lead", side_effect=crm.update):
                    result = sync_inbox_to_crm("Maya")
        self.assertEqual(result["action"], "update")
        self.assertEqual(crm.updated[0][0], 44)
        self.assertEqual(crm.updated[0][1]["instagram"], "maya.bff")
        self.assertEqual(fields["hometown"], "Battersea")
        self.assertEqual(crm.updated[0][1]["hometown"], "Battersea")

    def test_same_facts_do_not_update(self):
        fields = draft(
            "Maya",
            [
                {"side": "them", "body": "I'm in Battersea 07400999000"},
                {"side": "them", "body": "insta is maya.bff"},
            ],
        )
        existing = {
            "id": 44,
            "phone": "07400999000",
            "hometown": "Battersea",
            "instagram": "maya.bff",
            "closest_lgs_group_id": 2,
        }
        self.assertFalse(crm_should_update(existing, fields))

    def test_missing_number_never_posts(self):
        crm = FakeCRM()
        result, _ = self._sync(
            "Alex",
            [{"side": "them", "body": "I'm in Reading, keen for hiking"}],
            crm,
        )
        self.assertEqual(result["action"], "skip")
        self.assertEqual(crm.created, [])
        self.assertEqual(crm.updated, [])

    def test_new_event_yes_updates_lead(self):
        fields = draft(
            "Kiki",
            [
                {"side": "you", "body": "escape room and board games Saturday 26th September"},
                {"side": "them", "body": "I'm in Maidenhead. Yes I'm interested! 07333001122"},
            ],
        )
        fields["ok"] = True
        fields["lgs_lead_id"] = 7
        fields["existing_lead"] = {
            "id": 7,
            "phone": "07333001122",
            "hometown": "Maidenhead",
            "interested_event": "",
        }
        self.assertTrue(crm_should_update(fields["existing_lead"], fields))
        crm = FakeCRM()
        with patch("src.crm.crm_draft", return_value=fields):
            with patch("src.crm.create_lead", side_effect=crm.create):
                with patch("src.crm.update_lead", side_effect=crm.update):
                    result = sync_inbox_to_crm("Kiki")
        self.assertEqual(result["action"], "update")
        self.assertIn("escape room", (crm.updated[0][1].get("interested_event") or "").lower())


class ScenarioQueueWorkerTests(unittest.TestCase):
    def test_worker_creates_from_queued_thread_without_real_http(self):
        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "q.db")
        crm = FakeCRM()
        try:
            pid = upsert_chat(conn, "Marwan", last_from="them", last_text="hey")
            thread = [
                ("you", "whereabouts you based?"),
                ("them", "High Wycombe mate"),
                ("them", "07444111222"),
            ]
            replace_thread(conn, pid, thread)
            conn.commit()
            self.assertEqual(len(list_pending_crm_syncs(conn)), 1)
            fields = draft(
                "Marwan",
                [{"side": s, "body": b} for s, b in thread],
            )
            fields["ok"] = True

            def fake_sync(name, phone_id=None):
                with patch("src.crm.crm_draft", return_value=fields):
                    with patch("src.crm.create_lead", side_effect=crm.create):
                        return sync_inbox_to_crm(name, phone_id=phone_id)

            qdb = Path(tmp.name) / "q.db"
            with patch("src.crm._lgs_settings", return_value=("http://crm.test", "fake-token")):
                with patch("src.crm.db_path_from_config", return_value=qdb):
                    with patch("src.store.db_path_from_config", return_value=qdb):
                        with patch("src.crm.sync_inbox_to_crm", side_effect=fake_sync):
                            n = process_due_crm_syncs(limit=5)
            self.assertEqual(n, 1)
            self.assertEqual(len(crm.created), 1)
            self.assertEqual(crm.created[0]["name"], "Marwan LGS")
            self.assertEqual(list_pending_crm_syncs(conn), [])
        finally:
            conn.close()
            tmp.cleanup()

    def test_second_message_requeues_for_update(self):
        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "q.db")
        try:
            pid = upsert_chat(conn, "Hannah", last_from="them", last_text="hi")
            replace_thread(conn, pid, [("them", "I'm in Slough 07111000000")])
            set_lgs_lead_id(conn, "Hannah", 55)
            conn.execute(
                "UPDATE people SET crm_sync_status='done' WHERE id=?",
                (pid,),
            )
            conn.commit()
            self.assertFalse(
                enqueue_crm_sync_if_needed(conn, pid, [("them", "I'm in Slough 07111000000")])
            )
            self.assertTrue(
                enqueue_crm_sync_if_needed(
                    conn,
                    pid,
                    [("them", "I'm in Slough 07111000000"), ("them", "ig hannah.lgs")],
                )
            )
        finally:
            conn.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
