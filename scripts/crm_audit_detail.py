#!/usr/bin/env python3
import re
import sqlite3

from src.crm import get_lead
from src.crm_extract import extract_hometown
from src.store import connect, db_path_from_config

conn = connect(db_path_from_config())
conn.row_factory = sqlite3.Row

for name in ["Hannah", "Kalyani", "Hari", "Lauren", "Nollan", "Tino", "Alex"]:
    rows = list(conn.execute("SELECT id, name, phone_id, location FROM people WHERE name=?", (name,)))
    for row in rows:
        msgs = list(
            conn.execute("SELECT side, body FROM messages WHERE person_id=? ORDER BY id", (row["id"],))
        )
        them = "\n".join(m["body"] for m in msgs if m["side"] == "them")
        print("=" * 60)
        print(row["name"], row["phone_id"], "hometown=", extract_hometown(them, row["location"] or ""))
        for m in msgs:
            b = " ".join(m["body"].split())
            if m["side"] == "them" and (
                re.search(r"\d{8,}", b)
                or re.search(r"london|wycombe|hendon|wembley|where|from |based|live|leighton", b, re.I)
            ):
                print(f"  {m['side']}: {b[:220]}")

print("==== existing CRM cards (GET only) ====")
for lid, name in (
    (437, "Alexandru"),
    (433, "Krishna"),
    (432, "Max"),
    (434, "Parminder"),
    (431, "Pete"),
    (435, "Toby"),
    (438, "Harry"),
    (440, "Jonathan 2"),
    (439, "Ravi"),
    (436, "Will"),
):
    result = get_lead(lid)
    card = result.get("contact") or result.get("lead") or {}
    if not isinstance(card, dict):
        print(name, lid, result)
        continue
    print(
        name,
        lid,
        {k: card.get(k) for k in ("name", "phone", "hometown", "region", "instagram", "interested_event")},
    )
