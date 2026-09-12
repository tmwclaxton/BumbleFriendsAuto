"""Conservative LinkedIn feed cover + messaging capture."""

from __future__ import annotations

import logging
import random
import time

from src.config import load_config
from src.device import bring_app_foreground, connect, dump_hierarchy, wait_idle
from src.gestures import tap
from src.input_ime import type_into
from src.linkedin_screen import (
    PACKAGE,
    find_back_point,
    find_archive_action,
    find_conversation_hit,
    find_dismiss_point,
    find_inbox_search,
    find_more_options,
    find_home_tab,
    find_messaging_entry,
    find_nav_point,
    find_reaction_points,
    find_send_point,
    fold_thread_chunk,
    is_thread_chrome,
    looks_like_blocker,
    looks_like_inmail_promo,
    looks_like_feed,
    looks_like_messaging,
    looks_like_profile,
    message_visible,
    names_match,
    parse_messaging_list,
    parse_open_thread,
    parse_thread_profile,
    plausible_person_name,
    should_open_row,
)
from src.phones import DEFAULT_PHONE_ID
from src.linkedin_store import add_message, list_people, list_thread, replace_thread, update_person_profile, upsert_chat
from src.store import connect as db_connect, db_path_from_config
from src.unlock import wake_and_unlock

log = logging.getLogger(__name__)

SEED_LIMIT = 9
SCAN_OPEN_CAP = 4
BACKFILL_OPEN_MIN = 5
BACKFILL_OPEN_MAX = 10
BACKFILL_SCROLLS = (8, 14)


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


def _dismiss_blocker(device, xml: str) -> str:
    """Leave share sheets and in-app article/cookie WebViews."""
    for _ in range(8):
        if not looks_like_blocker(xml):
            return xml
        point = find_dismiss_point(xml) or find_back_point(xml)
        if point:
            tap(device, point[0], point[1])
        else:
            try:
                device.press("back")
            except Exception:
                pass
        wait_idle(device, 0.9)
        xml = _xml(device)
    return xml


def ensure_feed(device) -> str:
    xml = _dismiss_blocker(device, _xml(device))
    for _ in range(6):
        xml = _dismiss_blocker(device, xml)
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
    if not looks_like_feed(xml):
        log.info("skip feed cover — not on Home")
        return 0
    home = find_home_tab(xml)
    if home:
        tap(device, home[0], home[1])
        wait_idle(device, 1.0)
    reacted = 0
    info = device.info
    w, h = int(info["displayWidth"]), int(info["displayHeight"])
    for _ in range(random.randint(4, 8)):
        time.sleep(random.uniform(0.8, 2.0))
        xml = _dismiss_blocker(device, _xml(device))
        if not looks_like_feed(xml):
            xml = ensure_feed(device)
            if not looks_like_feed(xml):
                break
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


MAX_HISTORY_SCROLLS = 120


def _store_hit(
    conn,
    phone_id: str,
    name: str,
    preview: str,
    badge: str,
    messages: list[tuple],
    profile: dict | None = None,
) -> None:
    from src.phones import phone_scope

    if not plausible_person_name(name):
        log.info("skip chrome name %r", name)
        return
    with phone_scope(phone_id):
        last_from = messages[-1][0] if messages else None
        # Listing an InMail/Sponsored row must not clobber a captured body with chrome.
        last_text = messages[-1][1] if messages else (None if looks_like_inmail_promo(preview) else preview)
        from src.linkedin_store import extract_profile_url

        profile_url = extract_profile_url(preview, last_text, *(item[1] for item in messages))
        pid = upsert_chat(
            conn,
            name,
            preview=preview,
            badge=badge,
            last_from=last_from,
            last_text=last_text,
            profile_url=profile_url,
        )
        extra = profile or {}
        if extra.get("headline") or extra.get("verified"):
            update_person_profile(
                conn,
                name,
                phone_id=phone_id,
                headline=extra.get("headline") or None,
                verified=bool(extra.get("verified")),
            )
        if messages:
            replace_thread(conn, pid, messages)
        conn.commit()
        check_msgs = list(messages)
        if not check_msgs and looks_like_inmail_promo(preview):
            check_msgs = [("them", preview.strip())]
        if check_msgs:
            from src.linkedin_product import needs_product_check, schedule_product_check
            from src.linkedin_spam import needs_spam_check, schedule_spam_check
            from src.linkedin_store import get_person

            row = get_person(conn, name, phone_id)
            stored_fp = str(row["spam_fp"] or "") if row is not None and "spam_fp" in row.keys() else ""
            if needs_spam_check(stored_fp, check_msgs):
                schedule_spam_check(name, phone_id)
            if messages:
                product_fp = str(row["product_fp"] or "") if row is not None and "product_fp" in row.keys() else ""
                if needs_product_check(product_fp, messages):
                    schedule_product_check(name, phone_id)
        from src.linkedin_profile import maybe_after_store

        maybe_after_store(name, phone_id, last_from, preview or last_text or "")


