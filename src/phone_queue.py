"""Per-phone serialized action queues shared by dashboard, MCP, and cron."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import ROOT, load_config
from src.phones import (
    DEFAULT_PHONE_ID,
    current_phone_id,
    expand_phone_ids,
    list_phones,
    phone_scope,
    serial_for,
)
from src.store import connect as db_connect, db_path_from_config, find_person

log = logging.getLogger(__name__)

_PERSON_KINDS = {"reply", "refresh", "add_contact", "unmatch", "rematch"}
_INBOX_KINDS = {
    "recapture_all",
    "fast_scan",
    "message_new_friends",
    "grab_photos",
    "refresh_new_friends",
    "swipe",
    "liked_you_scan",
    "liked_you_run",
    "linkedin_scan",
    "linkedin_feed",
    "linkedin_seed",
    "linkedin_backfill",
    "linkedin_reply",
    "linkedin_session",
    "linkedin_archive",
    "linkedin_refresh",
    "linkedin_profile",
    "unmatch_expired",
    "rematch_expired",
    "whatsapp_group",
    "whatsapp_add",
    "instagram_prune",
    "instagram_feed",
    "hinge_swipe",
    "hinge_refresh",
    "hinge_reply",
    "hinge_scan",
}

_job_seq = 0
_seq_lock = threading.Lock()
_cancel_ids: set[int] = set()
_worker_started = False
_banks: dict[str, "_PhoneBank"] = {}
_banks_lock = threading.Lock()
_history_lock = threading.Lock()
_HISTORY_LIMIT = 500
_DEFAULT_DURATION_SECONDS = 600.0
_MIN_DURATION_SECONDS = 60.0


class QueueCancelled(Exception):
    """Raised by long phone jobs when the inbox asks them to stop."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _timed_job(job: dict) -> dict:
    """Add timing fields to old persisted queue records without rejecting them."""
    out = dict(job)
    out.setdefault("queued_at", None)
    out.setdefault("started_at", None)
    out.setdefault("finished_at", None)
    out.setdefault("duration_seconds", None)
    return out


def _history_path() -> Path:
    return ROOT / "data" / "queue_runtime_history.json"


def _load_history() -> list[dict]:
    path = _history_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [_timed_job(row) for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []


def _record_history(job: dict) -> None:
    record = _timed_job(job)
    with _history_lock:
        rows = _load_history()
        job_id = record.get("id")
        rows = [row for row in rows if row.get("id") != job_id]
        rows.append(record)
        rows = rows[-_HISTORY_LIMIT:]
        path = _history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def average_duration_seconds(kind: str, phone_id: str | None = None) -> float:
    """Rolling runtime average, preferring samples for this kind and phone.

    Unknown, missing, or zero-length samples fall back to 10 minutes. Averages
    under a minute are treated as unknown so calendar blocks never collapse.
    """
    samples = [
        row
        for row in _load_history()
        if str(row.get("kind") or "") == str(kind)
        and isinstance(row.get("duration_seconds"), (int, float))
        and float(row["duration_seconds"]) > 0
        and bool(row.get("started_at"))
    ]
    if phone_id:
        local = [row for row in samples if str(row.get("phone_id") or "") == str(phone_id)]
        if local:
            samples = local
    if not samples:
        return _DEFAULT_DURATION_SECONDS
    avg = round(sum(float(row["duration_seconds"]) for row in samples) / len(samples), 3)
    if avg < _MIN_DURATION_SECONDS:
        return _DEFAULT_DURATION_SECONDS
    return avg


class _PhoneBank:
    def __init__(self, phone_id: str) -> None:
        self.phone_id = phone_id
        self.path = ROOT / "data" / f"action_queue_{phone_id}.json"
        self.jobs: list[dict] = []
        self.lock = threading.Lock()
        self.wake = threading.Event()

    def persist(self) -> None:
        pending = [j for j in self.jobs if j.get("status") in {"queued", "running"}]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(pending, indent=2), encoding="utf-8")


def _bank(phone_id: str) -> _PhoneBank:
    pid = phone_id or DEFAULT_PHONE_ID
    with _banks_lock:
        bank = _banks.get(pid)
        if bank is None:
            bank = _PhoneBank(pid)
            _banks[pid] = bank
        return bank


