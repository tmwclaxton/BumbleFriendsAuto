"""Cron entry: enqueue a full inbox recapture on the shared phone queue.

When the dashboard is already running, POST to its API so work shares the queue.
Otherwise run recapture_inbox directly (standalone cron container).
"""

from __future__ import annotations

import json
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
    url = f"http://127.0.0.1:{port}/api/recapture"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json", **_basic_header()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            log.info("queued via dashboard: %s", body)
            return 0
    except urllib.error.URLError as exc:
        log.warning("dashboard not reachable (%s) — running recapture inline", exc)

    from src.sync_chats import recapture_inbox

    ok, msg = recapture_inbox()
    log.info("%s — %s", "ok" if ok else "fail", msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
