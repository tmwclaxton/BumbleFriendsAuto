"""Scroll Recent inbox to the end; print every row including duplicate names."""
from collections import Counter

from src.config import load_config
from src.device import bring_app_foreground, connect, dump_hierarchy, wait_idle
from src.sync_chats import (
    _go_top_of_inbox,
    _list_rows,
    _on_list,
    _scroll_inbox,
    _set_inbox_filter,
    recover_to_list,
)
from src.unlock import wake_and_unlock

cfg = load_config()
package = str(cfg["package"])
d = connect()
wake_and_unlock(d)
bring_app_foreground(d, package)
wait_idle(d, 0.8)
xml = recover_to_list(d, package)
_set_inbox_filter(d, "Recent")
w = int(d.info["displayWidth"])
h = int(d.info["displayHeight"])
_go_top_of_inbox(d, package, w, h)

appearances = []
last_key = None
stagnant = 0
for i in range(80):
    xml = dump_hierarchy(d)
    if not _on_list(xml):
        xml = recover_to_list(d, package)
    rows = _list_rows(xml, min_top=0, height=h, width=w)
    key = tuple((r["name"], r.get("preview") or "") for r in rows)
    print(f"screen {i}: {[(r['name'], r['y']) for r in rows]}")
    for r in rows:
        appearances.append((r["name"], (r.get("preview") or "")[:50]))
    if key == last_key:
        stagnant += 1
    else:
        stagnant = 0
        last_key = key
    if stagnant >= 8:
        print("END OF LIST")
        break
    _scroll_inbox(d, w, h, older=True, distance=int(h * 0.12), duration_ms=220)

names = [n for n, _ in appearances]
uniq = []
seen = set()
for n, p in appearances:
    if n not in seen:
        seen.add(n)
        uniq.append((n, p))
print("unique names", len(seen))
print("duplicate name hits:", {k: v for k, v in Counter(names).items() if v > 6})
# same name different preview
from collections import defaultdict
previews = defaultdict(set)
for n, p in appearances:
    previews[n].add(p)
dups = {n: ps for n, ps in previews.items() if len(ps) > 1}
print("same name different previews:", dups or "(none)")
print("last unique in order:")
order = []
seen2 = set()
for n, p in appearances:
    if n not in seen2:
        seen2.add(n)
        order.append(n)
print(", ".join(order))
print("count", len(order))