def _all_banks() -> list[_PhoneBank]:
    with _banks_lock:
        if not _banks:
            for row in list_phones():
                _bank(str(row["id"]))
        return list(_banks.values())


def _next_id() -> int:
    global _job_seq
    with _seq_lock:
        _job_seq += 1
        return _job_seq


def running_cancelled(phone_id: str | None = None) -> bool:
    pid = phone_id or current_phone_id()
    bank = _bank(pid)
    with bank.lock:
        return any(int(j["id"]) in _cancel_ids for j in bank.jobs if j.get("status") == "running")


def check_cancel() -> None:
    if running_cancelled():
        raise QueueCancelled("cancelled")


BUMBLE_OCCUPY = frozenset(
    {
        "recapture_all",
        "fast_scan",
        "refresh",
        "refresh_new_friends",
        "swipe",
        "liked_you_scan",
        "liked_you_run",
        "message_new_friends",
        "unmatch",
        "unmatch_expired",
        "rematch",
        "rematch_expired",
        "grab_photos",
        "reply",
        "instagram_prune",
        "instagram_feed",
    }
)
HINGE_OCCUPY = frozenset(
    {
        "hinge_swipe",
        "hinge_refresh",
        "hinge_reply",
        "hinge_scan",
    }
)
LINKEDIN_OCCUPY = frozenset(
    {
        "linkedin_scan",
        "linkedin_feed",
        "linkedin_seed",
        "linkedin_backfill",
        "linkedin_reply",
        "linkedin_session",
        "linkedin_archive",
        "linkedin_refresh",
        "linkedin_profile",
    }
)


def job_channel(kind: str | None) -> str:
    k = str(kind or "")
    if k in HINGE_OCCUPY or k.startswith("hinge_"):
        return "hinge"
    if k in LINKEDIN_OCCUPY or k.startswith("linkedin_"):
        return "linkedin"
    if k.startswith("instagram"):
        return "instagram"
    if k.startswith("whatsapp"):
        return "whatsapp"
    if (
        k in BUMBLE_OCCUPY
        or k in {"add_contact", "liked_you_scan", "liked_you_run"}
    ):
        return "bumble"
    return "other"


def job_title(job: dict) -> str:
    kind = str(job.get("kind") or "job")
    name = str(job.get("name") or "").strip()
    labels = {
        "fast_scan": "Fast reply scan",
        "recapture_all": "Refresh all chats",
        "refresh": "Refresh chat",
        "message_new_friends": "Send openers",
        "grab_photos": "Grab photos",
        "refresh_new_friends": "Refresh New friends",
        "swipe": "Swipe session",
        "unmatch": "Unmatch",
        "unmatch_expired": "Unmatch expired",
        "rematch": "Rematch",
        "rematch_expired": "Rematch expired",
        "add_contact": "Add Pixel contact",
        "whatsapp_group": "WhatsApp group",
        "whatsapp_add": "Add to WhatsApp",
        "liked_you_scan": "Liked you scan",
        "liked_you_run": "Liked you run",
        "linkedin_scan": "LinkedIn reply check",
        "linkedin_feed": "LinkedIn feed react",
        "linkedin_seed": "LinkedIn older chats",
        "linkedin_backfill": "LinkedIn older chats",
        "linkedin_reply": "LinkedIn reply",
        "linkedin_session": "LinkedIn session",
        "linkedin_archive": "LinkedIn archive",
        "linkedin_refresh": "LinkedIn refresh",
        "linkedin_profile": "LinkedIn profile",
        "reply": "Bumble reply",
        "instagram_prune": "Instagram prune",
        "instagram_feed": "Instagram following likes",
        "hinge_swipe": "Hinge swipe session",
        "hinge_refresh": "Hinge refresh",
        "hinge_reply": "Hinge reply",
        "hinge_scan": "Hinge reply check",
    }
    label = labels.get(kind, kind.replace("_", " "))
    if name and kind in {
        "refresh",
        "reply",
        "unmatch",
        "rematch",
        "add_contact",
        "linkedin_reply",
        "linkedin_archive",
        "linkedin_refresh",
        "linkedin_profile",
        "whatsapp_group",
        "whatsapp_add",
        "hinge_refresh",
        "hinge_reply",
    }:
        return f"{label} · {name}"
    return label


