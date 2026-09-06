"""Index every Chats filter + New-friends strip, then capture anyone still missing."""
from __future__ import annotations

import logging

from src.config import load_config
from src.device import bring_app_foreground, connect, wait_idle
from src.store import connect as db_connect, db_path_from_config, names_with_messages, upsert_chat
from src.sync_chats import (
    _INBOX_FILTERS,
    _set_inbox_filter,
    capture_new_friend_chats,
    collect_new_friend_names,
    fill_via_search,
    recover_to_list,
    scan_chat_list,
)
from src.unlock import sleep_screen, wake_and_unlock

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

cfg = load_config()
package = str(cfg["package"])
device = connect()
wake_and_unlock(device)
bring_app_foreground(device, package)
wait_idle(device, 1.0)
recover_to_list(device, package)

conn = db_connect(db_path_from_config(cfg))
try:
    union: dict[str, dict] = {}
    for filt in _INBOX_FILTERS:
        _set_inbox_filter(device, filt)
        rows = scan_chat_list(device, package)
        print(f"FILTER {filt}: {len(rows)} {', '.join(r['name'] for r in rows)}")
        for row in rows:
            name = str(row["name"])
            union[name] = row
            badge = str(row.get("badge") or "")
            upsert_chat(
                conn,
                name,
                preview=str(row.get("preview") or ""),
                badge=badge,
                last_text=str(row.get("preview") or "") or None,
                last_from="them" if badge.strip().lower() == "your turn" else None,
            )
        conn.commit()
        print(f"  union so far {len(union)}")

    strip = collect_new_friend_names(device, package)
    print(f"STRIP: {len(strip)} {', '.join(strip)}")
    for name in strip:
        if name not in union:
            upsert_chat(conn, name, preview="", last_text=None)
            union[name] = {"name": name, "preview": "", "badge": ""}
    conn.commit()

    already = {str(r[0]) for r in conn.execute("SELECT name FROM people")}
    print(f"DB after index: people={len(already)}")

    n_new = capture_new_friend_chats(device, conn, package)
    _set_inbox_filter(device, "Recent")
    n_search = fill_via_search(device, conn, package)

    people = [r[0] for r in conn.execute("SELECT name FROM people ORDER BY name COLLATE NOCASE")]
    have = names_with_messages(conn)
    print(f"CAPTURED new-friends={n_new} search={n_search}")
    print(f"FINAL people={len(people)} transcripts={len(have)}")
    print("NO transcript:")
    for name in people:
        if name in have:
            continue
        last = conn.execute(
            "SELECT last_text FROM chats WHERE person_id=(SELECT id FROM people WHERE name=?)",
            (name,),
        ).fetchone()
        print(f"  {name}\t{(last[0] if last else '')!r}")
    print("ALL NAMES:", ", ".join(people))
finally:
    conn.close()
    try:
        sleep_screen(device)
    except Exception:
        pass
