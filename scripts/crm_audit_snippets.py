#!/usr/bin/env python3
"""Print their-side snippets for named inbox people. Read-only."""

from __future__ import annotations

import sqlite3
import sys

from src.store import connect, db_path_from_config

NAMES = [a for a in sys.argv[1:] if a]


def main() -> None:
    conn = connect(db_path_from_config())
    conn.row_factory = sqlite3.Row
    names = NAMES or []
    q = """
        SELECT p.id, p.name, p.phone_id, p.location, p.lgs_lead_id, c.in_group, c.archived
        FROM people p
        LEFT JOIN chats c ON c.person_id = p.id
        WHERE p.name = ?
        """
    for name in names:
        rows = list(conn.execute(q, (name,)))
        if not rows:
            print(f"\n## {name} (not found)")
            continue
        for row in rows:
            print(
                f"\n## {row['name']} / {row['phone_id']}  loc={row['location'] or '—'}  "
                f"lead={row['lgs_lead_id'] or '—'}  group={row['in_group']}  arch={row['archived']}"
            )
            msgs = list(
                conn.execute(
                    "SELECT side, body FROM messages WHERE person_id=? ORDER BY id",
                    (row["id"],),
                )
            )
            them = [m for m in msgs if m["side"] == "them"]
            show = them[-8:] if len(them) > 8 else them
            if len(them) > 8:
                print(f"  ({len(them)} them msgs, last 8)")
            for m in show:
                body = " ".join(str(m["body"]).split())
                print(f"  them: {body[:220]}")
    conn.close()


if __name__ == "__main__":
    main()