def header_queue_jobs(
    jobs: list[dict] | None,
    *,
    prefix: str,
    limit: int = 2,
) -> list[dict]:
    """Active jobs shown in a page header strip (queued/running only)."""
    kind_prefix = str(prefix or "")
    active = [
        job
        for job in (jobs or [])
        if str(job.get("kind") or "").startswith(kind_prefix)
        and str(job.get("status") or "") in {"queued", "running"}
    ]
    return active[: max(0, int(limit))]


def queue_board() -> dict:
    """Phones, serial queues, and whether cron will wait for the other channel."""
    from src.phones import public_phones

    raw = queue_snapshot()
    known_ids = {job.get("id") for job in raw}
    raw.extend(row for row in _load_history() if row.get("id") not in known_ids)
    raw = project_job_times(raw)
    jobs = []
    for job in raw:
        item = dict(job)
        item["channel"] = job_channel(item.get("kind"))
        item["title"] = job_title(item)
        jobs.append(item)
    phones = []
    for phone in public_phones():
        pid = str(phone["id"])
        mine = [j for j in jobs if str(j.get("phone_id") or "") == pid]
        active = [j for j in mine if j.get("status") in {"queued", "running"}]
        running = next((j for j in active if j.get("status") == "running"), None)
        queued = [j for j in active if j.get("status") == "queued"]
        recent = [
            j for j in mine if j.get("status") in {"done", "completed", "error", "cancelled"}
        ][-8:]
        holding = (
            running["channel"]
            if running and running.get("channel") in {"bumble", "linkedin", "hinge", "instagram", "whatsapp"}
            else None
        )
        phones.append(
            {
                **phone,
                "holding": holding,
                "holding_title": running["title"] if running else None,
                "linkedin_cron_wait": cron_skip_reason(pid, "linkedin"),
                "bumble_cron_wait": cron_skip_reason(pid, "bumble"),
                "hinge_cron_wait": cron_skip_reason(pid, "hinge"),
                "running": running,
                "queued": queued,
                "recent": recent,
                "active": len(active),
            }
        )
    from src.daily_schedule import today_schedule

    return {
        "jobs": jobs,
        "phones": phones,
        "schedule": today_schedule(),
        "generated_at": _iso(),
        "timezone": "Europe/London",
    }


def project_job_times(jobs: list[dict], *, now: datetime | None = None) -> list[dict]:
    """Add expected durations and serial per-phone projections to queue jobs."""
    stamp = (now or _utc_now()).astimezone(timezone.utc)
    out = [_timed_job(job) for job in jobs]
    cursors: dict[str, datetime] = {}
    for job in out:
        pid = str(job.get("phone_id") or DEFAULT_PHONE_ID)
        expected = average_duration_seconds(str(job.get("kind") or ""), pid)
        job["average_duration_seconds"] = expected
        job["expected_duration_seconds"] = expected
        status = str(job.get("status") or "")
        started = _parse_time(job.get("started_at"))
        finished = _parse_time(job.get("finished_at"))
        if status in {"done", "completed", "error", "cancelled"}:
            job["projected_start_at"] = job.get("started_at")
            finished_at = job.get("finished_at")
            if started and (finished is None or finished <= started):
                finished_at = _iso(started + timedelta(seconds=expected))
            job["projected_end_at"] = finished_at
            continue
        if status == "running":
            start = started or stamp
            end = start + timedelta(seconds=expected)
            job["projected_start_at"] = _iso(start)
            job["projected_end_at"] = _iso(end)
            cursors[pid] = max(stamp, end)
            continue
        if status == "queued":
            start = cursors.get(pid, stamp)
            end = start + timedelta(seconds=expected)
            job["projected_start_at"] = _iso(start)
            job["projected_end_at"] = _iso(end)
            cursors[pid] = end
    return out


def _hold_reason(phone_id: str) -> str | None:
    pid = phone_id or DEFAULT_PHONE_ID
    path = ROOT / "data" / f"phone_hold_{pid}"
    if path.is_file():
        return f"phone hold {pid}"
    return None


