"""Open namesake chats (David 2, S 2, …) then recapture chrome-tainted threads."""
from __future__ import annotations

import logging

from src.config import load_config
from src.device import bring_app_foreground, connect, wait_idle
from src.store import connect as db_connect, db_path_from_config, names_with_messages
from src.sync_chats import (
    _CHROME,
    _set_inbox_filter,
    capture_all_chats,
    recapture_person,
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
    n = capture_all_chats(device, conn, package, recapture=False)
    print("namesake/missing captured", n)

    chrome_names = []
    for row in conn.execute(
        """
        SELECT p.name, c.last_text FROM people p
        JOIN chats c ON c.person_id = p.id
        """
    ):
        text = (row[1] or "").strip()
        if text and _CHROME.search(text):
            chrome_names.append(str(row[0]))
    print("chrome last_text", chrome_names)
    for name in chrome_names:
        ok = recapture_person(device, conn, package, name)
        print("recapture", name, ok)

    people = [r[0] for r in conn.execute("SELECT name FROM people ORDER BY name COLLATE NOCASE")]
    have = names_with_messages(conn)
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
    print("ALL:", ", ".join(people))
finally:
    conn.close()
    try:
        sleep_screen(device)
    except Exception:
        pass
