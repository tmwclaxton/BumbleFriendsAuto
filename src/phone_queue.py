"""Per-phone serialized action queues shared by dashboard, MCP, and cron."""

from __future__ import annotations

import json
import logging
import threading
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
    "unmatch_expired",
    "rematch_expired",
    "whatsapp_group",
    "whatsapp_add",
}

_job_seq = 0
_seq_lock = threading.Lock()
_cancel_ids: set[int] = set()
_worker_started = False
_banks: dict[str, "_PhoneBank"] = {}
_banks_lock = threading.Lock()


class QueueCancelled(Exception):
    """Raised by long phone jobs when the inbox asks them to stop."""


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
        job = dict(item)
        if job.get("status") == "running":
            job["status"] = "queued"
            job["error"] = None
        if job.get("status") != "queued":
            continue
        job["phone_id"] = str(job.get("phone_id") or default_phone)
        restored.append(job)
    return restored


def load_queue() -> None:
    global _job_seq
    legacy = ROOT / "data" / "action_queue.json"
    restored_total = 0
    max_id = 0
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
    for bank in _all_banks():
        with bank.lock:
            for job in bank.jobs:
                if int(job["id"]) != job_id:
                    continue
                job.update(fields)
                done = [j for j in bank.jobs if j["status"] in {"done", "error", "cancelled"}]
                if len(done) > 40:
                    keep_done = done[-20:]
                    keep_ids = {id(j) for j in keep_done}
                    bank.jobs[:] = [
                        j
                        for j in bank.jobs
                        if j["status"] in {"queued", "running"} or id(j) in keep_ids
                    ]
                bank.persist()
                return


def _next_queued(phone_id: str) -> dict | None:
    bank = _bank(phone_id)
    with bank.lock:
        for job in bank.jobs:
            if job["status"] == "queued":
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
        _update_job(job["id"], status="running")
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
        _update_job(
            job["id"],
            status="cancelled" if cancelled else ("done" if ok else "error"),
            message=message,
            error=None if cancelled or ok else message,
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
