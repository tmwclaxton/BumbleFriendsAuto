"""Conservative LinkedIn feed cover + messaging capture."""

from __future__ import annotations

import logging
import random
import time

from src.config import load_config
from src.device import bring_app_foreground, connect, dump_hierarchy, wait_idle
from src.gestures import tap
from src.linkedin_screen import (
    PACKAGE,
    find_back_point,
    find_home_tab,
    find_messaging_entry,
    find_nav_point,
    find_reaction_points,
    looks_like_feed,
    looks_like_messaging,
    looks_like_profile,
    parse_messaging_list,
    parse_open_thread,
    plausible_person_name,
    should_open_row,
)
from src.phones import DEFAULT_PHONE_ID
from src.linkedin_store import list_people, replace_thread, upsert_chat
from src.store import connect as db_connect, db_path_from_config
from src.unlock import wake_and_unlock

log = logging.getLogger(__name__)

SEED_LIMIT = 9
SCAN_OPEN_CAP = 4


def _unlock(serial: str | None):
    device = connect(serial)
    unlocked = False
    for _ in range(4):
        try:
            unlocked = wake_and_unlock(device, serial=serial)
            break
        except Exception as exc:
            log.warning("unlock dropped: %s", exc)
            time.sleep(2)
            try:
                device = connect(serial)
            except Exception:
                time.sleep(2)
    if not unlocked:
        return None, "phone still locked — unlock failed"
    bring_app_foreground(device, PACKAGE)
    wait_idle(device, 1.4)
    return device, ""


def _xml(device) -> str:
    wait_idle(device, 0.35)
    return dump_hierarchy(device)


def _leave_profile(device, xml: str) -> str:
    point = find_back_point(xml)
    if point:
        tap(device, point[0], point[1])
    else:
        try:
            device.press("back")
        except Exception:
            pass
    wait_idle(device, 1.0)
    return _xml(device)


def ensure_feed(device) -> str:
    xml = _xml(device)
    for _ in range(6):
        if looks_like_feed(xml) and not looks_like_profile(xml):
            return xml
        if looks_like_messaging(xml):
            home = find_home_tab(xml) or find_nav_point(xml, "Home")
            if home:
                tap(device, home[0], home[1])
                wait_idle(device, 1.2)
                xml = _xml(device)
                continue
        if looks_like_profile(xml):
            xml = _leave_profile(device, xml)
            continue
        home = find_home_tab(xml)
        if home:
            tap(device, home[0], home[1])
            wait_idle(device, 1.2)
            xml = _xml(device)
            continue
        break
    return xml


def ensure_messaging(device) -> str:
    xml = ensure_feed(device)
    if looks_like_messaging(xml):
        return xml
    point = find_messaging_entry(xml)
    if point is None:
        info = device.info
        w, h = int(info["displayWidth"]), int(info["displayHeight"])
        point = (int(w * 0.92), int(h * 0.07))
        log.info("LinkedIn messaging header fallback %s", point)
    tap(device, point[0], point[1])
    wait_idle(device, 1.5)
    xml = _xml(device)
    if looks_like_profile(xml):
        xml = _leave_profile(device, xml)
        point = find_messaging_entry(xml)
        if point:
            tap(device, point[0], point[1])
            wait_idle(device, 1.5)
            xml = _xml(device)
    return xml


def _tap_nav(device, xml: str, label: str, fallback_frac: float) -> None:
    point = find_nav_point(xml, label)
    if point is None:
        info = device.info
        w, h = int(info["displayWidth"]), int(info["displayHeight"])
        y = int(h * (0.07 if label.lower() == "messaging" else 0.95))
        point = (int(w * fallback_frac), y)
        log.info("LinkedIn %s tab fallback %s", label, point)
    tap(device, point[0], point[1])
    wait_idle(device, 1.2)