def capture_open_thread(device, expected: str | None = None) -> tuple[str | None, list[tuple], dict]:
    """Scroll to the conversation start, retaining every parsed bubble and timestamp."""
    from src.gestures import _adb_swipe
    from src.phone_queue import check_cancel

    info = device.info
    width, height = int(info["displayWidth"]), int(info["displayHeight"])
    x = width // 2
    thread: list[tuple] = []
    seen: set[tuple[str, str]] = set()
    profile = {"headline": "", "verified": False}
    name = expected

    def ingest(xml: str, *, prepend: bool) -> int:
        nonlocal name, profile
        open_name, chunk = parse_open_thread(xml)
        if open_name and plausible_person_name(open_name):
            if expected and not names_match(open_name, expected):
                return -2
            name = open_name
        card = parse_thread_profile(xml)
        if card.get("headline") and not profile.get("headline"):
            profile["headline"] = card["headline"]
        if card.get("verified"):
            profile["verified"] = True
        return fold_thread_chunk(thread, seen, chunk, prepend=prepend)

    def scroll_older() -> None:
        y1, y2 = int(height * 0.36), int(height * 0.78)
        try:
            _adb_swipe(device, x, y1, x, y2, 520)
        except Exception:
            try:
                device.swipe(x, y1, x, y2, 0.55)
            except Exception:
                pass
        wait_idle(device, 0.65)

    xml = _xml(device)
    if ingest(xml, prepend=False) == -2:
        return name, [], profile
    stagnant = 0
    for _ in range(MAX_HISTORY_SCROLLS):
        check_cancel()
        scroll_older()
        xml = _xml(device)
        code = ingest(xml, prepend=True)
        if code == -2:
            break
        if code == 0:
            stagnant += 1
        else:
            stagnant = 0
        at_start = bool(parse_thread_profile(xml).get("headline"))
        if (at_start and stagnant >= 1) or stagnant >= 3:
            break
    return name, thread, profile


def _open_conversation(device, hit) -> tuple[str, list[tuple[str, str]], dict]:
    tap(device, hit.x, hit.y)
    wait_idle(device, 1.3)
    name, msgs, profile = capture_open_thread(device, expected=hit.name)
    try:
        device.press("back")
    except Exception:
        pass
    wait_idle(device, 0.8)
    if not plausible_person_name(name):
        name = hit.name
    name = name or hit.name
    if msgs:
        return name, msgs, profile
    preview = (hit.preview or "").strip()
    if not preview or is_thread_chrome(preview, name):
        return name, [], profile
    from src.linkedin_screen import polish_message

    side, body = polish_message("them", preview, name)
    return name, [(side, body)], profile


def tracked_thread_names(conn, phone_id: str) -> set[str]:
    pid = phone_id or DEFAULT_PHONE_ID
    return {
        str(row["name"]).casefold()
        for row in list_people(conn)
        if str(row["phone_id"] or DEFAULT_PHONE_ID) == pid and int(row["message_count"] or 0) > 0
    }


def _scroll_inbox(device) -> None:
    info = device.info
    w, h = int(info["displayWidth"]), int(info["displayHeight"])
    try:
        device.swipe(
            w // 2 + random.randint(-18, 18),
            int(h * random.uniform(0.74, 0.80)),
            w // 2 + random.randint(-18, 18),
            int(h * random.uniform(0.34, 0.42)),
            random.uniform(0.55, 0.95),
        )
    except Exception:
        pass
    time.sleep(random.uniform(1.6, 3.4))


