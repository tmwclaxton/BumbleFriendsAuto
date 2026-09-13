"""Like recent person posts on Toby's Instagram Following feed (Pixel only)."""

from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.config import ROOT
from src.instagram_screen import (
    FeedPost,
    find_back_button,
    find_feed_mode_button,
    find_following_chip,
    find_home_tab,
    find_label_point,
    looks_like_following_feed,
    looks_like_home_chrome,
    looks_like_nested_viewer,
    parse_feed_posts,
    parse_unseen_post,
)
from src.phones import DEFAULT_PHONE_ID, normalize_phone_id, serial_for

log = logging.getLogger(__name__)

PREFS_PATH = ROOT / "data" / "instagram_feed_prefs.json"
SESSION_PATH = ROOT / "data" / "instagram_feed_last.json"
PACKAGE = "com.instagram.android"
PIXEL_SERIAL = "29081FDH200GZ8"
_LONDON = ZoneInfo("Europe/London")
_VISION_RE = None
_PAGE_HANDLES = {
    "thebabylonbee",
    "googleplay",
    "worldoftshirts",
    "skool_com",
    "the.alchemist",
    "lifesavinginfo",
    "monkeque",
    "lidlgb",
    "psychedelicarchives",
    "base44.app",
    "easyjet",
    "indeed.uk",
    "stoicthinkerz",
    "pridepersonality",
    "mindset.therapy",
    "nirealnews",
    "fireawayhigh.w",
    "reforgedera",
    "pintsofknowledge",
}
_PAGE_MARKERS = (".app", "memes", "quotes")


def default_prefs() -> dict[str, Any]:
    return {
        "phone_id": "toby",
        "min_posts": 40,
        "max_posts": 80,
        "max_age_hours": 168,
        "require_person": True,
        "skip_memes": True,
        "skip_sponsored": True,
        "pause_min_ms": 1600,
        "pause_max_ms": 3800,
    }


