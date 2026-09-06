"""Dump Chats search UI after typing a common word."""
from pathlib import Path
from xml.etree import ElementTree as ET

from src.config import load_config
from src.device import bring_app_foreground, connect, dump_hierarchy, wait_idle
from src.sync_chats import (
    _ensure_search,
    _list_rows,
    _search_field,
    recover_to_list,
)
from src.unlock import wake_and_unlock

cfg = load_config()
package = str(cfg["package"])
d = connect()
wake_and_unlock(d)
bring_app_foreground(d, package)
wait_idle(d, 0.8)
recover_to_list(d, package)
_ensure_search(d, package)
field = _search_field(d)
print("field", field)
if field is not None:
    field.click()
    wait_idle(d, 0.3)
    field.set_text("hi")
    wait_idle(d, 1.8)
xml = dump_hierarchy(d)
Path("/tmp/search_hi.xml").write_text(xml)
w = int(d.info["displayWidth"])
h = int(d.info["displayHeight"])
rows = _list_rows(xml, min_top=0, height=h, width=w)
print("parsed rows", [(r["name"], r.get("preview", "")[:40]) for r in rows])
print("emptyText" in xml, "searchActivity" in xml, "personName" in xml)
print("--- interesting nodes ---")
for node in ET.fromstring(xml).iter():
    rid = node.attrib.get("resource-id") or ""
    text = (node.attrib.get("text") or "").strip()
    desc = (node.attrib.get("content-desc") or "").strip()
    cls = (node.attrib.get("class") or "").split(".")[-1]
    if not (text or desc or "search" in rid.lower() or "recycler" in cls.lower() or "person" in rid.lower() or "connection" in rid.lower()):
        continue
    if cls in {"RecyclerView", "EditText"} or text or "person" in rid.lower() or "connection" in rid.lower() or "empty" in rid.lower() or "result" in rid.lower():
        print(f"  {cls:18} {rid[-48:]:48} {text[:50]!r:52} {desc[:40]!r} {node.attrib.get('bounds')}")
print("DONE")
