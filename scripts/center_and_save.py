"""Scroll target rows into the tap band, then save without overscrolling out of the thread."""
from __future__ import annotations

import logging
import re

from src.chats import chat_partner_name, is_empty_outbound_chat, list_new_friends
from src.config import load_config
from src.device import bring_app_foreground, connect, dump_hierarchy, wait_idle
from src.gestures import tap
from src.store import connect as db_connect, db_path_from_config, names_with_messages, replace_thread, upsert_chat
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
    extract_messages,
    recover_to_list,
)
from src.messenger import leave_chat
from src.unlock import sleep_screen, wake_and_unlock

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


def find_and_center(device, package, width, height, name: str, needle: str):
    needle_cf = needle.casefold()
    _go_top_of_inbox(device, package, width, height)
    last = None
    stagnant = 0
    for _ in range(100):
        xml = dump_hierarchy(device)
        rows = _list_rows(xml, min_top=0, height=height, width=width)
        key = tuple((r["name"], r.get("preview") or "") for r in rows)
        match = None
        for row in rows:
            if row["name"] != name:
                continue
            if needle_cf not in str(row.get("preview") or "").casefold():
                continue
            match = row
            break
        if match:
            y = int(match["y"])
            logging.info("found %s y=%s preview=%r", name, y, str(match.get("preview") or "")[:50])
            if 1200 <= y <= 1750:
                return match
            # Nudge into the middle band.
            if y > 1750:
                _scroll_inbox(device, width, height, older=True, distance=int(height * 0.12), duration_ms=220)
            else:
                _scroll_inbox(device, width, height, older=False, distance=int(height * 0.12), duration_ms=220)
            stagnant = 0
            last = None
            continue
        if key == last:
            stagnant += 1
        else:
            stagnant = 0
            last = key
        if stagnant >= 10:
            return None
        _scroll_inbox(device, width, height, older=True, distance=int(height * 0.10), duration_ms=240)
    return None


def open_and_save(device, conn, width, height, row, save_name: str | None = None) -> str | None:
    tap(device, int(row["x"]), int(row["y"]))
    wait_idle(device, 2.0)
    xml = _wait_thread(device)
    partner = chat_partner_name(xml)
    logging.info("opened toolbar=%s wanted=%s", partner, row["name"])
    if not partner:
        recover_to_list(device, "com.bumblebff.app")
        return None
    chunk = extract_messages(xml, width, height)
    thread = [(m["side"], m["text"]) for m in chunk]
    if len(thread) < 2:
        more = capture_thread(device, width, height, expected=partner)
        if more:
            thread = more
    logging.info("thread n=%d last=%s", len(thread), thread[-1] if thread else None)
    if not thread:
        try:
            leave_chat(device)
        except Exception:
            recover_to_list(device, "com.bumblebff.app")
        return None
    name = save_name or _save_name_for_thread(conn, partner, thread)
    pid = upsert_chat(conn, name, preview=str(row.get("preview") or ""), last_from=thread[-1][0], last_text=thread[-1][1])
    replace_thread(conn, pid, thread)
    conn.commit()
    logging.info("saved as %s", name)
    try:
        leave_chat(device)
    except Exception:
        recover_to_list(device, "com.bumblebff.app")
    return name


cfg = load_config()
package = str(cfg["package"])
device = connect()
wake_and_unlock(device)
device.app_stop(package)
wait_idle(device, 0.6)
device.app_start(package)
wait_idle(device, 2.5)
bring_app_foreground(device, package)
wait_idle(device, 1.0)
recover_to_list(device, package)
_set_inbox_filter(device, "Recent")
w = int(device.info["displayWidth"])
h = int(device.info["displayHeight"])
conn = db_connect(db_path_from_config(cfg))

try:
    row = find_and_center(device, package, w, h, "S", "Hey Toby")
    print("Hey Toby row", row)
    if row:
        print("saved", open_and_save(device, conn, w, h, row))

    row = find_and_center(device, package, w, h, "David", "Sweet what you up to")
    print("August David row", row)
    if row:
        print("saved", open_and_save(device, conn, w, h, row))

    xml = _go_top_of_inbox(device, package, w, h)
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
        billy = next((f for f in friends if f.name == "Billy" and 80 <= int(f.x) <= w - 80), None)
        if not billy:
            _scroll_new_friends_strip(device, xml, w, h, toward_end=True)
            continue
        tap(device, billy.x, billy.y)
        wait_idle(device, 2.0)
        xml = _wait_thread(device)
        print("Billy partner", chat_partner_name(xml), "empty", is_empty_outbound_chat(xml))
        print("Billy texts", _texts(xml)[:400])
        hours = re.search(r"\d+\s*hours?\s+left to message", _texts(xml), re.I)
        chunk = extract_messages(xml, w, h)
        print("Billy extract", chunk)
        if is_empty_outbound_chat(xml) or hours or not chunk:
            last_text = hours.group(0) if hours else "(no messages yet)"
            pid = upsert_chat(conn, "Billy", last_text=last_text, preview=last_text)
            replace_thread(conn, pid, [])
            conn.commit()
            print("Billy cleared to", last_text)
        recover_to_list(device, package)
        break

    people = [r[0] for r in conn.execute("SELECT name FROM people ORDER BY name COLLATE NOCASE")]
    have = names_with_messages(conn)
    print(f"FINAL people={len(people)} transcripts={len(have)}")
    for name in people:
        if name in have:
            continue
        last = conn.execute(
            "SELECT last_text FROM chats WHERE person_id=(SELECT id FROM people WHERE name=?)",
            (name,),
        ).fetchone()
        print("NO", name, repr(last[0] if last else None))
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
    print("ALL", ", ".join(people))
finally:
    conn.close()
    try:
        sleep_screen(device)
    except Exception:
        pass
