"""Human-paced LinkedIn profile harvest for new non-spam inbound."""

from __future__ import annotations

import json
import hashlib
import logging
import random
import time

from src.config import load_config
from src.linkedin_screen import (
    find_thread_profile_point,
    inmail_kind,
    looks_like_inmail_promo,
    looks_like_profile,
    parse_open_thread,
    parse_person_profile,
    plausible_person_name,
)
from src.linkedin_spam import inbound_from_stranger, is_flagged
from src.linkedin_store import get_person, list_thread, update_person_profile
from src.phones import DEFAULT_PHONE_ID, phone_scope
from src.store import connect as db_connect, db_path_from_config

log = logging.getLogger(__name__)


def _row_get(row, key: str, default: str = "") -> str:
    if row is None:
        return default
    try:
        if key not in row.keys():
            return default
    except Exception:
        return default
    return str(row[key] or default)


def is_inmail_thread(row) -> bool:
    if row is None:
        return False
    preview = _row_get(row, "preview")
    last = _row_get(row, "last_text")
    if inmail_kind(preview, last):
        return True
    return bool(looks_like_inmail_promo(preview) or looks_like_inmail_promo(last))


def profile_message_fp(pairs: list[tuple[str, str]]) -> str:
    inbound = "\n".join(body.strip() for side, body in pairs if side == "them" and body.strip())
    return hashlib.sha256(inbound.encode("utf-8")).hexdigest()[:16] if inbound else ""


def should_harvest(row, pairs: list[tuple[str, str]] | None = None) -> tuple[bool, str]:
    """True when this is a real person thread we have not harvested yet."""
    if row is None:
        return False, "person not found"
    name = _row_get(row, "name")
    if not plausible_person_name(name):
        return False, "not a real name"
    if is_inmail_thread(row):
        return False, "inmail skipped"
    if is_flagged(_row_get(row, "spam")):
        return False, "spam/pitch"
    last_from = _row_get(row, "last_from")
    them = False
    if pairs:
        them = any(side == "them" and (body or "").strip() for side, body in pairs)
    else:
        them = last_from == "them"
    if not them:
        return False, "no inbound"
    if pairs:
        fp = profile_message_fp(pairs)
        if fp and fp == _row_get(row, "profile_fp"):
            return False, "already captured for this inbound"
    elif _row_get(row, "profile_captured_at"):
        return False, "already captured"
    return True, "ok"


def schedule_if_needed(name: str, phone_id: str) -> dict:
    """Enqueue linkedin_profile on the right phone, or skip. Never steals the phone."""
    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        with phone_scope(phone_id):
            row = get_person(conn, name, phone_id)
            pairs = [(str(m["side"]), str(m["body"])) for m in list_thread(conn, name, phone_id=phone_id)]
            ok, why = should_harvest(row, pairs)
            if not ok:
                return {"ok": True, "queued": False, "reason": why, "name": name, "phone_id": phone_id}
    finally:
        conn.close()
    from src.phone_queue import enqueue

    job = enqueue("linkedin_profile", name, phone_id=phone_id)
    return {"ok": True, "queued": True, "job": job, "reason": "queued", "name": name, "phone_id": phone_id}


def _pause(lo: float = 0.7, hi: float = 1.8) -> None:
    time.sleep(random.uniform(lo, hi))


