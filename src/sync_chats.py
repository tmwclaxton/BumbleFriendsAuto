"""Scan BFF Chats into SQLite — list preview or full transcripts."""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from src.chats import chat_partner_name
from src.config import load_config
from src.device import bring_app_foreground, connect, dump_hierarchy, wait_idle
from src.gestures import _adb_swipe, tap
from src.messenger import leave_chat
from src.screen import find_tab_point
from src.store import (
    connect as db_connect,
    db_path_from_config,
    names_with_messages,
    replace_thread,
    upsert_chat,
)

log = logging.getLogger(__name__)

_CHROME = re.compile(
    r"(your turn to message|their turn to message|hours to reply|"
    r"match has expired|conversation expired|delivered|^seen$|"
    r"need more time|extend this match|not sure what to say|"
    r"let.?s help you break the ice)",
    re.I,
)
_OPENER = re.compile(r"putting together a wee group", re.I)
_SKIP_EXACT = {
    "Chats",
    "People",
    "Profile",
    "Plans",
    "Send",
    "Aa",
    "GIF",
    "Today",
    "Yesterday",
    "Back",
    "Camera",
    "Recent",
    "Start",
    "Learn more",
}

_DATE_LABEL = re.compile(
    r"^(?:\d{1,2} [A-Za-z]+ 20\d{2}|Today|Yesterday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)$",
    re.I,
)

# Layout fractions work on Honor (~1280x2800) and Pixel (~1080x2400).
_SEARCH_ICON_RIDS = (
    "com.bumblebff.app:id/navbar_search",
    "com.bumblebff.app:id/mainAppToolbarNavigation_openSearchIcon",
)
_SEARCH_FIELD_RIDS = (
    "com.bumblebff.app:id/navbar_search",
    "com.bumblebff.app:id/search_src_text",
    "com.bumblebff.app:id/openSearchBarText",
)
SEARCH_FIELD = _SEARCH_FIELD_RIDS[0]
_ITEM_RID_SUFFIXES = ("connectionItem", "connectionsItem")


def _screen_size(device) -> tuple[int, int]:
    info = device.info
    return int(info["displayWidth"]), int(info["displayHeight"])


