"""Slow-scroll Recent list; print every S/David row with preview; open unmatched S."""
from pathlib import Path
from xml.etree import ElementTree as ET

from src.config import load_config
from src.device import bring_app_foreground, connect, dump_hierarchy, wait_idle
from src.gestures import tap
from src.chats import chat_partner_name, list_new_friends
from src.store import (
    connect as db_connect,
    db_path_from_config,
    next_duplicate_name,
    replace_thread,
    upsert_chat,
)
from src.sync_chats import (
    _go_top_of_inbox,
    _list_rows,
    _on_list,
    _save_name_for_thread,
    _scroll_inbox,
    _scroll_new_friends_strip,
    _set_inbox_filter,
    _wait_thread,
    capture_thread,
    recover_to_list,
)
from src.unlock import sleep_screen, wake_and_unlock
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

cfg = load_config()
package = str(cfg["package"])
d = connect()
wake_and_unlock(d)
bring_app_foreground(d, package)
wait_idle(d, 0.8)
recover_to_list(d, package)
_set_inbox_filter(d, "Recent")
w = int(d.info["displayWidth"])
h = int(d.info["displayHeight"])
_go_top_of_inbox(d, package, w, h)

print("=== S and David rows ===")
hits = []
last = None
stagnant = 0
for i in range(90):
    xml = dump_hierarchy(d)
    rows = _list_rows(xml, min_top=0, height=h, width=w)
    key = tuple((r["name"], r.get("preview") or "") for r in rows)
    for r in rows:
        if r["name"] in {"S", "David"}:
            rec = (r["name"], (r.get("preview") or "")[:80], r["y"])
            print(f"screen {i}: {rec}")
            hits.append(rec)
    if key == last:
        stagnant += 1
    else:
        stagnant = 0
        last = key
    if stagnant >= 8:
        break
    _scroll_inbox(d, w, h, older=True, distance=int(h * 0.10), duration_ms=240)

print("unique S/David", sorted({(n, p) for n, p, _ in hits}))

# Recapture Billy from New-friends strip
print("=== Billy from strip ===")
xml = _go_top_of_inbox(d, package, w, h)
from src.sync_chats import _STRIP_RID
rv = d(resourceId=_STRIP_RID)
if rv.exists:
    try:
        rv.fling.horiz.toBeginning()
        wait_idle(d, 0.5)
    except Exception:
        pass
conn = db_connect(db_path_from_config(cfg))
captured_billy = False
for _ in range(12):
    xml = dump_hierarchy(d)
    friends = list_new_friends(xml)
    print("strip", [(f.name, f.x) for f in friends])
    billy = next((f for f in friends if f.name == "Billy" and 80 <= f.x <= w - 80), None)
    if billy:
        tap(d, billy.x, billy.y)
        wait_idle(d, 1.8)
        xml = _wait_thread(d)
        partner = chat_partner_name(xml) or "Billy"
        thread = capture_thread(d, w, h, expected=partner)
        print("Billy thread", thread)
        if thread:
            pid = upsert_chat(conn, "Billy", last_from=thread[-1][0], last_text=thread[-1][1], preview=thread[-1][1])
            replace_thread(conn, pid, thread)
            conn.commit()
            captured_billy = True
        recover_to_list(d, package)
        break
    _scroll_new_friends_strip(d, xml, w, h, toward_end=True)
print("billy recaptured", captured_billy)

# Open S whose preview is not Pakistan
print("=== unmatched S ===")
xml = _go_top_of_inbox(d, package, w, h)
opened = False
last = None
stagnant = 0
for i in range(90):
    xml = dump_hierarchy(d)
    rows = _list_rows(xml, min_top=0, height=h, width=w)
    key = tuple(r["name"] for r in rows)
    for r in rows:
        preview = (r.get("preview") or "").strip()
        if r["name"] != "S":
            continue
        if "pakistan" in preview.lower():
            print("skip stored S", preview[:60])
            continue
        if not preview:
            continue
        print("OPEN S namesake", preview[:60], "y", r["y"])
        tap(d, int(r["x"]), int(r["y"]))
        wait_idle(d, 1.8)
        xml = _wait_thread(d)
        partner = chat_partner_name(xml)
        print("toolbar", partner)
        thread = capture_thread(d, w, h, expected=partner or "S") if partner else []
        print("thread", [(s, t[:50]) for s, t in thread[:8]], "n", len(thread))
        if thread:
            save_as = _save_name_for_thread(conn, partner or "S", thread)
            print("save_as", save_as)
            pid = upsert_chat(
                conn,
                save_as,
                preview=preview,
                last_from=thread[-1][0],
                last_text=thread[-1][1],
            )
            replace_thread(conn, pid, thread)
            conn.commit()
            opened = True
        recover_to_list(d, package)
        break
    if opened:
        break
    if key == last:
        stagnant += 1
    else:
        stagnant = 0
        last = key
    if stagnant >= 8:
        break
    _scroll_inbox(d, w, h, older=True, distance=int(h * 0.10), duration_ms=240)

print("opened namesake S", opened)
people = [r[0] for r in conn.execute("SELECT name FROM people ORDER BY name COLLATE NOCASE")]
print("FINAL people", len(people))
print(", ".join(people))
conn.close()
try:
    sleep_screen(d)
except Exception:
    pass