def _short_scroll(device) -> None:
    info = device.info
    w, h = int(info["displayWidth"]), int(info["displayHeight"])
    try:
        device.swipe(w // 2, int(h * 0.68), w // 2, int(h * 0.42), 0.38)
    except Exception:
        pass
    _pause(0.5, 1.2)


def harvest_named(
    name: str,
    *,
    serial: str | None = None,
    phone_id: str | None = None,
) -> tuple[bool, str]:
    from src.linkedin_sync import _leave_profile, _leave_thread, _open_named_thread, _unlock, _xml
    from src.phone_queue import check_cancel
    from src.phones import serial_for
    from src.screen import tap, wait_idle

    name = (name or "").strip()
    if not name:
        return False, "name required"
    cfg = load_config()
    pid = phone_id or str(cfg.get("phone_id") or DEFAULT_PHONE_ID)
    conn = db_connect(db_path_from_config(cfg))
    try:
        with phone_scope(pid):
            row = get_person(conn, name, pid)
            pairs = [(str(m["side"]), str(m["body"])) for m in list_thread(conn, name, phone_id=pid)]
            ok, why = should_harvest(row, pairs)
            if not ok:
                return True, why
    finally:
        conn.close()

    serial = serial or serial_for(pid)
    device, err = _unlock(serial)
    if device is None:
        return False, err
    check_cancel()
    opened, why = _open_named_thread(device, name, force=False)
    if not opened:
        return False, why
    _pause(0.8, 2.0)
    xml = _xml(device)
    open_name, _ = parse_open_thread(xml)
    point = find_thread_profile_point(xml, open_name or name)
    if point is None:
        _leave_thread(device)
        return False, f"no profile control on thread for {name}"
    tap(device, point[0], point[1])
    wait_idle(device, 1.4)
    _pause(1.0, 2.4)
    xml = _xml(device)
    if not looks_like_profile(xml):
        # One retry — LinkedIn sometimes opens a mini-card first.
        point = find_thread_profile_point(xml, open_name or name)
        if point:
            tap(device, point[0], point[1])
            wait_idle(device, 1.4)
            _pause(0.8, 1.6)
            xml = _xml(device)
    if not looks_like_profile(xml):
        _leave_thread(device)
        return False, f"opened thread for {name} but not their profile"

    merged = parse_person_profile(xml, open_name or name)
    for _ in range(random.randint(2, 3)):
        check_cancel()
        _short_scroll(device)
        xml = _xml(device)
        extra = parse_person_profile(xml, open_name or name)
        if extra.get("about") and not merged.get("about"):
            merged["about"] = extra["about"]
        if extra.get("location") and not merged.get("location"):
            merged["location"] = extra["location"]
        if extra.get("title") and not merged.get("title"):
            merged["title"] = extra["title"]
        if extra.get("headline") and len(extra["headline"]) > len(merged.get("headline") or ""):
            merged["headline"] = extra["headline"]
        for post in extra.get("posts") or []:
            if post not in merged["posts"] and len(merged["posts"]) < 3:
                merged["posts"].append(post)
        if len(merged.get("posts") or []) >= 3 and merged.get("about"):
            break

    _leave_profile(device, xml)
    _leave_thread(device)

    posts = [p for p in (merged.get("posts") or []) if str(p).strip()][:3]
    conn = db_connect(db_path_from_config(cfg))
    try:
        with phone_scope(pid):
            update_person_profile(
                conn,
                name,
                phone_id=pid,
                headline=merged.get("headline") or None,
                about=merged.get("about") or None,
                location=merged.get("location") or None,
                title=merged.get("title") or None,
                posts_json=json.dumps(posts, ensure_ascii=False) if posts else None,
                captured=True,
                fingerprint=profile_message_fp(pairs),
            )
            conn.commit()
    finally:
        conn.close()
    return True, f"harvested profile for {name} ({len(posts)} posts)"


def maybe_after_spam_label(name: str, phone_id: str, label: str, *, cached: bool = False) -> None:
    if is_flagged(label):
        return
    if label and label != "ok":
        return
    try:
        schedule_if_needed(name, phone_id)
    except Exception:
        log.warning("profile harvest schedule failed for %s", name, exc_info=True)


def maybe_after_store(name: str, phone_id: str, last_from: str | None, preview: str) -> None:
    """After a scan store — harvest if they wrote and this is not stranger-spam-pending."""
    if last_from != "them":
        return
    if looks_like_inmail_promo(preview):
        return
    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        with phone_scope(phone_id):
            from src.linkedin_spam import _thread_pairs

            row = get_person(conn, name, phone_id)
            pairs = _thread_pairs(conn, name, phone_id)
            if inbound_from_stranger(pairs) and not _row_get(row, "spam"):
                return
            ok, _why = should_harvest(row, pairs)
            if not ok:
                return
    finally:
        conn.close()
    try:
        schedule_if_needed(name, phone_id)
    except Exception:
        log.warning("profile harvest schedule failed for %s", name, exc_info=True)
