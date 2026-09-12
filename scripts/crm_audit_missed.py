#!/usr/bin/env python3
"""Read-only: scan inbox threads for CRM-ready people we have not created/updated.

Never POSTs or PATCHes the LGS CRM.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import date

from src.crm import get_lead, list_groups
from src.crm_extract import crm_ready_to_save, crm_should_update, extract_crm_fields
from src.store import connect, db_path_from_config


def _digits(value: object) -> str:
    return re.sub(r"\D+", "", str(value or ""))[-10:]


def _messages(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    return [
        {"side": row["side"], "body": row["body"]}
        for row in conn.execute(
            "SELECT side, body FROM messages WHERE person_id = ? ORDER BY id",
            (person_id,),
        )
    ]


def _list_crm_contacts() -> list[dict]:
    """Best-effort read-only list. Empty if the API has no list route."""
    from src.crm import _request

    for path in (
        "/api/pipeline/contacts",
        "/api/pipeline/leads",
        "/api/pipeline/contacts?limit=500",
    ):
        result = _request("GET", path)
        if not result.get("ok"):
            continue
        for key in ("contacts", "leads", "items", "data"):
            rows = result.get(key)
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
        if isinstance(result.get("contact"), dict):
            return []
    return []


def main() -> int:
    os.environ.setdefault("PYTHONPATH", "/app")
    conn = connect(db_path_from_config())
    conn.row_factory = sqlite3.Row
    groups = []
    try:
        listed = list_groups()
        if listed.get("ok"):
            groups = listed.get("groups") or []
    except Exception:
        groups = []
    fallback = [
        {"id": 1, "name": "LGS High Wycombe", "slug": "lgs-high-wycombe", "town": "High Wycombe"},
        {"id": 2, "name": "LGS London", "slug": "lgs-london", "town": "Battersea"},
        {"id": 3, "name": "LGS Glos", "slug": "lgs-glos", "town": "Gloucester"},
    ]
    if not groups:
        groups = fallback

    people = list(
        conn.execute(
            """
            SELECT p.id, p.name, p.phone_id, p.location, p.age, p.ethnicity,
                   p.lgs_lead_id, p.crm_sync_status, p.crm_sync_error,
                   c.status, c.archived, c.last_text,
                   (SELECT COUNT(*) FROM messages m WHERE m.person_id = p.id) AS n
            FROM people p
            LEFT JOIN chats c ON c.person_id = p.id
            ORDER BY p.phone_id, p.name COLLATE NOCASE
            """
        )
    )

    crm_index: dict[str, dict] = {}
    crm_contacts = _list_crm_contacts()
    for card in crm_contacts:
        d = _digits(card.get("phone"))
        if d:
            crm_index[d] = card

    missed_create = []
    missed_update = []
    already = []
    not_ready = []
    no_thread = 0

    for row in people:
        if int(row["n"] or 0) == 0:
            no_thread += 1
            continue
        thread = _messages(conn, int(row["id"]))
        fields = extract_crm_fields(
            thread,
            inbox_name=row["name"],
            display_name=row["name"],
            profile_location=str(row["location"] or ""),
            profile_age=int(row["age"]) if row["age"] else None,
            ethnicity=str(row["ethnicity"] or ""),
            phone_id=str(row["phone_id"] or "toby"),
            groups=groups,
            today=date.today(),
        )
        ready = crm_ready_to_save(fields)
        lead_id = row["lgs_lead_id"]
        phone = fields.get("phone") or ""
        summary = {
            "name": row["name"],
            "phone_id": row["phone_id"],
            "archived": bool(row["archived"]),
            "status": row["status"],
            "sync": row["crm_sync_status"],
            "sync_error": (row["crm_sync_error"] or "")[:120],
            "lead_id": lead_id,
            "phone": phone,
            "hometown": fields.get("hometown") or "",
            "hub": fields.get("hub") or "",
            "instagram": fields.get("instagram") or "",
            "event": (fields.get("interested_event") or "")[:80],
            "ready": ready,
        }
        crm_card = None
        if lead_id:
            existing = get_lead(int(lead_id))
            crm_card = existing.get("contact") or existing.get("lead")
            if not existing.get("ok") or not isinstance(crm_card, dict):
                crm_card = None
        elif phone and _digits(phone) in crm_index:
            crm_card = crm_index[_digits(phone)]
            summary["lead_id"] = crm_card.get("id")
            summary["matched_via"] = "phone"

        if lead_id or crm_card:
            existing = crm_card or {}
            if ready and crm_should_update(existing, fields):
                diffs = []
                for key in ("phone", "hometown", "instagram", "interested_event", "email", "age"):
                    if key == "hometown":
                        old = str(existing.get("hometown") or existing.get("region") or "")
                    else:
                        old = str(existing.get(key) or "")
                    new = str(fields.get(key) or "")
                    if new and new.strip().casefold() != old.strip().casefold():
                        diffs.append(f"{key}: {old or '—'} → {new}")
                summary["diffs"] = diffs
                missed_update.append(summary)
            else:
                already.append(summary)
        elif ready:
            missed_create.append(summary)
        else:
            missing = []
            if not phone:
                missing.append("phone")
            if not (fields.get("hometown") or fields.get("hub")):
                missing.append("location")
            summary["missing"] = missing
            not_ready.append(summary)

    conn.close()

    print(f"people={len(people)} no_thread={no_thread} groups={len(groups)} crm_listed={len(crm_contacts)}")
    print(f"already_linked={len(already)} missed_create={len(missed_create)} missed_update={len(missed_update)} not_ready={len(not_ready)}")
    print()
    print("=== MISSED CREATE (name+phone+place, no CRM card) ===")
    if not missed_create:
        print("(none)")
    for row in missed_create:
        flag = " [archived]" if row["archived"] else ""
        print(
            f"- {row['name']} ({row['phone_id']}){flag}  {row['phone']}  "
            f"{row['hometown'] or row['hub']}  ig={row['instagram'] or '—'}  "
            f"sync={row['sync'] or '—'}  {row['sync_error']}"
        )
        if row.get("event"):
            print(f"    event: {row['event']}")
    print()
    print("=== MISSED UPDATE (linked card missing a new fact) ===")
    if not missed_update:
        print("(none)")
    for row in missed_update:
        print(
            f"- {row['name']} ({row['phone_id']}) lead={row['lead_id']}  "
            + "; ".join(row.get("diffs") or ["facts differ"])
        )
    print()
    print("=== ALMOST (have some facts, not ready to add) ===")
    almost = [r for r in not_ready if r["phone"] or r["hometown"] or r["instagram"]]
    almost.sort(key=lambda r: (0 if r["phone"] else 1, r["name"].casefold()))
    if not almost:
        print("(none with a phone, place, or insta)")
    for row in almost:
        print(
            f"- {row['name']} ({row['phone_id']}) missing={','.join(row['missing'])}  "
            f"phone={row['phone'] or '—'}  place={row['hometown'] or '—'}  "
            f"ig={row['instagram'] or '—'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
