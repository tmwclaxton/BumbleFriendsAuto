"""Restore overwritten S (Pakistan), save namesake S 2 and David 2, fix Billy."""
from __future__ import annotations

import logging
import re

from src.chats import chat_partner_name, is_empty_outbound_chat, list_new_friends
from src.config import load_config
from src.device import bring_app_foreground, connect, dump_hierarchy, wait_idle
from src.gestures import tap
from src.store import (
    connect as db_connect,
    db_path_from_config,
    names_with_messages,
    next_duplicate_name,
    replace_thread,
    upsert_chat,
)
from src.sync_chats import (
    _STRIP_RID,
    _go_top_of_inbox,
    _list_rows,
    _save_name_for_thread,
    _scroll_inbox,
    _scroll_new_friends_strip,
    _set_inbox_filter,
    _texts,
    _wait_thread,
    capture_thread,
    recover_to_list,
)
from src.unlock import sleep_screen, wake_and_unlock

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


def open_row_by_preview(device, package, width, height, name: str, preview_substr: str) -> tuple[str, list]:
    xml = _go_top_of_inbox(device, package, width, height)
    last = None
    stagnant = 0
    needle = preview_substr.casefold()
    for _ in range(90):
        xml = dump_hierarchy(device)
        rows = _list_rows(xml, min_top=0, height=height, width=width)
        key = tuple(r["name"] for r in rows)
        for row in rows:
            preview = str(row.get("preview") or "")
            if row["name"] != name:
                continue
            if needle not in preview.casefold():
                continue
            if not preview.strip():
                continue
            logging.info("tap %s preview=%r y=%s", name, preview[:60], row["y"])
            tap(device, int(row["x"]), int(row["y"]))
            wait_idle(device, 1.8)
            xml = _wait_thread(device)
            partner = chat_partner_name(xml) or name
            thread = capture_thread(device, width, height, expected=partner)
            recover_to_list(device, package)
            return partner, thread
        if key == last:
            stagnant += 1
        else:
            stagnant = 0
            last = key
        if stagnant >= 8:
            break
        _scroll_inbox(device, width, height, older=True, distance=int(height * 0.10), duration_ms=240)
    return "", []


cfg = load_config()
package = str(cfg["package"])
device = connect()
wake_and_unlock(device)
bring_app_foreground(device, package)
wait_idle(device, 1.0)
recover_to_list(device, package)
_set_inbox_filter(device, "Recent")
width = int(device.info["displayWidth"])
height = int(device.info["displayHeight"])
conn = db_connect(db_path_from_config(cfg))

try:
    # 1) Restore Pakistan S onto S (overwrite the short Hey Toby thread we just saved).
    partner, thread = open_row_by_preview(device, package, width, height, "S", "Pakistan")
    print("Pakistan S", partner, len(thread), thread[-1] if thread else None)
    if thread:
        pid = upsert_chat(conn, "S", last_from=thread[-1][0], last_text=thread[-1][1], preview=thread[-1][1])
        replace_thread(conn, pid, thread)
        conn.commit()
        print("restored S")

    # 2) Hey Toby S -> S 2
    partner, thread = open_row_by_preview(device, package, width, height, "S", "Hey Toby")
    print("Hey Toby S", partner, len(thread), thread[-1] if thread else None)
    if thread:
        save_as = _save_name_for_thread(conn, partner or "S", thread)
        print("save Hey Toby as", save_as)
        pid = upsert_chat(conn, save_as, last_from=thread[-1][0], last_text=thread[-1][1], preview=thread[-1][1])
        replace_thread(conn, pid, thread)
        conn.commit()

    # 3) Other David (August 29 last line, not the phone-number thread)
    partner, thread = open_row_by_preview(device, package, width, height, "David", "Sweet what you up to")
    print("August David", partner, len(thread), thread[-1] if thread else None)
    if thread:
        save_as = _save_name_for_thread(conn, partner or "David", thread)
        print("save August David as", save_as)
        pid = upsert_chat(conn, save_as, last_from=thread[-1][0], last_text=thread[-1][1], preview=thread[-1][1])
        replace_thread(conn, pid, thread)
        conn.commit()

    # 4) Billy from New-friends strip — empty icebreaker, not chrome bubbles
    xml = _go_top_of_inbox(device, package, width, height)
    rv = device(resourceId=_STRIP_RID)
    if rv.exists:
        try:
            rv.fling.horiz.toBeginning()
            wait_idle(device, 0.5)
        except Exception:
            pass
    for _ in range(12):
        xml = dump_hierarchy(device)
        friends = list_new_friends(xml)
        billy = next((f for f in friends if f.name == "Billy" and 80 <= int(f.x) <= width - 80), None)
        if not billy:
            _scroll_new_friends_strip(device, xml, width, height, toward_end=True)
            continue
        tap(device, billy.x, billy.y)
        wait_idle(device, 1.8)
        xml = _wait_thread(device)
        blob = _texts(xml)
        hours = re.search(r"\d+\s*hours?\s+left to message", blob, re.I)
        if is_empty_outbound_chat(xml) or hours:
            last_text = hours.group(0) if hours else "(no messages yet)"
            pid = upsert_chat(conn, "Billy", last_text=last_text, preview=last_text)
            replace_thread(conn, pid, [])
            conn.commit()
            print("Billy empty", last_text)
        else:
            thread = capture_thread(device, width, height, expected="Billy")
            print("Billy thread", thread)
            if thread:
                pid = upsert_chat(conn, "Billy", last_from=thread[-1][0], last_text=thread[-1][1])
                replace_thread(conn, pid, thread)
                conn.commit()
        recover_to_list(device, package)
        break

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
    for name in ("S", "S 2", "David", "David 2", "Billy"):
        n = conn.execute(
            "SELECT count(*) FROM messages WHERE person_id=(SELECT id FROM people WHERE name=?)",
            (name,),
        ).fetchone()[0]
        last = conn.execute(
            "SELECT last_text FROM chats WHERE person_id=(SELECT id FROM people WHERE name=?)",
            (name,),
        ).fetchone()
        print(f"check {name}: msgs={n} last={(last[0] if last else None)!r}")
finally:
    conn.close()
    try:
        sleep_screen(device)
    except Exception:
        pass
