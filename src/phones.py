"""Toby/Pixel and Archie/Galaxy registry, plus the active-phone context."""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

DEFAULT_PHONE_ID = "toby"

_PHONE_ALIASES = {
    "toby": "toby",
    "pixel": "toby",
    "archie": "archie",
    "galaxy": "archie",
}

_current_phone_id: ContextVar[str] = ContextVar("phone_id", default=DEFAULT_PHONE_ID)


def normalize_phone_id(phone_id: str | None) -> str:
    raw = (phone_id or "").strip().casefold()
    if not raw:
        return DEFAULT_PHONE_ID
    if raw in {"all", "both", "*"}:
        return "all"
    return _PHONE_ALIASES.get(raw, raw)


def current_phone_id() -> str:
    return _current_phone_id.get() or DEFAULT_PHONE_ID


@contextmanager
def phone_scope(phone_id: str | None) -> Iterator[str]:
    pid = normalize_phone_id(phone_id)
    if pid == "all":
        pid = DEFAULT_PHONE_ID
    token = _current_phone_id.set(pid)
    try:
        yield pid
    finally:
        _current_phone_id.reset(token)


def _defaults() -> dict[str, dict[str, Any]]:
    return {
        "toby": {
            "id": "toby",
            "label": "Toby",
            "device": "pixel",
            "serial": (os.environ.get("SERIAL") or os.environ.get("PIXEL_SERIAL") or "").strip(),
            "unlock_pin": (
                os.environ.get("TOBY_UNLOCK_PIN")
                or os.environ.get("PHONE_UNLOCK_PIN")
                or os.environ.get("PIXEL_UNLOCK_PIN")
                or ""
            ).strip(),
            "notes": "Pixel ~1080x2400",
        },
        "archie": {
            "id": "archie",
            "label": "Archie",
            "device": "galaxy",
            "serial": (
                os.environ.get("ARCHIE_SERIAL")
                or os.environ.get("GALAXY_SERIAL")
                or ""
            ).strip(),
            "adb": (os.environ.get("ARCHIE_ADB") or os.environ.get("GALAXY_ADB") or "").strip(),
            "unlock_pin": (
                os.environ.get("ARCHIE_UNLOCK_PIN")
                or os.environ.get("GALAXY_UNLOCK_PIN")
                or ""
            ).strip(),
            "notes": "Rooted Galaxy",
        },
    }


