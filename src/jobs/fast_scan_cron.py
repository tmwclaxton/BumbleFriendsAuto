"""Cron entry: enqueue a fast reply scan on the shared phone queue.

The fast scan scrolls the inbox list and opens only chats whose preview or
'Your turn' badge disagree with the stored last message — minutes, not ~40.
It also refreshes the New friends strip and rematches expired circles there.
When the dashboard is already running, POST to its API so work shares the
queue. Otherwise run fast_reply_scan directly (standalone cron container).

Supercronic ticks every 5 minutes in the 08:00–23:59 window. This module
skips until a random 30–90 minute gap after the last run has elapsed.
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_MIN_GAP_SEC = 30 * 60
_MAX_GAP_SEC = 90 * 60
_STATE_NAME = "fast_scan_next.txt"

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

    return ROOT / "data" / _STATE_NAME


def next_due_at() -> float:
    path = _state_path()
    if not path.is_file():
        return 0.0
    try:
        return float(path.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def should_run(now: float | None = None) -> bool:
    return (now if now is not None else time.time()) >= next_due_at()


def mark_ran(now: float | None = None, rng: random.Random | None = None) -> float:
    stamp = now if now is not None else time.time()
    wait = (rng or random).randint(_MIN_GAP_SEC, _MAX_GAP_SEC)
    due = stamp + wait
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{due:.0f}\n", encoding="utf-8")
    return due


def main() -> int:
    if not should_run():
        remain = max(0, int(next_due_at() - time.time()))
        log.info("fast scan skipped — next due in %ss", remain)
        return 0

    port = os.environ.get("PORT") or "8765"
    url = f"http://127.0.0.1:{port}/api/fast-scan"
    req = urllib.request.Request(
        url,
        data=b'{"phone_id":"all"}',
        headers={"Content-Type": "application/json", **_basic_header()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            log.info("queued via dashboard: %s", body)
            due = mark_ran()
            log.info("next fast scan at %s", time.strftime("%H:%M", time.localtime(due)))
            return 0
    except urllib.error.URLError as exc:
        log.warning("dashboard not reachable (%s) — running fast scan inline", exc)

    from src.phones import phone_ids
    from src.sync_chats import fast_reply_scan

    failed = False
    for pid in phone_ids() or ["toby"]:
        ok, msg = fast_reply_scan(phone_id=pid)
        log.info("%s %s — %s", pid, "ok" if ok else "fail", msg)
        failed = failed or not ok
    due = mark_ran()
    log.info("next fast scan at %s", time.strftime("%H:%M", time.localtime(due)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
