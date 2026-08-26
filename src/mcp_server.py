"""MCP tools for lgspipeline — start phone jobs, then poll until done."""

from __future__ import annotations

import logging
import time

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from src.config import load_config
from src.phone_queue import enqueue, ensure_worker, get_job, job_poll_payload, queue_snapshot
from src.store import connect as db_connect, db_path_from_config, is_new_friend, list_needs_reply, list_people, list_thread

log = logging.getLogger(__name__)

mcp = FastMCP(
    "lgspipeline",
    instructions=(
        "Bumble Friends (LGS) inbox on admin.grantgunner.org. "
        "Phone actions are asynchronous: call start_* tools, then poll get_job "
        "(or wait_for_job) until status is done or error. Never treat queued/running as success. "
        "Full inbox recapture can take up to ~40 minutes."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            "lgspipeline.grantgunner.org",
            "lgspipeline.grantgunner.org:*",
        ],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
            "https://lgspipeline.grantgunner.org",
            "https://lgspipeline.grantgunner.org:*",
        ],
    ),
)


def _db():
    return db_connect(db_path_from_config(load_config()))


@mcp.tool()
def list_inbox_people() -> dict:
    """List everyone in the SQLite inbox with status, last message, and phone_provided flag. Fast — no phone."""
    conn = _db()
    try:
        people = []
        for row in list_people(conn):
            people.append(
                {
                    "name": row["name"],
                    "status": row["status"] or "unknown",
                    "last_from": row["last_from"],
                    "last_text": row["last_text"],
                    "preview": row["preview"],
                    "phone_provided": bool(row["phone_provided"]),
                    "draft": row["draft"] or "",
                    "opener_sent": bool(row["opener_sent"]),
                    "new_friend": is_new_friend(row),
                    "message_until": row["message_until"],
                }
            )
        return {"count": len(people), "people": people}
    finally:
        conn.close()


@mcp.tool()
def list_needs_reply_people() -> dict:
    """People marked needs_reply (Your turn / last message from them). Fast — no phone."""
    conn = _db()
    try:
        rows = []
        for row in list_needs_reply(conn):
            rows.append(
                {
                    "name": row["name"],
                    "badge": row["badge"],
                    "last_from": row["last_from"],
                    "last_text": row["last_text"],
                    "preview": row["preview"],
                }
            )
        return {"count": len(rows), "people": rows}
    finally:
        conn.close()


@mcp.tool()
def get_thread(name: str) -> dict:
    """Return the stored message thread for a person. Fast — no phone."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    conn = _db()
    try:
        msgs = [{"side": r["side"], "body": r["body"]} for r in list_thread(conn, name)]
        return {"ok": True, "name": name, "messages": msgs, "count": len(msgs)}
    finally:
        conn.close()


@mcp.tool()
def list_jobs() -> dict:
    """List recent phone-queue jobs (queued, running, done, error)."""
    ensure_worker()
    return {"jobs": queue_snapshot()}


@mcp.tool()
def start_refresh_chat(name: str) -> dict:
    """Enqueue opening one chat on the phone and refreshing its transcript. Returns immediately with job_id — poll get_job."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    ensure_worker()
    job = enqueue("refresh", name)
    return {
        "ok": True,
        "job_id": job["id"],
        "status": job["status"],
        "suggested_wait_seconds": 8,
        "hint": "Poll get_job until status is done or error.",
    }


@mcp.tool()
def start_send_reply(name: str, text: str) -> dict:
    """Enqueue sending a reply on the phone. Returns immediately with job_id — poll get_job. Only use when the user clearly asked to send."""
    name = (name or "").strip()
    text = (text or "").strip()
    if not name or not text:
        return {"ok": False, "error": "name and text required"}
    ensure_worker()
    job = enqueue("reply", name, text)
    return {
        "ok": True,
        "job_id": job["id"],
        "status": job["status"],
        "suggested_wait_seconds": 8,
        "hint": "Poll get_job until status is done or error.",
    }


@mcp.tool()
def start_recapture_inbox() -> dict:
    """Enqueue a full inbox recapture (unlock phone, open every chat, save transcripts). Can take up to ~40 minutes. Returns job_id immediately — poll get_job."""
    ensure_worker()
    job = enqueue("recapture_all", "")
    return {
        "ok": True,
        "job_id": job["id"],
        "status": job["status"],
        "suggested_wait_seconds": 15,
        "hint": "Poll get_job every 10–15s until done/error. Do not treat queued/running as success.",
    }


@mcp.tool()
def start_message_new_friends() -> dict:
    """Enqueue sending the template opener to every empty New-friends match on the phone. Returns job_id — poll get_job. Only use when the user clearly asked to message all new friends."""
    ensure_worker()
    job = enqueue("message_new_friends", "")
    return {
        "ok": True,
        "job_id": job["id"],
        "status": job["status"],
        "suggested_wait_seconds": 15,
        "hint": "Poll get_job until status is done or error. Do not treat queued/running as success.",
    }


@mcp.tool()
def get_job(job_id: int) -> dict:
    """Poll a phone-queue job. Call repeatedly while status is queued or running; suggested_wait_seconds tells you how long to wait before the next poll."""
    ensure_worker()
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "job_id must be an integer"}
    return job_poll_payload(get_job(jid))


@mcp.tool()
def wait_for_job(job_id: int, timeout_seconds: float = 20.0) -> dict:
    """Wait up to timeout_seconds (max 25) for a job, then return its status. Use for short waits; for long jobs prefer get_job in a loop."""
    ensure_worker()
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "job_id must be an integer"}
    timeout = max(1.0, min(float(timeout_seconds or 20.0), 25.0))
    deadline = time.time() + timeout
    while True:
        job = get_job(jid)
        payload = job_poll_payload(job)
        status = payload.get("status")
        if status not in {"queued", "running"}:
            return payload
        if time.time() >= deadline:
            payload["timed_out"] = True
            payload["hint"] = (
                "Still in progress after wait_for_job timeout — call get_job again later."
            )
            return payload
        time.sleep(min(2.0, max(0.5, deadline - time.time())))