def phone_busy_reason(phone_id: str) -> str | None:
    """Any queued or running job on this phone — used for the quiet evening import."""
    pid = phone_id or DEFAULT_PHONE_ID
    held = _hold_reason(pid)
    if held:
        return held
    for job in queue_snapshot():
        if str(job.get("phone_id") or "") != pid:
            continue
        if job.get("status") not in {"queued", "running"}:
            continue
        kind = str(job.get("kind") or "job")
        return f"{kind} {job.get('status')}"
    return None


def cron_skip_reason(phone_id: str, incoming_channel: str) -> str | None:
    """If cron should not enqueue incoming_channel on this phone, return why."""
    pid = phone_id or DEFAULT_PHONE_ID
    held = _hold_reason(pid)
    if held:
        return held
    occupy = set()
    for job in queue_snapshot():
        if str(job.get("phone_id") or "") != pid:
            continue
        if job.get("status") not in {"queued", "running"}:
            continue
        occupy.add(str(job.get("kind") or ""))
    if any(k.startswith("instagram") for k in occupy):
        return "waiting for Instagram job"
    if incoming_channel == "linkedin" and occupy & (BUMBLE_OCCUPY | HINGE_OCCUPY):
        return "waiting for Bumble job" if occupy & BUMBLE_OCCUPY else "waiting for Hinge job"
    if incoming_channel == "linkedin" and occupy & LINKEDIN_OCCUPY:
        return "waiting for LinkedIn job"
    if incoming_channel == "bumble" and occupy & (LINKEDIN_OCCUPY | HINGE_OCCUPY):
        return "waiting for LinkedIn job" if occupy & LINKEDIN_OCCUPY else "waiting for Hinge job"
    if incoming_channel == "hinge" and occupy & (BUMBLE_OCCUPY | LINKEDIN_OCCUPY):
        return "waiting for Bumble job" if occupy & BUMBLE_OCCUPY else "waiting for LinkedIn job"
    if incoming_channel == "hinge" and occupy & HINGE_OCCUPY:
        return "waiting for Hinge job"
    if incoming_channel == "instagram" and occupy:
        return "waiting for other phone job"
    return None


def enqueue_cron(kind: str, *, channel: str, phone_id: str | None = "all") -> tuple[list[dict], list[dict]]:
    """Enqueue occupying jobs only on phones the other channel is not using."""
    jobs: list[dict] = []
    skipped: list[dict] = []
    for pid in expand_phone_ids(phone_id):
        reason = cron_skip_reason(pid, channel)
        if reason:
            skipped.append({"phone_id": pid, "reason": reason})
            continue
        jobs.append(enqueue(kind, phone_id=pid))
    return jobs, skipped


def queue_snapshot() -> list[dict]:
    out: list[dict] = []
    for bank in _all_banks():
        with bank.lock:
            out.extend(dict(j) for j in bank.jobs)
    out.sort(key=lambda j: int(j.get("id") or 0))
    return out


def get_job(job_id: int) -> dict | None:
    for bank in _all_banks():
        with bank.lock:
            for job in bank.jobs:
                if int(job["id"]) == job_id:
                    return dict(job)
    return None


def _load_file(path: Path, default_phone: str) -> list[dict]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    restored: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        job = _timed_job(item)
        if job.get("status") == "running":
            job["status"] = "queued"
            job["error"] = None
        if job.get("status") != "queued":
            continue
        job["phone_id"] = str(job.get("phone_id") or default_phone)
        if not job.get("queued_at"):
            job["queued_at"] = _iso()
        restored.append(job)
    return restored


def load_queue() -> None:
    global _job_seq
    legacy = ROOT / "data" / "action_queue.json"
    restored_total = 0
    max_id = max([int(j.get("id") or 0) for j in _load_history()] + [0])
    for row in list_phones():
        pid = str(row["id"])
        bank = _bank(pid)
        items = _load_file(bank.path, pid)
        if pid == DEFAULT_PHONE_ID and legacy.exists() and not bank.path.exists():
            items.extend(_load_file(legacy, DEFAULT_PHONE_ID))
        with bank.lock:
            bank.jobs[:] = items
            bank.persist()
        restored_total += len(items)
        max_id = max([int(j.get("id") or 0) for j in items] + [max_id])
        if items:
            bank.wake.set()
    with _seq_lock:
        _job_seq = max(_job_seq, max_id)
    if restored_total:
        log.info("restored %d queued phone action(s)", restored_total)


