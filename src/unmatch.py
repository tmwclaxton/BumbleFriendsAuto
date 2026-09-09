"""Unmatch or rematch Bumble Friends connections.

Unmatch: overflow ``chatToolbar_overflow`` / ``Chat options`` → ``Unmatch``
(not ``Unmatch and report``), then confirm if a second sheet appears.

Rematch (BFF Premium): tap ``Rematch`` on an expired thread, or tap the silver
``Name, BFF, expired match`` circle in the New friends strip and then Rematch.
"""

from __future__ import annotations

import logging
import re
from xml.etree import ElementTree as ET

from src.chats import is_expired_rematch_overlay
from src.config import load_config
from src.device import bring_app_foreground, connect, dump_hierarchy, wait_idle
from src.gestures import tap
from src.messenger import leave_chat
from src.phones import phone_scope, serial_for
from src.store import (
    connect as db_connect,
    db_path_from_config,
    delete_person,
    list_expired_names,
)
from src.sync_chats import open_chat_from_list, open_chat_via_search, recover_to_list
from src.unlock import sleep_screen, wake_and_unlock

log = logging.getLogger(__name__)

_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _center(bounds: str) -> tuple[int, int] | None:
    match = _BOUNDS.fullmatch(bounds or "")
    if not match:
        return None
    x1, y1, x2, y2 = map(int, match.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def find_labeled_point(
    xml: str,
    *,
    texts: tuple[str, ...] = (),
    descs: tuple[str, ...] = (),
    rids: tuple[str, ...] = (),
) -> tuple[int, int] | None:
    """First clickable node whose text/desc/id matches exactly (casefold)."""
    want_t = {t.casefold() for t in texts}
    want_d = {d.casefold() for d in descs}
    want_r = {r.casefold() for r in rids}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        rid = (node.attrib.get("resource-id") or "").split("/")[-1].casefold()
        text = (node.attrib.get("text") or "").strip().casefold()
        desc = (node.attrib.get("content-desc") or "").strip().casefold()
        click = (node.attrib.get("clickable") or "").lower() == "true"
        hit = (text and text in want_t) or (desc and desc in want_d) or (rid and rid in want_r)
        if not hit:
            continue
        # Never take "Unmatch and report" when looking for Unmatch.
        if "unmatch" in want_t or "unmatch" in want_d:
            if "report" in text or "report" in desc:
                continue
        point = _center(node.attrib.get("bounds") or "")
        if point and (click or text in want_t or desc in want_d):
            return point
    return None


def find_label_contains(
    xml: str,
    needles: tuple[str, ...],
    *,
    skip: tuple[str, ...] = ("report",),
) -> tuple[int, int] | None:
    """First node whose text/desc contains any needle (casefold)."""
    want = tuple(n.casefold() for n in needles if n)
    skip_l = tuple(s.casefold() for s in skip)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        rid = (node.attrib.get("resource-id") or "").split("/")[-1].casefold()
        text = (node.attrib.get("text") or "").strip().casefold()
        desc = (node.attrib.get("content-desc") or "").strip().casefold()
        click = (node.attrib.get("clickable") or "").lower() == "true"
        blob = f"{rid} {text} {desc}"
        if any(s and s in blob for s in skip_l):
            continue
        if not any(n in blob for n in want):
            continue
        point = _center(node.attrib.get("bounds") or "")
        if point and (click or any(n in text or n in desc for n in want)):
            return point
    return None


def _tap_chat_options(device) -> bool:
    xml = dump_hierarchy(device)
    point = find_labeled_point(
        xml,
        descs=("chat options",),
        rids=("chatToolbar_overflow",),
    )
    if point is None:
        log.warning("chat options not on screen")
        return False
    tap(device, point[0], point[1])
    wait_idle(device, 1.0)
    return True


def _tap_unmatch_sheet(device) -> bool:
    xml = dump_hierarchy(device)
    point = find_labeled_point(xml, texts=("unmatch",), descs=("unmatch",))
    if point is None:
        log.warning("Unmatch row not on sheet")
        return False
    tap(device, point[0], point[1])
    wait_idle(device, 1.2)
    return True


def _confirm_unmatch(device) -> None:
    xml = dump_hierarchy(device)
    blob = xml.lower()
    if "unmatch" not in blob and "are you sure" not in blob:
        return
    point = find_labeled_point(
        xml,
        texts=("unmatch", "yes", "confirm"),
        descs=("unmatch", "yes", "confirm"),
    )
    if point is None:
        return
    tap(device, point[0], point[1])
    wait_idle(device, 1.4)


def unmatch_open_thread(device) -> bool:
    if not _tap_chat_options(device):
        return False
    if not _tap_unmatch_sheet(device):
        device.press("back")
        wait_idle(device, 0.5)
        return False
    _confirm_unmatch(device)
    return True


def unmatch_named(
    name: str,
    *,
    serial: str | None = None,
    phone_id: str | None = None,
    sleep_after: bool = True,
) -> tuple[bool, str]:
    name = (name or "").strip()
    if not name:
        return False, "name required"
    from src.phone_queue import check_cancel

    cfg = load_config()
    package = str(cfg["package"])
    serial = serial or serial_for(phone_id)
    with phone_scope(phone_id):
        device = connect(serial)
        try:
            if not wake_and_unlock(device, serial=serial):
                return False, "phone still locked — unlock failed"
            check_cancel()
            bring_app_foreground(device, package)
            wait_idle(device, 0.6)
            recover_to_list(device, package)
            partner = open_chat_via_search(device, package, name) or open_chat_from_list(
                device, package, name
            )
            conn = db_connect(db_path_from_config(cfg))
            try:
                if not partner:
                    if delete_person(conn, name):
                        return True, f"{name} was already gone on the phone — removed from inbox"
                    return False, f"could not open {name}"
                check_cancel()
                if not unmatch_open_thread(device):
                    leave_chat(device)
                    return False, f"could not unmatch {name}"
                delete_person(conn, name)
            finally:
                conn.close()
            recover_to_list(device, package)
            return True, f"unmatched {name}"
        finally:
            if sleep_after:
                try:
                    sleep_screen(device, serial=serial)
                except Exception:
                    log.warning("could not sleep screen after unmatch")


def unmatch_expired(
    *,
    serial: str | None = None,
    phone_id: str | None = None,
    sleep_after: bool = True,
    new_friends_only: bool = False,
) -> tuple[bool, str]:
    from src.phone_queue import check_cancel

    cfg = load_config()
    package = str(cfg["package"])
    serial = serial or serial_for(phone_id)
    with phone_scope(phone_id):
        conn = db_connect(db_path_from_config(cfg))
        try:
            names = list_expired_names(conn, new_friends_only=new_friends_only)
        finally:
            conn.close()
        if not names:
            return True, "no expired connections"
        device = connect(serial)
        ok_n = 0
        missing: list[str] = []
        try:
            if not wake_and_unlock(device, serial=serial):
                return False, "phone still locked — unlock failed"
            bring_app_foreground(device, package)
            wait_idle(device, 0.6)
            recover_to_list(device, package)
            for name in names:
                check_cancel()
                partner = open_chat_via_search(device, package, name) or open_chat_from_list(
                    device, package, name
                )
                conn = db_connect(db_path_from_config(cfg))
                try:
                    if not partner:
                        if delete_person(conn, name):
                            ok_n += 1
                            missing.append(name)
                            log.info("expired %s already gone — dropped locally", name)
                        continue
                    if unmatch_open_thread(device):
                        delete_person(conn, name)
                        ok_n += 1
                        log.info("unmatched expired %s", name)
                    else:
                        leave_chat(device)
                        log.warning("skip unmatch %s", name)
                finally:
                    conn.close()
                recover_to_list(device, package)
        finally:
            if sleep_after:
                try:
                    sleep_screen(device, serial=serial)
                except Exception:
                    log.warning("could not sleep screen after expired unmatch")
        extra = f" ({len(missing)} already gone)" if missing else ""
        scope = " expired new friends" if new_friends_only else " expired"
        return True, f"unmatched {ok_n}/{len(names)}{scope}{extra}"


_REMATCH_NEEDLES = ("rematch", "extend match", "use rematch")
_PAYWALL_HINTS = (
    "get premium",
    "bumble premium",
    "subscribe now",
    "unlock rematch",
    "premium to rematch",
)


def _same_person(stored: str, seen: str) -> bool:
    a, b = stored.strip().casefold(), seen.strip().casefold()
    if not a or not b:
        return False
    return a == b or a.startswith(b + " ") or b.startswith(a + " ")


def _paywall_visible(xml: str) -> bool:
    blob = xml.lower()
    return any(h in blob for h in _PAYWALL_HINTS)


def rematch_on_screen(device) -> tuple[bool, str]:
    xml = dump_hierarchy(device)
    if _paywall_visible(xml) and find_label_contains(xml, _REMATCH_NEEDLES) is None:
        return False, "premium paywall"
    point = find_labeled_point(
        xml,
        texts=("rematch",),
        descs=("rematch",),
        rids=("myProfilePreview_rightButton",),
    )
    if point is None:
        point = find_label_contains(xml, _REMATCH_NEEDLES)
    if point is None:
        return False, "no rematch control"
    tap(device, point[0], point[1])
    wait_idle(device, 1.4)
    xml = dump_hierarchy(device)
    if _paywall_visible(xml):
        device.press("back")
        wait_idle(device, 0.5)
        return False, "rematch needs Premium"
    blob = xml.lower()
    if "are you sure" in blob:
        confirm = find_labeled_point(
            xml,
            texts=("yes", "confirm", "continue", "rematch"),
            descs=("yes", "confirm", "continue", "rematch"),
        )
        if confirm is not None:
            tap(device, confirm[0], confirm[1])
            wait_idle(device, 1.4)
            xml = dump_hierarchy(device)
            if _paywall_visible(xml):
                device.press("back")
                wait_idle(device, 0.5)
                return False, "rematch needs Premium"
    return True, "rematched"


def _refresh_photo_after_rematch(device, name: str) -> None:
    """Replace the grey expired avatar with the live profile photo."""
    from src.sync_chats import _maybe_grab_profile_photo

    wait_idle(device, 0.8)
    try:
        _maybe_grab_profile_photo(device, name, force=True)
    except Exception:
        log.warning("profile photo refresh after rematch failed for %s", name, exc_info=True)


def _open_expired_strip_match(device, package: str, name: str) -> bool:
    from src.chats import list_new_friends
    from src.sync_chats import (
        _STRIP_RID,
        _go_top_of_inbox,
        _scroll_new_friends_strip,
        _screen_size,
        recover_to_list,
    )

    width, height = _screen_size(device)
    xml = _go_top_of_inbox(device, package, width, height)
    rv = device(resourceId=_STRIP_RID)
    if rv.exists:
        try:
            rv.fling.horiz.toBeginning()
            wait_idle(device, 0.6)
        except Exception:
            _scroll_new_friends_strip(device, xml, width, height, toward_end=False)
            _scroll_new_friends_strip(device, xml, width, height, toward_end=False)
    stagnant = 0
    last_key: tuple[str, ...] | None = None
    for _ in range(40):
        xml = dump_hierarchy(device)
        friends = list_new_friends(xml)
        for friend in friends:
            if friend.expired and _same_person(name, friend.name):
                tap(device, friend.x, friend.y)
                wait_idle(device, 1.2)
                return True
        key = tuple(f"{f.name}:{int(f.expired)}" for f in friends)
        if key == last_key:
            stagnant += 1
        else:
            stagnant = 0
            last_key = key
        if stagnant >= 5:
            break
        _scroll_new_friends_strip(device, xml, width, height, toward_end=True)
    recover_to_list(device, package)
    return False


def rematch_visible_strip_expired(device, package: str, conn=None) -> tuple[int, list[str]]:
    """Rematch every expired circle currently in the New friends strip.

    Walks the live strip (not the SQLite expired list) so we only tap people
    Bumble is still offering Rematch for. Stops if Premium is required.
    """
    from src.chats import list_new_friends
    from src.phone_queue import check_cancel
    from src.store import mark_person_rematched
    from src.sync_chats import (
        _STRIP_RID,
        _go_top_of_inbox,
        _scroll_new_friends_strip,
        _screen_size,
    )

    width, height = _screen_size(device)
    xml = _go_top_of_inbox(device, package, width, height)
    rv = device(resourceId=_STRIP_RID)
    if rv.exists:
        try:
            rv.fling.horiz.toBeginning()
            wait_idle(device, 0.6)
        except Exception:
            _scroll_new_friends_strip(device, xml, width, height, toward_end=False)
            _scroll_new_friends_strip(device, xml, width, height, toward_end=False)

    rematched: list[str] = []
    attempted: set[str] = set()
    stagnant = 0
    last_key: tuple[str, ...] | None = None
    for _ in range(50):
        check_cancel()
        xml = dump_hierarchy(device)
        friends = list_new_friends(xml)
        pending = [f for f in friends if f.expired and f.name not in attempted]
        if pending:
            friend = pending[0]
            attempted.add(friend.name)
            log.info("strip rematch %s @ (%s,%s)", friend.name, friend.x, friend.y)
            tap(device, friend.x, friend.y)
            wait_idle(device, 1.2)
            ok, detail = rematch_on_screen(device)
            if ok:
                _refresh_photo_after_rematch(device, friend.name)
                if conn is not None:
                    try:
                        mark_person_rematched(conn, friend.name)
                    except Exception:
                        log.debug("mark rematched %s skipped", friend.name, exc_info=True)
                rematched.append(friend.name)
                log.info("rematched strip %s", friend.name)
            else:
                log.warning("strip rematch skip %s (%s)", friend.name, detail)
                device.press("back")
                wait_idle(device, 0.4)
                if detail in {"premium paywall", "rematch needs Premium"}:
                    recover_to_list(device, package)
                    break
            recover_to_list(device, package)
            xml = _go_top_of_inbox(device, package, width, height)
            if rv.exists:
                try:
                    rv.fling.horiz.toBeginning()
                    wait_idle(device, 0.5)
                except Exception:
                    pass
            stagnant = 0
            last_key = None
            continue
        key = tuple(f"{f.name}:{int(f.expired)}" for f in friends)
        if key == last_key:
            stagnant += 1
        else:
            stagnant = 0
            last_key = key
        if stagnant >= 5:
            break
        _scroll_new_friends_strip(device, xml, width, height, toward_end=True)
    log.info("strip rematch done: %d/%d — %s", len(rematched), len(attempted), ", ".join(rematched) or "-")
    return len(rematched), rematched


def rematch_named(
    name: str,
    *,
    serial: str | None = None,
    phone_id: str | None = None,
    sleep_after: bool = True,
) -> tuple[bool, str]:
    name = (name or "").strip()
    if not name:
        return False, "name required"
    from src.phone_queue import check_cancel
    from src.store import mark_person_rematched

    cfg = load_config()
    package = str(cfg["package"])
    serial = serial or serial_for(phone_id)
    with phone_scope(phone_id):
        device = connect(serial)
        try:
            if not wake_and_unlock(device, serial=serial):
                return False, "phone still locked — unlock failed"
            check_cancel()
            bring_app_foreground(device, package)
            wait_idle(device, 0.6)
            recover_to_list(device, package)
            ok, detail = _rematch_named_on_device(device, package, name)
            if ok:
                conn = db_connect(db_path_from_config(cfg))
                try:
                    mark_person_rematched(conn, name)
                finally:
                    conn.close()
                recover_to_list(device, package)
                return True, f"rematched {name}"
            return False, detail
        finally:
            if sleep_after:
                try:
                    sleep_screen(device, serial=serial)
                except Exception:
                    log.warning("could not sleep screen after rematch")


def _rematch_named_on_device(device, package: str, name: str) -> tuple[bool, str]:
    from src.phone_queue import check_cancel

    check_cancel()
    # Silver expired circles first (new friends). Then a short list hunt.
    # Search hides expired rows.
    if _open_expired_strip_match(device, package, name):
        ok, detail = rematch_on_screen(device)
        if ok:
            _refresh_photo_after_rematch(device, name)
            leave_chat(device)
            return True, detail
        device.press("back")
        wait_idle(device, 0.5)
        recover_to_list(device, package)
        if ok is False and detail != "no rematch control":
            return False, detail
    partner = open_chat_from_list(device, package, name, max_loops=18, max_stagnant=3)
    xml = dump_hierarchy(device)
    if partner or is_expired_rematch_overlay(xml):
        ok, detail = rematch_on_screen(device)
        if ok:
            _refresh_photo_after_rematch(device, name)
            leave_chat(device)
            return True, detail
        leave_chat(device)
        recover_to_list(device, package)
        return False, detail
    return False, f"could not open {name} to rematch"


def rematch_expired(
    *,
    serial: str | None = None,
    phone_id: str | None = None,
    sleep_after: bool = True,
    new_friends_only: bool = False,
) -> tuple[bool, str]:
    from src.phone_queue import check_cancel
    from src.store import mark_person_rematched

    cfg = load_config()
    package = str(cfg["package"])
    serial = serial or serial_for(phone_id)
    with phone_scope(phone_id):
        conn = db_connect(db_path_from_config(cfg))
        try:
            names = list_expired_names(
                conn, new_friends_only=new_friends_only, rematchable=True
            )
        finally:
            conn.close()
        if not names:
            return True, "no rematchable expired connections"
        log.info("rematch expired queue: %s", ", ".join(names))
        device = connect(serial)
        ok_n = 0
        failed: list[str] = []
        try:
            if not wake_and_unlock(device, serial=serial):
                return False, "phone still locked — unlock failed"
            bring_app_foreground(device, package)
            wait_idle(device, 0.6)
            recover_to_list(device, package)
            for name in names:
                check_cancel()
                log.info("trying rematch %s", name)
                ok, detail = _rematch_named_on_device(device, package, name)
                if ok:
                    conn = db_connect(db_path_from_config(cfg))
                    try:
                        mark_person_rematched(conn, name)
                    finally:
                        conn.close()
                    ok_n += 1
                    log.info("rematched expired %s", name)
                else:
                    conn = db_connect(db_path_from_config(cfg))
                    try:
                        if delete_person(conn, name):
                            ok_n += 1
                            log.info(
                                "could not rematch %s (%s) — dropped locally",
                                name,
                                detail,
                            )
                        else:
                            failed.append(f"{name}: {detail}")
                            log.warning("skip rematch %s (%s)", name, detail)
                    finally:
                        conn.close()
                recover_to_list(device, package)
        finally:
            if sleep_after:
                try:
                    sleep_screen(device, serial=serial)
                except Exception:
                    log.warning("could not sleep screen after expired rematch")
        scope = " expired new friends" if new_friends_only else " expired"
        extra = f" (failed: {'; '.join(failed[:4])})" if failed else ""
        return True, f"rematched {ok_n}/{len(names)}{scope}{extra}"
