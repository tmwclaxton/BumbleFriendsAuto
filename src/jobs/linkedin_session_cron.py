"""Once-a-day LinkedIn Home session at a random time when the phone is free.

Supercronic ticks every 5 minutes. Each selected phone gets its own due stamp
in a daytime window (10:00–20:00 Europe/London). Busy phones are retried on
the next tick without burning the day.
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
_STATE_NAME = "linkedin_session_due.json"
_WINDOW_START = 10
_WINDOW_END = 20

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
    after = datetime.fromtimestamp(stamp, _LONDON) + timedelta(hours=4)
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
    from src.linkedin_session import load_prefs
    from src.phone_queue import cron_skip_reason
    from src.phones import expand_phone_ids

    prefs = load_prefs()
    if not prefs.get("daily_auto"):
        log.info("linkedin session cron skipped — daily auto off")
        return 0
    now = time.time()
    targets = phones_due(expand_phone_ids(prefs["phone_id"]), now=now)
    if not targets:
        dues = due_map()
        remain = min((int(d - now) for d in dues.values() if d > now), default=0)
        log.info("linkedin session cron idle — next due in %ss", remain)
        return 0

    queued = 0
    port = os.environ.get("PORT") or "8765"
    url = f"http://127.0.0.1:{port}/api/li/session"
    for pid in targets:
        reason = cron_skip_reason(pid, "linkedin")
        if reason:
            log.info("%s session waiting — %s", pid, reason)
            continue
        payload = json.dumps({"phone_id": pid, "cron": True, **prefs}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", **_basic_header()},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                log.info("queued %s session: %s", pid, body)
                queued += 1
                continue
        except urllib.error.URLError as exc:
            log.warning("dashboard not reachable for %s (%s) — running inline", pid, exc)
        from src.linkedin_session import run_session

        ok, msg = run_session(phone_id=pid, prefs=prefs)
        log.info("%s %s — %s", pid, "ok" if ok else "fail", msg)
        queued += 1
    return 0 if queued or targets else 0


if __name__ == "__main__":
    sys.exit(main())
