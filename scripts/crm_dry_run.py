#!/usr/bin/env python3
"""Print CRM decisions for invented threads. Never calls the LGS API."""

from datetime import date

from src.crm import _lead_payload_from_draft
from src.crm_extract import crm_ready_to_save, crm_should_update, extract_crm_fields

GROUPS = [
    {"id": 1, "name": "LGS High Wycombe", "slug": "lgs-high-wycombe", "town": "High Wycombe"},
    {"id": 2, "name": "LGS London", "slug": "lgs-london", "town": "Battersea"},
    {"id": 3, "name": "LGS Glos", "slug": "lgs-glos", "town": "Gloucester"},
]

CASES = [
    (
        "Gianluca full",
        "Gianluca",
        [
            {"side": "them", "body": "I’m from Milton Keynes area but I drive"},
            {"side": "them", "body": "Yeah I’d be up for Saturday. 07853699997"},
            {"side": "them", "body": "add me on insta it’s gian103_"},
        ],
        {},
        None,
    ),
    (
        "no number",
        "Alex",
        [{"side": "them", "body": "I'm in Battersea, love hiking"}],
        {},
        None,
    ),
    (
        "number only",
        "Lee",
        [{"side": "them", "body": "07911112222 add me"}],
        {},
        None,
    ),
    (
        "profile town + number",
        "Priya",
        [{"side": "them", "body": "whatsapp me 07700900123"}],
        {"profile_location": "High Wycombe"},
        None,
    ),
    (
        "later instagram",
        "Maya",
        [
            {"side": "them", "body": "I'm in Battersea 07400999000"},
            {"side": "them", "body": "insta is maya.bff"},
        ],
        {},
        {"id": 44, "phone": "07400999000", "hometown": "Battersea"},
    ),
    (
        "same facts again",
        "Maya",
        [
            {"side": "them", "body": "I'm in Battersea 07400999000"},
            {"side": "them", "body": "insta is maya.bff"},
        ],
        {},
        {
            "id": 44,
            "phone": "07400999000",
            "hometown": "Battersea",
            "instagram": "maya.bff",
            "closest_lgs_group_id": 2,
        },
    ),
    (
        "Glos + event yes",
        "Owen",
        [
            {"side": "you", "body": "escape room Saturday 26th September"},
            {"side": "them", "body": "I live in Gloucester. Yes 07500111222"},
        ],
        {},
        None,
    ),
    (
        "Reading +44",
        "Nia",
        [{"side": "them", "body": "I'm in Reading, +44 7700 900111"}],
        {},
        None,
    ),
    (
        "age email Aylesbury",
        "Tinie",
        [
            {
                "side": "them",
                "body": "I'm 27 and based in Aylesbury. tinie@example.com 07988001122",
            }
        ],
        {},
        None,
    ),
    (
        "keen but no number",
        "Alex",
        [
            {"side": "you", "body": "escape room Saturday 26th September"},
            {"side": "them", "body": "I'm in Reading, yeah I'd be up for that"},
        ],
        {},
        None,
    ),
]


def main() -> None:
    print(f"{'case':<24} {'action':<8} name / phone / place / extra")
    print("-" * 88)
    for title, name, thread, kwargs, existing in CASES:
        fields = extract_crm_fields(
            thread,
            inbox_name=name,
            display_name=name,
            profile_location=str(kwargs.get("profile_location") or ""),
            groups=GROUPS,
            today=date(2026, 9, 12),
        )
        if existing:
            action = "update" if crm_should_update(existing, fields) else "skip"
        elif crm_ready_to_save(fields):
            action = "create"
        else:
            action = "skip"
        payload = _lead_payload_from_draft(fields, inbox_name=name, phone_id="toby")
        extra = payload.get("instagram") or payload.get("interested_event") or ""
        print(
            f"{title:<24} {action:<8} {payload.get('name')} / {payload.get('phone') or '-'} / "
            f"{payload.get('hometown') or '-'} / {extra}"
        )


if __name__ == "__main__":
    main()
