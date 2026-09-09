"""Cron entry: enqueue a fast reply scan on the shared phone queue.

The fast scan scrolls the inbox list and opens only chats whose preview or
'Your turn' badge disagree with the stored last message — minutes, not ~40.
It also refreshes the New friends strip and rematches expired circles there.
When the dashboard is already running, POST to its API so work shares the
queue. Otherwise run fast_reply_scan directly (standalone cron container).
"""

from __future__ import annotations

import logging
import os
import sys
import urllib.error
import urllib.request

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


def main() -> int:
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
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
