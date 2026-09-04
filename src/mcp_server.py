"""MCP tools for lgspipeline — start phone jobs, then poll until done."""

from __future__ import annotations

import logging
import time

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from src.config import load_config
from src.phone_queue import (
    cancel_job as cancel_phone_job,
    enqueue,
    ensure_worker,
    get_job as fetch_job,
    job_poll_payload,
    queue_snapshot,
)
from src.store import (
    connect as db_connect,
    db_path_from_config,
    is_new_friend,
    list_drafts as fetch_drafts,
    namesake_meta,
    list_needs_reply,
    list_people,
    list_thread,
    set_draft,
    set_ethnicity,
    set_in_group,
)

log = logging.getLogger(__name__)

mcp = FastMCP(
    "lgspipeline",
    instructions=(
        "Bumble Friends (LGS) inbox on admin.grantgunner.org. "
        "You cannot send Bumble messages. Draft with save_draft, revise with tweak_draft; "
        "Toby sends from the inbox UI. There is no send-reply or message-new-friends tool. "
        "Read tools (list_*, get_thread, prepare_contact) are instant. "
        "To save someone to the Pixel Contacts app: prepare_contact, decide the real name and "
        "number from the thread (do not invent a number), then start_add_to_contacts and poll "
        "get_job. There is no inbox button for this. "
        "Phone capture tools (start_refresh_chat, start_refresh_new_friends, "
        "start_recapture_inbox) are asynchronous: poll get_job until done, error, or cancelled. "
        "Use cancel_job to drop a queued action or stop a running one at the next checkpoint. "
        "Never treat queued/running as success. Full inbox recapture can take up to ~40 minutes."
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


def _read_draft(conn, name: str) -> str:
    row = conn.execute(
        """
        SELECT c.draft FROM people p
        LEFT JOIN chats c ON c.person_id = p.id
        WHERE p.name = ?
        """,
        (name,),
    ).fetchone()
    return (row["draft"] or "") if row else ""


@mcp.tool()
def list_inbox_people() -> dict:
    """List everyone in the SQLite inbox with status, last message, and phone_provided flag. Fast — no phone."""
    conn = _db()
    try:
        people = []
        labels = namesake_meta(conn)
        for row in list_people(conn):
            extra = labels.get(str(row["name"])) or {}
            people.append(
                {
                    "name": row["name"],
                    "display_name": extra.get("display_name") or row["name"],
                    "base_name": extra.get("base_name") or row["name"],
                    "distinguish": extra.get("distinguish") or "",
                    "same_name_count": int(extra.get("same_name_count") or 1),
                    "status": row["status"] or "unknown",
                    "last_from": row["last_from"],
                    "last_text": row["last_text"],
                    "preview": row["preview"],
                    "phone_provided": bool(row["phone_provided"]),
                    "draft": row["draft"] or "",
                    "opener_sent": bool(row["opener_sent"]),
                    "new_friend": is_new_friend(row),
                    "message_until": row["message_until"],
                    "in_group": bool(row["in_group"]),
                    "ethnicity": row["ethnicity"] or "",
                    "ethnicity_source": row["ethnicity_source"] or "",
                    "in_contacts": bool(row["in_contacts"]),
                }
            )
        return {"count": len(people), "people": people}
    finally:
        conn.close()


@mcp.tool()
def list_needs_reply_people() -> dict:
    """People marked needs_reply (Your turn / last message from them), excluding those filed as in the group chat. Fast — no phone."""
    conn = _db()
    try:
        rows = []
        labels = namesake_meta(conn)
        for row in list_needs_reply(conn):
            extra = labels.get(str(row["name"])) or {}
            rows.append(
                {
                    "name": row["name"],
                    "display_name": extra.get("display_name") or row["name"],
                    "distinguish": extra.get("distinguish") or "",
                    "same_name_count": int(extra.get("same_name_count") or 1),
                    "badge": row["badge"],
                    "last_from": row["last_from"],
                    "last_text": row["last_text"],
                    "preview": row["preview"],
                    "draft": row["draft"] or "",
                }
            )
        return {"count": len(rows), "people": rows}
    finally:
        conn.close()


@mcp.tool()
def mark_in_group(name: str, in_group: bool = True) -> dict:
    """File someone under In group (added to WhatsApp) or put them back in the main inbox. Does not send, does not touch the phone."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    conn = _db()
    try:
        ok = set_in_group(conn, name, bool(in_group))
    finally:
        conn.close()
    if not ok:
        return {"ok": False, "error": "person not found"}
    return {
        "ok": True,
        "name": name,
        "in_group": bool(in_group),
        "hint": "Filed under In group in the inbox." if in_group else "Back in the main inbox list.",
    }


@mcp.tool()
def set_person_ethnicity(name: str, ethnicity: str = "") -> dict:
    """Tag a person with a Hinge-style ethnicity (white, black, south asian, …) or clear with empty. Inbox filter only — does not touch the phone."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    conn = _db()
    try:
        ok = set_ethnicity(conn, name, ethnicity, source="manual")
    finally:
        conn.close()
    if not ok:
        return {"ok": False, "error": "person or ethnicity not valid"}
    return {"ok": True, "name": name, "ethnicity": (ethnicity or "").strip() or None}


@mcp.tool()
def guess_ethnicity_from_photos(name: str = "", force: bool = False) -> dict:
    """Start NanoGPT vision tagging from stored avatars. Empty name = everyone still untagged. Never overwrites tags you set by hand. Poll get_ethnicity_guess. Does not touch the phone."""
    from src.ethnicity_vision import start_guess

    return start_guess(name=(name or "").strip(), force=bool(force))


@mcp.tool()
def get_ethnicity_guess() -> dict:
    """Status of the photo ethnicity guess job (running, tagged count, last name). Fast — no phone."""
    from src.ethnicity_vision import guess_status

    return guess_status()


@mcp.tool()
def cancel_ethnicity_guess() -> dict:
    """Stop a running photo ethnicity guess after the current face. Does not touch the phone."""
    from src.ethnicity_vision import cancel_guess, guess_status

    cancel_guess()
    return {"ok": True, **guess_status()}


@mcp.tool()
def list_same_name_people() -> dict:
    """People who share a Bumble first name. distinguish is location or their words so you can tell them apart. Numbered Name 2 rows that are only recapture clones should already have been merged."""
    conn = _db()
    try:
        labels = namesake_meta(conn)
        groups: dict[str, list[dict]] = {}
        for row in list_people(conn):
            extra = labels.get(str(row["name"])) or {}
            count = int(extra.get("same_name_count") or 1)
            if count < 2:
                continue
            base = str(extra.get("base_name") or row["name"])
            groups.setdefault(base, []).append(
                {
                    "name": row["name"],
                    "display_name": extra.get("display_name") or row["name"],
                    "distinguish": extra.get("distinguish") or "",
                    "status": row["status"] or "unknown",
                    "last_text": row["last_text"],
                    "same_name_count": count,
                }
            )
        return {
            "group_count": len(groups),
            "groups": [{"base_name": k, "people": v} for k, v in sorted(groups.items())],
        }
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
        draft = _read_draft(conn, name)
        return {
            "ok": True,
            "name": name,
            "messages": msgs,
            "count": len(msgs),
            "draft": draft,
        }
    finally:
        conn.close()


@mcp.tool()
def list_jobs() -> dict:
    """List recent phone-queue jobs (queued, running, done, error, cancelled)."""
    ensure_worker()
    return {"jobs": queue_snapshot()}


@mcp.tool()
def prepare_contact(name: str) -> dict:
    """Gather name / location / phone / Insta candidates from the stored thread so you can decide what to save on the Pixel. Fast — no phone. Then call start_add_to_contacts with the fields you chose."""
    from src.contacts import contact_preview

    return contact_preview(name)


@mcp.tool()
def start_add_to_contacts(
    inbox_name: str,
    contact_name: str,
    phone: str = "",
    notes: str = "",
) -> dict:
    """Queue writing a contact onto the Pixel Contacts app. You must pick contact_name and phone from the thread (prepare_contact / get_thread) — do not invent a number. Returns job_id; poll get_job. Does not send a Bumble message."""
    import json

    inbox_name = (inbox_name or "").strip()
    contact_name = (contact_name or "").strip()
    phone = (phone or "").strip()
    notes = (notes or "").strip()
    if not inbox_name:
        return {"ok": False, "error": "inbox_name required"}
    if not contact_name:
        return {"ok": False, "error": "contact_name required — decide the name first via prepare_contact"}
    conn = _db()
    try:
        row = conn.execute("SELECT name FROM people WHERE name = ?", (inbox_name,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "error": "person not in inbox"}
    ensure_worker()
    job = enqueue(
        "add_contact",
        inbox_name,
        json.dumps({"contact_name": contact_name, "phone": phone, "notes": notes}),
    )
    return {
        "ok": True,
        "job_id": job["id"],
        "status": job["status"],
        "inbox_name": inbox_name,
        "contact_name": contact_name,
        "phone": phone or None,
        "suggested_wait_seconds": 8,
        "hint": "Poll get_job until done/error/cancelled. This writes the Pixel address book only.",
    }


@mcp.tool()
def start_refresh_chat(name: str) -> dict:
    """Wake/unlock the phone, open one chat, and refresh its transcript. Returns immediately with job_id — poll get_job."""
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
        "hint": "Poll get_job until status is done, error, or cancelled.",
    }


@mcp.tool()
def save_draft(name: str, text: str) -> dict:
    """Save a reply draft for Toby to send later from the inbox UI. Does not send, does not touch the phone."""
    name = (name or "").strip()
    text = (text or "").strip()
    if not name or not text:
        return {"ok": False, "error": "name and text required"}
    conn = _db()
    try:
        ok = set_draft(conn, name, text)
    finally:
        conn.close()
    if not ok:
        return {"ok": False, "error": "could not save draft"}
    return {
        "ok": True,
        "name": name,
        "draft": text,
        "sent": False,
        "hint": "Draft saved. It is not on Bumble until Toby sends it from the inbox UI.",
    }


@mcp.tool()
def tweak_draft(name: str, text: str) -> dict:
    """Revise an existing unsent draft. Fails if there is no draft yet — use save_draft to create one. Does not send."""
    name = (name or "").strip()
    text = (text or "").strip()
    if not name or not text:
        return {"ok": False, "error": "name and text required"}
    conn = _db()
    try:
        previous = _read_draft(conn, name)
        if not previous:
            return {
                "ok": False,
                "error": f"no draft for {name} — use save_draft to create one first",
                "name": name,
                "draft": "",
                "sent": False,
            }
        if previous == text:
            return {
                "ok": True,
                "name": name,
                "previous": previous,
                "draft": text,
                "changed": False,
                "sent": False,
                "hint": "Draft already matches that text. Nothing changed.",
            }
        ok = set_draft(conn, name, text)
    finally:
        conn.close()
    if not ok:
        return {"ok": False, "error": "could not update draft"}
    return {
        "ok": True,
        "name": name,
        "previous": previous,
        "draft": text,
        "changed": True,
        "sent": False,
        "hint": "Draft updated. It is not on Bumble until Toby sends it from the inbox UI.",
    }


@mcp.tool()
def clear_draft(name: str) -> dict:
    """Clear the saved inbox draft for a person. Does not send, does not touch the phone."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    conn = _db()
    try:
        ok = set_draft(conn, name, "")
    finally:
        conn.close()
    if not ok:
        return {"ok": False, "error": "could not clear draft"}
    return {"ok": True, "name": name, "draft": "", "sent": False}


@mcp.tool()
def list_drafts() -> dict:
    """People who currently have an unsent inbox draft. Fast — no phone."""
    conn = _db()
    try:
        rows = []
        for row in fetch_drafts(conn):
            rows.append(
                {
                    "name": row["name"],
                    "draft": row["draft"] or "",
                    "status": row["status"] or "unknown",
                    "last_from": row["last_from"],
                    "last_text": row["last_text"],
                    "preview": row["preview"],
                }
            )
        return {"count": len(rows), "people": rows}
    finally:
        conn.close()


@mcp.tool()
def start_refresh_new_friends() -> dict:
    """Enqueue a New friends strip scan (unlock phone, swipe the circles, open anyone not already stored). Lighter than a full inbox recapture. Returns job_id immediately — poll get_job."""
    ensure_worker()
    job = enqueue("refresh_new_friends", "")
    return {
        "ok": True,
        "job_id": job["id"],
        "status": job["status"],
        "suggested_wait_seconds": 15,
        "hint": "Poll get_job every 10–15s until done/error/cancelled. Do not treat queued/running as success.",
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
        "hint": "Poll get_job every 10–15s until done/error/cancelled. Do not treat queued/running as success.",
    }


@mcp.tool()
def cancel_job(job_id: int) -> dict:
    """Cancel a queued phone action, or ask a running one (recapture, photo grab, new-friend scan) to stop at the next safe checkpoint."""
    ensure_worker()
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "job_id must be an integer"}
    ok = cancel_phone_job(jid)
    payload = job_poll_payload(fetch_job(jid))
    payload["ok"] = ok
    if not ok:
        payload["error"] = "cannot cancel (already finished or missing)"
    return payload


@mcp.tool()
def get_job(job_id: int) -> dict:
    """Poll a phone-queue job. Call repeatedly while status is queued or running; suggested_wait_seconds tells you how long to wait before the next poll."""
    ensure_worker()
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "job_id must be an integer"}
    return job_poll_payload(fetch_job(jid))


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
        job = fetch_job(jid)
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
