"""Swipe-desk prefs and live approve/override state."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from src.config import ROOT
from src.phones import DEFAULT_PHONE_ID, normalize_phone_id
from src.profile_filters import ETHNICITY_CHOICES, canonicalize

log = logging.getLogger(__name__)

PREFS_PATH = ROOT / "data" / "swipe_desk.json"
CARD_PATH = ROOT / "data" / "swipe_card.jpg"

_RACE_IDS = [cid for cid, _ in ETHNICITY_CHOICES]
_MEN_DEFAULT = ["white", "east_asian", "southeast_asian"]
_WOMEN_DEFAULT = list(_RACE_IDS)
_MISSING = {"allow", "pass"}
_ACTIONS = {"approve", "like", "pass", "stop"}

_lock = threading.Lock()
_decision = threading.Event()
_state: dict[str, Any] = {
    "running": False,
    "waiting": False,
    "phone_id": DEFAULT_PHONE_ID,
    "final_say": False,
    "max_swipes": 20,
    "swipes": 0,
    "likes": 0,
    "passes": 0,
    "overrides": 0,
    "pending": None,
    "decision": None,
    "photo_seq": 0,
    "message": "",
    "log": [],
}


def _canon_races(raw: object, default: list[str]) -> list[str]:
    allowed = set(_RACE_IDS)
    if raw is None:
        return list(default)
    parts = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
    out: list[str] = []
    for part in parts:
        canon = canonicalize(str(part)) or str(part).strip().lower().replace(" ", "_")
        if canon in allowed and canon not in out:
            out.append(canon)
    return out


def _missing(raw: object, default: str) -> str:
    val = str(raw or default).strip().lower()
    return val if val in _MISSING else default


def default_prefs() -> dict[str, Any]:
    return {
        "phone_id": DEFAULT_PHONE_ID,
        "max_swipes": 20,
        "final_say": True,
        "pass_crazy": True,
        "men_include": list(_MEN_DEFAULT),
        "women_include": list(_WOMEN_DEFAULT),
        "men_if_missing": "pass",
        "women_if_missing": "allow",
    }


def normalize_prefs(raw: dict | None) -> dict[str, Any]:
    data = default_prefs()
    if isinstance(raw, dict):
        data.update({k: v for k, v in raw.items() if v is not None})
    pid = normalize_phone_id(str(data.get("phone_id") or DEFAULT_PHONE_ID))
    if pid == "all":
        pid = DEFAULT_PHONE_ID
    try:
        max_swipes = int(data.get("max_swipes") or 20)
    except (TypeError, ValueError):
        max_swipes = 20
    pass_crazy = data.get("pass_crazy", True)
    if isinstance(pass_crazy, str):
        pass_crazy = pass_crazy.strip().lower() not in {"0", "false", "off", "no"}
    return {
        "phone_id": pid,
        "max_swipes": max(1, min(max_swipes, 200)),
        "final_say": bool(data.get("final_say")),
        "pass_crazy": bool(pass_crazy),
        "men_include": _canon_races(data.get("men_include"), _MEN_DEFAULT),
        "women_include": _canon_races(data.get("women_include"), _WOMEN_DEFAULT),
        "men_if_missing": _missing(data.get("men_if_missing"), "pass"),
        "women_if_missing": _missing(data.get("women_if_missing"), "allow"),
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


def job_payload(prefs: dict[str, Any]) -> str:
    body = normalize_prefs(prefs)
    return json.dumps(body)


def apply_prefs_to_cfg(cfg: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    extra = extra if isinstance(extra, dict) else {}
    prefs = normalize_prefs(extra)
    if "final_say" not in extra:
        prefs["final_say"] = False
    filters = dict(cfg.get("filters") or {})
    vision = dict(filters.get("swipe_vision") or {})
    vision["enabled"] = True
    vision["men_include"] = list(prefs["men_include"])
    vision["women_include"] = list(prefs["women_include"])
    vision["men_exclude"] = []
    vision["men_if_missing"] = prefs["men_if_missing"]
    vision["women_if_missing"] = prefs["women_if_missing"]
    vision["pass_crazy"] = prefs["pass_crazy"]
    vision["final_say"] = prefs["final_say"]
    filters["swipe_vision"] = vision
    return {
        **cfg,
        "max_swipes": prefs["max_swipes"],
        "final_say": prefs["final_say"],
        "filters": filters,
    }


def save_card_photo(src: Path) -> int:
    CARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    CARD_PATH.write_bytes(src.read_bytes())
    with _lock:
        _state["photo_seq"] = int(_state.get("photo_seq") or 0) + 1
        return int(_state["photo_seq"])


def attach_session(*, phone_id: str, max_swipes: int, final_say: bool) -> None:
    with _lock:
        _state.update(
            {
                "running": True,
                "waiting": False,
                "phone_id": phone_id,
                "final_say": bool(final_say),
                "max_swipes": int(max_swipes),
                "swipes": 0,
                "likes": 0,
                "passes": 0,
                "overrides": 0,
                "pending": None,
                "decision": None,
                "message": "Looking for a card",
            }
        )
        _state["log"] = list(_state.get("log") or [])[-20:]
    _decision.clear()


def finish_session(message: str) -> None:
    with _lock:
        _state["running"] = False
        _state["waiting"] = False
        _state["pending"] = None
        _state["decision"] = None
        _state["message"] = message
    _decision.set()


def set_message(message: str) -> None:
    with _lock:
        _state["message"] = message


def begin_pending(card: dict[str, Any]) -> None:
    with _lock:
        _state["waiting"] = True
        _state["pending"] = dict(card)
        _state["decision"] = None
        name = card.get("name") or "this card"
        proposed = card.get("proposed") or "pass"
        _state["message"] = f"Your call: {name} → {proposed}"
    _decision.clear()


def wait_decision(*, timeout: float = 900.0) -> str:
    from src.phone_queue import check_cancel

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check_cancel()
        if _decision.wait(0.4):
            with _lock:
                action = str(_state.get("decision") or "stop")
                _state["waiting"] = False
                _state["decision"] = None
            if action not in _ACTIONS:
                return "stop"
            return action
    return "stop"


def submit_decision(action: str) -> bool:
    action = (action or "").strip().lower()
    if action not in _ACTIONS:
        return False
    with _lock:
        if action != "stop" and not _state.get("waiting"):
            return False
        _state["decision"] = action
        if action == "stop":
            _state["waiting"] = False
            _state["message"] = "Stopping"
    _decision.set()
    return True


def note_result(
    *,
    name: str,
    gender: str,
    ethnicity: str,
    proposed: str,
    action: str,
    reason: str,
    overridden: bool,
) -> None:
    row = {
        "at": time.strftime("%H:%M:%S"),
        "name": name,
        "gender": gender,
        "ethnicity": ethnicity,
        "proposed": proposed,
        "action": action,
        "reason": reason,
        "overridden": overridden,
    }
    with _lock:
        _state["swipes"] = int(_state.get("swipes") or 0) + 1
        if action == "like":
            _state["likes"] = int(_state.get("likes") or 0) + 1
        else:
            _state["passes"] = int(_state.get("passes") or 0) + 1
        if overridden:
            _state["overrides"] = int(_state.get("overrides") or 0) + 1
        log_rows = list(_state.get("log") or [])
        log_rows.append(row)
        _state["log"] = log_rows[-40:]
        _state["pending"] = None
        _state["waiting"] = False
        _state["message"] = f"{action} {name or 'card'}"


def snapshot() -> dict[str, Any]:
    with _lock:
        snap = dict(_state)
        snap["log"] = list(_state.get("log") or [])
        snap["pending"] = dict(_state["pending"]) if _state.get("pending") else None
    snap["prefs"] = load_prefs()
    snap["races"] = [{"id": cid, "label": label} for cid, label in ETHNICITY_CHOICES]
    return snap
