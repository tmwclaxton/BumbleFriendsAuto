"""Once a day, late, import 5–10 older LinkedIn chats we do not have yet.

Ticks every 5 minutes. Each phone gets a random slot in 21:00–23:00
Europe/London. If anything else is on that phone, wait for the next tick
without burning the day.
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_LONDON = ZoneInfo("Europe/London")
_STATE_NAME = "linkedin_backfill_due.json"
_WINDOW_START = 21
_WINDOW_END = 23

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _state_path() -> Path:
    from src.config import ROOT

    return ROOT / "data" / _STATE_NAME


def _load_state() -> dict:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def due_map() -> dict[str, float]:
    state = _load_state()
    out: dict[str, float] = {}
    for pid, row in state.items():
        if not isinstance(row, dict):
            continue
        try:
            out[str(pid)] = float(row.get("due") or 0)
        except (TypeError, ValueError):
            out[str(pid)] = 0.0
    return out


def _random_due(after: datetime, rng: random.Random | None = None) -> float:
    rng = rng or random.Random()
    day = after.date()
    if after.hour >= _WINDOW_END:
        day = (after + timedelta(days=1)).date()
    hour = rng.randint(_WINDOW_START, _WINDOW_END - 1)
    minute = rng.randint(0, 59)
    due = datetime(day.year, day.month, day.day, hour, minute, tzinfo=_LONDON)
    if due <= after:
        nxt = after + timedelta(days=1)
        due = datetime(nxt.year, nxt.month, nxt.day, hour, minute, tzinfo=_LONDON)
    return due.timestamp()


def ensure_due(phone_id: str, *, now: float | None = None, rng: random.Random | None = None) -> float:
    stamp = now if now is not None else time.time()
    state = _load_state()
    row = state.get(phone_id) if isinstance(state.get(phone_id), dict) else {}
    try:
        due = float((row or {}).get("due") or 0)
    except (TypeError, ValueError):
        due = 0.0
    if due > stamp:
        return due
    after = datetime.fromtimestamp(stamp, _LONDON)
    due = _random_due(after, rng)
    state[phone_id] = {**(row or {}), "due": due}
    _save_state(state)
    return due


def mark_phone_ran(phone_id: str, *, now: float | None = None, rng: random.Random | None = None) -> float:
    stamp = now if now is not None else time.time()
    after = datetime.fromtimestamp(stamp, _LONDON)
    if after.hour < _WINDOW_END:
        after = after.replace(hour=_WINDOW_END, minute=0, second=0, microsecond=0)
    due = _random_due(after, rng)
    state = _load_state()
    state[phone_id] = {"due": due, "last": stamp}
    _save_state(state)
    return due


def phones_due(phone_ids: list[str], *, now: float | None = None) -> list[str]:
    stamp = now if now is not None else time.time()
    ready: list[str] = []
    for pid in phone_ids:
        due = ensure_due(pid, now=stamp)
        if due <= stamp:
            ready.append(pid)
    return ready


def _basic_header() -> dict[str, str]:
    import base64

    user = (os.environ.get("DASHBOARD_BASIC_USER") or "").strip()
    password = os.environ.get("DASHBOARD_BASIC_PASSWORD") or ""
    if not user or not password:
        return {}
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def main() -> int:
    from src.phone_queue import phone_busy_reason
    from src.phones import phone_ids

    now = time.time()
    targets = phones_due(phone_ids() or ["toby"], now=now)
    if not targets:
        log.info("linkedin backfill idle")
        return 0

    port = os.environ.get("PORT") or "8765"
    url = f"http://127.0.0.1:{port}/api/li/backfill"
    queued = 0
    for pid in targets:
        reason = phone_busy_reason(pid)
        if reason:
            log.info("%s backfill waiting — %s", pid, reason)
            continue
        payload = json.dumps({"phone_id": pid, "cron": True}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", **_basic_header()},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                log.info("queued %s backfill: %s", pid, body)
                try:
                    payload_out = json.loads(body)
                except json.JSONDecodeError:
                    payload_out = {}
                if payload_out.get("queued"):
                    mark_phone_ran(pid, now=now)
                    queued += 1
                continue
        except urllib.error.URLError as exc:
            log.warning("dashboard not reachable for %s (%s) — running inline", pid, exc)
        from src.linkedin_sync import run_backfill

        ok, msg = run_backfill(phone_id=pid)
        log.info("%s %s — %s", pid, "ok" if ok else "fail", msg)
        mark_phone_ran(pid, now=now)
        queued += 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