def _resolve_phone_id(kind: str, name: str, phone_id: str | None) -> str:
    from src.phones import normalize_phone_id

    pid = normalize_phone_id(phone_id) if phone_id else ""
    if pid and pid != "all":
        return pid
    if kind.startswith("hinge_") or kind.startswith("instagram_"):
        return "toby"
    if kind in _PERSON_KINDS and name:
        conn = db_connect(db_path_from_config(load_config()))
        try:
            row = find_person(conn, name, phone_id if pid and pid != "all" else None)
            if row is not None:
                return str(row["phone_id"] or DEFAULT_PHONE_ID)
        finally:
            conn.close()
    return DEFAULT_PHONE_ID


def enqueue(
    kind: str,
    name: str = "",
    text: str = "",
    phone_id: str | None = None,
    *,
    force: bool = False,
) -> dict:
    pid = _resolve_phone_id(kind, name, phone_id)
    bank = _bank(pid)
    with bank.lock:
        for existing in bank.jobs:
            if (
                existing.get("status") in {"queued", "running"}
                and existing.get("kind") == kind
                and str(existing.get("name") or "") == name
                and str(existing.get("text") or "") == text
                and str(existing.get("phone_id") or pid) == pid
                and bool(existing.get("force")) == bool(force)
            ):
                return dict(existing)
        job = {
            "id": _next_id(),
            "kind": kind,
            "name": name,
            "text": text,
            "phone_id": pid,
            "force": bool(force),
            "status": "queued",
            "error": None,
            "message": None,
            "queued_at": _iso(),
            "started_at": None,
            "finished_at": None,
            "duration_seconds": None,
        }
        bank.jobs.append(job)
        bank.persist()
        snap = dict(job)
    bank.wake.set()
    return snap


def enqueue_many(kind: str, name: str = "", text: str = "", phone_id: str | None = "all") -> list[dict]:
    jobs = []
    for pid in expand_phone_ids(phone_id):
        jobs.append(enqueue(kind, name, text, phone_id=pid))
    return jobs


def cancel_job(job_id: int) -> bool:
    snap: dict | None = None
    restore_draft = False
    target: _PhoneBank | None = None
    for bank in _all_banks():
        with bank.lock:
            for job in bank.jobs:
                if int(job["id"]) != job_id:
                    continue
                target = bank
                if job["status"] == "queued":
                    job["status"] = "cancelled"
                    job["message"] = "cancelled"
                    job["finished_at"] = _iso()
                    job["duration_seconds"] = 0.0
                    snap = dict(job)
                    restore_draft = snap.get("kind") == "reply"
                    bank.persist()
                elif job["status"] == "running":
                    _cancel_ids.add(int(job["id"]))
                    job["message"] = "cancelling"
                    bank.persist()
                    snap = dict(job)
                break
        if snap is not None:
            break
    if snap is None:
        return False
    if snap.get("status") == "cancelled":
        _record_history(snap)
    if restore_draft and snap:
        from src.store import set_draft
        from src.phones import phone_scope

        conn = db_connect(db_path_from_config(load_config()))
        try:
            with phone_scope(str(snap.get("phone_id") or DEFAULT_PHONE_ID)):
                set_draft(conn, str(snap.get("name") or ""), str(snap.get("text") or ""))
        finally:
            conn.close()
    _ = target
    return True


def cancel_queued() -> int:
    ids: list[int] = []
    for bank in _all_banks():
        with bank.lock:
            for job in bank.jobs:
                if job.get("status") == "queued":
                    ids.append(int(job["id"]))
    n = 0
    for job_id in ids:
        if cancel_job(job_id):
            n += 1
    return n


