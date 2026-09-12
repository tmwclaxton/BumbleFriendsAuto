"""Cron entry: enqueue a conservative LinkedIn scan (feed cover + unread opens).

Scheduled 11:00 and 19:00. Skips if the last cron run was under 4 hours ago.
Dashboard enqueue also skips a phone that already has an occupying Bumble job.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_MIN_GAP_SEC = 4 * 60 * 60
_STATE_NAME = "linkedin_scan_last.txt"

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


def last_ran_at() -> float:
    path = _state_path()
    if not path.is_file():
        return 0.0
    try:
        return float(path.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def should_run(now: float | None = None) -> bool:
    stamp = now if now is not None else time.time()
    last = last_ran_at()
    return last <= 0 or (stamp - last) >= _MIN_GAP_SEC


def mark_ran(now: float | None = None) -> float:
    stamp = now if now is not None else time.time()
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{stamp:.0f}\n", encoding="utf-8")
    return stamp


def main() -> int:
    if not should_run():
        remain = max(0, int(_MIN_GAP_SEC - (time.time() - last_ran_at())))
        log.info("linkedin scan skipped — last run %ss ago", int(time.time() - last_ran_at()))
        log.info("next eligible in %ss", remain)
        return 0

    port = os.environ.get("PORT") or "8765"
    url = f"http://127.0.0.1:{port}/api/li/scan"
    req = urllib.request.Request(
        url,
        data=b'{"phone_id":"all","cron":true}',
        headers={"Content-Type": "application/json", **_basic_header()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            log.info("queued via dashboard: %s", body)
            mark_ran()
            return 0
    except urllib.error.URLError as exc:
        log.warning("dashboard not reachable (%s) — running LinkedIn scan inline", exc)

    from src.linkedin_sync import run_scan
    from src.phones import phone_ids

    failed = False
    for pid in phone_ids() or ["toby"]:
        ok, msg = run_scan(phone_id=pid)
        log.info("%s %s — %s", pid, "ok" if ok else "fail", msg)
        failed = failed or not ok
    mark_ran()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
