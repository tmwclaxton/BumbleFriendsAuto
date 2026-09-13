"""Hinge Discover swipe prefs and Galaxy-only runner."""

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
from src.phones import normalize_phone_id

log = logging.getLogger(__name__)

PREFS_PATH = ROOT / "data" / "hinge_swipe_prefs.json"
USAGE_PATH = ROOT / "data" / "hinge_swipe_usage.json"
_LONDON = ZoneInfo("Europe/London")

# Toby's Hinge account, driven on Archie Galaxy. Pixel is never the Hinge device.
LIVE_ACCOUNT = "toby"
LIVE_DEVICE = "archie"
LIVE_PHONE = LIVE_ACCOUNT
GALAXY_SERIAL_FALLBACK = "192.168.0.168:5555"
PIXEL_SERIAL_MARKS = ("29081fdh200gz8", "panther")
ATTRACTIVENESS_STUB_SCORE = 5.0


def default_prefs() -> dict[str, Any]:
    return {
        "phone_id": LIVE_ACCOUNT,
        "age_min": 21,
        "age_max": 35,
        "distance_max_miles": 25,
        "dealbreakers": [],
        "ethnicity_include": [],
        "ethnicity_exclude": [],
        "ethnicity_if_missing": "allow",
        "attractiveness_min": 8,
        "like_percent": 25,
        "daily_cap": 20,
        "max_likes_session": 8,
        "max_swipes_session": 20,
        "pause_min_ms": 1800,
        "pause_max_ms": 4200,
        "human_pacing": True,
        "skip_verified_only": True,
        "require_verified": False,
        "gender_pref": "",
        "comment_on_like": False,
        "stop_on_like_limit": True,
        "stop_on_out_of_cards": True,
        "apply_on_device_filters": False,
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


def _dealbreakers(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
    elif isinstance(raw, (list, tuple)):
        parts = [str(p).strip() for p in raw]
    else:
        return []
    return [p for p in parts if p][:24]


def live_account_id(phone_id: str | None = None) -> str:
    """Galaxy Hinge is Toby's account. Archie is an empty future slot."""
    pid = normalize_phone_id(phone_id) if phone_id else LIVE_ACCOUNT
    if pid in {"", "all", "archie"}:
        return LIVE_ACCOUNT
    return pid


def serial_is_pixel(serial: str | None) -> bool:
    blob = (serial or "").strip().lower()
    return any(mark in blob for mark in PIXEL_SERIAL_MARKS)


def hinge_live_serial(explicit: str | None = None) -> str:
    """Always the Galaxy. Never the Instagram Pixel."""
    if explicit and not serial_is_pixel(explicit):
        return explicit
    from src.phones import serial_for

    serial = serial_for(LIVE_DEVICE) or GALAXY_SERIAL_FALLBACK
    if serial_is_pixel(serial):
        return GALAXY_SERIAL_FALLBACK
    return serial


def _ethnicity_ids(raw: object) -> list[str]:
    from src.profile_filters import parse_include

    return sorted(parse_include(raw))


def normalize_prefs(raw: dict | None) -> dict[str, Any]:
    base = default_prefs()
    data = dict(raw or {})
    phone = live_account_id(str(data.get("phone_id") or base["phone_id"]))
    age_min = _clamp_int(data.get("age_min"), 18, 80, base["age_min"])
    age_max = _clamp_int(data.get("age_max"), 18, 80, base["age_max"])
    if age_max < age_min:
        age_max = age_min
    if_missing = str(data.get("ethnicity_if_missing") or base["ethnicity_if_missing"]).strip().lower()
    if if_missing in {"pass", "deny", "block"}:
        if_missing = "skip"
    if if_missing not in {"allow", "skip"}:
        if_missing = "allow"
    out = {
        "phone_id": phone,
        "age_min": age_min,
        "age_max": age_max,
        "distance_max_miles": _clamp_int(data.get("distance_max_miles"), 1, 200, base["distance_max_miles"]),
        "dealbreakers": _dealbreakers(data.get("dealbreakers")),
        "ethnicity_include": _ethnicity_ids(data.get("ethnicity_include")),
        "ethnicity_exclude": _ethnicity_ids(data.get("ethnicity_exclude")),
        "ethnicity_if_missing": if_missing,
        "attractiveness_min": _clamp_int(data.get("attractiveness_min"), 1, 10, base["attractiveness_min"]),
        "like_percent": _clamp_int(data.get("like_percent"), 0, 100, base["like_percent"]),
        "daily_cap": _clamp_int(data.get("daily_cap"), 1, 80, base["daily_cap"]),
        "max_likes_session": _clamp_int(data.get("max_likes_session"), 0, 40, base["max_likes_session"]),
        "max_swipes_session": _clamp_int(data.get("max_swipes_session"), 1, 80, base["max_swipes_session"]),
        "pause_min_ms": _clamp_int(data.get("pause_min_ms"), 400, 15000, base["pause_min_ms"]),
        "pause_max_ms": _clamp_int(data.get("pause_max_ms"), 400, 20000, base["pause_max_ms"]),
        "human_pacing": _as_bool(data.get("human_pacing"), True),
        "skip_verified_only": _as_bool(data.get("skip_verified_only"), True),
        "require_verified": _as_bool(data.get("require_verified"), False),
        "gender_pref": str(data.get("gender_pref") or "").strip()[:40],
        "comment_on_like": _as_bool(data.get("comment_on_like"), False),
        "stop_on_like_limit": _as_bool(data.get("stop_on_like_limit"), True),
        "stop_on_out_of_cards": _as_bool(data.get("stop_on_out_of_cards"), True),
        "apply_on_device_filters": _as_bool(data.get("apply_on_device_filters"), False),
    }
    if out["pause_max_ms"] < out["pause_min_ms"]:
        out["pause_max_ms"] = out["pause_min_ms"]
    return out


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


def prefs_payload(prefs: dict | None = None) -> dict[str, Any]:
    from src.profile_filters import ETHNICITY_CHOICES

    data = dict(prefs or load_prefs())
    data["ethnicity_choices"] = [{"id": cid, "label": label} for cid, label in ETHNICITY_CHOICES]
    data["live_account"] = LIVE_ACCOUNT
    data["live_device"] = "galaxy"
    data["attractiveness_stub_score"] = ATTRACTIVENESS_STUB_SCORE
    data["usage"] = _usage()
    return data


def _today() -> str:
    return datetime.now(_LONDON).strftime("%Y-%m-%d")


def _usage() -> dict:
    if not USAGE_PATH.is_file():
        return {"date": _today(), "swipes": 0, "likes": 0}
    try:
        data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if str(data.get("date") or "") != _today():
        return {"date": _today(), "swipes": 0, "likes": 0}
    return {
        "date": _today(),
        "swipes": int(data.get("swipes") or 0),
        "likes": int(data.get("likes") or 0),
    }


def _save_usage(usage: dict) -> None:
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USAGE_PATH.write_text(json.dumps(usage, indent=2) + "\n", encoding="utf-8")


def pixel_blocked(phone_id: str, serial: str | None = None) -> str | None:
    """Refuse Pixel hardware. Toby account on Galaxy is the live Hinge."""
    if serial and serial_is_pixel(serial):
        if os.environ.get("HINGE_ALLOW_PIXEL", "").strip() in {"1", "true", "yes"}:
            return None
        return "Pixel is reserved for Instagram / Bumble. Hinge live work is Galaxy only (Toby account)."
    return None


def score_attractiveness(
    *,
    profile_text: str = "",
    extras: dict | None = None,
    photo_path: str | None = None,
) -> tuple[float, str]:
    """Conservative placeholder. pitchPerfect vision scoring is not ported.

    Always returns 5/10 so the default threshold (8) skips. Does not like everyone.
    Wire a real scorer here later; `_should_like` already respects attractiveness_min.
    """
    del profile_text, extras, photo_path
    return ATTRACTIVENESS_STUB_SCORE, "attractiveness stub (no live vision)"


def _ethnicity_decision(prefs: dict, profile_text: str, extras: dict | None) -> tuple[bool, str]:
    from src.profile_filters import canonicalize, ethnicities_on_card

    extras = extras or {}
    found: set[str] = set()
    raw = str(extras.get("ethnicity") or extras.get("ethnicities") or extras.get("race") or "")
    if raw:
        hit = canonicalize(raw)
        if hit:
            found.add(hit)
    found |= set(ethnicities_on_card([profile_text, *raw.split(","), *found]))
    include = set(prefs.get("ethnicity_include") or [])
    exclude = set(prefs.get("ethnicity_exclude") or [])
    if found & exclude:
        return False, f"ethnicity {sorted(found & exclude)} excluded"
    if not found:
        if prefs.get("ethnicity_if_missing") == "skip":
            return False, "ethnicity missing"
        return True, "ethnicity missing allowed"
    if include and not (found & include):
        return False, f"ethnicity {sorted(found)} not in include"
    return True, "ethnicity ok"


def _sleep_prefs(prefs: dict) -> None:
    lo = int(prefs["pause_min_ms"]) / 1000
    hi = int(prefs["pause_max_ms"]) / 1000
    if not prefs.get("human_pacing"):
        time.sleep(max(0.4, lo))
        return
    time.sleep(random.uniform(lo, hi))


def _should_like(
    prefs: dict,
    profile_text: str,
    *,
    likes: int,
    swipes: int,
    extras: dict | None = None,
    photo_path: str | None = None,
) -> tuple[bool, str]:
    ok, reason = _ethnicity_decision(prefs, profile_text, extras)
    if not ok:
        return False, reason
    score, score_why = score_attractiveness(
        profile_text=profile_text, extras=extras, photo_path=photo_path
    )
    threshold = int(prefs.get("attractiveness_min") or 8)
    if score < threshold:
        return False, f"attractiveness {score:g} < {threshold} ({score_why})"
    age = None
    match = __import__("re").search(r"\bage[:\s]+(\d{2})\b", profile_text, __import__("re").I)
    if match:
        age = int(match.group(1))
    if age is not None and (age < prefs["age_min"] or age > prefs["age_max"]):
        return False, f"age {age} outside {prefs['age_min']}-{prefs['age_max']}"
    blob = profile_text.lower()
    for item in prefs["dealbreakers"]:
        if item.lower() in blob:
            return False, f"dealbreaker {item!r}"
    if prefs["require_verified"] and "verified" not in blob:
        return False, "require verified"
    if likes >= prefs["max_likes_session"]:
        return False, "session like cap"
    if swipes and (likes * 100 / max(1, swipes + 1)) >= prefs["like_percent"] + 8:
        return False, "like ratio"
    roll = random.randint(1, 100)
    if roll > int(prefs["like_percent"]):
        return False, f"ratio roll {roll}"
    return True, f"like (attractiveness {score:g})"


def run_swipe(
    *,
    serial: str | None = None,
    phone_id: str | None = None,
    prefs: dict | None = None,
) -> tuple[bool, str]:
    from src.device import bring_app_foreground, connect, dump_hierarchy, wait_idle
    from src.gestures import tap
    from src.hinge_screen import (
        PACKAGE,
        classify_screen,
        discover_card_name,
        discover_card_name_on_device,
        find_like_on_device,
        find_like_photo,
        find_nav_point,
        find_send_like,
        find_skip,
        find_skip_on_device,
        like_limit_reached,
        out_of_cards,
        parse_profile,
    )
    from src.unlock import wake_and_unlock

    pid = live_account_id(phone_id or (prefs or {}).get("phone_id") or LIVE_ACCOUNT)
    serial = hinge_live_serial(serial)
    blocked = pixel_blocked(pid, serial)
    if blocked:
        return False, blocked
    settings = normalize_prefs({**load_prefs(), **(prefs or {}), "phone_id": pid})
    usage = _usage()
    if usage["swipes"] >= settings["daily_cap"]:
        return True, f"daily cap reached ({usage['swipes']})"
    try:
        device = connect(serial)
    except Exception as exc:
        return False, f"could not connect to Galaxy: {exc}"
    if not wake_and_unlock(device, serial=serial):
        return False, "Galaxy still locked"
    bring_app_foreground(device, PACKAGE)
    wait_idle(device, 1.2)
    # Compressed dumps on Galaxy omit the Compose Discover card and can
    # stall uiautomator. Prefer live selectors first; dump only to navigate.
    xml = ""
    if not find_skip_on_device(device) and not find_like_on_device(device):
        xml = dump_hierarchy(device)
        kind = classify_screen(xml)
        if kind in {"match_chat", "match_profile"}:
            point = find_nav_point(xml, "Back") or find_nav_point(xml, "Matches")
            if point:
                tap(device, *point)
                wait_idle(device, 0.8)
                xml = dump_hierarchy(device)
                kind = classify_screen(xml)
        if kind != "discover":
            point = find_nav_point(xml, "Discover")
            if point:
                tap(device, *point)
                wait_idle(device, 1.2)
                xml = ""
    if (
        not find_like_photo(xml)
        and not find_skip(xml)
        and not find_like_on_device(device)
        and not find_skip_on_device(device)
    ):
        from src.config import ROOT as _ROOT

        dump_dir = _ROOT / "data" / "hinge_dumps"
        try:
            dump_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            (dump_dir / f"swipe-fail-{stamp}.xml").write_text(xml or "", encoding="utf-8")
        except OSError:
            pass
        return False, (
            f"Hinge Discover UI not parsed (Like photo / Skip missing, screen={classify_screen(xml)}, "
            f"xml_chars={len(xml or '')}). Open Discover on Galaxy and retry."
        )

    likes = 0
    swipes = 0
    notes: list[str] = []
    for _ in range(int(settings["max_swipes_session"])):
        if usage["swipes"] + swipes >= settings["daily_cap"]:
            notes.append("daily cap")
            break
        skip = find_skip(xml) if xml else None
        skip = skip or find_skip_on_device(device)
        heart = find_like_photo(xml) if xml else None
        heart = heart or find_like_on_device(device)
        if not skip and not heart:
            xml = dump_hierarchy(device)
            if settings["stop_on_out_of_cards"] and out_of_cards(xml):
                notes.append("out of cards")
                break
            if settings["stop_on_like_limit"] and like_limit_reached(xml):
                notes.append("like limit")
                break
            skip = find_skip(xml) or find_skip_on_device(device)
            heart = find_like_photo(xml) or find_like_on_device(device)
        profile = parse_profile(xml) if xml else parse_profile("")
        name = profile.name or (discover_card_name(xml) if xml else "") or discover_card_name_on_device(device) or "card"
        blob = " ".join(
            [profile.about, profile.job, profile.location, *(f"{q} {a}" for q, a in profile.prompts)]
        )
        like, reason = _should_like(
            settings,
            blob,
            likes=likes,
            swipes=swipes,
            extras=profile.extras,
        )
        if like:
            heart = heart or find_like_photo(xml) or find_like_on_device(device)
            if not heart:
                return False, "Like photo control not on screen; aborting rather than guessing."
            tap(device, *heart)
            wait_idle(device, 0.9)
            send = find_send_like(dump_hierarchy(device))
            if not send:
                return False, "Send like not found after heart tap; not guessing."
            tap(device, *send)
            likes += 1
            notes.append(f"like {name} ({reason})")
        else:
            skip = skip or find_skip(xml) or find_skip_on_device(device)
            if not skip:
                from src.config import ROOT as _ROOT

                dump_dir = _ROOT / "data" / "hinge_dumps"
                try:
                    dump_dir.mkdir(parents=True, exist_ok=True)
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    (dump_dir / f"swipe-skip-missing-{stamp}.xml").write_text(xml or "", encoding="utf-8")
                except OSError:
                    pass
                return False, (
                    f"Skip control not on screen (screen={classify_screen(xml)}, "
                    f"card={name}, xml_chars={len(xml or '')}); aborting rather than guessing."
                )
            tap(device, *skip)
            notes.append(f"pass {name} ({reason})")
        swipes += 1
        _sleep_prefs(settings)
    usage["swipes"] += swipes
    usage["likes"] += likes
    _save_usage(usage)
    summary = f"swiped {swipes}, liked {likes}"
    if notes:
        summary += " — " + "; ".join(notes[-6:])
    return True, summary