def run_backfill(cfg: dict | None = None, serial: str | None = None, *, phone_id: str | None = None) -> tuple[bool, str]:
    """Open 5–10 older inbox rows we have no thread for. No feed cover."""
    from src.phone_queue import check_cancel

    cfg = cfg or load_config()
    pid = phone_id or str(cfg.get("phone_id") or DEFAULT_PHONE_ID)
    device, err = _unlock(serial)
    if device is None:
        return False, err
    xml = ensure_messaging(device)
    if not looks_like_messaging(xml):
        return False, "could not open LinkedIn Messaging"
    conn = db_connect(db_path_from_config(cfg))
    opened = 0
    seen_untracked = 0
    try:
        tracked = tracked_thread_names(conn, pid)
        first_names = {hit.name.casefold() for hit in parse_messaging_list(xml) if hit.name}
        allow_first = len(tracked) < 12
        target = random.randint(BACKFILL_OPEN_MIN, BACKFILL_OPEN_MAX)
        time.sleep(random.uniform(2.0, 5.0))
        for _ in range(random.randint(*BACKFILL_SCROLLS)):
            check_cancel()
            if opened >= target:
                break
            _scroll_inbox(device)
            xml = _dismiss_blocker(device, _xml(device))
            if not looks_like_messaging(xml):
                xml = ensure_messaging(device)
            opened_this_screen = False
            for hit in parse_messaging_list(xml):
                if opened >= target or opened_this_screen:
                    break
                key = hit.name.casefold()
                if not plausible_person_name(hit.name) or key in tracked:
                    continue
                if looks_like_inmail_promo(hit.preview):
                    _store_hit(conn, pid, hit.name, hit.preview, "unread" if hit.unread else "", [])
                    continue
                seen_untracked += 1
                if key in first_names and not allow_first:
                    continue
                if random.random() < 0.38:
                    continue
                time.sleep(random.uniform(2.8, 7.5))
                name, msgs, profile = _open_conversation(device, hit)
                if not msgs:
                    continue
                _store_hit(conn, pid, name, hit.preview, "unread" if hit.unread else "", msgs, profile)
                tracked.add(name.casefold())
                opened += 1
                opened_this_screen = True
                time.sleep(random.uniform(7.0, 16.0))
        return True, f"imported {opened} older chats ({seen_untracked} unseen on the list)"
    finally:
        conn.close()


def run_seed(cfg: dict | None = None, serial: str | None = None, *, phone_id: str | None = None) -> tuple[bool, str]:
    return run_backfill(cfg, serial, phone_id=phone_id)


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
                stored[str(row["name"]).casefold()] = {
                    "preview": str(row["preview"] or row["last_text"] or ""),
                    "messages": int(row["message_count"] or 0),
                }
        for hit in hits:
            check_cancel()
            listed += 1
            info = stored.get(hit.name.casefold()) or {}
            empty = int(info.get("messages") or 0) == 0
            _store_hit(conn, pid, hit.name, hit.preview, "unread" if hit.unread else "", [])
            if looks_like_inmail_promo(hit.preview):
                continue
            if opened >= SCAN_OPEN_CAP:
                continue
            if not empty and not should_open_row(hit, info.get("preview")):
                continue
            name, msgs, profile = _open_conversation(device, hit)
            _store_hit(conn, pid, name, hit.preview, "", msgs, profile)
            opened += 1
        return True, f"listed {listed}, opened {opened}, feed reactions {reacted}"
    finally:
        conn.close()


COMPOSE_RID = "com.linkedin.android:id/messaging_keyboard_text_input_container"