def _update_job(job_id: int, **fields: object) -> None:
    terminal: dict | None = None
    found = False
    for bank in _all_banks():
        with bank.lock:
            for job in bank.jobs:
                if int(job["id"]) != job_id:
                    continue
                found = True
                job.update(fields)
                done = [
                    j
                    for j in bank.jobs
                    if j["status"] in {"done", "completed", "error", "cancelled"}
                ]
                if len(done) > 40:
                    keep_done = done[-20:]
                    keep_ids = {id(j) for j in keep_done}
                    bank.jobs[:] = [
                        j
                        for j in bank.jobs
                        if j["status"] in {"queued", "running"} or id(j) in keep_ids
                    ]
                bank.persist()
                if job.get("status") in {"done", "completed", "error", "cancelled"}:
                    terminal = dict(job)
                break
        if found:
            break
    if terminal is not None:
        _record_history(terminal)


def mark_job_started(job_id: int, *, now: datetime | None = None) -> str:
    started_at = _iso(now)
    _update_job(
        job_id,
        status="running",
        started_at=started_at,
        finished_at=None,
        duration_seconds=None,
    )
    return started_at


def mark_job_finished(
    job_id: int,
    *,
    ok: bool,
    message: str,
    cancelled: bool = False,
    now: datetime | None = None,
) -> None:
    job = get_job(job_id)
    finished_at = _iso(now)
    started = _parse_time((job or {}).get("started_at"))
    finished = _parse_time(finished_at)
    duration = max(0.0, (finished - started).total_seconds()) if started and finished else 0.0
    _update_job(
        job_id,
        status="cancelled" if cancelled else ("completed" if ok else "error"),
        message=message,
        error=None if cancelled or ok else message,
        finished_at=finished_at,
        duration_seconds=round(duration, 3),
    )


def _next_queued(phone_id: str) -> dict | None:
    held = _hold_reason(phone_id)
    bank = _bank(phone_id)
    with bank.lock:
        for job in bank.jobs:
            if job["status"] != "queued":
                continue
            kind = str(job.get("kind") or "")
            if held and (kind.startswith("linkedin_") or kind in LINKEDIN_OCCUPY):
                continue
            return dict(job)
    return None


def _job_flag(job: dict, key: str) -> bool:
    raw = str(job.get("text") or "").strip()
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return bool(isinstance(data, dict) and data.get(key))


