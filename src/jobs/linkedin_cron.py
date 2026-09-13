"""Cron entry: enqueue LinkedIn reply checks and short feed reacts.

Supercronic ticks every 5 minutes in the 08:00–23:59 window. This module
follows the persisted daily plan (30–90m inbox checks, 90–180m feed reacts)
instead of showing every tick as its own job.
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
from pathlib import Path

_SCAN_STATE = "linkedin_scan_next.txt"
_FEED_STATE = "linkedin_feed_next.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _basic_header() -> dict[str, str]:
    import base64

    user = (os.environ.get("DASHBOARD_BASIC_USER") or "").strip()
    password = os.environ.get("DASHBOARD_BASIC_PASSWORD") or ""
    if not user or not password:
        return {}
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _state_path(name: str) -> Path:
    from src.config import ROOT

    return ROOT / "data" / name


def _read_due(name: str) -> float:
    path = _state_path(name)
    if not path.is_file():
        return 0.0
    try:
        return float(path.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _write_due(name: str, due: float) -> float:
    path = _state_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{due:.0f}\n", encoding="utf-8")
    return due


def next_scan_due_at() -> float:
    return _read_due(_SCAN_STATE)


def next_feed_due_at() -> float:
    return _read_due(_FEED_STATE)


def write_scan_next_due(due: float) -> float:
    return _write_due(_SCAN_STATE, due)


def write_feed_next_due(due: float) -> float:
    return _write_due(_FEED_STATE, due)


def last_ran_at() -> float:
    """Compat for older callers that stored the last scan time."""
    due = next_scan_due_at()
    return due if due > 0 else 0.0


def should_run_kind(key: str, *, now: float | None = None) -> bool:
    stamp = now if now is not None else time.time()
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from src.daily_schedule import next_unconsumed_slot

        planned = next_unconsumed_slot(
            key, now=datetime.fromtimestamp(stamp, ZoneInfo("Europe/London"))
        )
        if planned > 0:
            return stamp >= planned
    except Exception:
        log.debug("linkedin %s plan unavailable", key, exc_info=True)
    fallback = next_scan_due_at() if key == "linkedin_scan" else next_feed_due_at()
    return stamp >= fallback


def should_run(now: float | None = None) -> bool:
    return should_run_kind("linkedin_scan", now=now)


def should_run_feed(now: float | None = None) -> bool:
    return should_run_kind("linkedin_feed", now=now)


def mark_scan_ran(now: float | None = None, rng: random.Random | None = None) -> float:
    stamp = now if now is not None else time.time()
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from src.daily_schedule import consume_linkedin_scan_slot

        return consume_linkedin_scan_slot(
            now=datetime.fromtimestamp(stamp, ZoneInfo("Europe/London")),
            rng=rng or random.Random(),
        )
    except Exception:
        log.debug("linkedin scan consume failed", exc_info=True)
    wait = (rng or random).randint(30 * 60, 90 * 60)
    return write_scan_next_due(stamp + wait)


def mark_feed_ran(now: float | None = None, rng: random.Random | None = None) -> float:
    stamp = now if now is not None else time.time()
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from src.daily_schedule import consume_linkedin_feed_slot

        return consume_linkedin_feed_slot(
            now=datetime.fromtimestamp(stamp, ZoneInfo("Europe/London")),
            rng=rng or random.Random(),
        )
    except Exception:
        log.debug("linkedin feed consume failed", exc_info=True)
    wait = (rng or random).randint(90 * 60, 180 * 60)
    return write_feed_next_due(stamp + wait)


def mark_ran(now: float | None = None, rng: random.Random | None = None) -> float:
    return mark_scan_ran(now=now, rng=rng)


def _post(path: str) -> dict:
    port = os.environ.get("PORT") or "8765"
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(
        url,
        data=b'{"phone_id":"all","cron":true}',
        headers={"Content-Type": "application/json", **_basic_header()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            log.info("queued via dashboard %s: %s", path, body)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload.setdefault("status", "queued")
            return payload
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            log.info("dashboard has no %s yet — skip until the app reloads", path)
            return {"status": "missing"}
        log.warning("dashboard error for %s (%s)", path, exc)
        return {"status": "error"}
    except urllib.error.URLError as exc:
        log.warning("dashboard not reachable for %s (%s)", path, exc)
        return {"status": "down"}


def _should_consume_slot(payload: dict) -> bool:
    """Keep the slot if a phone was skipped (Toby on Hinge/Instagram, etc.)."""
    if payload.get("status") != "queued":
        return False
    jobs = payload.get("jobs") or []
    skipped = payload.get("skipped") or []
    if skipped:
        return False
    return bool(jobs or payload.get("queued"))


def _held_phones() -> list[str]:
    from src.phone_queue import cron_skip_reason
    from src.phones import phone_ids

    held = []
    for pid in phone_ids() or ["toby"]:
        reason = cron_skip_reason(pid, "linkedin")
        if reason:
            held.append(f"{pid}:{reason}")
    return held


def _run_inline(kind: str) -> bool:
    from src.phone_queue import cron_skip_reason
    from src.phones import phone_ids

    failed = False
    if kind == "linkedin_feed":
        from src.linkedin_sync import run_feed

        runner = run_feed
    else:
        from src.linkedin_sync import run_scan

        runner = run_scan
    ran = False
    for pid in phone_ids() or ["toby"]:
        reason = cron_skip_reason(pid, "linkedin")
        if reason:
            log.info("skip inline %s %s — %s", kind, pid, reason)
            continue
        ok, msg = runner(phone_id=pid)
        log.info("%s %s %s — %s", kind, pid, "ok" if ok else "fail", msg)
        failed = failed or not ok
        ran = True
    return (not failed) if ran else True


def _tick(kind: str, path: str) -> int:
    if kind == "linkedin_feed":
        due_fn, mark_fn, label = should_run_feed, mark_feed_ran, "feed react"
        remain_due = next_feed_due_at
    else:
        due_fn, mark_fn, label = should_run, mark_scan_ran, "reply check"
        remain_due = next_scan_due_at
    if not due_fn():
        remain = max(0, int(remain_due() - time.time()))
        log.info("linkedin %s skipped — next due in %ss", label, remain)
        return 0
    held = _held_phones()
    payload = _post(path)
    status = str(payload.get("status") or "")
    if status == "queued":
        if _should_consume_slot(payload):
            due = mark_fn()
            log.info("next linkedin %s at %s", label, time.strftime("%H:%M", time.localtime(due)))
        else:
            skipped = payload.get("skipped") or []
            log.info(
                "linkedin %s not marked done — queued=%s skipped=%s",
                label,
                bool(payload.get("jobs") or payload.get("queued")),
                skipped or held,
            )
        return 0
    if status == "missing":
        return 0
    if held:
        log.info("linkedin %s waiting — %s", label, "; ".join(held))
        return 0
    ok = _run_inline(kind)
    due = mark_fn()
    log.info("next linkedin %s at %s", label, time.strftime("%H:%M", time.localtime(due)))
    return 0 if ok else 1


def main() -> int:
    feed_code = _tick("linkedin_feed", "/api/li/feed")
    scan_code = _tick("linkedin_scan", "/api/li/scan")
    return scan_code or feed_code


if __name__ == "__main__":
    sys.exit(main())
