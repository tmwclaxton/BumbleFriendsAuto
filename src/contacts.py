"""Add a Bumble person to the Pixel Contacts app via ADB content provider."""

from __future__ import annotations

import logging
import re
import shlex
import subprocess

from src.config import load_config
from src.device import connect
from src.store import (
    connect as db_connect,
    db_path_from_config,
    extract_phones,
    list_thread,
    namesake_meta,
    set_in_contacts,
)
from src.unlock import wake_and_unlock

log = logging.getLogger(__name__)

_IG_RE = re.compile(
    r"(?:instagram\.com/|insta(?:gram)?\s*[:@]?\s*|@)([A-Za-z0-9._]{2,30})",
    re.I,
)
_URI_ID_RE = re.compile(r"/(\d+)\s*$")


def _serial(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    return str(cfg.get("serial") or "").strip()


def _adb(serial: str, *args: str, timeout: float = 20) -> str:
    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=timeout)


def _shell(serial: str, command: str, timeout: float = 20) -> str:
    return _adb(serial, "shell", command, timeout=timeout)


def _bind(key: str, typ: str, value: str | int | None = None) -> str:
    if typ == "n" or value is None:
        return f"--bind {shlex.quote(f'{key}:n:')}"
    return f"--bind {shlex.quote(f'{key}:{typ}:{value}')}"


