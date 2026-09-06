"""Probe Recent filter sheet + New-friends RecyclerView fling."""
from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from src.chats import list_new_friends
from src.config import load_config
from src.device import bring_app_foreground, connect, dump_hierarchy, wait_idle
from src.gestures import _adb_swipe, tap
from src.sync_chats import _list_rows, recover_to_list
from src.unlock import wake_and_unlock

RID_STRIP = "com.bumblebff.app:id/connections_connectionsListExpiring"


def dump_clickables(xml: str, tag: str) -> None:
    print(f"=== {tag} ===")
    for node in ET.fromstring(xml).iter():
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        rid = node.attrib.get("resource-id") or ""
        click = (node.attrib.get("clickable") or "").lower() == "true"
        if not (text or desc or "filter" in rid.lower() or "nudge" in rid.lower()):
            continue
        if not (text or desc or click):
            continue
        print(
            f"  click={click} rid={rid[-48:]} text={text!r} desc={desc[:70]!r} bounds={node.attrib.get('bounds')}"
        )


cfg = load_config()
package = str(cfg["package"])
d = connect()
wake_and_unlock(d)
bring_app_foreground(d, package)
wait_idle(d, 0.8)
xml = recover_to_list(d, package)
w = int(d.info["displayWidth"])
h = int(d.info["displayHeight"])

print("NUDGE nodes:")
for node in ET.fromstring(xml).iter():
    rid = node.attrib.get("resource-id") or ""
    if "nudge" in rid.lower() or "Nudge" in rid:
        print(
            " ",
            rid,
            repr(node.attrib.get("text") or ""),
            repr(node.attrib.get("content-desc") or ""),
            node.attrib.get("bounds"),
        )

print("tap filter icon")
tap(d, (963 + 1069) // 2, (898 + 1004) // 2)
wait_idle(d, 1.6)
xml2 = dump_hierarchy(d)
Path("/tmp/filter_sheet.xml").write_text(xml2)
dump_clickables(xml2, "after filter tap")

# If nothing changed, try tapping the Recent text too
if "Recent" in xml2 and "All" not in (ET.fromstring(xml2).find(".//*") is not None and xml2):
    print("tap Recent text")
    tap(d, (846 + 963) // 2, (926 + 976) // 2)
    wait_idle(d, 1.6)
    xml3 = dump_hierarchy(d)
    Path("/tmp/filter_sheet2.xml").write_text(xml3)
    dump_clickables(xml3, "after Recent text tap")

# Back to list, try RecyclerView fling both ways
d.press("back")
wait_idle(d, 0.8)
xml = recover_to_list(d, package)
print("strip before fling", [(f.name, f.x) for f in list_new_friends(xml)])

rv = d(resourceId=RID_STRIP)
print("strip exists", rv.exists)
if rv.exists:
    print("fling left (forward)")
    try:
        rv.fling.horiz.toEnd()
    except Exception as exc:
        print("toEnd failed", exc)
        try:
            rv.swipe("left", steps=40)
        except Exception as exc2:
            print("swipe left failed", exc2)
    wait_idle(d, 0.8)
    xml = dump_hierarchy(d)
    print("strip after toEnd", [(f.name, f.x) for f in list_new_friends(xml)])

    print("fling right (backward / toBeginning)")
    try:
        rv.fling.horiz.toBeginning()
    except Exception as exc:
        print("toBeginning failed", exc)
        try:
            rv.swipe("right", steps=40)
        except Exception as exc2:
            print("swipe right failed", exc2)
    wait_idle(d, 0.8)
    xml = dump_hierarchy(d)
    print("strip after toBeginning", [(f.name, f.x) for f in list_new_friends(xml)])

# Manual swipe in the gap between rings (y just below rings, still in RV)
print("adb swipe LEFT in gap y=875")
_adb_swipe(d, 900, 875, 180, 875, 500)
wait_idle(d, 0.7)
xml = dump_hierarchy(d)
print("after gap-left", [(f.name, f.x) for f in list_new_friends(xml)])

print("adb swipe RIGHT in gap y=875")
_adb_swipe(d, 180, 875, 900, 875, 500)
wait_idle(d, 0.7)
xml = dump_hierarchy(d)
print("after gap-right", [(f.name, f.x) for f in list_new_friends(xml)])

print("adb swipe RIGHT on title y=606")
_adb_swipe(d, 180, 606, 900, 606, 500)
wait_idle(d, 0.7)
xml = dump_hierarchy(d)
print("after title-right", [(f.name, f.x) for f in list_new_friends(xml)])

print("VISIBLE LIST", [r["name"] for r in _list_rows(xml, height=h, width=w)])
print("DONE")
