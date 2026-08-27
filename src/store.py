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
    if "draft" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN draft TEXT")
    if "message_until" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN message_until TEXT")
    _backfill_message_until(conn)
    conn.commit()


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


def next_duplicate_name(conn: sqlite3.Connection, base: str) -> str:
    """Return 'Ada 2', 'Ada 3', … for a second person with the same first name."""
    base = base.strip()
    n = 2
    while True:
        candidate = f"{base} {n}"
        if conn.execute("SELECT 1 FROM people WHERE name = ? COLLATE NOCASE", (candidate,)).fetchone() is None:
            return candidate
        n += 1


def name_aliases(conn: sqlite3.Connection, name: str) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM people WHERE name = ? COLLATE NOCASE OR name GLOB ?",
        (name, f"{name} [0-9]*"),
    ).fetchall()
    return [str(r[0]) for r in rows]


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
    last_norm = " ".join((last_text or "").split()).casefold()
    dismissed_norm = " ".join((dismissed_reply_text or "").split()).casefold()
    if dismissed_norm and last_norm == dismissed_norm:
        return "waiting"
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
    """Clear needs_reply until they send a different last message."""
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
    last_text = (row["last_text"] or row["preview"] or "").strip()
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
        SET dismissed_reply_text = ?, status = 'waiting', updated_at = ?
        WHERE person_id = ?
        """,
        (last_text, _now(), person_id),
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
            SELECT p.name, p.location, p.distance, p.age, p.phone_provided,
                   c.badge, c.status, c.last_from, c.last_text, c.preview,
                   c.opener_sent, c.draft, c.message_until, c.updated_at,
                   (SELECT COUNT(*) FROM messages m WHERE m.person_id = p.id) AS message_count
            FROM people p
            LEFT JOIN chats c ON c.person_id = p.id
            ORDER BY
                CASE c.status
                    WHEN 'needs_reply' THEN 0
                    WHEN 'unknown' THEN 1
                    WHEN 'waiting' THEN 2
                    WHEN 'expired' THEN 3
                    ELSE 4
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
            WHERE c.status = 'needs_reply'
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
        choices=["list", "needs-reply", "thread"],
    )
    parser.add_argument("name", nargs="?", help="Person name for the thread command")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    path = args.db or db_path_from_config(cfg)
    conn = connect(path)
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
