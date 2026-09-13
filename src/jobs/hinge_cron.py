"""Cron entry: enqueue Toby Hinge reply checks and the daily swipe session.

Supercronic ticks every 5 minutes. This module follows the persisted daily
plan (Matches refreshes on Galaxy, plus one Discover swipe session).
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

_SCAN_STATE = "hinge_scan_next.txt"
_SWIPE_STATE = "hinge_swipe_next.txt"

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


def _state_path() -> Path:
    from src.config import ROOT

    return ROOT / "data" / _SCAN_STATE


def next_scan_due_at() -> float:
    path = _state_path()
    if not path.is_file():
        return 0.0
    try:
        return float(path.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def write_scan_next_due(due: float) -> float:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{due:.0f}\n", encoding="utf-8")
    return due


def _swipe_state_path() -> Path:
    from src.config import ROOT

    return ROOT / "data" / _SWIPE_STATE


def next_swipe_due_at() -> float:
    path = _swipe_state_path()
    if not path.is_file():
        return 0.0
    try:
        return float(path.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def write_swipe_next_due(due: float) -> float:
    path = _swipe_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{due:.0f}\n", encoding="utf-8")
    return due


def should_run(now: float | None = None) -> bool:
    stamp = now if now is not None else time.time()
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from src.daily_schedule import next_unconsumed_slot

        planned = next_unconsumed_slot(
            "hinge_scan", now=datetime.fromtimestamp(stamp, ZoneInfo("Europe/London"))
        )
        if planned > 0:
            return stamp >= planned
    except Exception:
        log.debug("hinge plan unavailable", exc_info=True)
    return stamp >= next_scan_due_at()


def mark_scan_ran(now: float | None = None, rng: random.Random | None = None) -> float:
    stamp = now if now is not None else time.time()
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from src.daily_schedule import consume_hinge_scan_slot

        return consume_hinge_scan_slot(
            now=datetime.fromtimestamp(stamp, ZoneInfo("Europe/London")),
            rng=rng or random.Random(),
        )
    except Exception:
        log.debug("hinge scan consume failed", exc_info=True)
    wait = (rng or random).randint(50 * 60, 100 * 60)
    return write_scan_next_due(stamp + wait)


def swipe_hour_ok(now: float | None = None) -> bool:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src.daily_schedule import HINGE_SWIPE_END_HOUR, HINGE_SWIPE_START_HOUR

    stamp = now if now is not None else time.time()
    hour = datetime.fromtimestamp(stamp, ZoneInfo("Europe/London")).hour
    return HINGE_SWIPE_START_HOUR <= hour < HINGE_SWIPE_END_HOUR


def should_swipe(now: float | None = None) -> bool:
    stamp = now if now is not None else time.time()
    if not swipe_hour_ok(stamp):
        return False
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from src.daily_schedule import next_unconsumed_slot

        planned = next_unconsumed_slot(
            "hinge_swipe", now=datetime.fromtimestamp(stamp, ZoneInfo("Europe/London"))
        )
        if planned > 0:
            return stamp >= planned
    except Exception:
        log.debug("hinge swipe plan unavailable", exc_info=True)
    due = next_swipe_due_at()
    return due > 0 and stamp >= due


def mark_swipe_ran(now: float | None = None, rng: random.Random | None = None) -> float:
    stamp = now if now is not None else time.time()
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from src.daily_schedule import consume_hinge_swipe_slot

        return consume_hinge_swipe_slot(
            now=datetime.fromtimestamp(stamp, ZoneInfo("Europe/London")),
            rng=rng or random.Random(),
        )
    except Exception:
        log.debug("hinge swipe consume failed", exc_info=True)
    return write_swipe_next_due(stamp + 20 * 60 * 60)


def _post() -> dict:
    port = os.environ.get("PORT") or "8765"
    url = f"http://127.0.0.1:{port}/api/hinge/scan"
    req = urllib.request.Request(
        url,
        data=b'{"phone_id":"toby","cron":true}',
        headers={"Content-Type": "application/json", **_basic_header()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            log.info("queued via dashboard /api/hinge/scan: %s", body)
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
            log.info("dashboard has no /api/hinge/scan yet — skip until the app reloads")
            return {"status": "missing"}
        log.warning("dashboard error for hinge scan (%s)", exc)
        return {"status": "error"}
    except urllib.error.URLError as exc:
        log.warning("dashboard not reachable for hinge scan (%s)", exc)
        return {"status": "down"}


def _should_consume_slot(payload: dict) -> bool:
    if payload.get("status") != "queued":
        return False
    if payload.get("skipped"):
        return False
    return bool(payload.get("jobs") or payload.get("queued"))


def _post_swipe() -> dict:
    port = os.environ.get("PORT") or "8765"
    url = f"http://127.0.0.1:{port}/api/hinge/swipe"
    req = urllib.request.Request(
        url,
        data=b'{"phone_id":"toby","cron":true}',
        headers={"Content-Type": "application/json", **_basic_header()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            log.info("queued via dashboard /api/hinge/swipe: %s", body)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {}
            return payload if isinstance(payload, dict) else {}
    except urllib.error.HTTPError as exc:
        log.warning("dashboard error for hinge swipe (%s)", exc)
        return {"status": "error"}
    except urllib.error.URLError as exc:
        log.warning("dashboard not reachable for hinge swipe (%s)", exc)
        return {"status": "down"}


def _maybe_swipe() -> None:
    if not should_swipe():
        return
    from src.phone_queue import cron_skip_reason

    reason = cron_skip_reason("toby", "hinge")
    if reason:
        log.info("hinge swipe waiting — toby:%s", reason)
        return
    payload = _post_swipe()
    if payload.get("status") == "queued" and _should_consume_slot(payload):
        due = mark_swipe_ran()
        log.info("next hinge swipe at %s", time.strftime("%H:%M", time.localtime(due)))


def main() -> int:
    _maybe_swipe()
    if not should_run():
        remain = max(0, int(next_scan_due_at() - time.time()))
        log.info("hinge reply check skipped — next due in %ss", remain)
        return 0
    from src.phone_queue import cron_skip_reason

    reason = cron_skip_reason("toby", "hinge")
    if reason:
        log.info("hinge reply check waiting — toby:%s", reason)
        return 0
    payload = _post()
    status = str(payload.get("status") or "")
    if status == "queued":
        if _should_consume_slot(payload):
            due = mark_scan_ran()
            log.info("next hinge reply check at %s", time.strftime("%H:%M", time.localtime(due)))
        else:
            log.info(
                "hinge reply check not marked done — queued=%s skipped=%s",
                bool(payload.get("jobs") or payload.get("queued")),
                payload.get("skipped"),
            )
        return 0
    if status == "missing":
        return 0
    from src.hinge_sync import run_scan

    ok, msg = run_scan(phone_id="toby")
    log.info("hinge scan %s — %s", "ok" if ok else "fail", msg)
    due = mark_scan_ran()
    log.info("next hinge reply check at %s", time.strftime("%H:%M", time.localtime(due)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
