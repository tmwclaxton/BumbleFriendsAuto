"""LinkedIn Home session: scroll the feed and react like a person, not a bot."""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.config import ROOT, load_config
from src.device import wait_idle
from src.gestures import _adb_swipe, long_press, tap
from src.linkedin_screen import find_reaction_tray, parse_feed_posts
from src.linkedin_sync import _dismiss_blocker, _xml, ensure_feed
from src.phones import DEFAULT_PHONE_ID, expand_phone_ids

log = logging.getLogger(__name__)

_LONDON = ZoneInfo("Europe/London")
PREFS_PATH = ROOT / "data" / "linkedin_session_prefs.json"
AUDIT_PATH = ROOT / "data" / "linkedin_session_audit.json"
REACTIONS = ("like", "celebrate", "support", "love", "insightful", "funny")
_PHONE_CHOICES = {"toby", "archie", "all"}

_REACT_SYSTEM = """You pick a LinkedIn reaction for one Home feed post.
Reply with exactly one word: skip, like, celebrate, support, love, insightful, or funny.
Lean toward Like on ordinary human posts — a work update, a win, a useful take, a photo from an event.
Skip ads, jobs, company funnels, and engagement-bait. Skip only when it would feel fake to react.
Like is the usual choice. Celebrate for a real win. Insightful for a sharp useful point.
Funny only if it is actually funny. Love and Support almost never.
"""


def _as_bool(raw: object, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "off", "no"}
    return bool(raw)


def default_prefs() -> dict[str, Any]:
    return {
        "phone_id": "all",
        "daily_auto": True,
        "people_only": True,
    }


def normalize_prefs(raw: dict | None) -> dict[str, Any]:
    data = default_prefs()
    if isinstance(raw, dict):
        data.update({k: v for k, v in raw.items() if v is not None})
    pid = str(data.get("phone_id") or "all").strip().lower()
    if pid not in _PHONE_CHOICES:
        pid = "all"
    return {
        "phone_id": pid,
        "daily_auto": _as_bool(data.get("daily_auto"), True),
        "people_only": _as_bool(data.get("people_only"), True),
    }


def load_prefs() -> dict[str, Any]:
    if PREFS_PATH.is_file():
        try:
            raw = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            return normalize_prefs(raw)
    return default_prefs()


def save_prefs(raw: dict | None) -> dict[str, Any]:
    prefs = normalize_prefs(raw)
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREFS_PATH.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    return prefs


def parse_reaction_reply(raw: str) -> str:
    word = (raw or "").strip().split()[0].lower() if (raw or "").strip() else "skip"
    word = word.strip(".,:;!?\"'")
    if word in REACTIONS or word == "skip":
        return word
    return "skip"


def choose_reaction(actor: str, text: str, kind: str, *, cfg: dict | None = None) -> str:
    from src.draft_llm import _chat_completion

    user = f"Author ({kind}): {actor or 'Unknown'}\nPost: {(text or '').strip() or '(little visible text)'}\nWord:"
    try:
        raw = _chat_completion(
            [
                {"role": "system", "content": _REACT_SYSTEM},
                {"role": "user", "content": user},
            ],
            cfg,
            temperature=0.3,
            max_tokens=12,
        )
    except Exception as exc:
        log.warning("reaction model failed (%s) — skip", exc)
        return "skip"
    return parse_reaction_reply(raw)


def load_audit() -> list[dict]:
    if not AUDIT_PATH.is_file():
        return []
    try:
        raw = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return raw if isinstance(raw, list) else []


def append_audit(entry: dict) -> dict:
    rows = load_audit()
    rows.append(entry)
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text(json.dumps(rows[-40:], indent=2), encoding="utf-8")
    return entry


def _fingerprint(post) -> str:
    return f"{post.actor.casefold()}|{post.text[:80].casefold()}"


def _eligible(post, *, people_only: bool) -> bool:
    if post.already:
        return False
    if post.promoted:
        return False
    if people_only and post.kind != "person":
        return False
    return True


def read_pause_seconds(text: str, rng: random.Random | None = None) -> float:
    """How long a person would look at this post before moving on or reacting."""
    rng = rng or random.Random()
    words = len((text or "").split())
    base = 1.6 + min(words, 80) * 0.045
    return max(1.4, min(base + rng.uniform(-0.4, 1.3), 8.5))


def soften_reaction(reaction: str, rng: random.Random | None = None) -> str:
    """Most people tap Like even when a fancier emoji would also fit."""
    rng = rng or random.Random()
    if reaction in {"", "skip"}:
        return "skip"
    if reaction == "like":
        return "like"
    if reaction in {"love", "support", "funny"} and rng.random() < 0.72:
        return "like"
    if rng.random() < 0.45:
        return "like"
    return reaction


def in_thumb_zone(like_y: int, height: int) -> bool:
    """Skip half-off-screen posts — people wait until the buttons sit naturally."""
    if height <= 0:
        return True
    return 0.22 * height < like_y < 0.86 * height