def list_phones(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    from src.config import load_config

    cfg = cfg or load_config()
    raw = cfg.get("phones")
    merged = _defaults()
    if isinstance(raw, dict):
        for key, value in raw.items():
            pid = normalize_phone_id(str(key))
            if pid == "all":
                continue
            base = dict(merged.get(pid) or {"id": pid, "label": str(key).title(), "device": pid})
            if isinstance(value, dict):
                base.update({k: v for k, v in value.items() if v not in (None, "")})
            base["id"] = pid
            merged[pid] = base
    env_serials = {
        "toby": (os.environ.get("SERIAL") or os.environ.get("PIXEL_SERIAL") or "").strip(),
        "archie": (os.environ.get("ARCHIE_SERIAL") or os.environ.get("GALAXY_SERIAL") or "").strip(),
    }
    env_adb = {
        "archie": (os.environ.get("ARCHIE_ADB") or os.environ.get("GALAXY_ADB") or "").strip(),
    }
    env_pins = {
        "toby": (
            os.environ.get("TOBY_UNLOCK_PIN")
            or os.environ.get("PHONE_UNLOCK_PIN")
            or os.environ.get("PIXEL_UNLOCK_PIN")
            or ""
        ).strip(),
        "archie": (
            os.environ.get("ARCHIE_UNLOCK_PIN")
            or os.environ.get("GALAXY_UNLOCK_PIN")
            or os.environ.get("PHONE_UNLOCK_PIN")
            or ""
        ).strip(),
    }
    out: list[dict[str, Any]] = []
    for pid in ("toby", "archie"):
        row = dict(merged.get(pid) or _defaults()[pid])
        row["id"] = pid
        if env_serials.get(pid):
            row["serial"] = env_serials[pid]
        if env_pins.get(pid):
            row["unlock_pin"] = env_pins[pid]
        if env_adb.get(pid):
            row["adb"] = env_adb[pid]
        row.setdefault("label", pid.title())
        row.setdefault("device", "pixel" if pid == "toby" else "galaxy")
        row.setdefault("serial", "")
        row.setdefault("adb", "")
        row.setdefault("unlock_pin", "")
        row.setdefault("notes", "")
        out.append(row)
    for pid, row in merged.items():
        if pid in {"toby", "archie"}:
            continue
        item = dict(row)
        item["id"] = pid
        item.setdefault("label", pid.title())
        item.setdefault("device", pid)
        item.setdefault("serial", "")
        item.setdefault("unlock_pin", "")
        out.append(item)
    return out


def configured_phones(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Phones that have an ADB serial and can take jobs."""
    return [p for p in list_phones(cfg) if str(p.get("serial") or "").strip()]


def phone_ids(cfg: dict[str, Any] | None = None) -> list[str]:
    return [str(p["id"]) for p in configured_phones(cfg)]


def phone_by_id(phone_id: str | None, cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    pid = normalize_phone_id(phone_id)
    if pid == "all":
        return None
    for row in list_phones(cfg):
        if row["id"] == pid:
            return row
    return None


def phone_by_serial(serial: str | None, cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    want = (serial or "").strip()
    if not want:
        return None
    for row in list_phones(cfg):
        serial = str(row.get("serial") or "").strip()
        adb = str(row.get("adb") or "").strip()
        if serial == want or adb == want:
            return row
    return None


def serial_for(phone_id: str | None, cfg: dict[str, Any] | None = None) -> str:
    row = phone_by_id(phone_id, cfg)
    if not row:
        return ""
    from src.adb_wireless import wireless_endpoint

    return (wireless_endpoint(row) or str(row.get("serial") or "")).strip()


def pin_for(phone_id: str | None, cfg: dict[str, Any] | None = None) -> str:
    row = phone_by_id(phone_id, cfg)
    return str((row or {}).get("unlock_pin") or "").strip()


def phone_is_online(row: dict[str, Any] | None, devices: str | None = None) -> bool:
    """True when ADB currently lists this phone as `device`."""
    if not row:
        return False
    from src.adb_wireless import devices_text, line_is_online, wireless_endpoint

    blob = devices if devices is not None else devices_text()
    hints = [
        wireless_endpoint(row),
        str(row.get("serial") or "").strip(),
        str(row.get("adb") or "").strip(),
    ]
    for hint in hints:
        if hint and any(line_is_online(line, hint) for line in blob.splitlines()):
            return True
    return False


def public_phones(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Safe payload for the dashboard (no PINs)."""
    from src.adb_wireless import devices_text, wireless_endpoint

    blob = devices_text()
    out: list[dict[str, Any]] = []
    for p in list_phones(cfg):
        ready = bool(
            str(p.get("serial") or p.get("adb") or wireless_endpoint(p) or "").strip()
        )
        out.append(
            {
                "id": str(p["id"]),
                "label": str(p.get("label") or p["id"]),
                "device": str(p.get("device") or ""),
                "ready": ready,
                "online": phone_is_online(p, devices=blob),
            }
        )
    return out


def expand_phone_ids(phone_id: str | None, cfg: dict[str, Any] | None = None) -> list[str]:
    pid = normalize_phone_id(phone_id)
    if pid in {"all", ""}:
        ids = phone_ids(cfg)
        return ids or [DEFAULT_PHONE_ID]
    if phone_by_id(pid, cfg) is None:
        raise ValueError(f"unknown phone {phone_id}")
    return [pid]