def _run_job(job: dict) -> tuple[bool, str]:
    kind = job["kind"]
    name = job.get("name") or ""
    pid = str(job.get("phone_id") or DEFAULT_PHONE_ID)
    serial = serial_for(pid)
    if kind == "reply":
        from src.messenger import send_named_message

        return send_named_message(
            name,
            str(job.get("text") or ""),
            serial=serial,
            phone_id=pid,
            force=bool(job.get("force")),
        )
    if kind == "refresh":
        from src.sync_chats import refresh_named_chat

        return refresh_named_chat(name, serial=serial, phone_id=pid)
    if kind == "recapture_all":
        from src.sync_chats import recapture_inbox

        return recapture_inbox(serial=serial, phone_id=pid)
    if kind == "fast_scan":
        from src.sync_chats import fast_reply_scan

        return fast_reply_scan(serial=serial, phone_id=pid)
    if kind == "message_new_friends":
        from src.messenger import message_new_friends

        return message_new_friends(serial=serial, phone_id=pid)
    if kind == "grab_photos":
        from src.sync_chats import grab_inbox_photos

        return grab_inbox_photos(serial=serial, phone_id=pid)
    if kind == "refresh_new_friends":
        from src.sync_chats import refresh_new_friends_strip

        return refresh_new_friends_strip(serial=serial, phone_id=pid)
    if kind == "unmatch":
        from src.unmatch import unmatch_named

        return unmatch_named(name, serial=serial, phone_id=pid)
    if kind == "unmatch_expired":
        from src.unmatch import unmatch_expired

        return unmatch_expired(
            serial=serial, phone_id=pid, new_friends_only=_job_flag(job, "new_friends_only")
        )
    if kind == "rematch":
        from src.unmatch import rematch_named

        return rematch_named(name, serial=serial, phone_id=pid)
    if kind == "rematch_expired":
        from src.unmatch import rematch_expired

        return rematch_expired(
            serial=serial, phone_id=pid, new_friends_only=_job_flag(job, "new_friends_only")
        )
    if kind == "swipe":
        from src.swipe_desk import apply_prefs_to_cfg
        from src.swiper import run_session

        cfg = load_config()
        extra: dict = {}
        raw = str(job.get("text") or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                extra = parsed
        extra.setdefault("phone_id", pid)
        cfg = apply_prefs_to_cfg(cfg, extra)
        cfg["phone_id"] = pid
        code = run_session(cfg, serial=serial)
        return code == 0, f"swipe session exit {code}"
    if kind in {"liked_you_scan", "liked_you_run"}:
        from src.liked_you import run_go, run_scan
        from src.swipe_desk import apply_prefs_to_cfg

        cfg = load_config()
        extra: dict = {}
        raw = str(job.get("text") or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                extra = parsed
        extra.setdefault("phone_id", pid)
        cfg = apply_prefs_to_cfg(cfg, extra)
        cfg["phone_id"] = pid
        if kind == "liked_you_scan":
            return run_scan(cfg, serial=serial)
        return run_go(cfg, serial=serial)
    if kind == "linkedin_archive":
        from src.linkedin_sync import archive_named

        return archive_named(name, serial=serial, phone_id=pid)
    if kind == "linkedin_refresh":
        from src.linkedin_sync import refresh_named

        return refresh_named(name, serial=serial, phone_id=pid)
    if kind == "linkedin_profile":
        from src.linkedin_profile import harvest_named

        return harvest_named(name, serial=serial, phone_id=pid)
    if kind == "linkedin_session":
        from src.linkedin_session import run_session

        payload = {}
        raw = str(job.get("text") or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                payload = parsed
        return run_session(load_config(), serial=serial, phone_id=pid, prefs=payload)
    if kind == "linkedin_feed":
        from src.linkedin_sync import run_feed

        return run_feed(load_config(), serial=serial, phone_id=pid)
    if kind in {"linkedin_scan", "linkedin_seed", "linkedin_backfill", "linkedin_reply"}:
        from src.linkedin_sync import run_backfill, run_scan, send_named_message

        if kind == "linkedin_reply":
            return send_named_message(
                name,
                str(job.get("text") or ""),
                serial=serial,
                phone_id=pid,
                force=bool(job.get("force")),
            )
        if kind in {"linkedin_seed", "linkedin_backfill"}:
            return run_backfill(load_config(), serial=serial, phone_id=pid)
        return run_scan(load_config(), serial=serial, phone_id=pid)
    if kind == "add_contact":
        if pid != DEFAULT_PHONE_ID:
            return False, "add_contact is Pixel/Toby only"
        from src.contacts import add_pixel_contact

        payload = {}
        raw = str(job.get("text") or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                payload = parsed
        return add_pixel_contact(
            inbox_name=name,
            contact_name=str(payload.get("contact_name") or name),
            phone=str(payload.get("phone") or ""),
            notes=str(payload.get("notes") or ""),
        )
    if kind in {"whatsapp_group", "whatsapp_add"}:
        if pid != DEFAULT_PHONE_ID:
            return False, "WhatsApp groups run on the Pixel only"
        from src.whatsapp import add_to_group, create_group

        payload = {}
        raw = str(job.get("text") or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                payload = parsed
        people = payload.get("people") if isinstance(payload.get("people"), list) else []
        if kind == "whatsapp_group":
            result = create_group(
                title=str(payload.get("title") or name or ""),
                people=people,
                phone_id=pid,
            )
        else:
            result = add_to_group(
                group=str(payload.get("group") or name or ""),
                people=people,
                phone_id=pid,
            )
        return bool(result.get("ok")), json.dumps(result, ensure_ascii=False)
    if kind == "instagram_prune":
        if pid != DEFAULT_PHONE_ID:
            return False, "instagram_prune is Pixel/Toby only"
        from src.instagram_prune import run_prune

        return run_prune(serial=serial, phone_id=pid)
    if kind == "instagram_feed":
        if pid != DEFAULT_PHONE_ID:
            return False, "instagram feed likes are Pixel/Toby only"
        from src.instagram_feed import run_feed

        return run_feed(serial=serial, phone_id=pid, job_text=str(job.get("text") or ""))
    if kind in {"hinge_swipe", "hinge_refresh", "hinge_reply", "hinge_scan"}:
        from src.hinge_swipe import hinge_live_serial, live_account_id, run_swipe
        from src.hinge_sync import refresh_named, run_scan, send_named_message

        pid = live_account_id(pid)
        serial = hinge_live_serial()
        if kind == "hinge_swipe":
            return run_swipe(serial=serial, phone_id=pid)
        if kind == "hinge_refresh":
            return refresh_named(name, serial=serial, phone_id=pid)
        if kind == "hinge_reply":
            return send_named_message(
                name,
                str(job.get("text") or ""),
                serial=serial,
                phone_id=pid,
                force=bool(job.get("force")),
            )
        return run_scan(serial=serial, phone_id=pid)
    return False, f"unknown action {kind}"


def _queue_worker(phone_id: str) -> None:
    bank = _bank(phone_id)
    log.info("phone action queue ready for %s", phone_id)
    while True:
        job = _next_queued(phone_id)
        if job is None:
            bank.wake.wait(timeout=1.0)
            bank.wake.clear()
            continue
        latest = get_job(int(job["id"]))
        if latest is None or latest.get("status") != "queued":
            continue
        mark_job_started(job["id"])
        log.info(
            "queue %s run #%s %s %s",
            phone_id,
            job["id"],
            job["kind"],
            job.get("name") or "",
        )
        try:
            with phone_scope(phone_id):
                ok, message = _run_job(job)
        except QueueCancelled:
            ok, message = False, "cancelled"
        except Exception as exc:
            log.exception("queue job failed")
            ok, message = False, str(exc)
        cancelled = int(job["id"]) in _cancel_ids or message == "cancelled"
        _cancel_ids.discard(int(job["id"]))
        mark_job_finished(
            job["id"],
            ok=ok,
            message=message,
            cancelled=cancelled,
        )
        if job.get("kind") == "reply":
            from src.store import set_draft

            conn = db_connect(db_path_from_config(load_config()))
            try:
                person = str(job.get("name") or "")
                with phone_scope(phone_id):
                    if ok:
                        set_draft(conn, person, "")
                    else:
                        set_draft(conn, person, str(job.get("text") or ""))
            except Exception:
                log.exception("could not save draft after reply job")
            finally:
                conn.close()
        log.info(
            "queue %s #%s %s — %s",
            phone_id,
            job["id"],
            "cancelled" if cancelled else ("ok" if ok else "fail"),
            message,
        )
        if _next_queued(phone_id) is None:
            try:
                from src.unlock import sleep_screen

                sleep_screen(serial=serial_for(phone_id))
            except Exception:
                log.warning("could not sleep %s after queue went idle", phone_id)


def ensure_worker() -> None:
    global _worker_started
    with _banks_lock:
        if _worker_started:
            return
        _worker_started = True
    load_queue()
    ids = [str(p["id"]) for p in list_phones()]
    if not ids:
        ids = [DEFAULT_PHONE_ID]
    for pid in ids:
        _bank(pid)
        threading.Thread(
            target=_queue_worker, args=(pid,), name=f"phone-queue-{pid}", daemon=True
        ).start()


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
        wait = (
            20
            if kind
            in {
                "recapture_all",
                "fast_scan",
                "message_new_friends",
                "grab_photos",
                "refresh_new_friends",
                "swipe",
                "unmatch_expired",
                "rematch_expired",
                "instagram_prune",
                "instagram_feed",
            }
            else 6
        )
    return {
        "ok": True,
        "job_id": job.get("id"),
        "kind": job.get("kind"),
        "name": job.get("name"),
        "phone_id": job.get("phone_id"),
        "status": status,
        "message": job.get("message"),
        "error": job.get("error"),
        "suggested_wait_seconds": wait,
        "hint": (
            "Job still in progress — call get_job again after suggested_wait_seconds. "
            "Do not treat queued/running as success. Use cancel_job to stop it."
            if status in {"queued", "running"}
            else None
        ),
    }
