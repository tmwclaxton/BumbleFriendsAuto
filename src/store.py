"""SQLite store for BFF people and chat progress."""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import ROOT, load_config

DEFAULT_DB = ROOT / "data" / "friends.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    location TEXT,
    distance TEXT,
    age INTEGER,
    notes TEXT,
    phone_provided INTEGER NOT NULL DEFAULT 0,
    ethnicity TEXT,
    ethnicity_source TEXT,
    in_contacts INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    person_id INTEGER PRIMARY KEY REFERENCES people(id) ON DELETE CASCADE,
    preview TEXT,
    badge TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    last_from TEXT,
    last_text TEXT,
    opener_sent INTEGER NOT NULL DEFAULT 0,
    dismissed_reply_text TEXT,
    draft TEXT,
    message_until TEXT,
    in_group INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    side TEXT NOT NULL,
    body TEXT NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chats_status ON chats(status);
CREATE INDEX IF NOT EXISTS idx_messages_person ON messages(person_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_messages_seq ON messages(person_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path_from_config(cfg: dict[str, Any] | None = None) -> Path:
    cfg = cfg or load_config()
    raw = cfg.get("db_path") or str(DEFAULT_DB)
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or db_path_from_config()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    chat_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(chats)")}
    if "dismissed_reply_text" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN dismissed_reply_text TEXT")
    people_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(people)")}
    if "phone_provided" not in people_cols:
        conn.execute("ALTER TABLE people ADD COLUMN phone_provided INTEGER NOT NULL DEFAULT 0")
    if "ethnicity" not in people_cols:
        conn.execute("ALTER TABLE people ADD COLUMN ethnicity TEXT")
    if "ethnicity_source" not in people_cols:
        conn.execute("ALTER TABLE people ADD COLUMN ethnicity_source TEXT")
    if "in_contacts" not in people_cols:
        conn.execute("ALTER TABLE people ADD COLUMN in_contacts INTEGER NOT NULL DEFAULT 0")
    if "draft" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN draft TEXT")
    if "message_until" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN message_until TEXT")
    if "in_group" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN in_group INTEGER NOT NULL DEFAULT 0")
    _backfill_message_until(conn)
    _reapply_dismissals(conn)
    conn.commit()


def _reapply_dismissals(conn: sqlite3.Connection) -> None:
    """Re-derive status so a stored dismiss survives after a refresh / Your-turn badge."""
    rows = conn.execute(
        """
        SELECT person_id, badge, preview, last_from, last_text, status,
               dismissed_reply_text, message_until
        FROM chats
        WHERE dismissed_reply_text IS NOT NULL AND trim(dismissed_reply_text) != ''
          AND IFNULL(status, '') != 'dismissed'
        """
    ).fetchall()
    for row in rows:
        status = derive_status(
            badge=row["badge"] or "",
            preview=row["preview"] or "",
            last_from=row["last_from"],
            last_text=row["last_text"] or "",
            dismissed_reply_text=row["dismissed_reply_text"],
            message_until=row["message_until"],
        )
        conn.execute(
            "UPDATE chats SET status = ? WHERE person_id = ?",
            (status, row["person_id"]),
        )


def _parse_iso(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


_HOURS_LEFT_RE = re.compile(r"(\d+)\s*hours?\s+left to message", re.I)
_NEW_FRIEND_HINT = re.compile(
    r"hours?\s+left to message|no messages yet|\bextend\b",
    re.I,
)


def parse_hours_left(text: str | None) -> int | None:
    match = _HOURS_LEFT_RE.search(text or "")
    if not match:
        return None
    return int(match.group(1))


def is_match_chrome(text: str | None) -> bool:
    return bool(_NEW_FRIEND_HINT.search((text or "").strip()))


def message_until_from_hours(hours: int, *, observed_at: datetime | None = None) -> str:
    base = observed_at or datetime.now(timezone.utc)
    return (base + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _backfill_message_until(conn: sqlite3.Connection) -> None:
    for row in conn.execute(
        "SELECT person_id, last_text, preview, updated_at, message_until FROM chats"
    ):
        if row["message_until"]:
            continue
        hours = parse_hours_left(row["last_text"]) or parse_hours_left(row["preview"])
        if hours is None:
            continue
        observed = _parse_iso(row["updated_at"]) or datetime.now(timezone.utc)
        last_text = None if parse_hours_left(row["last_text"]) else row["last_text"]
        preview = None if parse_hours_left(row["preview"]) else row["preview"]
        conn.execute(
            """
            UPDATE chats
            SET message_until = ?, last_text = ?, preview = ?
            WHERE person_id = ?
            """,
            (
                message_until_from_hours(hours, observed_at=observed),
                last_text,
                preview,
                int(row["person_id"]),
            ),
        )


_PHONE_RE = re.compile(
    r"(?<!\d)(?:"
    r"(?:\+|00)[\s.\-()]*\d(?:[\s.\-()]*\d){7,14}"
    r"|"
    r"07[\s.\-()]*\d(?:[\s.\-()]*\d){8}"
    r")(?!\d)"
)


def message_has_phone(text: str) -> bool:
    return bool(_PHONE_RE.search(text or ""))


def extract_phones(text: str) -> list[str]:
    """UK-ish numbers from a message, digits normalised, order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _PHONE_RE.finditer(text or ""):
        digits = re.sub(r"\D+", "", match.group(0))
        if digits.startswith("00"):
            digits = digits[2:]
        if digits.startswith("44") and len(digits) >= 12:
            digits = "0" + digits[2:]
        if len(digits) < 10:
            continue
        if digits not in seen:
            seen.add(digits)
            out.append(digits)
    return out


def set_in_contacts(conn: sqlite3.Connection, name: str, in_contacts: bool = True) -> bool:
    name = (name or "").strip()
    if not name:
        return False
    if conn.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone() is None:
        return False
    conn.execute(
        "UPDATE people SET in_contacts = ? WHERE name = ?",
        (1 if in_contacts else 0, name),
    )
    conn.commit()
    return True


def refresh_phone_flags(conn: sqlite3.Connection) -> int:
    """Tag people whose stored texts include a phone number."""
    hits: set[int] = set()
    for row in conn.execute("SELECT person_id, body FROM messages"):
        if message_has_phone(row["body"]):
            hits.add(int(row["person_id"]))
    for row in conn.execute("SELECT person_id, last_text, preview FROM chats"):
        blob = f"{row['last_text'] or ''} {row['preview'] or ''}"
        if message_has_phone(blob):
            hits.add(int(row["person_id"]))
    conn.execute("UPDATE people SET phone_provided = 0")
    for person_id in hits:
        conn.execute("UPDATE people SET phone_provided = 1 WHERE id = ?", (person_id,))
    conn.commit()
    return len(hits)


def upsert_person(
    conn: sqlite3.Connection,
    name: str,
    *,
    location: str | None = None,
    distance: str | None = None,
    age: int | None = None,
    notes: str | None = None,
) -> int:
    name = name.strip()
    now = _now()
    row = conn.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO people (name, location, distance, age, notes, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, location, distance, age, notes, now, now),
        )
        return int(cur.lastrowid)
    fields: list[str] = ["last_seen_at = ?"]
    values: list[Any] = [now]
    if location:
        fields.append("location = ?")
        values.append(location)
    if distance:
        fields.append("distance = ?")
        values.append(distance)
    if age is not None:
        fields.append("age = ?")
        values.append(age)
    if notes:
        fields.append("notes = ?")
        values.append(notes)
    values.append(name)
    conn.execute(f"UPDATE people SET {', '.join(fields)} WHERE name = ?", values)
    return int(row["id"])


def base_person_name(name: str) -> str:
    """'David 2' → 'David'; 'Lauren Mizaela' stays intact."""
    name = (name or "").strip()
    parts = name.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return name


def next_duplicate_name(conn: sqlite3.Connection, base: str) -> str:
    """Return 'Ada 2', 'Ada 3', … for a second person with the same first name."""
    base = base_person_name(base)
    n = 2
    while True:
        candidate = f"{base} {n}"
        if conn.execute("SELECT 1 FROM people WHERE name = ? COLLATE NOCASE", (candidate,)).fetchone() is None:
            return candidate
        n += 1


def name_aliases(conn: sqlite3.Connection, name: str) -> list[str]:
    base = base_person_name(name)
    rows = conn.execute(
        "SELECT name FROM people WHERE name = ? COLLATE NOCASE OR name GLOB ?",
        (base, f"{base} [0-9]*"),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _norm_msg(text: str) -> str:
    t = (text or "").replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    return " ".join(t.split()).casefold()


def _generic_opener(text: str) -> bool:
    return "putting together a wee group" in _norm_msg(text)


def _reply_stub(text: str) -> str:
    norm = _norm_msg(text)
    if norm.endswith("..."):
        return norm[:-3].rstrip(". ").strip()
    if norm.endswith("…"):
        return norm[:-1].rstrip(". ").strip()
    return norm


def same_reply_text(left: str | None, right: str | None) -> bool:
    """True when two last-bubbles are the same message (inbox preview vs full capture)."""
    a, b = _norm_msg(left or ""), _norm_msg(right or "")
    if not a or not b:
        return False
    if a == b:
        return True
    sa, sb = _reply_stub(left or ""), _reply_stub(right or "")
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    a_cut = a.endswith("...") or (left or "").rstrip().endswith("…")
    b_cut = b.endswith("...") or (right or "").rstrip().endswith("…")
    if a_cut and b.startswith(sa):
        return True
    if b_cut and a.startswith(sb):
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 12 and longer.startswith(shorter)


def still_dismissed(
    *,
    last_from: str | None = None,
    last_text: str = "",
    preview: str = "",
    dismissed_reply_text: str | None = None,
) -> bool:
    """Keep a dismiss until they send a different last message — not merely a refresh."""
    dismissed = (dismissed_reply_text or "").strip()
    if not dismissed:
        return False
    if last_from == "you":
        return False
    if (last_text or "").strip():
        return same_reply_text(last_text, dismissed)
    if (preview or "").strip():
        return same_reply_text(preview, dismissed)
    return True


def _is_thread_chrome(text: str) -> bool:
    blob = (text or "").strip()
    if not blob:
        return True
    if is_match_chrome(blob):
        return True
    if "expired" in blob.lower():
        return True
    if re.match(r"^[A-Za-z][A-Za-z'’\-]+,\s*\d{2}$", blob):
        return True
    return blob.lower() in {"extend", "seen", "delivered"}


def _chat_head(conn: sqlite3.Connection, name: str) -> dict[str, str]:
    row = conn.execute(
        """
        SELECT c.last_from, c.last_text, c.preview FROM chats c
        JOIN people p ON p.id = c.person_id WHERE p.name = ?
        """,
        (name,),
    ).fetchone()
    if row is None:
        return {"last_from": "", "last_text": "", "preview": ""}
    return {
        "last_from": str(row["last_from"] or ""),
        "last_text": str(row["last_text"] or ""),
        "preview": str(row["preview"] or ""),
    }


def them_matches_person(conn: sqlite3.Connection, name: str, them_new: set[str]) -> bool:
    """True when newly captured them-text already belongs to this namesake."""
    them_new = {_norm_msg(t) for t in them_new if _norm_msg(t) and not _is_thread_chrome(t)}
    if not them_new:
        return False
    old = _them_bodies(conn, name)
    if them_new & old:
        return True
    for a in them_new:
        for b in old:
            if same_reply_text(a, b):
                return True
    head = _chat_head(conn, name)
    for field in (head["last_text"], head["preview"]):
        if not field or _generic_opener(field):
            continue
        if any(same_reply_text(a, field) for a in them_new):
            return True
    return False


def namesake_same_person(conn: sqlite3.Connection, keep: str, other: str) -> bool:
    """True when two same-first-name rows are the same human, not a real namesake.

    Shared incoming text wins over photos (list crop vs profile crop of the same
    face often looks 'different'). Opener-only stubs stay split when the faces
    clearly differ.
    """
    them_k = _them_bodies(conn, keep)
    them_o = _them_bodies(conn, other)
    if them_k and them_o and them_k & them_o:
        return True
    for a in them_k:
        for b in them_o:
            if same_reply_text(a, b):
                return True
    if them_matches_person(conn, keep, them_o) or them_matches_person(conn, other, them_k):
        return True
    head_k, head_o = _chat_head(conn, keep), _chat_head(conn, other)
    texts_k = [t for t in (head_k["last_text"], head_k["preview"]) if t]
    texts_o = [t for t in (head_o["last_text"], head_o["preview"]) if t]
    if head_k["last_from"] == "them" and head_o["last_from"] == "them":
        for a in texts_k:
            for b in texts_o:
                if same_reply_text(a, b) and not _generic_opener(a):
                    return True
    stub_k, stub_o = not them_k, not them_o
    if stub_k or stub_o:
        from src.photos import photos_conflict

        if photos_conflict(keep, other):
            return False
        return True
    return False


def _them_bodies(conn: sqlite3.Connection, name: str) -> set[str]:
    return {
        _norm_msg(str(r[0]))
        for r in conn.execute(
            """
            SELECT m.body FROM messages m
            JOIN people p ON p.id = m.person_id
            WHERE p.name = ? AND m.side = 'them'
            """,
            (name,),
        )
        if not _is_thread_chrome(str(r[0]))
    }


def _person_id(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT id FROM people WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    return int(row["id"]) if row else None


def _thread_score(conn: sqlite3.Connection, name: str) -> tuple[int, int]:
    them = 0
    total = 0
    for row in list_thread(conn, name):
        if _is_thread_chrome(row["body"]):
            continue
        total += 1
        if row["side"] == "them":
            them += 1
    return them, total


def absorb_person(conn: sqlite3.Connection, keep_name: str, drop_name: str) -> None:
    """Merge drop into keep, preferring the richer transcript."""
    keep_id = _person_id(conn, keep_name)
    drop_id = _person_id(conn, drop_name)
    if keep_id is None or drop_id is None or keep_id == drop_id:
        return
    keep_score = _thread_score(conn, keep_name)
    drop_score = _thread_score(conn, drop_name)
    if drop_score > keep_score:
        richer = [(str(r["side"]), str(r["body"])) for r in list_thread(conn, drop_name)]
        replace_thread(conn, keep_id, richer)
        drop_chat = conn.execute("SELECT * FROM chats WHERE person_id = ?", (drop_id,)).fetchone()
        if drop_chat:
            conn.execute(
                """
                UPDATE chats SET last_from = ?, last_text = ?, preview = ?, badge = ?,
                    status = ?, opener_sent = MAX(opener_sent, ?),
                    message_until = COALESCE(message_until, ?), draft = COALESCE(draft, ?),
                    in_group = MAX(IFNULL(in_group, 0), ?)
                WHERE person_id = ?
                """,
                (
                    drop_chat["last_from"],
                    drop_chat["last_text"],
                    drop_chat["preview"],
                    drop_chat["badge"],
                    drop_chat["status"],
                    int(drop_chat["opener_sent"] or 0),
                    drop_chat["message_until"],
                    drop_chat["draft"],
                    int(drop_chat["in_group"] or 0) if "in_group" in drop_chat.keys() else 0,
                    keep_id,
                ),
            )
    else:
        drop_chat = conn.execute(
            "SELECT draft, in_group FROM chats WHERE person_id = ?",
            (drop_id,),
        ).fetchone()
        if drop_chat and drop_chat["draft"]:
            keep_chat = conn.execute("SELECT draft FROM chats WHERE person_id = ?", (keep_id,)).fetchone()
            if keep_chat is None or not keep_chat["draft"]:
                conn.execute("UPDATE chats SET draft = ? WHERE person_id = ?", (drop_chat["draft"], keep_id))
        if drop_chat and int(drop_chat["in_group"] or 0):
            conn.execute("UPDATE chats SET in_group = 1 WHERE person_id = ?", (keep_id,))
    drop_person = conn.execute(
        "SELECT location, distance, age, phone_provided, ethnicity, ethnicity_source FROM people WHERE id = ?",
        (drop_id,),
    ).fetchone()
    keep_person = conn.execute(
        "SELECT location, distance, age, phone_provided, ethnicity, ethnicity_source FROM people WHERE id = ?",
        (keep_id,),
    ).fetchone()
    if drop_person and keep_person:
        conn.execute(
            """
            UPDATE people SET
                location = COALESCE(NULLIF(location, ''), ?),
                distance = COALESCE(NULLIF(distance, ''), ?),
                age = COALESCE(age, ?),
                phone_provided = MAX(phone_provided, ?),
                ethnicity = COALESCE(NULLIF(ethnicity, ''), ?),
                ethnicity_source = CASE
                    WHEN NULLIF(ethnicity, '') IS NOT NULL THEN ethnicity_source
                    ELSE ?
                END
            WHERE id = ?
            """,
            (
                drop_person["location"],
                drop_person["distance"],
                drop_person["age"],
                int(drop_person["phone_provided"] or 0),
                drop_person["ethnicity"],
                drop_person["ethnicity_source"],
                keep_id,
            ),
        )
    conn.execute("DELETE FROM messages WHERE person_id = ?", (drop_id,))
    conn.execute("DELETE FROM chats WHERE person_id = ?", (drop_id,))
    conn.execute("DELETE FROM people WHERE id = ?", (drop_id,))


def collapse_cloned_namesakes(conn: sqlite3.Connection) -> list[str]:
    """Merge ghost clones (same person stored as Name 2 / Name 3) but keep real namesakes."""
    groups: dict[str, list[str]] = {}
    for row in conn.execute("SELECT name FROM people"):
        name = str(row["name"])
        groups.setdefault(base_person_name(name), []).append(name)
    log = []
    for base, names in groups.items():
        if len(names) < 2:
            continue
        names.sort(key=lambda n: (0 if n.casefold() == base.casefold() else 1, n))
        keep = names[0]
        for other in names[1:]:
            if not namesake_same_person(conn, keep, other):
                continue
            from src.photos import adopt_photo

            absorb_person(conn, keep, other)
            adopt_photo(other, keep)
            log.append(f"{other} → {keep}")
    if log:
        _resync_chat_heads(conn)
        conn.commit()
    return log


def _resync_chat_heads(conn: sqlite3.Connection) -> None:
    """Point last_from / last_text / status at the real last stored bubble."""
    for row in conn.execute(
        """
        SELECT p.name, p.id, c.badge, c.dismissed_reply_text, c.message_until
        FROM people p
        LEFT JOIN chats c ON c.person_id = p.id
        """
    ):
        msgs = [m for m in list_thread(conn, str(row["name"])) if (m["body"] or "").strip()]
        if not msgs:
            continue
        last = msgs[-1]
        status = derive_status(
            badge=row["badge"] or "",
            last_from=last["side"],
            last_text=last["body"],
            dismissed_reply_text=row["dismissed_reply_text"],
            message_until=row["message_until"],
        )
        conn.execute(
            "UPDATE chats SET last_from = ?, last_text = ?, status = ? WHERE person_id = ?",
            (last["side"], last["body"], status, row["id"]),
        )


def namesake_meta(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """How to tell same-name people apart in the inbox / MCP."""
    groups: dict[str, list[str]] = {}
    for row in conn.execute("SELECT name FROM people"):
        name = str(row["name"])
        groups.setdefault(base_person_name(name), []).append(name)
    out: dict[str, dict[str, object]] = {}
    for base, names in groups.items():
        count = len(names)
        for name in names:
            loc = conn.execute("SELECT location FROM people WHERE name = ?", (name,)).fetchone()
            location = (loc["location"] or "").strip() if loc else ""
            them = [
                str(r[0]).strip()
                for r in conn.execute(
                    """
                    SELECT m.body FROM messages m
                    JOIN people p ON p.id = m.person_id
                    WHERE p.name = ? AND m.side = 'them'
                    ORDER BY m.id
                    """,
                    (name,),
                )
                if not _is_thread_chrome(str(r[0])) and len(str(r[0]).strip()) > 2
            ]
            hint = location
            if not hint and them:
                hint = max(them, key=len)
            if not hint:
                chat = conn.execute(
                    """
                    SELECT c.last_text, c.preview FROM chats c
                    JOIN people p ON p.id = c.person_id WHERE p.name = ?
                    """,
                    (name,),
                ).fetchone()
                hint = ((chat["last_text"] or chat["preview"] or "") if chat else "").strip()
            hint = " ".join(hint.split())
            if len(hint) > 42:
                hint = hint[:41].rstrip() + "…"
            display = name if count == 1 else (f"{base} · {hint}" if hint else name)
            out[name] = {
                "base_name": base,
                "display_name": display,
                "distinguish": hint,
                "same_name_count": count,
            }
    return out


def person_them_bodies(conn: sqlite3.Connection, name: str) -> set[str]:
    return {
        str(r[0])
        for r in conn.execute(
            """
            SELECT m.body FROM messages m
            JOIN people p ON p.id = m.person_id
            WHERE p.name = ? AND m.side = 'them'
            """,
            (name,),
        )
    }


def person_message_bodies(conn: sqlite3.Connection, name: str) -> set[str]:
    return {
        str(r[0])
        for r in conn.execute(
            """
            SELECT m.body FROM messages m
            JOIN people p ON p.id = m.person_id
            WHERE p.name = ?
            """,
            (name,),
        )
    }


def derive_status(
    *,
    badge: str = "",
    preview: str = "",
    last_from: str | None = None,
    last_text: str = "",
    dismissed_reply_text: str | None = None,
    message_until: str | None = None,
) -> str:
    blob = f"{badge} {preview} {last_text}".lower()
    until = _parse_iso(message_until)
    if until is not None and until <= datetime.now(timezone.utc) and not last_from:
        return "expired"
    if "expired" in blob:
        return "expired"
    if still_dismissed(
        last_from=last_from,
        last_text=last_text or "",
        preview=preview or "",
        dismissed_reply_text=dismissed_reply_text,
    ):
        return "dismissed"
    # Phone "Your turn" wins over a stale last_from=you from an older opener.
    if badge.strip().lower() == "your turn" or "your turn" in blob:
        return "needs_reply"
    if last_from == "them":
        return "needs_reply"
    if last_from == "you":
        return "waiting"
    if "their turn" in blob:
        return "waiting"
    return "unknown"


def upsert_chat(
    conn: sqlite3.Connection,
    name: str,
    *,
    preview: str | None = None,
    badge: str | None = None,
    last_from: str | None = None,
    last_text: str | None = None,
    opener_sent: bool | None = None,
    message_until: str | None = None,
    location: str | None = None,
    distance: str | None = None,
    age: int | None = None,
) -> int:
    person_id = upsert_person(conn, name, location=location, distance=distance, age=age)
    existing = conn.execute(
        "SELECT preview, badge, last_from, last_text, opener_sent, dismissed_reply_text, message_until FROM chats WHERE person_id = ?",
        (person_id,),
    ).fetchone()
    preview_v = preview if preview is not None else (existing["preview"] if existing else None)
    badge_v = badge if badge is not None else (existing["badge"] if existing else None)
    last_from_v = last_from if last_from is not None else (existing["last_from"] if existing else None)
    last_text_v = last_text if last_text is not None else (existing["last_text"] if existing else None)
    sent_v = (
        int(opener_sent)
        if opener_sent is not None
        else (int(existing["opener_sent"]) if existing else 0)
    )
    hours = parse_hours_left(last_text_v) or parse_hours_left(preview_v)
    if parse_hours_left(last_text_v):
        last_text_v = None
    if parse_hours_left(preview_v):
        preview_v = None
    if message_until is not None:
        until_v = (message_until or "").strip() or None
    elif hours is not None:
        until_v = message_until_from_hours(hours)
    elif sent_v or (last_text_v and not is_match_chrome(last_text_v)):
        until_v = None
    else:
        until_v = existing["message_until"] if existing else None
    if sent_v:
        until_v = None
    status = derive_status(
        badge=badge_v or "",
        preview=preview_v or "",
        last_from=last_from_v,
        last_text=last_text_v or "",
        dismissed_reply_text=existing["dismissed_reply_text"] if existing else None,
        message_until=until_v,
    )
    conn.execute(
        """
        INSERT INTO chats (
            person_id, preview, badge, status, last_from, last_text, opener_sent,
            message_until, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(person_id) DO UPDATE SET
            preview = excluded.preview,
            badge = excluded.badge,
            status = excluded.status,
            last_from = excluded.last_from,
            last_text = excluded.last_text,
            opener_sent = excluded.opener_sent,
            message_until = excluded.message_until,
            updated_at = excluded.updated_at
        """,
        (person_id, preview_v, badge_v, status, last_from_v, last_text_v, sent_v, until_v, _now()),
    )
    return person_id


def dismiss_needs_reply(conn: sqlite3.Connection, name: str) -> bool:
    """Hide needs_reply until they send a different last message (refresh does not undo this)."""
    name = name.strip()
    row = conn.execute(
        """
        SELECT p.id, c.last_text, c.last_from, c.preview, c.badge
        FROM people p
        LEFT JOIN chats c ON c.person_id = p.id
        WHERE p.name = ?
        """,
        (name,),
    ).fetchone()
    if row is None:
        return False
    them = [
        str(m["body"]).strip()
        for m in list_thread(conn, name)
        if (m["body"] or "").strip() and m["side"] == "them" and not _is_thread_chrome(str(m["body"]))
    ]
    last_text = (them[-1] if them else "") or (row["last_text"] or row["preview"] or "").strip()
    person_id = upsert_chat(
        conn,
        name,
        last_from=row["last_from"],
        last_text=row["last_text"],
        preview=row["preview"],
        badge=row["badge"],
    )
    conn.execute(
        """
        UPDATE chats
        SET dismissed_reply_text = ?, status = 'dismissed', updated_at = ?
        WHERE person_id = ?
        """,
        (last_text, _now(), person_id),
    )
    conn.commit()
    return True


def set_in_group(conn: sqlite3.Connection, name: str, in_group: bool) -> bool:
    """File the chat under In group (WhatsApp) or put it back in the main list."""
    name = name.strip()
    if not name:
        return False
    if conn.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone() is None:
        return False
    person_id = upsert_chat(conn, name)
    conn.execute(
        "UPDATE chats SET in_group = ?, updated_at = ? WHERE person_id = ?",
        (1 if in_group else 0, _now(), person_id),
    )
    conn.commit()
    return True


def set_ethnicity(
    conn: sqlite3.Connection,
    name: str,
    ethnicity: str | None,
    *,
    source: str | None = "manual",
) -> bool:
    """Save a Hinge-style ethnicity tag, or clear it with empty/unknown."""
    name = name.strip()
    if not name:
        return False
    if conn.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone() is None:
        return False
    from src.profile_filters import canonicalize

    raw = (ethnicity or "").strip()
    src = (source or "").strip() or None
    if src not in {None, "manual", "vision"}:
        return False
    if not raw or raw.lower() in {"unknown", "none", "clear"}:
        value = None
        if src != "vision":
            src = None
    else:
        value = canonicalize(raw)
        if value is None:
            return False
        if src is None:
            src = "manual"
    upsert_chat(conn, name)
    conn.execute(
        "UPDATE people SET ethnicity = ?, ethnicity_source = ? WHERE name = ?",
        (value, src, name),
    )
    conn.commit()
    return True


def replace_thread(
    conn: sqlite3.Connection,
    person_id: int,
    messages: list[tuple[str, str]],
) -> None:
    """Replace stored transcript with a chronological (oldest-first) capture."""
    conn.execute("DELETE FROM messages WHERE person_id = ?", (person_id,))
    now = _now()
    for seq, (side, body) in enumerate(messages):
        body = body.strip()
        if not body:
            continue
        conn.execute(
            "INSERT INTO messages (person_id, side, body, captured_at) VALUES (?, ?, ?, ?)",
            (person_id, side, body, f"{now}#{seq:04d}"),
        )


def list_thread(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT m.side, m.body, m.captured_at
            FROM messages m
            JOIN people p ON p.id = m.person_id
            WHERE p.name = ?
            ORDER BY m.id
            """,
            (name,),
        )
    )


def add_message(conn: sqlite3.Connection, person_id: int, side: str, body: str) -> None:
    body = body.strip()
    if not body:
        return
    dup = conn.execute(
        """
        SELECT id FROM messages
        WHERE person_id = ? AND side = ? AND body = ?
        ORDER BY id DESC LIMIT 1
        """,
        (person_id, side, body),
    ).fetchone()
    if dup:
        return
    conn.execute(
        "INSERT INTO messages (person_id, side, body, captured_at) VALUES (?, ?, ?, ?)",
        (person_id, side, body, _now()),
    )


def mark_opener_sent(conn: sqlite3.Connection, name: str, body: str) -> None:
    person_id = upsert_chat(
        conn,
        name,
        last_from="you",
        last_text=body,
        opener_sent=True,
        badge="",
    )
    add_message(conn, person_id, "you", body)
    conn.commit()


def set_draft(conn: sqlite3.Connection, name: str, text: str) -> bool:
    """Save or clear the inbox composer draft for this person."""
    name = name.strip()
    if not name:
        return False
    person_id = upsert_chat(conn, name)
    conn.execute(
        "UPDATE chats SET draft = ? WHERE person_id = ?",
        ((text or "").strip() or None, person_id),
    )
    conn.commit()
    return True


def is_new_friend(row: sqlite3.Row | dict) -> bool:
    """True for empty New-friends matches that have not had the opener yet."""
    getter = row.keys() if hasattr(row, "keys") else None

    def _get(key: str, default=None):
        if getter is not None and key in getter:
            return row[key]
        if isinstance(row, dict):
            return row.get(key, default)
        return default

    if (_get("status") or "") == "dismissed":
        return False
    if int(_get("in_group") or 0):
        return False
    if int(_get("opener_sent") or 0):
        return False
    blob = f"{_get('status') or ''} {_get('last_text') or ''} {_get('preview') or ''}"
    if "expired" in blob.lower():
        return False
    until = _parse_iso(_get("message_until"))
    if until is not None and until <= datetime.now(timezone.utc):
        return False
    if until is not None:
        return True
    if _NEW_FRIEND_HINT.search(blob.strip()):
        return True
    n = int(_get("message_count") or 0)
    last = f"{_get('last_text') or ''} {_get('preview') or ''}".strip()
    return n == 0 and not last


def list_people(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    refresh_phone_flags(conn)
    return list(
        conn.execute(
            """
            SELECT p.name, p.location, p.distance, p.age, p.phone_provided, p.ethnicity,
                   p.ethnicity_source, p.in_contacts,
                   c.badge, c.status, c.last_from, c.last_text, c.preview,
                   c.dismissed_reply_text, c.opener_sent, c.draft, c.message_until,
                   c.in_group,
                   c.updated_at,
                   (SELECT COUNT(*) FROM messages m WHERE m.person_id = p.id) AS message_count
            FROM people p
            LEFT JOIN chats c ON c.person_id = p.id
            ORDER BY
                CASE c.status
                    WHEN 'needs_reply' THEN 0
                    WHEN 'unknown' THEN 1
                    WHEN 'waiting' THEN 2
                    WHEN 'expired' THEN 3
                    WHEN 'dismissed' THEN 4
                    ELSE 5
                END,
                p.name COLLATE NOCASE
            """
        )
    )


def names_with_messages(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute(
            """
            SELECT p.name
            FROM people p
            JOIN messages m ON m.person_id = p.id
            GROUP BY p.id
            """
        )
    }


def list_needs_reply(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT p.name, c.badge, c.last_from, c.last_text, c.preview, c.draft, c.updated_at
            FROM chats c
            JOIN people p ON p.id = c.person_id
            WHERE c.status = 'needs_reply' AND IFNULL(c.in_group, 0) = 0
            ORDER BY p.name COLLATE NOCASE
            """
        )
    )


def list_drafts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT p.name, c.draft, c.status, c.last_from, c.last_text, c.preview
            FROM chats c
            JOIN people p ON p.id = c.person_id
            WHERE c.draft IS NOT NULL AND trim(c.draft) != ''
            ORDER BY p.name COLLATE NOCASE
            """
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query the BFF people/chat SQLite store")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--db", type=Path, help="Override SQLite path")
    parser.add_argument(
        "command",
        nargs="?",
        default="list",
        choices=["list", "needs-reply", "thread", "collapse-clones"],
    )
    parser.add_argument("name", nargs="?", help="Person name for the thread command")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    path = args.db or db_path_from_config(cfg)
    conn = connect(path)
    if args.command == "collapse-clones":
        merged = collapse_cloned_namesakes(conn)
        if not merged:
            print("(no cloned namesakes)")
            return 0
        for line in merged:
            print(line)
        print(f"{len(merged)} merged")
        return 0
    if args.command == "thread":
        if not args.name:
            parser.error("thread requires a name")
        rows = list_thread(conn, args.name)
        if not rows:
            print("(no messages)")
            return 0
        for row in rows:
            print(f"[{row['side']}] {row['body']}")
        return 0
    rows = list_needs_reply(conn) if args.command == "needs-reply" else list_people(conn)
    if not rows:
        print("(empty)")
        return 0
    for row in rows:
        if args.command == "needs-reply":
            snippet = row["last_text"] or row["preview"] or ""
            print(f"{row['name']}\t{snippet}")
        else:
            loc = row["location"] or ""
            dist = row["distance"] or ""
            where = " · ".join(x for x in (loc, dist) if x)
            snippet = (row["last_text"] or row["preview"] or "")[:70]
            extra = f" ({where})" if where else ""
            print(f"{row['name']}{extra}\t{row['status'] or '-'}\t{snippet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
