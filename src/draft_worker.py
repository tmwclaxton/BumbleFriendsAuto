"""Background worker: auto-draft needs_reply turns via GPT Sol + Obsidian."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from src.config import load_config
from src.draft_llm import api_key, chat_model, generate_draft
from src.obsidian_mcp import mcp_token, mcp_url
from src.store import (
    claim_auto_draft,
    complete_auto_draft,
    connect as db_connect,
    db_path_from_config,
    fail_auto_draft,
    list_pending_auto_drafts,
    list_thread,
    incoming_turn_fingerprint,
)

log = logging.getLogger(__name__)

_lock = threading.Lock()
_state: dict = {
    "running": False,
    "enabled": True,
    "last_name": "",
    "message": "",
    "error": None,
    "done": 0,
    "failed": 0,
    "skipped": 0,
}
_worker: threading.Thread | None = None
_stop = threading.Event()

_POLL_SEC = 3.0
_MAX_ATTEMPTS_DEFAULT = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _backoff_seconds(attempt: int) -> int:
    # 30s, 60s, 120s, 240s, 480s…
    return min(30 * (2 ** max(0, attempt - 1)), 30 * 60)


def draft_status() -> dict:
    cfg = load_config()
    draft_cfg = dict(cfg.get("draft") or {})
    with _lock:
        snap = {
            "worker": bool(_state["running"]),
            "enabled": bool(_state["enabled"]),
            "last_name": str(_state["last_name"] or ""),
            "message": str(_state["message"] or ""),
            "error": _state["error"],
            "done": int(_state["done"] or 0),
            "failed": int(_state["failed"] or 0),
            "skipped": int(_state["skipped"] or 0),
        }
    snap["configured"] = bool(api_key(cfg) and mcp_token(cfg))
    snap["model"] = chat_model(cfg)
    snap["obsidian_url"] = mcp_url(cfg)
    snap["config_enabled"] = bool(draft_cfg.get("enabled", True))
    return snap


def _bump(**fields: object) -> None:
    with _lock:
        _state.update(fields)


def _max_attempts(cfg: dict) -> int:
    draft_cfg = dict(cfg.get("draft") or {})
    try:
        return max(1, int(draft_cfg.get("max_attempts") or _MAX_ATTEMPTS_DEFAULT))
    except (TypeError, ValueError):
        return _MAX_ATTEMPTS_DEFAULT


def _process_one(conn, row, cfg: dict) -> None:
    person_id = int(row["person_id"])
    name = str(row["name"])
    pending_fp = str(row["draft_pending_fp"] or "")
    attempts = int(row["draft_attempts"] or 0) + 1
    if not pending_fp:
        return
    if not claim_auto_draft(conn, person_id, pending_fp):
        return
    _bump(last_name=name, message=f"drafting {name}")

    # Stale check: live transcript must still end on this fingerprint
    thread = [(str(m["side"]), str(m["body"])) for m in list_thread(conn, name)]
    live_fp = incoming_turn_fingerprint(thread)
    if live_fp != pending_fp:
        fail_auto_draft(
            conn,
            person_id,
            pending_fp=pending_fp,
            error="stale turn — newer messages arrived",
            attempts=attempts,
            next_attempt_at=None,
            give_up=True,
        )
        # Clear pending by completing as skipped: re-enqueue happens on next capture
        conn.execute(
            """
            UPDATE chats SET draft_pending_fp = NULL, draft_status = 'idle',
                   draft_error = NULL, draft_updated_at = ?
            WHERE person_id = ?
            """,
            (_iso(_now()), person_id),
        )
        conn.commit()
        _bump(skipped=int(_state["skipped"]) + 1, message=f"skipped stale {name}")
        return

    try:
        text = generate_draft(conn, name, cfg)
    except Exception as exc:
        log.warning("auto-draft failed for %s: %s", name, exc)
        max_a = _max_attempts(cfg)
        give_up = attempts >= max_a
        nxt = None if give_up else _iso(_now() + timedelta(seconds=_backoff_seconds(attempts)))
        fail_auto_draft(
            conn,
            person_id,
            pending_fp=pending_fp,
            error=str(exc),
            attempts=attempts,
            next_attempt_at=nxt,
            give_up=give_up,
        )
        _bump(failed=int(_state["failed"]) + 1, error=str(exc), message=f"failed {name}")
        return

    # Re-check fingerprint immediately before save
    thread2 = [(str(m["side"]), str(m["body"])) for m in list_thread(conn, name)]
    if incoming_turn_fingerprint(thread2) != pending_fp:
        conn.execute(
            """
            UPDATE chats SET draft_pending_fp = NULL, draft_status = 'idle',
                   draft_error = NULL, draft_updated_at = ?
            WHERE person_id = ? AND draft_pending_fp = ?
            """,
            (_iso(_now()), person_id, pending_fp),
        )
        conn.commit()
        _bump(skipped=int(_state["skipped"]) + 1, message=f"skipped race {name}")
        return

    if not complete_auto_draft(conn, name, pending_fp=pending_fp, text=text):
        fail_auto_draft(
            conn,
            person_id,
            pending_fp=pending_fp,
            error="could not save draft (fingerprint changed)",
            attempts=attempts,
            next_attempt_at=_iso(_now() + timedelta(seconds=30)),
        )
        _bump(failed=int(_state["failed"]) + 1, message=f"save miss {name}")
        return

    _bump(done=int(_state["done"]) + 1, last_name=name, message=f"drafted {name}", error=None)
    log.info("auto-drafted %s (%d chars)", name, len(text))


def process_due_drafts(*, limit: int = 5) -> int:
    """Process up to `limit` due auto-draft jobs. Returns how many were attempted."""
    cfg = load_config()
    draft_cfg = dict(cfg.get("draft") or {})
    if not bool(draft_cfg.get("enabled", True)):
        _bump(message="disabled")
        return 0
    if not api_key(cfg):
        _bump(message="NANOGPT_API_KEY missing", error="NANOGPT_API_KEY missing")
        return 0
    if not mcp_token(cfg):
        _bump(message="OBSIDIAN_MCP_TOKEN missing", error="OBSIDIAN_MCP_TOKEN missing")
        return 0

    conn = db_connect(db_path_from_config(cfg))
    tried = 0
    try:
        rows = list_pending_auto_drafts(conn, limit=limit)
        for row in rows:
            tried += 1
            _process_one(conn, row, cfg)
    finally:
        conn.close()
    return tried


def _loop() -> None:
    _bump(running=True, message="started")
    try:
        while not _stop.is_set():
            try:
                process_due_drafts()
            except Exception as exc:
                log.exception("draft worker loop error")
                _bump(error=str(exc), message="loop error")
            _stop.wait(_POLL_SEC)
    finally:
        _bump(running=False, message="stopped")


def ensure_draft_worker() -> None:
    """Start the daemon auto-draft worker once."""
    global _worker
    cfg = load_config()
    draft_cfg = dict(cfg.get("draft") or {})
    enabled = bool(draft_cfg.get("enabled", True))
    with _lock:
        _state["enabled"] = enabled
        if not enabled:
            return
        if _worker is not None and _worker.is_alive():
            return
        _stop.clear()
        _worker = threading.Thread(target=_loop, name="auto-draft", daemon=True)
        _worker.start()
