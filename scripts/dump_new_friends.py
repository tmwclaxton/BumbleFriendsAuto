"""Wake phone, dump inbox + New-friends strip, swipe the strip, print coverage."""
from __future__ import annotations

import logging
import re
from xml.etree import ElementTree as ET

from src.chats import list_new_friends
from src.config import load_config
from src.device import bring_app_foreground, connect, dump_hierarchy, wait_idle
from src.gestures import _adb_swipe
from src.store import connect as db_connect, db_path_from_config, names_with_messages
from src.sync_chats import _go_top_of_inbox, _list_rows, _on_list, recover_to_list
from src.unlock import wake_and_unlock

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

cfg = load_config()
package = str(cfg["package"])
conn = db_connect(db_path_from_config(cfg))
people = [r[0] for r in conn.execute("SELECT name FROM people ORDER BY name COLLATE NOCASE")]
have = names_with_messages(conn)
print(f"DB people={len(people)} transcripts={len(have)}")
missing = [n for n in people if n not in have]
print("no-message people:", ", ".join(missing) or "(none)")
print("ALL PEOPLE:")
for name in people:
    n = conn.execute("SELECT count(*) FROM messages WHERE person_id=(SELECT id FROM people WHERE name=?)", (name,)).fetchone()[0]
    last = conn.execute(
        "SELECT last_text FROM chats WHERE person_id=(SELECT id FROM people WHERE name=?)",
        (name,),
    ).fetchone()
    print(f"  {name}\tmsgs={n}\t{(last[0] if last else '')[:70]!r}")

device = connect()
wake_and_unlock(device)
bring_app_foreground(device, package)
wait_idle(device, 1.0)
width = int(device.info["displayWidth"])
height = int(device.info["displayHeight"])
xml = _go_top_of_inbox(device, package, width, height)
print("on_list", _on_list(xml), "size", width, height)

# Dump filter chips / headers
for node in ET.fromstring(xml).iter():
    rid = node.attrib.get("resource-id") or ""
    text = (node.attrib.get("text") or "").strip()
    desc = (node.attrib.get("content-desc") or "").strip()
    if any(k in (rid + text + desc).lower() for k in ("filter", "recent", "unread", "new friend", "all", "your turn")):
        print(f"CHIP rid={rid[-40:]} text={text!r} desc={desc!r} click={node.attrib.get('clickable')} sel={node.attrib.get('selected')} bounds={node.attrib.get('bounds')}")

rows = _list_rows(xml, height=height, width=width)
print("VISIBLE LIST", [(r["name"], r["y"], r.get("preview", "")[:40]) for r in rows])

seen_strip: list[str] = []
print("--- NEW FRIENDS SWIPES ---")
y_strip = int(height * 0.32)
for i in range(20):
    xml = dump_hierarchy(device)
    friends = list_new_friends(xml)
    names = [f"{f.name}@{f.x}" for f in friends]
    print(f"swipe {i}: {names}")
    for f in friends:
        if f.name not in seen_strip:
            seen_strip.append(f.name)
    if i == 0:
        # save first dump
        Path = __import__("pathlib").Path
        Path("/tmp/inbox_now.xml").write_text(xml)
    _adb_swipe(device, int(width * 0.82), y_strip, int(width * 0.18), y_strip, 480)
    wait_idle(device, 0.6)

print(f"STRIP UNIQUE {len(seen_strip)}:", ", ".join(seen_strip))
not_in_db = [n for n in seen_strip if n not in people]
print("strip not in DB:", ", ".join(not_in_db) or "(none)")
print("DONE")
