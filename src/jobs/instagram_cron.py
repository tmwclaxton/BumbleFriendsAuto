"""Cron entry: enqueue Toby's Instagram Following likes twice a day.

Supercronic ticks every 5 minutes. This module follows the persisted daily plan.
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

_FEED_STATE = "instagram_feed_next.txt"

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


def _feed_state_path() -> Path:
    from src.config import ROOT

    return ROOT / "data" / _FEED_STATE


def next_feed_due_at() -> float:
    path = _feed_state_path()
    if not path.is_file():
        return 0.0
    try:
        return float(path.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def write_feed_next_due(due: float) -> float:
    path = _feed_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{due:.0f}\n", encoding="utf-8")
    return due


def should_feed(now: float | None = None) -> bool:
    stamp = now if now is not None else time.time()
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from src.daily_schedule import next_unconsumed_slot

        planned = next_unconsumed_slot(
            "instagram_feed", now=datetime.fromtimestamp(stamp, ZoneInfo("Europe/London"))
        )
        if planned > 0:
            return stamp >= planned
    except Exception:
        log.debug("instagram feed plan unavailable", exc_info=True)
    due = next_feed_due_at()
    return due > 0 and stamp >= due


def mark_feed_ran(now: float | None = None, rng: random.Random | None = None) -> float:
    stamp = now if now is not None else time.time()
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from src.daily_schedule import consume_instagram_feed_slot

        return consume_instagram_feed_slot(
            now=datetime.fromtimestamp(stamp, ZoneInfo("Europe/London")),
            rng=rng or random.Random(),
        )
    except Exception:
        log.debug("instagram feed consume failed", exc_info=True)
    return write_feed_next_due(stamp + 8 * 60 * 60)


def _should_consume_slot(payload: dict) -> bool:
    if payload.get("status") != "queued":
        return False
    if payload.get("skipped"):
        return False
    return bool(payload.get("jobs") or payload.get("queued"))


def _post_feed() -> dict:
    port = os.environ.get("PORT") or "8765"
    url = f"http://127.0.0.1:{port}/api/instagram/feed"
    req = urllib.request.Request(
        url,
        data=b'{"phone_id":"toby","cron":true}',
        headers={"Content-Type": "application/json", **_basic_header()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            log.info("queued via dashboard /api/instagram/feed: %s", body)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {}
            payload = payload if isinstance(payload, dict) else {}
            payload.setdefault("status", "queued" if payload.get("queued") else "error")
            return payload
    except urllib.error.HTTPError as exc:
        log.warning("dashboard error for instagram feed (%s)", exc)
        return {"status": "error"}
    except urllib.error.URLError as exc:
        log.warning("dashboard not reachable for instagram feed (%s)", exc)
        return {"status": "down"}


def main() -> int:
    if not should_feed():
        remain = max(0, int(next_feed_due_at() - time.time()))
        log.info("instagram feed skipped — next due in %ss", remain)
        return 0
    from src.phone_queue import cron_skip_reason

    reason = cron_skip_reason("toby", "instagram")
    if reason:
        log.info("instagram feed waiting — toby:%s", reason)
        return 0
    payload = _post_feed()
    if payload.get("status") == "queued" and _should_consume_slot(payload):
        due = mark_feed_ran()
        log.info("next instagram feed at %s", time.strftime("%H:%M", time.localtime(due)))
        return 0
    log.info(
        "instagram feed not marked done — queued=%s skipped=%s",
        bool(payload.get("jobs") or payload.get("queued")),
        payload.get("skipped"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
