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


def _list_rows(xml: str, *, min_top: int = 1200) -> list[dict]:
    rows: list[dict] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return rows
    for item in root.iter():
        if item.attrib.get("resource-id") != "com.bumblebff.app:id/connectionItem":
            continue
        name = badge = preview = ""
        for node in item.iter():
            rid = node.attrib.get("resource-id") or ""
            text = (node.attrib.get("text") or "").strip()
            if rid.endswith("personName"):
                name = text
            elif rid.endswith("connectionItem_badge") and "Unread" not in rid:
                badge = text
            elif rid.endswith("_message"):
                preview = text
        if not name:
            continue
        bounds = item.attrib.get("bounds") or ""
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue
        x1, y1, x2, y2 = map(int, match.groups())
        if y2 < min_top or y1 > 2480:
            continue
        rows.append(
            {
                "name": name,
                "badge": badge,
                "preview": preview,
                "x": 640,
                "y": (y1 + y2) // 2,
                "y1": y1,
                "y2": y2,
                "clipped": (y2 - y1) < 220,
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
    return "connectionItem" in xml or "New friends" in xml


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
        if SEARCH_FIELD in xml:
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


def extract_messages(xml: str, width: int) -> list[dict]:
    msgs: list[dict] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return msgs
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
        if y1 < 310 or y1 >= 2540 or len(text) < 2:
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
        chunk = extract_messages(xml, width)
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
    _go_top_of_inbox(device, package, width, height)

    seen: dict[str, dict[str, str]] = {}
    stagnant = 0
    last_key: tuple[str, ...] | None = None
    for _ in range(60):
        xml = dump_hierarchy(device)
        if not _on_list(xml):
            xml = recover_to_list(device, package)
        visible = _list_rows(xml, min_top=200)
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
        if stagnant >= 6:
            break
        _scroll_inbox(device, width, height, toward_top=False)
    return list(seen.values())


def _pick_row(rows: list[dict], done: set[str]) -> dict | None:
    """Only a middle-of-screen unfinished row. Edge rows are scrolled, not tapped."""
    pending = [r for r in rows if r["name"] not in done and not r.get("clipped")]
    safe = [r for r in pending if 1450 <= int(r["y"]) <= 2200]
    if not safe:
        return None
    for row in safe:
        others = [r for r in rows if r["name"] != row["name"]]
        if any(abs(int(r["y"]) - int(row["y"])) < 90 for r in others):
            continue
        return row
    return safe[0]


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
    toward_top: bool,
    distance: int = 650,
    duration_ms: int = 180,
) -> None:
    """Scroll the Chats list. toward_top is finger-up, toward Jorge / newest."""
    x = width // 2
    distance = max(80, min(int(distance), 700))
    if toward_top:
        _adb_swipe(device, x, 2000, x, 2000 - distance, duration_ms)
    else:
        _adb_swipe(device, x, 1700, x, 1700 + distance, duration_ms)
    wait_idle(device, 0.55)


def _inbox_key(xml: str) -> tuple[str, ...]:
    return tuple(str(r["name"]) for r in _list_rows(xml))


def _go_top_of_inbox(device, package: str, width: int, height: int) -> str:
    xml = recover_to_list(device, package)
    last: tuple[str, ...] | None = None
    for _ in range(12):
        if not _on_list(xml):
            xml = recover_to_list(device, package)
        key = _inbox_key(xml)
        log.info("scroll-top inbox: %s", ", ".join(key) or "(none)")
        if key and key == last:
            return xml
        last = key
        _scroll_inbox(device, width, height, toward_top=True)
        xml = dump_hierarchy(device)
    return xml


def capture_all_chats(device, conn, package: str, limit: int = 0, *, recapture: bool = False) -> int:
    width = int(device.info["displayWidth"])
    height = int(device.info["displayHeight"])
    xml = _go_top_of_inbox(device, package, width, height)

    done = set() if recapture else names_with_messages(conn)
    misses: dict[str, int] = {}
    if recapture:
        log.info("recapture: opening every inbox row (not skipping saved chats)")
    elif done:
        log.info("resume: skipping %d already-saved chats", len(done))
    stagnant = 0
    captured = 0
    for _step in range(200):
        if limit > 0 and captured >= limit:
            break
        try:
            xml = dump_hierarchy(device)
            if not _on_list(xml):
                xml = recover_to_list(device, package)
            rows = _list_rows(xml, min_top=200)
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
            row = _pick_row(rows, done)
            if row is None:
                stagnant += 1
                if stagnant >= 14:
                    break
                if _on_list(xml):
                    _scroll_inbox(
                        device, width, height, toward_top=False, distance=700, duration_ms=180
                    )
                continue
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


SEARCH_FIELD = "com.bumblebff.app:id/navbar_search"


def _ensure_search(device, package: str) -> None:
    xml = dump_hierarchy(device)
    if SEARCH_FIELD in xml:
        return
    recover_to_list(device, package)
    tap(device, 1168, 239)
    wait_idle(device, 1.4)


def open_chat_via_search(device, package: str, name: str) -> str | None:
    """Open a named inbox thread via Chats search. Returns toolbar title or None."""
    _ensure_search(device, package)
    field = device(resourceId=SEARCH_FIELD)
    if not field.exists(timeout=3.0):
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
    rows = _list_rows(xml, min_top=200)
    match = next((r for r in rows if str(r["name"]).lower() == name.strip().lower()), None)
    if match is None:
        device.press("back")
        wait_idle(device, 0.5)
        xml = dump_hierarchy(device)
        rows = _list_rows(xml, min_top=300)
        match = next((r for r in rows if str(r["name"]).lower() == name.strip().lower()), None)
    if match is None:
        log.warning("search had no row for %s (got %s)", name, [r["name"] for r in rows])
        return None
    tap(device, 640, int(match["y"]))
    wait_idle(device, 1.6)
    xml = _wait_thread(device)
    partner = chat_partner_name(xml)
    if not partner or not _same_person(partner, name):
        log.warning("search opened %s wanted %s", partner, name)
        return None
    return partner


_TAP_Y_LO = 1550
_TAP_Y_HI = 2150
_TAP_Y_AIM = 1850


def _row_in_tap_zone(row: dict) -> bool:
    return _TAP_Y_LO <= int(row["y"]) <= _TAP_Y_HI


def open_chat_from_list(device, package: str, name: str) -> str | None:
    """Scroll the Chats inbox and tap an exact name. Used when search misses."""
    width = int(device.info["displayWidth"])
    height = int(device.info["displayHeight"])
    xml = recover_to_list(device, package)
    want = name.strip().lower()
    if not any(str(r["name"]).lower() == want for r in _list_rows(xml, min_top=200)):
        xml = _go_top_of_inbox(device, package, width, height)
    last_key: tuple[str, ...] | None = None
    stagnant = 0
    tap_tries = 0
    for _ in range(60):
        if not _on_list(xml):
            xml = recover_to_list(device, package)
        rows = _list_rows(xml, min_top=200)
        names = [str(r["name"]) for r in rows]
        hit = next((r for r in rows if str(r["name"]).lower() == want), None)
        if hit is not None and not _row_in_tap_zone(hit):
            y = int(hit["y"])
            delta = y - _TAP_Y_AIM
            dist = min(380, max(140, abs(delta) * 2 // 3))
            log.info("nudge %s y=%s → %s (%s)", name, y, _TAP_Y_AIM, names)
            _scroll_inbox(
                device,
                width,
                height,
                toward_top=delta > 0,
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
            if name_node.exists(timeout=1.2):
                name_node.click()
            else:
                tap(device, 480, int(hit["y1"]) + 70)
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
        _scroll_inbox(device, width, height, toward_top=False, distance=320, duration_ms=240)
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


def fill_via_search(device, conn, package: str) -> int:
    """Open Chats search and capture every indexed person still missing a transcript."""
    have = names_with_messages(conn)
    names = [str(r[0]) for r in conn.execute("SELECT name FROM people ORDER BY name COLLATE NOCASE")]
    targets: list[str] = []
    for name in names + ["Absa", "P", "Rishi", "Zhal"]:
        if name not in have and name not in targets:
            targets.append(name)
    log.info("search-fill %d people", len(targets))
    width = int(device.info["displayWidth"])
    height = int(device.info["displayHeight"])
    captured = 0
    for name in targets:
        try:
            _ensure_search(device, package)
            field = device(resourceId=SEARCH_FIELD)
            if not field.exists(timeout=3.0):
                log.warning("search field missing for %s", name)
                continue
            field.click()
            wait_idle(device, 0.3)
            field.set_text(name)
            wait_idle(device, 1.6)
            device.press("back")
            wait_idle(device, 0.5)
            xml = dump_hierarchy(device)
            rows = _list_rows(xml, min_top=300)
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
            tap(device, 640, int(match["y"]))
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
