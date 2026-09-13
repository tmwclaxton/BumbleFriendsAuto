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
SESSION_PATH = ROOT / "data" / "hinge_swipe_last.json"
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
        "ethnicity_include": ["white"],
        "ethnicity_exclude": [],
        "ethnicity_if_missing": "skip",
        "hair_include": ["blonde"],
        "eye_include": ["blue"],
        "look_if_missing": "skip",
        "attractiveness_min": 8,
        "like_percent": 100,
        "daily_cap": 25,
        "max_likes_session": 8,
        "max_swipes_session": 25,
        "gender_pref": "female",
        "pause_min_ms": 1800,
        "pause_max_ms": 4200,
        "human_pacing": True,
        "skip_verified_only": True,
        "require_verified": False,
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


def _look_ids(raw: object, *, default: list[str] | None = None) -> list[str]:
    if raw is None:
        return list(default or [])
    if isinstance(raw, str):
        parts = [p.strip().lower() for p in raw.replace("\n", ",").split(",")]
    elif isinstance(raw, (list, tuple)):
        parts = [str(p).strip().lower() for p in raw]
    else:
        return list(default or [])
    aliases = {"blond": "blonde", "blue eyes": "blue", "blue-eyed": "blue"}
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        out.append(aliases.get(part, part))
    return sorted(set(out))[:12]


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
    look_missing = str(data.get("look_if_missing") or base["look_if_missing"]).strip().lower()
    if look_missing not in {"allow", "skip"}:
        look_missing = "skip"
    out = {
        "phone_id": phone,
        "age_min": age_min,
        "age_max": age_max,
        "distance_max_miles": _clamp_int(data.get("distance_max_miles"), 1, 200, base["distance_max_miles"]),
        "dealbreakers": _dealbreakers(data.get("dealbreakers")),
        "ethnicity_include": _ethnicity_ids(data.get("ethnicity_include")),
        "ethnicity_exclude": _ethnicity_ids(data.get("ethnicity_exclude")),
        "ethnicity_if_missing": if_missing,
        "hair_include": _look_ids(data.get("hair_include"), default=base["hair_include"]),
        "eye_include": _look_ids(data.get("eye_include"), default=base["eye_include"]),
        "look_if_missing": look_missing,
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
        "gender_pref": str(data.get("gender_pref") or base["gender_pref"] or "").strip()[:40],
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
    data["last_session"] = last_session()
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


def card_identity(name: str, extras: dict | None = None) -> str:
    extras = extras or {}
    return "|".join(
        [
            (name or "").casefold().strip(),
            str(extras.get("ethnicity") or ""),
            str(extras.get("hair") or ""),
            str(extras.get("eyes") or ""),
            str(extras.get("attractiveness") or ""),
        ]
    )


def looks_like_wrong_app(xml: str) -> bool:
    """Galaxy sometimes wakes on Bumble; Discover Skip then reads as Plans 'Start'."""
    blob = xml or ""
    if "com.bumblebff.app" in blob:
        return True
    return bool(blob) and "<hierarchy" in blob and "co.hinge.app" not in blob


def xml_fingerprint(name: str, profile: Any) -> str:
    about = getattr(profile, "about", "") or ""
    job = getattr(profile, "job", "") or ""
    location = getattr(profile, "location", "") or ""
    photos = getattr(profile, "photos", None) or []
    return "|".join(
        [
            (name or "").casefold().strip(),
            about[:80],
            job[:40],
            location[:40],
            ",".join(str(p) for p in photos[:3]),
        ]
    )


def card_display_name(profile: Any, xml: str = "", device: Any = None) -> str:
    from collections import Counter

    from src.hinge_screen import discover_card_name, discover_card_name_on_device

    photos = [str(p).strip() for p in (getattr(profile, "photos", None) or []) if str(p).strip()]
    if photos:
        return Counter(photos).most_common(1)[0][0]
    name = (getattr(profile, "name", None) or "").strip()
    if name:
        return name
    return (discover_card_name(xml) or (discover_card_name_on_device(device) if device is not None else "") or "").strip()


def session_summary(swipes: int, likes: int, cards: list[dict[str, Any]], notes: list[str]) -> str:
    names: list[str] = []
    seen: set[str] = set()
    for card in cards:
        name = str(card.get("name") or "").strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    liked = [str(c.get("name") or "").strip() for c in cards if c.get("action") == "like"]
    liked = [n for n in liked if n]
    bits = [f"swiped {swipes}", f"liked {likes}", f"{len(names)} people"]
    if liked:
        bits.append("likes: " + ", ".join(liked[:8]))
    uniq_notes: list[str] = []
    seen_notes: set[str] = set()
    for note in notes:
        if note and note not in seen_notes:
            seen_notes.add(note)
            uniq_notes.append(note)
    line = ", ".join(bits)
    if uniq_notes:
        line += " — " + "; ".join(uniq_notes[-5:])
    return line


def _read_discover_card(device) -> tuple[str, tuple[int, int] | None, tuple[int, int] | None, Any, str]:
    from src.device import dump_hierarchy
    from src.gestures import tap
    from src.device import wait_idle
    from src.hinge_screen import (
        find_close_on_device,
        find_like_on_device,
        find_like_photo,
        find_skip,
        find_skip_on_device,
        parse_profile,
    )

    try:
        xml = dump_hierarchy(device) or ""
    except Exception as exc:
        log.warning("discover card dump failed: %s", exc)
        xml = ""
    if looks_like_wrong_app(xml):
        from src.device import bring_app_foreground
        from src.hinge_screen import PACKAGE

        bring_app_foreground(device, PACKAGE)
        wait_idle(device, 1.6)
        try:
            xml = dump_hierarchy(device) or ""
        except Exception:
            xml = ""
    skip = find_skip(xml) or find_skip_on_device(device)
    heart = find_like_photo(xml) or find_like_on_device(device)
    if not skip and not heart:
        close = find_close_on_device(device)
        if close:
            tap(device, *close)
            wait_idle(device, 1.1)
            try:
                xml = dump_hierarchy(device) or ""
            except Exception:
                xml = ""
            skip = find_skip(xml) or find_skip_on_device(device)
            heart = find_like_photo(xml) or find_like_on_device(device)
    profile = parse_profile(xml)
    name = card_display_name(profile, xml, device)
    return xml, skip, heart, profile, name


def _wait_card_advance(device, prev_name: str, prev_fp: str) -> bool:
    from src.device import wait_idle

    wait_idle(device, 0.9)
    want = (prev_name or "").casefold().strip()
    for attempt in range(6):
        xml, _skip, _heart, profile, name = _read_discover_card(device)
        fp = xml_fingerprint(name, profile)
        changed_name = bool(name) and name.casefold().strip() != want
        changed_fp = bool(fp) and fp != prev_fp
        if changed_name or changed_fp:
            return True
        wait_idle(device, 0.4 + attempt * 0.15)
    return False


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
    extras = extras or {}
    raw = extras.get("attractiveness")
    try:
        score = float(raw)
    except (TypeError, ValueError):
        del profile_text, photo_path
        return ATTRACTIVENESS_STUB_SCORE, "attractiveness stub (no live vision)"
    return max(1.0, min(10.0, score)), "vision"


_HINGE_VISION_RE = __import__("re").compile(
    r"gender\s*=\s*(male|female|unknown).*?"
    r"ethnicity\s*=\s*([a-z_]+).*?"
    r"hair\s*=\s*([a-z]+).*?"
    r"eyes\s*=\s*([a-z]+).*?"
    r"attractiveness\s*=\s*(\d+(?:\.\d+)?)",
    __import__("re").I | __import__("re").S,
)


def parse_hinge_vision(text: str) -> dict[str, Any]:
    match = _HINGE_VISION_RE.search(text or "")
    if not match:
        return {}
    return {
        "gender": match.group(1).lower(),
        "ethnicity": match.group(2).lower(),
        "hair": "blonde" if match.group(3).lower() == "blond" else match.group(3).lower(),
        "eyes": match.group(4).lower(),
        "attractiveness": float(match.group(5)),
    }


def classify_hinge_card(path: Path) -> dict[str, Any]:
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
        "max_tokens": 60,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "This is a Hinge dating profile screenshot. Reply with ONE line:\n"
                            "gender=<male|female|unknown>; ethnicity=<white|black|east_asian|south_asian|"
                            "southeast_asian|hispanic|middle_eastern|mixed|other|unknown>; "
                            "hair=<blonde|brunette|black|red|other|unknown>; "
                            "eyes=<blue|brown|green|hazel|other|unknown>; attractiveness=<1-10>\n"
                            "attractiveness 8+ means clearly attractive. Blonde includes dirty/platinum blonde. "
                            "Eyes: only blue if they look blue. No extra text."
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
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        return {"error": f"NanoGPT {exc.code}: {detail}"}
    except Exception as exc:
        return {"error": str(exc)}
    try:
        text = str(body["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return {"error": "vision response missing text"}
    parsed = parse_hinge_vision(text)
    if not parsed:
        return {"error": f"unparsed vision: {text[:120]}"}
    return parsed


def _look_decision(prefs: dict, extras: dict, field: str, include_key: str, label: str) -> tuple[bool, str]:
    include = [str(x).lower() for x in (prefs.get(include_key) or [])]
    if not include:
        return True, f"{label} any"
    value = str(extras.get(field) or "").strip().lower()
    if value in {"", "unknown"}:
        if prefs.get("look_if_missing") == "skip":
            return False, f"{label} missing"
        return True, f"{label} missing allowed"
    if value in include or any(item in value for item in include):
        return True, f"{label} {value}"
    return False, f"{label} {value} not in {include}"


def _gender_decision(prefs: dict, extras: dict, profile_text: str) -> tuple[bool, str]:
    want = str(prefs.get("gender_pref") or "").strip().lower()
    if want in {"", "any"}:
        return True, "gender any"
    raw = str(extras.get("gender") or extras.get("sex") or "").strip().lower()
    blob = f"{raw} {profile_text}".lower()
    if want in {"female", "woman", "women"}:
        if raw in {"female", "woman"} or "she/her" in blob or "woman" == raw:
            return True, "gender female"
        if raw in {"male", "man"} or "he/him" in blob:
            return False, "gender not female"
        if prefs.get("look_if_missing") == "skip" and not raw:
            return False, "gender missing"
        return raw in {"female", "woman"}, "gender " + (raw or "unknown")
    return True, "gender any"


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
    extras = extras or {}
    ok, reason = _gender_decision(prefs, extras, profile_text)
    if not ok:
        return False, reason
    ok, reason = _ethnicity_decision(prefs, profile_text, extras)
    if not ok:
        return False, reason
    ok, reason = _look_decision(prefs, extras, "hair", "hair_include", "hair")
    if not ok:
        return False, reason
    ok, reason = _look_decision(prefs, extras, "eyes", "eye_include", "eyes")
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
        find_close_on_device,
        find_like_on_device,
        find_like_photo,
        find_nav_point,
        find_send_like,
        find_send_like_on_device,
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
        _save_session(
            {
                "started_at": datetime.now(_LONDON).isoformat(),
                "finished_at": datetime.now(_LONDON).isoformat(),
                "ok": False,
                "summary": blocked,
                "swipes": 0,
                "likes": 0,
                "filter": "8/10 blonde white · blue eyes",
                "cards": [],
            }
        )
        return False, blocked
    settings = normalize_prefs({**load_prefs(), **(prefs or {}), "phone_id": pid})
    usage = _usage()
    if usage["swipes"] >= settings["daily_cap"]:
        return True, f"daily cap reached ({usage['swipes']})"
    try:
        device = connect(serial)
    except Exception as exc:
        msg = f"could not connect to Galaxy: {exc}"
        _save_session(
            {
                "started_at": datetime.now(_LONDON).isoformat(),
                "finished_at": datetime.now(_LONDON).isoformat(),
                "ok": False,
                "summary": msg,
                "swipes": 0,
                "likes": 0,
                "filter": "8/10 blonde white · blue eyes",
                "cards": [],
            }
        )
        return False, msg
    if not wake_and_unlock(device, serial=serial):
        _save_session(
            {
                "started_at": datetime.now(_LONDON).isoformat(),
                "finished_at": datetime.now(_LONDON).isoformat(),
                "ok": False,
                "summary": "Galaxy still locked",
                "swipes": 0,
                "likes": 0,
                "filter": "8/10 blonde white · blue eyes",
                "cards": [],
            }
        )
        return False, "Galaxy still locked"
    bring_app_foreground(device, PACKAGE)
    wait_idle(device, 2.0)
    try:
        probe = dump_hierarchy(device) or ""
    except Exception:
        probe = ""
    if looks_like_wrong_app(probe):
        log.warning("Galaxy was not on Hinge; bringing Hinge to the front")
        bring_app_foreground(device, PACKAGE)
        wait_idle(device, 2.0)

    def _send_like_point(xml_blob: str = "") -> tuple[int, int] | None:
        return find_send_like(xml_blob) or find_send_like_on_device(device)

    def _dump() -> str:
        try:
            return dump_hierarchy(device) or ""
        except Exception as exc:
            log.warning("hinge hierarchy dump failed: %s", exc)
            return ""

    pending_likes = 0
    pending_cards: list[dict[str, Any]] = []
    pending = find_send_like_on_device(device)
    if pending:
        tap(device, *pending)
        wait_idle(device, 1.6)
        pending_likes = 1
        pending_cards.append(
            {
                "name": "card",
                "action": "like",
                "reason": "completed pending send like",
                "gender": "",
                "ethnicity": "",
                "hair": "",
                "eyes": "",
                "attractiveness": None,
            }
        )

    # Compressed dumps on Galaxy omit the Compose Discover card and can
    # stall uiautomator. Prefer live selectors first; dump only to navigate.
    xml = ""
    ready = bool(find_skip_on_device(device) or find_like_on_device(device))
    for attempt in range(6):
        if ready:
            break
        close = find_close_on_device(device)
        if close and not find_skip_on_device(device) and not find_like_on_device(device):
            tap(device, *close)
            wait_idle(device, 1.3)
            if find_skip_on_device(device) or find_like_on_device(device):
                ready = True
                break
            continue
        if attempt == 2:
            bring_app_foreground(device, PACKAGE)
            wait_idle(device, 1.4)
        xml = _dump()
        kind = classify_screen(xml)
        if kind in {"match_chat", "match_profile"}:
            point = find_nav_point(xml, "Back") or find_nav_point(xml, "Matches")
            if point:
                tap(device, *point)
                wait_idle(device, 0.9)
                xml = _dump()
                kind = classify_screen(xml)
        if kind != "discover":
            point = find_nav_point(xml, "Discover")
            if point:
                tap(device, *point)
                wait_idle(device, 1.3)
                xml = ""
        if (
            find_skip_on_device(device)
            or find_like_on_device(device)
            or find_skip(xml)
            or find_like_photo(xml)
        ):
            ready = True
            break
        wait_idle(device, 0.8 + attempt * 0.25)
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
        msg = (
            f"Hinge Discover UI not parsed (Like photo / Skip missing, screen={classify_screen(xml)}, "
            f"xml_chars={len(xml or '')}). Open Discover on Galaxy and retry."
        )
        _save_session(
            {
                "started_at": datetime.now(_LONDON).isoformat(),
                "finished_at": datetime.now(_LONDON).isoformat(),
                "ok": False,
                "summary": msg,
                "swipes": 0,
                "likes": 0,
                "filter": "8/10 blonde white · blue eyes",
                "cards": [],
            }
        )
        return False, msg

    likes = pending_likes
    swipes = pending_likes
    notes: list[str] = ["completed pending send like"] if pending_likes else []
    cards: list[dict[str, Any]] = list(pending_cards)
    started = datetime.now(_LONDON).isoformat()
    last_key = ""
    stuck_same = 0
    for _ in range(int(settings["max_swipes_session"])):
        if usage["swipes"] + swipes >= settings["daily_cap"]:
            notes.append("daily cap")
            break
        xml, skip, heart, profile, name = _read_discover_card(device)
        if not skip and not heart:
            if settings["stop_on_out_of_cards"] and out_of_cards(xml):
                notes.append("out of cards")
                break
            if settings["stop_on_like_limit"] and like_limit_reached(xml):
                notes.append("like limit")
                break
            xml, skip, heart, profile, name = _read_discover_card(device)
        name = name or "card"
        xml_fp = xml_fingerprint(name, profile)
        if last_key and xml_fp == last_key:
            stuck_same += 1
            notes.append(f"same card still on screen ({name})")
            if skip:
                tap(device, *skip)
                _wait_card_advance(device, name, xml_fp)
            if stuck_same >= 2:
                notes.append("card did not advance")
                break
            continue
        stuck_same = 0
        last_key = xml_fp
        blob = " ".join(
            [profile.about, profile.job, profile.location, *(f"{q} {a}" for q, a in profile.prompts)]
        )
        extras = dict(profile.extras or {})
        vision: dict[str, Any] = {}
        try:
            from src.swipe_vision import screenshot_card

            shot = screenshot_card(device)
            try:
                vision = classify_hinge_card(shot)
            finally:
                try:
                    shot.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception as exc:
            vision = {"error": str(exc)}
        if vision.get("error"):
            extras.setdefault("vision_error", str(vision["error"]))
        else:
            extras.update({k: vision[k] for k in vision if k != "error"})
        like, reason = _should_like(
            settings,
            blob,
            likes=likes,
            swipes=swipes,
            extras=extras,
        )
        if like:
            heart = heart or find_like_photo(xml) or find_like_on_device(device)
            if not heart:
                return False, "Like photo control not on screen; aborting rather than guessing."
            tap(device, *heart)
            send = None
            for attempt in range(4):
                wait_idle(device, 0.8 + attempt * 0.3)
                blob = dump_hierarchy(device)
                send = _send_like_point(blob)
                if send:
                    break
            if not send:
                from src.config import ROOT as _ROOT
                from src.device import dump_artifacts

                try:
                    dump_artifacts(device, _ROOT / "data" / "hinge_dumps", prefix="swipe-send-like-missing")
                except Exception:
                    log.debug("send-like dump skipped", exc_info=True)
                return False, "Send like not found after heart tap; not guessing."
            tap(device, *send)
            likes += 1
            notes.append(f"like {name} ({reason})")
            cards.append(
                {
                    "name": name,
                    "action": "like",
                    "reason": reason,
                    "gender": extras.get("gender") or "",
                    "ethnicity": extras.get("ethnicity") or "",
                    "hair": extras.get("hair") or "",
                    "eyes": extras.get("eyes") or "",
                    "attractiveness": extras.get("attractiveness"),
                }
            )
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
            cards.append(
                {
                    "name": name,
                    "action": "pass",
                    "reason": reason,
                    "gender": extras.get("gender") or "",
                    "ethnicity": extras.get("ethnicity") or "",
                    "hair": extras.get("hair") or "",
                    "eyes": extras.get("eyes") or "",
                    "attractiveness": extras.get("attractiveness"),
                }
            )
        swipes += 1
        if not _wait_card_advance(device, name, xml_fp):
            notes.append(f"deck slow after {name}")
        _sleep_prefs(settings)
    usage["swipes"] += swipes
    usage["likes"] += likes
    _save_usage(usage)
    summary = session_summary(swipes, likes, cards, notes)
    unique_names = []
    seen_names: set[str] = set()
    for card in cards:
        n = str(card.get("name") or "").strip()
        k = n.casefold()
        if n and k not in seen_names:
            seen_names.add(k)
            unique_names.append(n)
    _save_session(
        {
            "started_at": started,
            "finished_at": datetime.now(_LONDON).isoformat(),
            "ok": True,
            "summary": summary,
            "swipes": swipes,
            "likes": likes,
            "people": len(unique_names),
            "unique_names": unique_names,
            "filter": "8/10 blonde white · blue eyes",
            "cards": cards,
        }
    )
    return True, summary