def _clamp_int(value: object, lo: int, hi: int, default: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def normalize_prefs(raw: dict | None) -> dict[str, Any]:
    base = default_prefs()
    data = dict(raw or {})
    lo = _clamp_int(data.get("min_posts"), 10, 80, base["min_posts"])
    hi = _clamp_int(data.get("max_posts"), 20, 120, base["max_posts"])
    if hi < lo:
        hi = lo
    return {
        "phone_id": "toby",
        "min_posts": lo,
        "max_posts": hi,
        "max_age_hours": _clamp_int(data.get("max_age_hours"), 1, 24 * 30, base["max_age_hours"]),
        "require_person": _as_bool(data.get("require_person"), True),
        "skip_memes": _as_bool(data.get("skip_memes"), True),
        "skip_sponsored": _as_bool(data.get("skip_sponsored"), True),
        "pause_min_ms": _clamp_int(data.get("pause_min_ms"), 400, 15000, base["pause_min_ms"]),
        "pause_max_ms": _clamp_int(data.get("pause_max_ms"), 400, 20000, base["pause_max_ms"]),
    }


def load_prefs() -> dict[str, Any]:
    if not PREFS_PATH.is_file():
        return default_prefs()
    try:
        data = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_prefs()
    return normalize_prefs(data if isinstance(data, dict) else {})


def save_prefs(raw: dict | None) -> dict[str, Any]:
    prefs = normalize_prefs(raw)
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREFS_PATH.write_text(json.dumps(prefs, indent=2) + "\n", encoding="utf-8")
    return prefs


def last_session() -> dict[str, Any]:
    if not SESSION_PATH.is_file():
        return {}
    try:
        data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_session(payload: dict[str, Any]) -> None:
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def live_serial(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    return serial_for("toby") or PIXEL_SERIAL


def serial_is_pixel(serial: str | None) -> bool:
    blob = (serial or "").lower()
    return "29081fdh200gz8" in blob or "panther" in blob


def parse_feed_vision(text: str) -> dict[str, Any]:
    import re

    global _VISION_RE
    if _VISION_RE is None:
        _VISION_RE = re.compile(
            r"kind\s*=\s*(person|meme|ad|other)\b(?:.*?\bperson\s*=\s*(yes|no))?",
            re.I | re.S,
        )
    blob = text or ""
    match = _VISION_RE.search(blob)
    if not match:
        low = blob.lower()
        if any(w in low for w in ("quote", "cartoon", "screenshot", "graphic")):
            return {"kind": "meme", "person": False}
        return {}
    kind = match.group(1).lower()
    person = (match.group(2) or ("yes" if kind == "person" else "no")).lower()
    return {"kind": kind, "person": person == "yes"}


def classify_feed_image(path: Path) -> dict[str, Any]:
    from src.ethnicity_vision import api_key, vision_model

    key = api_key()
    if not key:
        return {"error": "no vision key"}
    raw = path.read_bytes()
    if len(raw) < 80:
        return {"error": "empty screenshot"}
    import base64
    import urllib.error
    import urllib.request

    from src.config import load_config

    data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
    payload = {
        "model": vision_model(load_config()),
        "temperature": 0,
        "max_tokens": 40,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "This is an Instagram Following-feed screenshot. "
                            "Is the main post a real person (selfie, friends, everyday photo of people) "
                            "or a meme / screenshot / cartoon / quote card / brand graphic?\n"
                            "Reply with ONE line only:\n"
                            "kind=<person|meme|ad|other>; person=<yes|no>\n"
                            "person=yes only when a real human is the subject, not a meme page."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    req = urllib.request.Request(
        "https://nano-gpt.com/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    last_err = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            last_err = ""
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            return {"error": f"NanoGPT {exc.code}: {detail}"}
        except Exception as exc:
            last_err = str(exc)
            log.warning("feed vision dropped (%s); retry %s", exc, attempt + 1)
            time.sleep(0.8 + attempt)
    else:
        return {"error": last_err or "vision failed"}
    try:
        text = str(body["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return {"error": "vision response missing text"}
    parsed = parse_feed_vision(text)
    if not parsed:
        return {"error": f"unparsed vision: {text[:120]}"}
    return parsed


def looks_like_page_handle(handle: str) -> bool:
    raw = (handle or "").strip().lstrip("@").casefold()
    if not raw:
        return False
    if raw in _PAGE_HANDLES:
        return True
    if raw.endswith("news") or raw.endswith(".app"):
        return True
    return any(mark in raw for mark in _PAGE_MARKERS)


def decide_post(post: FeedPost, vision: dict[str, Any], prefs: dict[str, Any]) -> tuple[str, str]:
    """Return (action, reason) where action is like or skip."""
    if post.sponsored and prefs.get("skip_sponsored", True):
        return "skip", "sponsored"
    if looks_like_page_handle(post.handle):
        return "skip", "creator page"
    if post.already_liked:
        return "skip", "already liked"
    max_age = float(prefs.get("max_age_hours") or 168)
    if post.age_hours is None:
        # Following Reels often omit the stamp; the tab is already recent.
        post.age_hours = 0.0
        if not post.age_label:
            post.age_label = "following"
    if post.age_hours >= max_age:
        return "skip", f"older than {int(max_age / 24)} days ({post.age_label})"
    if vision.get("error"):
        return "skip", str(vision["error"])
    kind = str(vision.get("kind") or "")
    is_person = bool(vision.get("person")) or kind == "person"
    if prefs.get("require_person", True) and not is_person:
        return "skip", f"{kind or 'not a person'}"
    if prefs.get("skip_memes", True) and kind == "meme":
        return "skip", "meme"
    if kind == "ad":
        return "skip", "ad"
    return "like", "person · recent"


def _job_overrides(job_text: str) -> dict[str, Any]:
    raw = (job_text or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_feed(
    serial: str | None = None,
    phone_id: str | None = None,
    job_text: str = "",
) -> tuple[bool, str]:
    pid = normalize_phone_id(phone_id) if phone_id else DEFAULT_PHONE_ID
    if pid not in {"toby", "pixel"}:
        msg = "Instagram feed likes are Pixel / Toby only — Galaxy is not logged in"
        _save_session(
            {
                "ok": False,
                "error": msg,
                "summary": msg,
                "started_at": _iso_now(),
                "finished_at": _iso_now(),
                "likes": 0,
                "skips": 0,
                "reviewed": 0,
                "cards": [],
                "filter": "person · under 1 week",
            }
        )
        return False, msg
    serial = live_serial(serial)
    if not serial_is_pixel(serial):
        msg = "Instagram feed likes stay on the Pixel"
        _save_session(
            {
                "ok": False,
                "error": msg,
                "summary": msg,
                "started_at": _iso_now(),
                "finished_at": _iso_now(),
                "likes": 0,
                "skips": 0,
                "reviewed": 0,
                "cards": [],
                "filter": "person · under 1 week",
            }
        )
        return False, msg

    prefs = load_prefs()
    extra = _job_overrides(job_text)
    if extra:
        prefs = normalize_prefs({**prefs, **extra})
    target = int(prefs["max_posts"])
    if extra.get("limit"):
        target = _clamp_int(extra.get("limit"), 8, 120, target)
    started = _iso_now()
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    likes = 0
    skips = 0
    error = ""

    from src.device import bring_app_foreground, dump_hierarchy, wait_idle
    from src.gestures import tap
    from src.instagram_prune import _dismiss_popups, _unlock, looks_blocked, looks_like_login
    from src.phone_queue import check_cancel
    from src.swipe_vision import screenshot_card

    device, unlock_err = _unlock(serial)
    if device is None:
        error = unlock_err or "unlock failed"
        payload = _session_payload(started, False, error, likes, skips, cards, target)
        _save_session(payload)
        return False, error

    try:
        bring_app_foreground(device, PACKAGE)
        wait_idle(device, 1.4)
        xml = _dismiss_popups(device, dump_hierarchy(device))
        if looks_like_login(xml):
            raise RuntimeError("Instagram login wall on Pixel")
        if looks_blocked(xml):
            raise RuntimeError("Instagram action block")
        xml = _open_following_feed(device, xml)
        if looks_like_login(xml):
            raise RuntimeError("Instagram login wall on Pixel")
        if not parse_feed_posts(xml):
            _launch_instagram_home(device)
            wait_idle(device, 1.4)
            xml = _open_following_feed(device, dump_hierarchy(device))
        if not looks_like_following_feed(xml) and find_following_chip(xml) is None:
            log.warning("following chip not confirmed — continuing on current feed")
        if not parse_feed_posts(xml):
            raise RuntimeError("Following feed opened but no likeable posts were visible")

        stuck = 0
        loops = 0
        handle_counts: dict[str, int] = {}
        while len(cards) < target and loops < max(target * 6, 240):
            loops += 1
            check_cancel()
            xml = _dismiss_popups(device, dump_hierarchy(device))
            if looks_blocked(xml):
                raise RuntimeError("Instagram action block")
            if looks_like_login(xml):
                raise RuntimeError("Instagram login wall on Pixel")
            post = parse_unseen_post(xml, seen)
            if post is None:
                stuck += 1
                _scroll_feed(device, large=True)
                if stuck in {6, 12, 18, 24}:
                    xml = _open_following_feed(device, xml)
                    for _ in range(3):
                        _scroll_feed(device, large=True)
                if stuck >= 36:
                    error = "could not find more Following posts"
                    break
                continue
            if handle_counts.get(post.handle, 0) >= 2:
                seen.add(post.fingerprint)
                _scroll_feed(device, large=True)
                continue
            stuck = 0
            seen.add(post.fingerprint)
            handle_counts[post.handle] = handle_counts.get(post.handle, 0) + 1
            vision: dict[str, Any] = {}
            skip_vision = post.sponsored or looks_like_page_handle(post.handle)
            if skip_vision:
                vision = {"kind": "ad" if post.sponsored else "other", "person": False}
            else:
                shot = None
                try:
                    shot = screenshot_card(device)
                    vision = classify_feed_image(shot)
                except Exception as exc:
                    vision = {"error": str(exc)}
                finally:
                    if shot is not None:
                        try:
                            os.unlink(shot)
                        except OSError:
                            pass
            action, reason = decide_post(post, vision, prefs)
            if action == "like" and post.like_xy:
                tap(device, post.like_xy[0], post.like_xy[1])
                likes += 1
                wait_idle(device, 0.6)
            else:
                skips += 1
            cards.append(
                {
                    "handle": post.handle,
                    "action": action,
                    "kind": vision.get("kind") or "",
                    "age": post.age_label,
                    "reason": reason,
                }
            )
            log.info(
                "instagram feed %s @%s %s — %s",
                action,
                post.handle,
                post.age_label,
                reason,
            )
            if len(cards) == 1 or len(cards) % 4 == 0:
                _save_session(_session_payload(started, True, "", likes, skips, cards, target))
            if skip_vision:
                time.sleep(random.uniform(0.35, 0.7))
                _scroll_feed(device, large=True)
            else:
                _pause(prefs)
                _scroll_feed(device, large=False)
                _pause(prefs)
    except Exception as exc:
        error = str(exc)
        log.exception("instagram feed failed")

    fatal = any(mark in error.lower() for mark in ("login wall", "action block", "still locked"))
    ok = (not error) or (bool(cards) and not fatal)
    payload = _session_payload(started, ok, error, likes, skips, cards, target)
    _save_session(payload)
    return ok, payload["summary"]


def _session_payload(
    started: str,
    ok: bool,
    error: str,
    likes: int,
    skips: int,
    cards: list[dict[str, Any]],
    target: int,
) -> dict[str, Any]:
    reviewed = len(cards)
    handles = []
    seen_handles: set[str] = set()
    for card in cards:
        handle = str(card.get("handle") or "").strip()
        if handle and handle not in seen_handles:
            seen_handles.add(handle)
            handles.append(handle)
    liked = [str(c.get("handle") or "") for c in cards if c.get("action") == "like" and c.get("handle")]
    friends = [
        handle
        for handle in handles
        if handle and not looks_like_page_handle(handle)
        and not any(c.get("handle") == handle and c.get("reason") == "sponsored" for c in cards)
    ]
    if error and not reviewed:
        summary = error
    else:
        summary = (
            f"Walked {reviewed} Following posts (aim {target}) across {len(handles)} accounts: "
            f"{likes} liked, {skips} skipped"
            + (f", {len(friends)} look like people you follow" if friends else ", no friend posts yet")
            + (f" — {error}" if error else "")
            + "."
        )
    return {
        "ok": ok,
        "error": error,
        "summary": summary,
        "started_at": started,
        "finished_at": _iso_now(),
        "likes": likes,
        "skips": skips,
        "reviewed": reviewed,
        "accounts": len(handles),
        "handles": handles,
        "friend_handles": friends,
        "liked_handles": liked,
        "target": target,
        "cards": cards,
        "filter": "person · under 1 week · Following",
        "phone_id": "toby",
    }


def _open_following_feed(device, xml: str) -> str:
    from src.device import dump_hierarchy, wait_idle
    from src.gestures import tap
    from src.instagram_prune import _dismiss_popups

    xml = _leave_nested_screens(device, xml)
    if not looks_like_home_chrome(xml):
        _launch_instagram_home(device)
        wait_idle(device, 1.6)
        xml = _dismiss_popups(device, dump_hierarchy(device))
        xml = _leave_nested_screens(device, xml)
    home = find_home_tab(xml)
    if home:
        tap(device, home[0], home[1])
        wait_idle(device, 1.0)
        xml = _dismiss_popups(device, dump_hierarchy(device))
    if looks_like_following_feed(xml) and parse_feed_posts(xml):
        return xml
    mode = find_feed_mode_button(xml)
    if mode:
        tap(device, mode[0], mode[1])
        wait_idle(device, 1.1)
        xml = _dismiss_popups(device, dump_hierarchy(device))
    chip = find_following_chip(xml)
    if chip:
        tap(device, chip[0], chip[1])
        wait_idle(device, 1.2)
        xml = _dismiss_popups(device, dump_hierarchy(device))
    elif not looks_like_following_feed(xml):
        alt = find_label_point(xml, "Following")
        if alt:
            tap(device, alt[0], alt[1])
            wait_idle(device, 1.2)
            xml = _dismiss_popups(device, dump_hierarchy(device))
    return xml


def _leave_nested_screens(device, xml: str) -> str:
    from src.device import dump_hierarchy, wait_idle
    from src.gestures import tap
    from src.instagram_prune import _dismiss_popups

    for _ in range(6):
        if looks_like_nested_viewer(xml) or not looks_like_home_chrome(xml):
            back = find_back_button(xml)
            if not back:
                break
            tap(device, *back)
            wait_idle(device, 0.9)
            xml = _dismiss_popups(device, dump_hierarchy(device))
            continue
        return xml
    return xml


def _launch_instagram_home(device) -> None:
    for activity in (
        "com.instagram.android.activity.MainTabActivity",
        "com.instagram.mainactivity.InstagramMainActivity",
    ):
        try:
            device.app_start(PACKAGE, activity=activity, wait=True, stop=False)
            return
        except Exception:
            continue
    try:
        device.app_start(PACKAGE, wait=True, stop=False)
    except Exception:
        log.warning("could not relaunch Instagram home", exc_info=True)


def _scroll_feed(device, *, large: bool = False) -> None:
    from src.gestures import _adb_swipe

    try:
        info = device.info or {}
        width = int(info.get("displayWidth") or 1080)
        height = int(info.get("displayHeight") or 2400)
    except Exception:
        width, height = 1080, 2400
    x = width // 2
    y1 = int(height * 0.82)
    y2 = int(height * (0.16 if large else 0.38))
    sweeps = 2 if large else 1
    for _ in range(sweeps):
        duration = random.randint(320, 520) if large else random.randint(260, 420)
        try:
            _adb_swipe(device, x, y1, x, y2, duration)
        except Exception:
            device.swipe(x, y1, x, y2, duration=duration / 1000.0)
        time.sleep(random.uniform(0.35, 0.7))


def _pause(prefs: dict[str, Any]) -> None:
    lo = int(prefs.get("pause_min_ms") or 1600) / 1000.0
    hi = int(prefs.get("pause_max_ms") or 3800) / 1000.0
    if hi < lo:
        hi = lo
    time.sleep(random.uniform(lo, hi))