def cover_feed(device) -> int:
    """Scroll a few posts and react only when Like/Celebrate is obvious."""
    xml = ensure_feed(device)
    home = find_home_tab(xml)
    if home:
        tap(device, home[0], home[1])
        wait_idle(device, 1.0)
    reacted = 0
    info = device.info
    w, h = int(info["displayWidth"]), int(info["displayHeight"])
    for _ in range(random.randint(4, 8)):
        time.sleep(random.uniform(0.8, 2.0))
        xml = _xml(device)
        pts = find_reaction_points(xml)
        choice = None
        if pts.get("celebrate") and random.random() < 0.25:
            choice = pts["celebrate"]
        elif pts.get("like"):
            choice = pts["like"]
        if choice:
            tap(device, choice[0], choice[1])
            reacted += 1
            time.sleep(random.uniform(0.4, 1.1))
        try:
            device.swipe(w // 2, int(h * 0.72), w // 2, int(h * 0.32), 0.45)
        except Exception:
            pass
    return reacted


def _store_hit(conn, phone_id: str, name: str, preview: str, badge: str, messages: list[tuple[str, str]]) -> None:
    from src.phones import phone_scope

    if not plausible_person_name(name):
        log.info("skip chrome name %r", name)
        return
    with phone_scope(phone_id):
        last_from = messages[-1][0] if messages else None
        last_text = messages[-1][1] if messages else preview
        pid = upsert_chat(
            conn,
            name,
            preview=preview,
            badge=badge,
            last_from=last_from,
            last_text=last_text,
        )
        if messages:
            replace_thread(conn, pid, messages)
        conn.commit()


def _open_conversation(device, hit) -> tuple[str, list[tuple[str, str]]]:
    tap(device, hit.x, hit.y)
    wait_idle(device, 1.3)
    xml = _xml(device)
    name, msgs = parse_open_thread(xml)
    try:
        device.press("back")
    except Exception:
        pass
    wait_idle(device, 0.8)
    if not plausible_person_name(name):
        name = hit.name
    return name or hit.name, msgs or ([("them", hit.preview)] if hit.preview else [])


def run_seed(cfg: dict | None = None, serial: str | None = None, *, phone_id: str | None = None) -> tuple[bool, str]:
    from src.phone_queue import check_cancel

    cfg = cfg or load_config()
    pid = phone_id or str(cfg.get("phone_id") or DEFAULT_PHONE_ID)
    device, err = _unlock(serial)
    if device is None:
        return False, err
    reacted = cover_feed(device)
    xml = ensure_messaging(device)
    hits = parse_messaging_list(xml)[:SEED_LIMIT]
    conn = db_connect(db_path_from_config(cfg))
    opened = 0
    try:
        for hit in hits:
            check_cancel()
            name, msgs = _open_conversation(device, hit)
            _store_hit(conn, pid, name, hit.preview, "unread" if hit.unread else "", msgs)
            opened += 1
        return True, f"seeded {opened} chats (feed reactions {reacted})"
    finally:
        conn.close()


def run_scan(cfg: dict | None = None, serial: str | None = None, *, phone_id: str | None = None) -> tuple[bool, str]:
    from src.phone_queue import check_cancel

    cfg = cfg or load_config()
    pid = phone_id or str(cfg.get("phone_id") or DEFAULT_PHONE_ID)
    device, err = _unlock(serial)
    if device is None:
        return False, err
    reacted = cover_feed(device)
    xml = ensure_messaging(device)
    hits = parse_messaging_list(xml)
    conn = db_connect(db_path_from_config(cfg))
    opened = 0
    listed = 0
    try:
        from src.phones import phone_scope

        stored = {}
        with phone_scope(pid):
            for row in list_people(conn):
                stored[str(row["name"]).casefold()] = str(row["preview"] or row["last_text"] or "")
        for hit in hits:
            check_cancel()
            listed += 1
            _store_hit(conn, pid, hit.name, hit.preview, "unread" if hit.unread else "", [])
            if opened >= SCAN_OPEN_CAP:
                continue
            if not should_open_row(hit, stored.get(hit.name.casefold())):
                continue
            name, msgs = _open_conversation(device, hit)
            _store_hit(conn, pid, name, hit.preview, "", msgs)
            opened += 1
        return True, f"listed {listed}, opened {opened}, feed reactions {reacted}"
    finally:
        conn.close()
