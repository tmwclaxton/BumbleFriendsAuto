"""Serialized phone action queue shared by dashboard, MCP, and cron."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from src.config import ROOT, load_config
from src.store import connect as db_connect, db_path_from_config

log = logging.getLogger(__name__)

_QUEUE_PATH = ROOT / "data" / "action_queue.json"

_jobs: list[dict] = []
_jobs_lock = threading.Lock()
_jobs_wake = threading.Event()
_job_seq = 0
_worker_started = False


def queue_snapshot() -> list[dict]:
    with _jobs_lock:
        return [dict(j) for j in _jobs]


def get_job(job_id: int) -> dict | None:
    with _jobs_lock:
        for job in _jobs:
            if int(job["id"]) == job_id:
                return dict(job)
    return None


def _persist_queue() -> None:
    pending = [j for j in _jobs if j.get("status") in {"queued", "running"}]
    _QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _QUEUE_PATH.write_text(json.dumps(pending, indent=2), encoding="utf-8")


def load_queue() -> None:
    global _job_seq
    if not _QUEUE_PATH.exists():
        return
    try:
        raw = json.loads(_QUEUE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    restored: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        job = dict(item)
        if job.get("status") == "running":
            job["status"] = "queued"
            job["error"] = None
        if job.get("status") != "queued":
            continue
        restored.append(job)
    with _jobs_lock:
        _jobs[:] = restored
        _job_seq = max([int(j.get("id") or 0) for j in _jobs] + [_job_seq])
    if restored:
        log.info("restored %d queued phone action(s)", len(restored))
        _jobs_wake.set()


def enqueue(kind: str, name: str = "", text: str = "") -> dict:
    global _job_seq
    with _jobs_lock:
        _job_seq += 1
        job = {
            "id": _job_seq,
            "kind": kind,
            "name": name,
            "text": text,
            "status": "queued",
            "error": None,
            "message": None,
        }
        _jobs.append(job)
        _persist_queue()
        snap = dict(job)
    _jobs_wake.set()
    return snap


def cancel_job(job_id: int) -> bool:
    with _jobs_lock:
        for job in _jobs:
            if int(job["id"]) == job_id and job["status"] == "queued":
                job["status"] = "cancelled"
                snap = dict(job)
                _persist_queue()
                break
        else:
            return False
    if snap.get("kind") == "reply":
        from src.store import set_draft

        conn = db_connect(db_path_from_config(load_config()))
        try:
            set_draft(conn, str(snap.get("name") or ""), str(snap.get("text") or ""))
        finally:
            conn.close()
    return True


def _update_job(job_id: int, **fields: object) -> None:
    with _jobs_lock:
        for job in _jobs:
            if int(job["id"]) == job_id:
                job.update(fields)
                done = [j for j in _jobs if j["status"] in {"done", "error", "cancelled"}]
                if len(done) > 40:
                    keep_done = done[-20:]
                    keep_ids = {id(j) for j in keep_done}
                    _jobs[:] = [
                        j
                        for j in _jobs
                        if j["status"] in {"queued", "running"} or id(j) in keep_ids
                    ]
                _persist_queue()
                return


def _next_queued() -> dict | None:
    with _jobs_lock:
        for job in _jobs:
            if job["status"] == "queued":
                return dict(job)
    return None


def _run_job(job: dict) -> tuple[bool, str]:
    kind = job["kind"]
    name = job.get("name") or ""
    if kind == "reply":
        from src.messenger import send_named_message

        return send_named_message(name, str(job.get("text") or ""))
    if kind == "refresh":
        from src.sync_chats import refresh_named_chat

        return refresh_named_chat(name)
    if kind == "recapture_all":
        from src.sync_chats import recapture_inbox

        return recapture_inbox()
    if kind == "message_new_friends":
        from src.messenger import message_new_friends

        return message_new_friends()
    return False, f"unknown action {kind}"


def _queue_worker() -> None:
    log.info("phone action queue ready")
    while True:
        job = _next_queued()
        if job is None:
            _jobs_wake.wait(timeout=1.0)
            _jobs_wake.clear()
            continue
        _update_job(job["id"], status="running")
        log.info("queue run #%s %s %s", job["id"], job["kind"], job.get("name") or "")
        try:
            ok, message = _run_job(job)
        except Exception as exc:
            log.exception("queue job failed")
            ok, message = False, str(exc)
        _update_job(
            job["id"],
            status="done" if ok else "error",
            message=message,
            error=None if ok else message,
        )
        if job.get("kind") == "reply":
            from src.store import set_draft

            conn = db_connect(db_path_from_config(load_config()))
            try:
                person = str(job.get("name") or "")
                if ok:
                    set_draft(conn, person, "")
                else:
                    set_draft(conn, person, str(job.get("text") or ""))
            except Exception:
                log.exception("could not save draft after reply job")
            finally:
                conn.close()
        log.info("queue #%s %s — %s", job["id"], "ok" if ok else "fail", message)


def ensure_worker() -> None:
    global _worker_started
    with _jobs_lock:
        if _worker_started:
            return
        _worker_started = True
    load_queue()
    threading.Thread(target=_queue_worker, name="phone-queue", daemon=True).start()


def job_poll_payload(job: dict | None) -> dict:
    if job is None:
        return {
            "ok": False,
            "error": "job not found",
            "status": "missing",
            "suggested_wait_seconds": 0,
        }
    status = str(job.get("status") or "unknown")
    wait = 0
    if status == "queued":
        wait = 8
    elif status == "running":
        kind = str(job.get("kind") or "")
        wait = 20 if kind in {"recapture_all", "message_new_friends"} else 6
    return {
        "ok": True,
        "job_id": job.get("id"),
        "kind": job.get("kind"),
        "name": job.get("name"),
        "status": status,
        "message": job.get("message"),
        "error": job.get("error"),
        "suggested_wait_seconds": wait,
        "hint": (
            "Job still in progress — call get_job again after suggested_wait_seconds. "
            "Do not treat queued/running as success."
            if status in {"queued", "running"}
            else None
        ),
    }
