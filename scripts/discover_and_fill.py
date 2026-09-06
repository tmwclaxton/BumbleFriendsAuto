"""Open remaining New-friends (Cesar) and discover chats via message search."""
from __future__ import annotations

import logging

from src.config import load_config
from src.device import bring_app_foreground, connect, wait_idle
from src.store import connect as db_connect, db_path_from_config, names_with_messages, upsert_chat
from src.sync_chats import (
    _set_inbox_filter,
    capture_new_friend_chats,
    discover_via_message_search,
    fill_via_search,
    recover_to_list,
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
_set_inbox_filter(device, "Recent")

conn = db_connect(db_path_from_config(cfg))
try:
    before = {str(r[0]) for r in conn.execute("SELECT name FROM people")}
    print("before people", len(before))

    n_new = capture_new_friend_chats(device, conn, package)
    print("new-friend captured", n_new)

    rows = discover_via_message_search(device, package)
    new_from_search = []
    for row in rows:
        name = str(row["name"])
        if name not in before:
            new_from_search.append(name)
        upsert_chat(
            conn,
            name,
            preview=str(row.get("preview") or ""),
            badge=str(row.get("badge") or ""),
            last_text=str(row.get("preview") or "") or None,
        )
    conn.commit()
    print(f"message-search {len(rows)} names, new={new_from_search}")

    n_search = fill_via_search(device, conn, package)
    people = [r[0] for r in conn.execute("SELECT name FROM people ORDER BY name COLLATE NOCASE")]
    have = names_with_messages(conn)
    print(f"search-fill {n_search}")
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
    added = [n for n in people if n not in before]
    print("ADDED this run:", ", ".join(added) or "(none)")
    print("ALL:", ", ".join(people))
finally:
    conn.close()
    try:
        sleep_screen(device)
    except Exception:
        pass