def _norm_body(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _already_sent(conn, name: str, phone_id: str, text: str) -> bool:
    from src.phones import phone_scope

    with phone_scope(phone_id):
        return any(
            row["side"] == "you" and _norm_body(str(row["body"])) == _norm_body(text)
            for row in list_thread(conn, name, phone_id=phone_id)
        )


def _record_sent(conn, name: str, phone_id: str, text: str) -> None:
    from src.phones import phone_scope

    with phone_scope(phone_id):
        person_id = upsert_chat(conn, name, last_from="you", last_text=text, badge="", phone_id=phone_id)
        add_message(conn, person_id, "you", text)
        conn.commit()


def _leave_thread(device) -> None:
    try:
        device.press("back")
    except Exception:
        pass
    wait_idle(device, 0.8)


def _leave_search(device) -> None:
    for _ in range(3):
        xml = _xml(device)
        if looks_like_messaging(xml) and find_inbox_search(xml):
            field = device(className="android.widget.EditText")
            try:
                focused = field.exists(timeout=0.3)
            except Exception:
                focused = False
            if focused:
                try:
                    device.press("back")
                except Exception:
                    pass
                wait_idle(device, 0.6)
                continue
            return
        try:
            device.press("back")
        except Exception:
            pass
        wait_idle(device, 0.6)
    ensure_messaging(device)


def _verify_open_name(device, name: str, *, force: bool) -> tuple[bool, str]:
    xml = _xml(device)
    open_name, _ = parse_open_thread(xml)
    if names_match(open_name or "", name):
        return True, ""
    if force and open_name:
        log.warning("FORCE LinkedIn send — open thread is %s, asked for %s", open_name, name)
        return True, ""
    _leave_thread(device)
    return False, f"could not verify the open thread is {name} — NOT sent"


def _search_named_thread(device, name: str, *, force: bool) -> tuple[bool, str]:
    """Inbox Search messages — finds chats that sit below the first 8 screens."""
    xml = ensure_messaging(device)
    point = find_inbox_search(xml)
    if point is None:
        return False, f"could not find {name} in LinkedIn Messaging — NOT sent"
    tap(device, point[0], point[1])
    wait_idle(device, 1.0)
    field = device(className="android.widget.EditText")
    if not field.exists(timeout=2.5):
        field = device(resourceId="com.linkedin.android:id/pill_inbox_search_box")
        if not field.exists(timeout=1.0):
            _leave_search(device)
            return False, f"inbox search field missing — could not find {name}"
    type_into(device, field, name)
    wait_idle(device, 1.5)
    xml = _xml(device)
    open_name, _ = parse_open_thread(xml)
    if open_name and names_match(open_name, name):
        return True, ""
    hit = find_conversation_hit(xml, name)
    if hit is None:
        wait_idle(device, 1.0)
        xml = _xml(device)
        hit = find_conversation_hit(xml, name)
    if hit is None:
        _leave_search(device)
        return False, f"could not find {name} in LinkedIn Messaging — NOT sent"
    tap(device, hit.x, hit.y)
    wait_idle(device, 1.3)
    ok, why = _verify_open_name(device, name, force=force)
    if not ok:
        _leave_search(device)
    return ok, why


def _open_named_thread(device, name: str, *, force: bool) -> tuple[bool, str]:
    xml = ensure_messaging(device)
    open_name, _msgs = parse_open_thread(xml)
    if open_name and names_match(open_name, name):
        return True, ""
    if open_name:
        _leave_thread(device)
        xml = _xml(device)
    for _ in range(8):
        hit = find_conversation_hit(xml, name)
        if hit:
            tap(device, hit.x, hit.y)
            wait_idle(device, 1.3)
            return _verify_open_name(device, name, force=force)
        info = device.info
        w, h = int(info["displayWidth"]), int(info["displayHeight"])
        try:
            device.swipe(w // 2, int(h * 0.72), w // 2, int(h * 0.32), 0.45)
        except Exception:
            pass
        wait_idle(device, 0.7)
        xml = _xml(device)
        if not looks_like_messaging(xml):
            xml = ensure_messaging(device)
    return _search_named_thread(device, name, force=force)


def _send_in_thread(device, text: str) -> bool:
    field = device(resourceId=COMPOSE_RID)
    if not field.exists(timeout=3.0):
        field = device(className="android.widget.EditText")
        if not field.exists(timeout=1.5):
            log.warning("LinkedIn composer not found")
            return False
    field.click()
    wait_idle(device, 0.4)
    type_into(device, field, text)
    wait_idle(device, 0.8)
    xml = _xml(device)
    point = find_send_point(xml)
    if point:
        tap(device, point[0], point[1])
    else:
        btn = device(description="Send")
        if btn.exists(timeout=2.0):
            btn.click()
        else:
            btn = device(resourceId="com.linkedin.android:id/messaging_keyboard_send")
            if btn.exists(timeout=1.0):
                btn.click()
            else:
                log.warning("LinkedIn send button not found")
                try:
                    field.clear_text()
                except Exception:
                    pass
                return False
    for _ in range(6):
        wait_idle(device, 0.6)
        if message_visible(_xml(device), text):
            return True
    log.warning("LinkedIn send tapped but message was not confirmed")
    return False


def send_named_message(
    name: str,
    text: str,
    *,
    serial: str | None = None,
    phone_id: str | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    """Open a LinkedIn conversation on the phone, send `text`, record it."""
    from src.phone_queue import check_cancel
    from src.phones import serial_for

    name = (name or "").strip()
    text = (text or "").strip()
    if not name or not text:
        return False, "name and message required"
    cfg = load_config()
    pid = phone_id or str(cfg.get("phone_id") or DEFAULT_PHONE_ID)
    serial = serial or serial_for(pid)
    conn = db_connect(db_path_from_config(cfg))
    try:
        if _already_sent(conn, name, pid, text):
            return True, f"already sent to {name} — not sent twice"
    finally:
        conn.close()
    device, err = _unlock(serial)
    if device is None:
        return False, err
    check_cancel()
    ok, why = _open_named_thread(device, name, force=force)
    if not ok:
        return False, why
    xml = _xml(device)
    if message_visible(xml, text):
        conn = db_connect(db_path_from_config(cfg))
        try:
            _record_sent(conn, name, pid, text)
        finally:
            conn.close()
        _leave_thread(device)
        return True, f"already visible on phone for {name} — not sent twice"
    if not _send_in_thread(device, text):
        _leave_thread(device)
        return False, "send could not be confirmed — inspect the phone before retrying"
    conn = db_connect(db_path_from_config(cfg))
    try:
        _record_sent(conn, name, pid, text)
    finally:
        conn.close()
    _leave_thread(device)
    return True, f"sent to {name}"


def archive_named(
    name: str,
    *,
    serial: str | None = None,
    phone_id: str | None = None,
) -> tuple[bool, str]:
    """Open the conversation and archive it in the LinkedIn app."""
    from src.phone_queue import check_cancel
    from src.phones import serial_for

    name = (name or "").strip()
    if not name:
        return False, "name required"
    cfg = load_config()
    pid = phone_id or str(cfg.get("phone_id") or DEFAULT_PHONE_ID)
    serial = serial or serial_for(pid)
    device, err = _unlock(serial)
    if device is None:
        return False, err
    check_cancel()
    ok, why = _open_named_thread(device, name, force=False)
    if not ok:
        return False, why
    xml = _xml(device)
    more = find_more_options(xml)
    if more is None:
        _leave_thread(device)
        return False, "could not find More Options on the LinkedIn thread"
    tap(device, more[0], more[1])
    wait_idle(device, 1.0)
    xml = _xml(device)
    action = find_archive_action(xml)
    if action is None:
        _leave_thread(device)
        return False, "Archive was not in the LinkedIn menu — conversation left open"
    tap(device, action[0], action[1])
    wait_idle(device, 1.0)
    return True, f"archived {name} on LinkedIn"


def refresh_named(
    name: str,
    *,
    serial: str | None = None,
    phone_id: str | None = None,
) -> tuple[bool, str]:
    """Open a conversation and replace the stored thread from the start of the chat."""
    from src.phone_queue import check_cancel
    from src.phones import serial_for

    name = (name or "").strip()
    if not name:
        return False, "name required"
    cfg = load_config()
    pid = phone_id or str(cfg.get("phone_id") or DEFAULT_PHONE_ID)
    serial = serial or serial_for(pid)
    device, err = _unlock(serial)
    if device is None:
        return False, err
    check_cancel()
    ok, why = _open_named_thread(device, name, force=False)
    if not ok:
        return False, why
    open_name, msgs, profile = capture_open_thread(device, expected=name)
    _leave_thread(device)
    if not msgs:
        from src.linkedin_store import get_person
        from src.phones import phone_scope

        conn = db_connect(db_path_from_config(cfg))
        try:
            with phone_scope(pid):
                row = get_person(conn, name, pid)
            preview = ""
            if row is not None:
                preview = str(row["preview"] or row["last_text"] or "")
            if looks_like_inmail_promo(preview):
                _store_hit(conn, pid, open_name or name, preview, "", [], profile)
                return True, f"kept InMail preview for {open_name or name} (no thread bubbles)"
        finally:
            conn.close()
        return False, f"opened {name} but captured no messages"
    conn = db_connect(db_path_from_config(cfg))
    try:
        _store_hit(conn, pid, open_name or name, msgs[-1][1], "", msgs, profile)
    finally:
        conn.close()
    return True, f"refreshed {open_name or name} ({len(msgs)} messages)"