def jitter_point(x: int, y: int, rng: random.Random | None = None, amount: int = 16) -> tuple[int, int]:
    rng = rng or random.Random()
    return x + rng.randint(-amount, amount), y + rng.randint(-amount, amount)


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _human_scroll(device, width: int, height: int, rng: random.Random) -> str:
    """Varied flings, the odd nudge, and a rare scroll-back."""
    roll = rng.random()
    x1 = int(width * rng.uniform(0.40, 0.64))
    if roll < 0.10:
        y1 = int(height * rng.uniform(0.38, 0.48))
        y2 = int(height * rng.uniform(0.58, 0.70))
        kind = "back"
    elif roll < 0.28:
        y1 = int(height * rng.uniform(0.62, 0.74))
        y2 = int(height * rng.uniform(0.50, 0.58))
        kind = "nudge"
    else:
        y1 = int(height * rng.uniform(0.64, 0.82))
        y2 = int(height * rng.uniform(0.26, 0.46))
        kind = "fling"
    x2 = x1 + rng.randint(-28, 28)
    duration = rng.randint(280, 720) if kind != "nudge" else rng.randint(220, 420)
    try:
        _adb_swipe(device, x1, y1, x2, y2, duration)
    except Exception:
        try:
            device.swipe(x1, y1, x2, y2, duration / 1000.0)
        except Exception:
            pass
    return kind


def _apply_reaction(device, post, reaction: str, rng: random.Random) -> bool:
    x, y = jitter_point(post.like_x, post.like_y, rng)
    if reaction == "like":
        _sleep(rng.uniform(0.25, 0.9))
        tap(device, x, y)
        _sleep(rng.uniform(0.7, 1.8))
        return True
    _sleep(rng.uniform(0.4, 1.1))
    long_press(device, x, y, duration_ms=rng.randint(520, 820))
    _sleep(rng.uniform(0.45, 1.0))
    tray = find_reaction_tray(_xml(device))
    point = tray.get(reaction) or tray.get("like")
    if point:
        tx, ty = jitter_point(point[0], point[1], rng, amount=10)
        _sleep(rng.uniform(0.2, 0.7))
        tap(device, tx, ty)
        _sleep(rng.uniform(0.8, 2.0))
        return True
    log.info("no tray for %s — tapping Like", reaction)
    tap(device, x, y)
    _sleep(rng.uniform(0.6, 1.4))
    return True


def run_session(
    cfg: dict | None = None,
    serial: str | None = None,
    *,
    phone_id: str | None = None,
    prefs: dict | None = None,
) -> tuple[bool, str]:
    from src.phone_queue import check_cancel
    from src.phones import serial_for
    from src.linkedin_sync import _unlock

    cfg = cfg or load_config()
    pid = phone_id or str(cfg.get("phone_id") or DEFAULT_PHONE_ID)
    prefs = normalize_prefs(prefs or load_prefs())
    serial = serial or serial_for(pid)
    device, err = _unlock(serial)
    if device is None:
        return False, err
    xml = ensure_feed(device)
    if "linkedin" not in (xml or "").lower():
        return False, "LinkedIn Home did not open"
    info = device.info
    w, h = int(info["displayWidth"]), int(info["displayHeight"])
    people_only = bool(prefs["people_only"])
    seen: set[str] = set()
    reacted: list[dict] = []
    skipped = 0
    scrolled = 0
    considered = 0
    since_react = 99
    max_react = random.randint(5, 8)
    max_scroll = random.randint(14, 22)
    rng = random.Random()
    _sleep(rng.uniform(2.2, 5.0))
    for step in range(max_scroll):
        check_cancel()
        xml = _dismiss_blocker(device, _xml(device))
        posts = parse_feed_posts(xml)
        acted = False
        for post in posts:
            key = _fingerprint(post)
            if key in seen:
                continue
            seen.add(key)
            _sleep(read_pause_seconds(post.text, rng))
            if not _eligible(post, people_only=people_only) or not in_thumb_zone(post.like_y, h):
                skipped += 1
                continue
            if len(seen) <= 1 or since_react < 1:
                skipped += 1
                continue
            if len(reacted) >= max_react:
                skipped += 1
                continue
            if rng.random() < 0.22:
                skipped += 1
                continue
            considered += 1
            reaction = soften_reaction(
                choose_reaction(post.actor, post.text, post.kind, cfg=cfg),
                rng,
            )
            if reaction == "skip":
                skipped += 1
                continue
            log.info("%s react %s on %s", pid, reaction, post.actor)
            _apply_reaction(device, post, reaction, rng)
            reacted.append(
                {
                    "actor": post.actor,
                    "reaction": reaction,
                    "preview": (post.text or "")[:140],
                }
            )
            since_react = 0
            acted = True
            _sleep(rng.uniform(1.4, 3.6))
            break
        if not acted:
            since_react += 1
        if step and step % rng.randint(4, 7) == 0:
            log.info("%s pause mid-feed", pid)
            _sleep(rng.uniform(4.5, 11.0))
        _human_scroll(device, w, h, rng)
        scrolled += 1
        wait_idle(device, rng.uniform(0.7, 2.0))
    msg = f"scrolled {scrolled}, reacted {len(reacted)}, skipped {skipped}, considered {considered}"
    append_audit(
        {
            "at": datetime.now(_LONDON).isoformat(timespec="seconds"),
            "phone_id": pid,
            "ok": True,
            "message": msg,
            "reacted": reacted,
            "skipped": skipped,
            "scrolled": scrolled,
            "people_only": people_only,
        }
    )
    from src.jobs.linkedin_session_cron import mark_phone_ran

    mark_phone_ran(pid)
    return True, msg


def session_phones(prefs: dict | None = None) -> list[str]:
    prefs = normalize_prefs(prefs or load_prefs())
    return expand_phone_ids(prefs["phone_id"])
