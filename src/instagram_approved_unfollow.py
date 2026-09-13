"""Execute a small, explicitly approved Instagram unfollow batch on Toby's Pixel."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from src.config import ROOT
from src.instagram_prune import (
    GALAXY_MARK,
    PACKAGE,
    PIXEL_SERIAL,
    _dismiss_popups,
    _open_own_following,
    _unlock,
    _xml,
    find_label_point,
    find_unfollow_confirm,
    looks_blocked,
    looks_like_following_list,
    looks_like_login,
)
from src.gestures import tap
from src.screen import _bounds_center

log = logging.getLogger(__name__)

SOURCE = ROOT / "data" / "instagram-approved-unfollows.json"
PROGRESS = ROOT / "data" / "instagram_unfollow_progress_toby.json"
HOLD = ROOT / "data" / "phone_hold_toby"
QUEUE = ROOT / "data" / "action_queue_toby.json"
EXPECTED_COUNT = 634
SUCCESS_PAUSE = (1.0, 3.0)
STOP_WARNING = re.compile(
    r"try again later|action blocked|checkpoint|challenge|unusual activity|"
    r"suspicious|confirm it.?s you|verify (?:it.?s you|your account)|"
    r"log in|login|temporarily blocked|we restrict certain activity",
    re.I,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(value: str) -> str:
    return (value or "").strip().lstrip("@").lower()


def _save(state: dict) -> None:
    state["updated_at"] = _now()
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=PROGRESS.name + ".", dir=PROGRESS.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, PROGRESS)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _load_source() -> tuple[list[str], str]:
    raw = SOURCE.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    data = json.loads(raw)
    handles = [_norm(str(value)) for value in data.get("handles", [])]
    if len(handles) != EXPECTED_COUNT or len(set(handles)) != EXPECTED_COUNT:
        raise RuntimeError(
            f"approved source validation failed: count={len(handles)} unique={len(set(handles))}"
        )
    if any(not re.fullmatch(r"[a-z0-9._]{1,30}", handle) for handle in handles):
        raise RuntimeError("approved source contains an invalid handle")
    return handles, digest


def _load_state(source_hash: str) -> dict:
    if PROGRESS.is_file():
        state = json.loads(PROGRESS.read_text(encoding="utf-8"))
        prior_hash = str(state.get("approved_source_hash") or "")
        if prior_hash and prior_hash != source_hash:
            raise RuntimeError("approved source hash differs from existing progress")
    else:
        state = {}
    state.setdefault("approved_source_hash", source_hash)
    state.setdefault("approved_count", EXPECTED_COUNT)
    state.setdefault("attempted", [])
    state.setdefault("successfully_unfollowed", [])
    state.setdefault("already_not_following", [])
    state.setdefault("failed", [])
    state.setdefault("success_events", [])
    state.setdefault("cooldowns", [])
    state.setdefault("last_cooldown_success_count", 0)
    state.setdefault("created_at", _now())
    state["this_run_successfully_unfollowed"] = []
    state["current_handle"] = None
    state["started_at"] = _now()
    state["finished_at"] = None
    state["stop_reason"] = "running"
    return state


def _queue_is_clear() -> bool:
    if not QUEUE.is_file():
        return True
    try:
        jobs = json.loads(QUEUE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return not any(
        isinstance(job, dict) and job.get("status") in {"queued", "running"}
        for job in (jobs if isinstance(jobs, list) else [])
    )


def _warning(xml: str) -> bool:
    if looks_blocked(xml) or looks_like_login(xml):
        return True
    text = "\n".join(
        (node.attrib.get("text") or "") + " " + (node.attrib.get("content-desc") or "")
        for node in ET.fromstring(xml).iter()
    )
    return bool(STOP_WARNING.search(text))


def _row(xml: str, wanted: str) -> tuple[tuple[int, int] | None, str] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        rid = (node.attrib.get("resource-id") or "").rsplit("/", 1)[-1]
        if rid != "follow_list_container":
            continue
        handle = ""
        button_point = None
        button_label = ""
        for child in node.iter():
            child_rid = (child.attrib.get("resource-id") or "").rsplit("/", 1)[-1]
            text = (child.attrib.get("text") or "").strip()
            desc = (child.attrib.get("content-desc") or "").strip()
            if child_rid == "follow_list_username" and text:
                handle = _norm(text)
            if child_rid == "follow_list_row_large_follow_button":
                button_point = _bounds_center(child.attrib.get("bounds") or "")
                button_label = (text or desc).strip()
                if not button_label:
                    button_label = "Following"
        if handle == wanted:
            return button_point, button_label
    return None


def _search_point(xml: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    candidates: list[tuple[int, tuple[int, int]]] = []
    for node in root.iter():
        text = (node.attrib.get("text") or "").strip().casefold()
        desc = (node.attrib.get("content-desc") or "").strip().casefold()
        rid = (node.attrib.get("resource-id") or "").casefold()
        cls = (node.attrib.get("class") or "").casefold()
        point = _bounds_center(node.attrib.get("bounds") or "")
        if point is None:
            continue
        if "edittext" in cls and ("search" in rid or text or desc == "search"):
            candidates.append((0, point))
        elif "search" in rid and ("input" in rid or "edit" in rid or "field" in rid):
            candidates.append((1, point))
        elif text == "search" or desc == "search":
            candidates.append((2, point))
    return min(candidates, default=(99, None), key=lambda item: item[0])[1]


def _clear_point(xml: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        text = (node.attrib.get("text") or "").strip().casefold()
        desc = (node.attrib.get("content-desc") or "").strip().casefold()
        rid = (node.attrib.get("resource-id") or "").casefold()
        if text not in {"clear", "clear search", "clear text"} and desc not in {
            "clear",
            "clear search",
            "clear text",
        }:
            if not ("search" in rid and ("clear" in rid or "close" in rid)):
                continue
        point = _bounds_center(node.attrib.get("bounds") or "")
        if point:
            return point
    return None


def _clear_search_naturally(device, xml: str, previous: str) -> tuple[str, str]:
    clear = _clear_point(xml)
    if clear is not None:
        tap(device, clear[0], clear[1])
        time.sleep(random.uniform(0.5, 0.9))
        return _xml(device), ""
    point = _search_point(xml)
    if point is None:
        return xml, "Following search field unavailable"
    tap(device, point[0], point[1])
    time.sleep(random.uniform(0.3, 0.6))
    try:
        device.press("move_end")
        for _ in range(len(previous) + 2):
            device.press("delete")
            time.sleep(random.uniform(0.05, 0.12))
    except Exception as exc:
        return _xml(device), f"could not naturally clear search: {exc}"
    return _xml(device), ""


def _search(device, handle: str) -> tuple[str, str]:
    xml = _dismiss_popups(device, _xml(device))
    if _warning(xml):
        return xml, "rate-limit/checkpoint/login warning"
    point = _search_point(xml)
    if point is None and not looks_like_following_list(xml):
        xml, err, _ = _open_own_following(device)
        if err:
            return xml, err
        point = _search_point(xml)
    if point is None:
        return xml, "Following search field unavailable"
    existing = ""
    try:
        root = ET.fromstring(xml)
        for node in root.iter():
            rid = (node.attrib.get("resource-id") or "").casefold()
            cls = (node.attrib.get("class") or "").casefold()
            if "edittext" in cls and "search" in rid:
                existing = (node.attrib.get("text") or "").strip()
                break
    except ET.ParseError:
        pass
    if existing:
        xml, clear_err = _clear_search_naturally(device, xml, existing)
        if clear_err:
            return xml, clear_err
        point = _search_point(xml)
        if point is None:
            return xml, "Following search field unavailable after clearing"
    tap(device, point[0], point[1])
    time.sleep(random.uniform(0.5, 0.9))
    try:
        for char in handle:
            device.send_keys(char, clear=False)
            time.sleep(random.uniform(0.180, 0.350))
    except Exception as exc:
        return _xml(device), f"could not type search handle character: {exc}"
    time.sleep(random.uniform(1.0, 2.0))
    xml = _xml(device)
    if _warning(xml):
        return xml, "rate-limit/checkpoint/login warning"
    return xml, ""


def _record_attempt(state: dict, handle: str) -> None:
    state["current_handle"] = handle
    state["attempted"].append({"handle": handle, "at": _now()})
    _save(state)


def _finish(state: dict, reason: str) -> None:
    state["current_handle"] = None
    state["finished_at"] = _now()
    state["stop_reason"] = reason
    _save(state)


def run(*, serial: str = PIXEL_SERIAL) -> tuple[bool, str]:
    if serial != PIXEL_SERIAL or GALAXY_MARK in serial:
        return False, "refusing non-Pixel serial"
    handles, source_hash = _load_source()
    state = _load_state(source_hash)
    HOLD.touch(exist_ok=True)
    _save(state)
    if not _queue_is_clear():
        _finish(state, "Toby action queue is not empty")
        return False, state["stop_reason"]

    completed = set(map(_norm, state["successfully_unfollowed"]))
    already = set(map(_norm, state["already_not_following"]))
    consecutive_ui_failures = 0

    device, err = _unlock(serial)
    if device is None:
        _finish(state, err)
        return False, err
    xml, err, _ = _open_own_following(device)
    if err or _warning(xml):
        reason = err or "rate-limit/checkpoint/login warning before start"
        _finish(state, reason)
        return False, reason

    for handle in handles:
        if handle in completed or handle in already:
            continue
        if not HOLD.is_file():
            _finish(state, "Pixel hold disappeared")
            return False, state["stop_reason"]
        if not _queue_is_clear():
            _finish(state, "Toby action queue became non-empty")
            return False, state["stop_reason"]

        _record_attempt(state, handle)
        xml, search_err = _search(device, handle)
        if search_err:
            state["failed"].append({"handle": handle, "at": _now(), "reason": search_err})
            consecutive_ui_failures += 1
            state["current_handle"] = None
            _save(state)
            if "warning" in search_err or consecutive_ui_failures >= 2:
                _finish(state, search_err if "warning" in search_err else "two consecutive UI failures")
                return False, state["stop_reason"]
            continue

        match = _row(xml, handle)
        if match is None:
            time.sleep(random.uniform(1.5, 2.5))
            xml = _xml(device)
            if _warning(xml):
                _finish(state, "rate-limit/checkpoint/login warning")
                return False, state["stop_reason"]
            match = _row(xml, handle)
        if match is None:
            state["already_not_following"].append(handle)
            already.add(handle)
            state["current_handle"] = None
            consecutive_ui_failures = 0
            _save(state)
            time.sleep(random.uniform(1.0, 2.2))
            continue

        button_point, button_label = match
        if _norm(button_label) != "following":
            state["already_not_following"].append(handle)
            already.add(handle)
            state["current_handle"] = None
            consecutive_ui_failures = 0
            _save(state)
            continue
        if button_point is None:
            reason = "exact row had no tappable Following button"
            state["failed"].append({"handle": handle, "at": _now(), "reason": reason})
            state["current_handle"] = None
            consecutive_ui_failures += 1
            _save(state)
            if consecutive_ui_failures >= 2:
                _finish(state, "two consecutive UI failures")
                return False, state["stop_reason"]
            continue

        tap(device, button_point[0], button_point[1])
        time.sleep(random.uniform(0.8, 1.3))
        xml = _xml(device)
        if _warning(xml):
            _finish(state, "rate-limit/checkpoint/login warning after Following tap")
            return False, state["stop_reason"]
        confirm = find_unfollow_confirm(xml)
        if confirm is not None:
            tap(device, confirm[0], confirm[1])
            time.sleep(random.uniform(1.2, 1.9))
            xml = _xml(device)
        if _warning(xml):
            _finish(state, "rate-limit/checkpoint/login warning after Unfollow confirmation")
            return False, state["stop_reason"]

        after = _row(xml, handle)
        still_following = after is not None and _norm(after[1]) == "following"
        if still_following:
            time.sleep(2.0)
            xml = _xml(device)
            after = _row(xml, handle)
            still_following = after is not None and _norm(after[1]) == "following"
        if still_following:
            reason = "Following UI remained unchanged after confirmed unfollow"
            state["failed"].append({"handle": handle, "at": _now(), "reason": reason})
            state["current_handle"] = None
            _finish(state, "repeated unchanged UI")
            return False, state["stop_reason"]

        at = _now()
        state["successfully_unfollowed"].append(handle)
        state["success_events"].append({"handle": handle, "at": at})
        state["this_run_successfully_unfollowed"].append(handle)
        completed.add(handle)
        consecutive_ui_failures = 0
        state["current_handle"] = None
        _save(state)
        time.sleep(random.uniform(*SUCCESS_PAUSE))

    unresolved = [
        handle for handle in handles if handle not in completed and handle not in already
    ]
    if unresolved:
        _finish(state, f"pass ended with {len(unresolved)} unresolved handles")
        return False, state["stop_reason"]
    _finish(state, "all approved handles successfully unfollowed or already not-following")
    return True, state["stop_reason"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ok = False
    message = "runner terminated unexpectedly"
    try:
        ok, message = run()
    finally:
        HOLD.unlink(missing_ok=True)
    print(json.dumps({"ok": ok, "message": message, "progress": str(PROGRESS)}))
    raise SystemExit(0 if ok else 1)