def _bounds_center(bounds: str) -> tuple[int, int] | None:
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not match:
        return None
    x1, y1, x2, y2 = map(int, match.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def _tab_bar_top(xml: str, height: int) -> int:
    """Y where the bottom tab bar starts; fallback ~91% of height."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return int(height * 0.91)
    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        if rid.endswith("mainApp_navigationTabBar"):
            bounds = node.attrib.get("bounds") or ""
            match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
            if match:
                return int(match.group(2))
    return int(height * 0.91)


def _list_rows(xml: str, *, min_top: int = 0, height: int | None = None, width: int | None = None) -> list[dict]:
    rows: list[dict] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return rows
    if height is None:
        # Infer from hierarchy root bounds when caller has no device size.
        for node in root.iter():
            bounds = node.attrib.get("bounds") or ""
            match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
            if match:
                height = max(height or 0, int(match.group(4)))
                width = max(width or 0, int(match.group(3)))
        height = height or 2400
        width = width or 1080
    max_y1 = int(height * 0.94)
    mid_x = int(width // 2) if width else 540
    min_row_h = max(80, int(height * 0.055))
    for item in root.iter():
        rid = item.attrib.get("resource-id") or ""
        if not any(rid.endswith(suffix) for suffix in _ITEM_RID_SUFFIXES):
            continue
        name = badge = preview = ""
        for node in item.iter():
            child_rid = node.attrib.get("resource-id") or ""
            text = (node.attrib.get("text") or "").strip()
            if child_rid.endswith("personName"):
                name = text
            elif child_rid.endswith("connectionItem_badge") and "Unread" not in child_rid:
                badge = text
            elif child_rid.endswith("_message"):
                preview = text
        if not name:
            continue
        bounds = item.attrib.get("bounds") or ""
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue
        x1, y1, x2, y2 = map(int, match.groups())
        if y2 < min_top or y1 > max_y1:
            continue
        rows.append(
            {
                "name": name,
                "badge": badge,
                "preview": preview,
                "x": mid_x,
                "y": (y1 + y2) // 2,
                "y1": y1,
                "y2": y2,
                "clipped": (y2 - y1) < min_row_h or y2 > int(height * 0.90),
            }
        )
    # Pixel sometimes omits connectionItem; still index personName nodes.
    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        if not rid.endswith("personName"):
            continue
        name = (node.attrib.get("text") or "").strip()
        if not name or name in _SKIP_EXACT:
            continue
        bounds = node.attrib.get("bounds") or ""
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue
        x1, y1, x2, y2 = map(int, match.groups())
        if y2 < min_top or y1 > max_y1:
            continue
        y = (y1 + y2) // 2
        if any(r["name"] == name and abs(int(r["y"]) - y) < 50 for r in rows):
            continue
        rows.append(
            {
                "name": name,
                "badge": "",
                "preview": "",
                "x": mid_x,
                "y": y,
                "y1": y1,
                "y2": y2,
                "clipped": (y2 - y1) < min_row_h,
            }
        )
    rows.sort(key=lambda r: r["y"])
    return rows


def _dump(device) -> str:
    dump_hierarchy(device)
    wait_idle(device, 0.2)
    return dump_hierarchy(device)


def _texts(xml: str) -> str:
    try:
        return " ".join((n.attrib.get("text") or "") for n in ET.fromstring(xml).iter())
    except ET.ParseError:
        return ""


def _on_thread(xml: str) -> bool:
    return bool(chat_partner_name(xml))


def _on_list(xml: str) -> bool:
    if _on_thread(xml):
        return False
    return (
        "connectionItem" in xml
        or "connectionsItem" in xml
        or "personName" in xml
        or "New friends" in xml
    )


def _on_profile(xml: str) -> bool:
    blob = _texts(xml).lower()
    return "unmatch and report" in blob or "block and report" in blob or "my location" in blob


def _tap_chats_tab(device) -> bool:
    xml = _dump(device)
    if _on_thread(xml):
        log.info("still in a thread; not tapping Chats tab")
        return False
    point = find_tab_point(xml, "Chats")
    if point is None:
        log.warning("Chats tab not on screen")
        return False
    log.info("Chats tab @ %s", point)
    tap(device, point[0], point[1])
    wait_idle(device, 1.6)
    return True


def recover_to_list(device, package: str) -> str:
    """Leave threads/profiles and land on the Chats inbox. Never tap the composer."""
    device.shell("cmd statusbar collapse")
    xml = _dump(device)
    for attempt in range(8):
        if any(rid in xml for rid in _SEARCH_FIELD_RIDS):
            log.info("leave chats search (%d)", attempt)
            device.press("back")
            wait_idle(device, 0.8)
            xml = dump_hierarchy(device)
            continue
        if _on_list(xml):
            return xml
        if _on_profile(xml):
            log.info("profile overlay — system back (%d)", attempt)
            device.press("back")
            wait_idle(device, 1.0)
            xml = dump_hierarchy(device)
            continue
        if _on_thread(xml):
            partner = chat_partner_name(xml) or "?"
            log.info("leave thread %s (%d)", partner, attempt)
            leave_chat(device)
            wait_idle(device, 1.0)
            xml = dump_hierarchy(device)
            continue
        if find_tab_point(xml, "Chats"):
            _tap_chats_tab(device)
            xml = dump_hierarchy(device)
            continue
        log.info("unknown screen — system back (%d)", attempt)
        device.press("back")
        wait_idle(device, 0.9)
        bring_app_foreground(device, package)
        wait_idle(device, 0.8)
        xml = dump_hierarchy(device)
    log.warning("could not reach Chats list")
    return xml


def _search_field(device):
    for rid in _SEARCH_FIELD_RIDS:
        node = device(resourceId=rid)
        if node.exists(timeout=0.4):
            return node
    node = device(className="android.widget.EditText")
    if node.exists(timeout=0.4):
        return node
    return None


def _ensure_inbox_all(device, xml: str) -> str:
    """Tap the All filter chip when the inbox is on Your turn / Unread."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return xml
    all_pt = None
    need_all = False
    for node in root.iter():
        text = (node.attrib.get("text") or "").strip().lower()
        selected = (node.attrib.get("selected") or "").lower() == "true"
        clickable = (node.attrib.get("clickable") or "").lower() == "true"
        if text in {"your turn", "unread", "expired"} and selected:
            need_all = True
        if text == "all" and clickable:
            all_pt = _bounds_center(node.attrib.get("bounds") or "")
    if all_pt and need_all:
        log.info("inbox filter → All @ %s", all_pt)
        tap(device, all_pt[0], all_pt[1])
        wait_idle(device, 1.0)
        return dump_hierarchy(device)
    return xml


def discover_via_letter_search(device, package: str) -> list[dict[str, str]]:
    """Type a–z in Chats search and collect every result row."""
    width, height = _screen_size(device)
    seen: dict[str, dict[str, str]] = {}
    for letter in list("abcdefghijklmnopqrstuvwxyz"):
        try:
            _ensure_search(device, package)
            field = _search_field(device)
            if field is None:
                log.warning("search field missing for letter %r", letter)
                recover_to_list(device, package)
                continue
            field.click()
            wait_idle(device, 0.25)
            field.set_text(letter)
            wait_idle(device, 1.1)
            last_key: tuple[str, ...] | None = None
            stagnant = 0
            for _ in range(20):
                xml = dump_hierarchy(device)
                rows = _list_rows(xml, min_top=0, height=height, width=width)
                for row in rows:
                    name = str(row["name"])
                    if name not in seen:
                        seen[name] = {
                            "name": name,
                            "badge": str(row.get("badge") or ""),
                            "preview": str(row.get("preview") or ""),
                        }
                        log.info("search-seen %s via %r", name, letter)
                key = tuple(str(r["name"]) for r in rows)
                if key == last_key:
                    stagnant += 1
                else:
                    stagnant = 0
                    last_key = key
                if stagnant >= 3 or not rows:
                    break
                _scroll_inbox(device, width, height, older=True, distance=int(height * 0.16))
        except Exception as exc:
            log.warning("letter search %r failed: %s", letter, exc)
        finally:
            device.press("back")
            wait_idle(device, 0.35)
    recover_to_list(device, package)
    log.info("letter-search indexed %d people", len(seen))
    return list(seen.values())


def _message_side(x1: int, x2: int, text: str, width: int) -> str:
    if _OPENER.search(text):
        return "you"
    right_frac = x2 / max(width, 1)
    left_frac = x1 / max(width, 1)
    if right_frac >= 0.82 or left_frac >= 0.38:
        return "you"
    if left_frac <= 0.12:
        return "them"
    return "them" if left_frac < 0.28 else "you"


def extract_messages(xml: str, width: int, height: int | None = None) -> list[dict]:
    msgs: list[dict] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return msgs
    height = height or 2400
    y_min = int(height * 0.12)
    y_max = int(height * 0.92)
    for node in root.iter():
        text = (node.attrib.get("text") or "").strip()
        rid = node.attrib.get("resource-id") or ""
        if not text or text in _SKIP_EXACT or _DATE_LABEL.match(text):
            continue
        if _CHROME.search(text):
            continue
        if any(
            k in rid
            for k in (
                "toolbar",
                "tabBar",
                "Input",
                "clock",
                "battery",
                "filter",
                "Title",
                "timestamp",
            )
        ):
            continue
        if text.lower().startswith("let's go social"):
            continue
        bounds = node.attrib.get("bounds") or ""
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue
        x1, y1, x2, y2 = map(int, match.groups())
        if y1 < y_min or y1 >= y_max or len(text) < 2:
            continue
        msgs.append(
            {
                "side": _message_side(x1, x2, text, width),
                "text": text,
                "y": y1,
            }
        )
    msgs.sort(key=lambda m: m["y"])
    out: list[dict] = []
    for msg in msgs:
        if out and out[-1]["text"] == msg["text"] and out[-1]["side"] == msg["side"]:
            continue
        out.append(msg)
    return out


def _same_person(a: str, b: str) -> bool:
    """Exact display name. First-name-only matched Lauren to Lauren Mizaela."""
    return a.strip().casefold() == b.strip().casefold()


def capture_thread(device, width: int, height: int, expected: str | None = None) -> list[tuple[str, str]]:
    """Scroll to the newest, then oldest, then newest again; return oldest→newest bubbles."""
    seen: set[tuple[str, str]] = set()
    thread: list[tuple[str, str]] = []
    x = width // 2
    y_mid = int(height * 0.52)
    y_low = int(height * 0.72)

    def _ingest(xml: str, prepend: bool) -> int:
        nonlocal thread
        partner = chat_partner_name(xml)
        if not partner:
            return -1
        if expected and not _same_person(partner, expected):
            return -2
        chunk = extract_messages(xml, width, height)
        unseen = [(m["side"], m["text"]) for m in chunk if (m["side"], m["text"]) not in seen]
        for item in unseen:
            seen.add(item)
        if prepend:
            thread = unseen + thread
        else:
            thread.extend(unseen)
        return len(unseen)

    def _scroll(toward_older: bool) -> None:
        if toward_older:
            _adb_swipe(device, x, y_mid, x, y_low, 380)
        else:
            _adb_swipe(device, x, y_low, x, y_mid, 300)
        wait_idle(device, 0.4)

    def _until_end(*, toward_older: bool, prepend: bool, limit: int) -> int | None:
        stagnant = 0
        last_code = 0
        for pass_i in range(limit):
            _scroll(toward_older)
            xml = dump_hierarchy(device)
            code = _ingest(xml, prepend)
            if code == -2:
                log.warning(
                    "wrong thread %s (wanted %s) — abort capture",
                    chat_partner_name(xml),
                    expected,
                )
                return -2
            if code == -1:
                log.warning("left the thread while capturing (pass %d)", pass_i)
                return -1
            last_code = code
            if code == 0:
                stagnant += 1
            else:
                stagnant = 0
            if stagnant >= 3:
                break
        return last_code

    # Newest messages sit at the bottom, just above the composer.
    xml = dump_hierarchy(device)
    code = _ingest(xml, prepend=False)
    if code == -2:
        log.warning("wrong thread while capturing (wanted %s)", expected)
        return []
    if code == -1:
        log.warning("left the thread while capturing")
        return []
    if _until_end(toward_older=False, prepend=False, limit=24) == -2:
        return []

    if _until_end(toward_older=True, prepend=True, limit=40) == -2:
        return []

    if _until_end(toward_older=False, prepend=False, limit=40) == -2:
        return []
    return thread


def scan_chat_list(device, package: str) -> list[dict[str, str]]:
    width = int(device.info["displayWidth"])
    height = int(device.info["displayHeight"])
    xml = _go_top_of_inbox(device, package, width, height)
    xml = _ensure_inbox_all(device, xml)
    _go_top_of_inbox(device, package, width, height)

    seen: dict[str, dict[str, str]] = {}
    stagnant = 0
    last_key: tuple[str, ...] | None = None
    for _ in range(150):
        xml = dump_hierarchy(device)
        if not _on_list(xml):
            xml = recover_to_list(device, package)
        visible = _list_rows(xml, min_top=0, height=height, width=width)
        for row in visible:
            name = str(row["name"])
            if name not in seen:
                seen[name] = {
                    "name": name,
                    "badge": str(row["badge"]),
                    "preview": str(row["preview"]),
                }
                log.info("seen %s badge=%r", name, row["badge"])
        key = tuple(str(r["name"]) for r in visible)
        if key == last_key:
            stagnant += 1
        else:
            stagnant = 0
            last_key = key
        if stagnant >= 10:
            break
        _scroll_inbox(device, width, height, older=True, distance=int(height * 0.12), duration_ms=220)
    return list(seen.values())


def _usable_list_band(height: int, tab_top: int | None = None) -> tuple[int, int, int]:
    """Y range where a chat row can be tapped (below header, above tab bar)."""
    lo = int(height * 0.20)
    hi = int((tab_top if tab_top else int(height * 0.90)) - max(16, int(height * 0.02)))
    aim = int(height * 0.62)
    return lo, hi, aim


def _pick_row(
    rows: list[dict],
    done: set[str],
    *,
    height: int,
    tab_top: int | None = None,
) -> dict | None:
    """Prefer a mid-screen unfinished row; otherwise any row in the list well.

    Top-of-inbox rows (Pixel ~y=1100 on 2400) can never be scrolled into a
    Honor-style 0.55–0.82 band, so refusing those left Nollan/Kavya unopened.
    """
    pending = [r for r in rows if r["name"] not in done and not r.get("clipped")]
    if not pending:
        pending = [r for r in rows if r["name"] not in done]
    if not pending:
        return None
    prefer_lo, prefer_hi, aim = _tap_zone(height)
    well_lo, well_hi, well_aim = _usable_list_band(height, tab_top)
    safe = [r for r in pending if prefer_lo <= int(r["y"]) <= prefer_hi]
    if not safe:
        safe = [r for r in pending if well_lo <= int(r["y"]) <= well_hi]
        aim = well_aim
    if not safe:
        return None
    gap = max(48, int(height * 0.02))
    isolated = []
    for row in safe:
        others = [r for r in rows if r["name"] != row["name"]]
        if any(abs(int(r["y"]) - int(row["y"])) < gap for r in others):
            continue
        isolated.append(row)
    pool = isolated or safe
    return min(pool, key=lambda r: abs(int(r["y"]) - aim))


def _open_named_chat(device, name: str, x: int, y: int) -> None:
    log.info("tap row %s @ (%s,%s)", name, x, y)
    tap(device, x, y)


def _wait_thread(device) -> str:
    xml = ""
    for _ in range(12):
        wait_idle(device, 0.45)
        xml = dump_hierarchy(device)
        blob = _texts(xml).lower()
        if chat_partner_name(xml) or _on_profile(xml):
            return xml
        if "match has expired" in blob or "conversation expired" in blob:
            if "chatInput" in xml or "chatToolbar" in xml:
                return xml
    return xml


def _scroll_inbox(
    device,
    width: int,
    height: int,
    *,
    older: bool,
    distance: int | None = None,
    duration_ms: int = 180,
) -> None:
    """Scroll the Chats list. older=True swipes up (further down the inbox)."""
    x = width // 2
    if distance is None:
        distance = int(height * 0.25)
    distance = max(80, min(int(distance), int(height * 0.35)))
    # Stay above the tab bar (~0.91h) and below the New-friends strip.
    y_hi = int(height * 0.78)
    y_lo = int(height * 0.52)
    if older:
        _adb_swipe(device, x, y_hi, x, y_hi - distance, duration_ms)
    else:
        _adb_swipe(device, x, y_lo, x, y_lo + distance, duration_ms)
    wait_idle(device, 0.55)


def _inbox_key(xml: str, *, height: int | None = None, width: int | None = None) -> tuple[str, ...]:
    return tuple(str(r["name"]) for r in _list_rows(xml, height=height, width=width))


def _go_top_of_inbox(device, package: str, width: int, height: int) -> str:
    xml = recover_to_list(device, package)
    last: tuple[str, ...] | None = None
    for _ in range(12):
        if not _on_list(xml):
            xml = recover_to_list(device, package)
        key = _inbox_key(xml, height=height, width=width)
        log.info("scroll-top inbox: %s", ", ".join(key) or "(none)")
        if key and key == last:
            return xml
        last = key
        _scroll_inbox(device, width, height, older=False)
        xml = dump_hierarchy(device)
    return xml


def capture_all_chats(device, conn, package: str, limit: int = 0, *, recapture: bool = False) -> int:
    width = int(device.info["displayWidth"])
    height = int(device.info["displayHeight"])
    xml = _go_top_of_inbox(device, package, width, height)
    _ensure_inbox_all(device, xml)
    xml = _go_top_of_inbox(device, package, width, height)

    done = set() if recapture else names_with_messages(conn)
    misses: dict[str, int] = {}
    if recapture:
        log.info("recapture: opening every inbox row (not skipping saved chats)")
    elif done:
        log.info("resume: skipping %d already-saved chats", len(done))
    stagnant = 0
    captured = 0
    last_inbox_key: tuple[str, ...] | None = None
    for _step in range(400):
        if limit > 0 and captured >= limit:
            break
        try:
            xml = dump_hierarchy(device)
            if not _on_list(xml):
                xml = recover_to_list(device, package)
            rows = _list_rows(xml, min_top=int(height * 0.08), height=height, width=width)
            log.info("inbox: %s", ", ".join(f"{r['name']}@{r['y']}" for r in rows) or "(none)")
            for seen in rows:
                badge = str(seen["badge"] or "")
                upsert_chat(
                    conn,
                    str(seen["name"]),
                    preview=str(seen["preview"]),
                    badge=badge,
                    last_text=str(seen["preview"]) or None,
                    last_from="them" if badge.strip().lower() == "your turn" else None,
                )
            conn.commit()
            tab_top = _tab_bar_top(xml, height)
            key = tuple(str(r["name"]) for r in rows)
            row = _pick_row(rows, done, height=height, tab_top=tab_top)
            if row is None:
                if key == last_inbox_key:
                    stagnant += 1
                else:
                    stagnant = 0
                    last_inbox_key = key
                pending = [r for r in rows if r["name"] not in done]
                if stagnant >= 6:
                    if pending:
                        row = min(
                            pending,
                            key=lambda r: abs(int(r["y"]) - int(height * 0.62)),
                        )
                        log.info("fallback tap %s after stagnant scroll", row["name"])
                    else:
                        log.info("inbox exhausted (%d captured)", captured)
                        break
                if row is None:
                    if _on_list(xml):
                        _scroll_inbox(
                            device,
                            width,
                            height,
                            older=True,
                            distance=int(height * 0.28),
                            duration_ms=180,
                        )
                    continue
            last_inbox_key = key
            stagnant = 0
            _open_named_chat(device, str(row["name"]), int(row["x"]), int(row["y"]))
            wait_idle(device, 1.5)
            xml = _wait_thread(device)
            device.shell("cmd statusbar collapse")
            if _on_profile(xml):
                log.info("profile for %s — back to list", row["name"])
                device.press("back")
                wait_idle(device, 1.0)
                continue
            partner = chat_partner_name(xml)
            if not partner:
                preview = f"{row.get('preview') or ''} {row.get('badge') or ''}"
                if "expired" in preview.lower() or "expired" in _texts(xml).lower():
                    log.info("expired / unopenable %s — recording list preview", row["name"])
                    upsert_chat(
                        conn,
                        str(row["name"]),
                        preview=str(row["preview"]),
                        badge=str(row["badge"]),
                        last_text=str(row["preview"]) or "expired",
                    )
                    conn.commit()
                    done.add(str(row["name"]))
                    recover_to_list(device, package)
                    continue
                misses[str(row["name"])] = misses.get(str(row["name"]), 0) + 1
                log.warning("tap missed %s (%d)", row["name"], misses[str(row["name"])])
                recover_to_list(device, package)
                if misses[str(row["name"])] >= 2:
                    log.warning("giving up opening %s — indexing from list", row["name"])
                    upsert_chat(
                        conn,
                        str(row["name"]),
                        preview=str(row["preview"]),
                        badge=str(row["badge"]),
                        last_text=str(row["preview"]) or None,
                    )
                    conn.commit()
                    done.add(str(row["name"]))
                else:
                    _adb_swipe(device, width // 2, int(height * 0.72), width // 2, int(height * 0.52), 280)
                    wait_idle(device, 0.5)
                continue
            if not _same_person(partner, str(row["name"])):
                log.warning("opened %s not %s — leaving without save", partner, row["name"])
                recover_to_list(device, package)
                continue
            thread = capture_thread(device, width, height, expected=partner)
            if not thread:
                log.warning("empty/wrong transcript for %s — not saving", partner)
                misses[str(row["name"])] = misses.get(str(row["name"]), 0) + 1
                if misses[str(row["name"])] >= 2:
                    done.add(str(row["name"]))
                recover_to_list(device, package)
                continue
            last_from = thread[-1][0]
            last_text = thread[-1][1]
            banner = _texts(dump_hierarchy(device)).lower()
            if "match has expired" in banner or "conversation expired" in banner:
                last_from = last_from or "them"
                last_text = last_text or "expired"
            person_id = upsert_chat(
                conn,
                partner,
                preview=str(row["preview"]),
                badge=str(row["badge"]),
                last_from=last_from,
                last_text=last_text,
                opener_sent=any(_OPENER.search(body) for _side, body in thread),
            )
            replace_thread(conn, person_id, thread)
            conn.commit()
            done.add(str(row["name"]))
            done.add(partner)
            captured += 1
            log.info("saved %s msgs=%d last=%s", partner, len(thread), last_from)
            recover_to_list(device, package)
            xml = dump_hierarchy(device)
            if not _on_list(xml):
                log.warning("not on inbox after %s", partner)
                recover_to_list(device, package)
        except Exception as exc:
            log.warning("capture error (%s); reconnect", exc)
            time.sleep(2)
            device = connect()
            bring_app_foreground(device, package)
            wait_idle(device, 1.2)
            recover_to_list(device, package)
            width = int(device.info["displayWidth"])
            height = int(device.info["displayHeight"])
    return captured


def _tap_search_icon(device, xml: str, width: int, height: int) -> bool:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        root = None
    if root is not None:
        for node in root.iter():
            rid = node.attrib.get("resource-id") or ""
            desc = (node.attrib.get("content-desc") or "").strip().lower()
            if rid in _SEARCH_ICON_RIDS or (desc == "search" and "toolbar" in rid.lower()):
                center = _bounds_center(node.attrib.get("bounds") or "")
                if center:
                    tap(device, center[0], center[1])
                    return True
            if desc == "search" and (node.attrib.get("clickable") or "").lower() == "true":
                center = _bounds_center(node.attrib.get("bounds") or "")
                if center and center[1] < int(height * 0.2):
                    tap(device, center[0], center[1])
                    return True
    # Fallback: top-right toolbar
    tap(device, int(width * 0.92), int(height * 0.085))
    return True


def _ensure_search(device, package: str) -> None:
    width, height = _screen_size(device)
    xml = dump_hierarchy(device)
    if _search_field(device) is not None:
        return
    recover_to_list(device, package)
    xml = dump_hierarchy(device)
    _tap_search_icon(device, xml, width, height)
    wait_idle(device, 1.4)


def open_chat_via_search(device, package: str, name: str) -> str | None:
    """Open a named inbox thread via Chats search. Returns toolbar title or None."""
    width, height = _screen_size(device)
    _ensure_search(device, package)
    field = _search_field(device)
    if field is None:
        log.warning("search field missing")
        return None
    field.click()
    wait_idle(device, 0.3)
    field.set_text(name)
    wait_idle(device, 1.8)
    xml = dump_hierarchy(device)
    blob = _texts(xml).lower()
    if "no results" in blob:
        log.warning("search no results for %s", name)
        device.press("back")
        wait_idle(device, 0.4)
        return None
    rows = _list_rows(xml, min_top=int(height * 0.08), height=height, width=width)
    match = next((r for r in rows if str(r["name"]).lower() == name.strip().lower()), None)
    if match is None:
        device.press("back")
        wait_idle(device, 0.5)
        xml = dump_hierarchy(device)
        rows = _list_rows(xml, min_top=int(height * 0.12), height=height, width=width)
        match = next((r for r in rows if str(r["name"]).lower() == name.strip().lower()), None)
    if match is None:
        log.warning("search had no row for %s (got %s)", name, [r["name"] for r in rows])
        return None
    tap(device, int(match["x"]), int(match["y"]))
    wait_idle(device, 1.6)
    xml = _wait_thread(device)
    partner = chat_partner_name(xml)
    if not partner or not _same_person(partner, name):
        log.warning("search opened %s wanted %s", partner, name)
        return None
    return partner


def _tap_zone(height: int) -> tuple[int, int, int]:
    lo = int(height * 0.55)
    hi = int(height * 0.82)
    aim = int(height * 0.68)
    return lo, hi, aim


def _row_in_tap_zone(row: dict, *, height: int) -> bool:
    lo, hi, _ = _tap_zone(height)
    return lo <= int(row["y"]) <= hi


def open_chat_from_list(device, package: str, name: str) -> str | None:
    """Scroll the Chats inbox and tap an exact name. Used when search misses."""
    width, height = _screen_size(device)
    tap_lo, tap_hi, tap_aim = _tap_zone(height)
    xml = recover_to_list(device, package)
    want = name.strip().lower()
    if not any(
        str(r["name"]).lower() == want
        for r in _list_rows(xml, min_top=int(height * 0.08), height=height, width=width)
    ):
        xml = _go_top_of_inbox(device, package, width, height)
    last_key: tuple[str, ...] | None = None
    stagnant = 0
    tap_tries = 0
    for _ in range(60):
        if not _on_list(xml):
            xml = recover_to_list(device, package)
        rows = _list_rows(xml, min_top=int(height * 0.08), height=height, width=width)
        names = [str(r["name"]) for r in rows]
        hit = next((r for r in rows if str(r["name"]).lower() == want), None)
        if hit is not None:
            y = int(hit["y"])
            well_lo, well_hi, _ = _usable_list_band(height, _tab_bar_top(xml, height))
            if not (well_lo <= y <= well_hi):
                delta = y - tap_aim
                dist = min(int(height * 0.16), max(int(height * 0.06), abs(delta) * 2 // 3))
                log.info("nudge %s y=%s → %s (%s)", name, y, tap_aim, names)
                _scroll_inbox(
                    device,
                    width,
                    height,
                    older=delta > 0,
                    distance=dist,
                    duration_ms=260,
                )
                xml = dump_hierarchy(device)
                stagnant = 0
                last_key = None
                continue
        if hit is not None:
            wait_idle(device, 0.7)
            log.info("list-tap %s @ y=%s y1=%s", name, hit["y"], hit["y1"])
            name_node = device(resourceId="com.bumblebff.app:id/personName", text=name)
            if not name_node.exists(timeout=0.4):
                name_node = device(resourceId="com.bumblebff.app:id/connectionsItem_personName", text=name)
            if name_node.exists(timeout=1.0):
                name_node.click()
            else:
                tap(device, int(width * 0.38), int(hit["y1"]) + max(40, int(height * 0.025)))
            wait_idle(device, 1.6)
            partner = chat_partner_name(_wait_thread(device))
            if partner and _same_person(partner, name):
                return partner
            log.warning("list opened %s wanted %s", partner, name)
            tap_tries += 1
            if partner:
                leave_chat(device)
            if tap_tries >= 3:
                return None
            xml = recover_to_list(device, package)
            continue
        log.info("inbox looking for %s: %s", name, ", ".join(names) or "(none)")
        key = tuple(names)
        if key == last_key:
            stagnant += 1
        else:
            stagnant = 0
            last_key = key
        if stagnant >= 6:
            break
        _scroll_inbox(
            device, width, height, older=True, distance=int(height * 0.14), duration_ms=240
        )
        xml = dump_hierarchy(device)
    log.warning("list had no row for %s", name)
    return None


def recapture_person(device, conn, package: str, name: str) -> bool:
    """Search-open one chat and replace its stored transcript."""
    partner = open_chat_via_search(device, package, name)
    if not partner:
        recover_to_list(device, package)
        partner = open_chat_from_list(device, package, name)
    if not partner:
        log.warning("could not open %s", name)
        return False
    width = int(device.info["displayWidth"])
    height = int(device.info["displayHeight"])
    thread = capture_thread(device, width, height, expected=partner)
    try:
        leave_chat(device)
    except Exception:
        pass
    if not thread:
        log.warning("empty recapture for %s", partner)
        return False
    person_id = upsert_chat(
        conn,
        partner,
        last_from=thread[-1][0],
        last_text=thread[-1][1],
        opener_sent=any(_OPENER.search(body) for _side, body in thread),
    )
    replace_thread(conn, person_id, thread)
    conn.commit()
    log.info("recaptured %s msgs=%d last=%s %r", partner, len(thread), thread[-1][0], thread[-1][1][:80])
    return True


def refresh_named_chat(name: str, *, serial: str | None = None) -> tuple[bool, str]:
    """Open a named chat on the phone and replace the stored transcript."""
    name = name.strip()
    if not name:
        return False, "name required"
    cfg = load_config()
    package = str(cfg["package"])
    device = connect(serial)
    bring_app_foreground(device, package)
    wait_idle(device, 0.6)
    conn = db_connect(db_path_from_config(cfg))
    try:
        ok = recapture_person(device, conn, package, name)
    finally:
        conn.close()
    if ok:
        return True, f"refreshed {name} from the phone"
    return False, f"could not recapture {name} — keep the phone unlocked on Chats"


def recapture_inbox(*, serial: str | None = None, sleep_after: bool = True) -> tuple[bool, str]:
    """Unlock (if PIN set), open every Chats row, save transcripts, optionally sleep."""
    from src.unlock import sleep_screen, wake_and_unlock

    cfg = load_config()
    package = str(cfg["package"])
    device = connect(serial)
    try:
        wake_and_unlock(device, serial=serial)
        bring_app_foreground(device, package)
        wait_idle(device, 1.0)
        conn = db_connect(db_path_from_config(cfg))
        try:
            def _index(rows: list[dict]) -> None:
                for row in rows:
                    badge = str(row.get("badge") or "")
                    upsert_chat(
                        conn,
                        str(row["name"]),
                        preview=str(row.get("preview") or ""),
                        badge=badge,
                        last_text=str(row.get("preview") or "") or None,
                        last_from="them" if badge.strip().lower() == "your turn" else None,
                    )
                conn.commit()

            indexed = scan_chat_list(device, package)
            _index(indexed)
            log.info("indexed %d inbox rows", len(indexed))
            extra = discover_via_letter_search(device, package)
            _index(extra)
            total_names = conn.execute("SELECT count(*) FROM people").fetchone()[0]
            log.info("after letter-search: %d people in db (%d new from search)", total_names, max(0, total_names - len(indexed)))
            n = capture_all_chats(device, conn, package, recapture=True)
            n_search = fill_via_search(device, conn, package)
        finally:
            conn.close()
        people_n = total_names
        msg = f"recaptured {n} chats ({people_n} people, {n_search} via search)"
        log.info(msg)
        return True, msg
    except Exception as exc:
        log.exception("recapture_inbox failed")
        return False, str(exc)
    finally:
        if sleep_after:
            try:
                sleep_screen(device, serial=serial)
            except Exception:
                log.warning("could not sleep screen after recapture")


def fill_via_search(device, conn, package: str) -> int:
    """Open Chats search and capture every indexed person still missing a transcript."""
    have = names_with_messages(conn)
    names = [str(r[0]) for r in conn.execute("SELECT name FROM people ORDER BY name COLLATE NOCASE")]
    targets: list[str] = []
    for name in names:
        if name in have or name in targets:
            continue
        row = conn.execute(
            """
            SELECT c.last_text, c.preview FROM chats c
            JOIN people p ON p.id = c.person_id WHERE p.name = ?
            """,
            (name,),
        ).fetchone()
        blob = f"{row[0] or ''} {row[1] or ''}".lower() if row else ""
        if "expired" in blob:
            log.info("search-fill skip expired %s", name)
            continue
        targets.append(name)
    log.info("search-fill %d people", len(targets))
    width = int(device.info["displayWidth"])
    height = int(device.info["displayHeight"])
    captured = 0
    for name in targets:
        try:
            _ensure_search(device, package)
            field = _search_field(device)
            if field is None:
                log.warning("search field missing for %s", name)
                continue
            field.click()
            wait_idle(device, 0.3)
            field.set_text(name)
            wait_idle(device, 1.6)
            device.press("back")
            wait_idle(device, 0.5)
            xml = dump_hierarchy(device)
            rows = _list_rows(xml, min_top=int(height * 0.12), height=height, width=width)
            for row in rows:
                upsert_chat(
                    conn,
                    str(row["name"]),
                    preview=str(row["preview"]),
                    badge=str(row["badge"]),
                    last_text=str(row["preview"]) or None,
                )
            conn.commit()
            match = next((r for r in rows if str(r["name"]).lower() == name.lower()), None)
            if match is None:
                log.warning("search had no row for %s (got %s)", name, [r["name"] for r in rows])
                continue
            log.info("search-open %s @ %s", name, match["y"])
            tap(device, int(match["x"]), int(match["y"]))
            wait_idle(device, 1.6)
            xml = _wait_thread(device)
            partner = chat_partner_name(xml)
            if not partner or not _same_person(partner, name):
                log.warning("search opened %s wanted %s", partner, name)
                if partner:
                    leave_chat(device)
                continue
            thread = capture_thread(device, width, height, expected=partner)
            if not thread:
                log.warning("empty search transcript %s", partner)
                leave_chat(device)
                continue
            person_id = upsert_chat(
                conn,
                partner,
                preview=str(match["preview"]),
                badge=str(match["badge"]),
                last_from=thread[-1][0],
                last_text=thread[-1][1],
                opener_sent=any(_OPENER.search(body) for _side, body in thread),
            )
            replace_thread(conn, person_id, thread)
            conn.commit()
            have.add(partner)
            captured += 1
            log.info("saved %s msgs=%d last=%s", partner, len(thread), thread[-1][0])
            leave_chat(device)
            wait_idle(device, 0.6)
        except Exception as exc:
            log.warning("search-fill error %s (%s)", name, exc)
            time.sleep(1)
            device = connect()
            bring_app_foreground(device, package)
    return captured


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync BFF Chats into SQLite")
    parser.add_argument("--serial")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--no-foreground", action="store_true")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Open every chat and save the full transcript",
    )
    parser.add_argument(
        "--recapture",
        action="store_true",
        help="With --full, reopen chats that already have a saved transcript",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max chats to open this run (0 = until the list is exhausted)",
    )
    parser.add_argument(
        "--search-fill",
        action="store_true",
        help="Search for each person missing a transcript and save the thread",
    )
    parser.add_argument(
        "--person",
        action="append",
        default=[],
        help="Recapture this chat via search (repeatable)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)
    path = args.db or db_path_from_config(cfg)
    device = connect(args.serial)
    if not args.no_foreground:
        bring_app_foreground(device, str(cfg["package"]))
        wait_idle(device, 1.0)

    conn = db_connect(path)
    package = str(cfg["package"])
    if args.search_fill:
        n = fill_via_search(device, conn, package)
        log.info("search-filled %d chats → %s", n, path)
        return 0
    if args.person:
        n = 0
        for name in args.person:
            if recapture_person(device, conn, package, name):
                n += 1
        log.info("recaptured %d/%d → %s", n, len(args.person), path)
        return 0
    if args.full:
        n = capture_all_chats(
            device, conn, package, limit=args.limit, recapture=args.recapture
        )
        log.info("captured %d chats → %s", n, path)
        return 0

    rows = scan_chat_list(device, package)
    for row in rows:
        badge = str(row["badge"] or "")
        upsert_chat(
            conn,
            row["name"],
            preview=row["preview"],
            badge=badge,
            last_text=row["preview"] or None,
            last_from="them" if badge.strip().lower() == "your turn" else None,
        )
    conn.commit()
    log.info("synced %d people → %s", len(rows), path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