def extract_instagrams(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    skip = {"letsgosocialuk", "bumble", "instagram"}
    for match in _IG_RE.finditer(text or ""):
        handle = match.group(1).strip("._").lower()
        if len(handle) < 2 or handle in skip or handle in seen:
            continue
        seen.add(handle)
        out.append(handle)
    return out


def _parse_insert_id(raw: str) -> int | None:
    text = (raw or "").strip()
    match = _URI_ID_RE.search(text.splitlines()[-1] if text else "")
    if match:
        return int(match.group(1))
    return None


def _existing_phone_id(serial: str, phone: str) -> int | None:
    digits = re.sub(r"\D+", "", phone or "")
    if len(digits) < 10:
        return None
    tail = digits[-10:]
    where = (
        "mimetype='vnd.android.cursor.item/phone_v2' AND "
        f"replace(replace(replace(data1,' ',''),'-',''),'+','') LIKE '%{tail}'"
    )
    try:
        out = _shell(
            serial,
            "content query --uri content://com.android.contacts/data "
            f"--projection raw_contact_id:data1 --where {shlex.quote(where)}",
        )
    except subprocess.CalledProcessError:
        return None
    match = re.search(r"raw_contact_id=(\d+)", out or "")
    return int(match.group(1)) if match else None


def _google_account(serial: str) -> tuple[str, str] | None:
    try:
        out = _shell(
            serial,
            "content query --uri content://com.android.contacts/raw_contacts "
            "--projection account_name:account_type "
            "--where \"account_type='com.google'\"",
        )
    except subprocess.CalledProcessError:
        return None
    # content query prints "account_name=foo@bar.com, account_type=com.google"
    # — the comma is a field separator, not part of the value.
    name = re.search(r"account_name=([^,\s]+)", out or "")
    typ = re.search(r"account_type=([^,\s]+)", out or "")
    if name and typ and "@" in name.group(1):
        return name.group(1), typ.group(1)
    return None


def add_pixel_contact(
    *,
    inbox_name: str,
    contact_name: str,
    phone: str = "",
    notes: str = "",
    serial: str | None = None,
) -> tuple[bool, str]:
    """Unlock, write a Contacts row, mark the inbox person. Does not open Bumble."""
    inbox_name = (inbox_name or "").strip()
    contact_name = (contact_name or "").strip()
    phone = re.sub(r"\s+", "", phone or "")
    notes = (notes or "").strip()
    if not contact_name:
        return False, "contact_name required"
    cfg = load_config()
    serial = (serial or _serial(cfg)).strip()
    device = connect(serial)
    if not wake_and_unlock(device, serial=serial):
        return False, "phone still locked — unlock failed"

    if phone:
        existing = _existing_phone_id(serial, phone)
        if existing is not None:
            if inbox_name:
                conn = db_connect(db_path_from_config(cfg))
                try:
                    set_in_contacts(conn, inbox_name, True)
                finally:
                    conn.close()
            return True, f"already on Pixel contacts ({contact_name}, id {existing})"

    account = _google_account(serial)
    if account:
        acc_name, acc_type = account
        raw_binds = f"{_bind('account_name', 's', acc_name)} {_bind('account_type', 's', acc_type)}"
    else:
        raw_binds = f"{_bind('account_name', 'n')} {_bind('account_type', 'n')}"
    try:
        inserted = _shell(
            serial,
            "content insert --uri content://com.android.contacts/raw_contacts " + raw_binds,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return False, f"could not create contact: {exc}"
    raw_id = _parse_insert_id(inserted)
    if raw_id is None:
        try:
            tail = _shell(
                serial,
                "content query --uri content://com.android.contacts/raw_contacts "
                "--projection _id --sort '_id DESC'",
            )
        except subprocess.CalledProcessError as exc:
            return False, f"could not create contact: {exc}"
        match = re.search(r"_id=(\d+)", tail)
        raw_id = int(match.group(1)) if match else None
    if raw_id is None:
        return False, f"could not read new contact id ({inserted[:120]!r})"

    given = contact_name.split()[0]
    family = " ".join(contact_name.split()[1:])
    _shell(
        serial,
        "content insert --uri content://com.android.contacts/data "
        f"{_bind('raw_contact_id', 'i', raw_id)} "
        f"{_bind('mimetype', 's', 'vnd.android.cursor.item/name')} "
        f"{_bind('data1', 's', contact_name)} "
        f"{_bind('data2', 's', given)} "
        + (f"{_bind('data3', 's', family)} " if family else ""),
    )
    if phone:
        _shell(
            serial,
            "content insert --uri content://com.android.contacts/data "
            f"{_bind('raw_contact_id', 'i', raw_id)} "
            f"{_bind('mimetype', 's', 'vnd.android.cursor.item/phone_v2')} "
            f"{_bind('data1', 's', phone)} "
            f"{_bind('data2', 'i', 2)}",
        )
    if notes:
        _shell(
            serial,
            "content insert --uri content://com.android.contacts/data "
            f"{_bind('raw_contact_id', 'i', raw_id)} "
            f"{_bind('mimetype', 's', 'vnd.android.cursor.item/note')} "
            f"{_bind('data1', 's', notes)}",
        )
    if inbox_name:
        conn = db_connect(db_path_from_config(cfg))
        try:
            set_in_contacts(conn, inbox_name, True)
        finally:
            conn.close()
    log.info("added Pixel contact %s id=%s phone=%s", contact_name, raw_id, phone or "-")
    return True, f"added {contact_name} to Pixel contacts" + (f" ({phone})" if phone else "")


def contact_preview(name: str) -> dict:
    """What an agent needs to decide the Pixel contact fields. No phone."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        row = conn.execute(
            """
            SELECT p.name, p.location, p.age, p.phone_provided, p.in_contacts, p.notes,
                   c.last_text, c.preview
            FROM people p
            LEFT JOIN chats c ON c.person_id = p.id
            WHERE p.name = ?
            """,
            (name,),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "person not found"}
        extra = namesake_meta(conn).get(name) or {}
        blobs: list[str] = []
        for msg in list_thread(conn, name):
            blobs.append(str(msg["body"] or ""))
        blobs.append(f"{row['last_text'] or ''} {row['preview'] or ''}")
        blob = "\n".join(blobs)
        phones = extract_phones(blob)
        insta = extract_instagrams(blob)
        location = (row["location"] or "").strip()
        distinguish = str(extra.get("distinguish") or "")
        base = str(extra.get("base_name") or name)
        suggested = name
        if extra.get("same_name_count", 1) and int(extra.get("same_name_count") or 1) > 1:
            if location:
                suggested = f"{base} ({location})"
            elif distinguish:
                suggested = f"{base} ({distinguish})"
        note_bits = ["Bumble Friends / LGS"]
        if location:
            note_bits.append(location)
        if insta:
            note_bits.append("ig @" + ", @".join(insta))
        return {
            "ok": True,
            "inbox_name": name,
            "display_name": extra.get("display_name") or name,
            "base_name": base,
            "distinguish": distinguish,
            "location": location,
            "age": row["age"],
            "phone_provided": bool(row["phone_provided"]),
            "in_contacts": bool(row["in_contacts"]),
            "phone_candidates": phones,
            "instagram_candidates": insta,
            "suggested_contact_name": suggested,
            "suggested_notes": " · ".join(note_bits),
            "hint": (
                "Read get_thread if you need more context. Then call start_add_to_contacts "
                "with the contact_name and phone you chose. Do not invent a number that is "
                "not in the thread. Phone is optional if they only gave Insta."
            ),
        }
    finally:
        conn.close()
