"""LinkedIn inbox model — separate tables from Bumble people/chats/messages."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.phones import DEFAULT_PHONE_ID, current_phone_id, normalize_phone_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS li_people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE,
    phone_id TEXT NOT NULL DEFAULT 'toby',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (phone_id, name COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS li_chats (
    person_id INTEGER PRIMARY KEY REFERENCES li_people(id) ON DELETE CASCADE,
    preview TEXT,
    badge TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    last_from TEXT,
    last_text TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS li_messages (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES li_people(id) ON DELETE CASCADE,
    side TEXT NOT NULL,
    body TEXT NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_li_messages_person ON li_messages(person_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scope_phone(phone_id: str | None = None) -> str:
    pid = normalize_phone_id(phone_id) if phone_id else current_phone_id()
    if pid == "all":
        pid = current_phone_id()
    return pid or DEFAULT_PHONE_ID


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _move_channel_rows(conn)


def _move_channel_rows(conn: sqlite3.Connection) -> None:
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(people)")}
    if "channel" not in cols:
        return
    leftover = list(conn.execute("SELECT * FROM people WHERE channel = 'linkedin'"))
    if not leftover:
        return
    now = _now()
    for person in leftover:
        pid = str(person["phone_id"] or DEFAULT_PHONE_ID)
        name = str(person["name"])
        existing = conn.execute(
            "SELECT id FROM li_people WHERE name = ? COLLATE NOCASE AND phone_id = ?",
            (name, pid),
        ).fetchone()
        if existing:
            li_id = int(existing["id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO li_people (name, phone_id, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                """,
                (name, pid, person["first_seen_at"] or now, person["last_seen_at"] or now),
            )
            li_id = int(cur.lastrowid)
        chat = conn.execute(
            "SELECT * FROM chats WHERE person_id = ?", (int(person["id"]),)
        ).fetchone()
        if chat is not None:
            conn.execute(
                """
                INSERT INTO li_chats (
                    person_id, preview, badge, status, last_from, last_text, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(person_id) DO UPDATE SET
                    preview = excluded.preview,
                    badge = excluded.badge,
                    status = excluded.status,
                    last_from = excluded.last_from,
                    last_text = excluded.last_text,
                    updated_at = excluded.updated_at
                """,
                (
                    li_id,
                    chat["preview"],
                    chat["badge"],
                    chat["status"] or "unknown",
                    chat["last_from"],
                    chat["last_text"],
                    chat["updated_at"] or now,
                ),
            )
        for msg in conn.execute(
            "SELECT side, body, captured_at FROM messages WHERE person_id = ? ORDER BY id",
            (int(person["id"]),),
        ):
            conn.execute(
                "INSERT INTO li_messages (person_id, side, body, captured_at) VALUES (?, ?, ?, ?)",
                (li_id, msg["side"], msg["body"], msg["captured_at"]),
            )
    conn.execute("DELETE FROM people WHERE channel = 'linkedin'")


def upsert_person(conn: sqlite3.Connection, name: str, *, phone_id: str | None = None) -> int:
    name = name.strip()
    now = _now()
    pid = _scope_phone(phone_id)
    row = conn.execute(
        "SELECT id FROM li_people WHERE name = ? COLLATE NOCASE AND phone_id = ?",
        (name, pid),
    ).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO li_people (name, phone_id, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
            (name, pid, now, now),
        )
        return int(cur.lastrowid)
    conn.execute("UPDATE li_people SET last_seen_at = ? WHERE id = ?", (now, int(row["id"])))
    return int(row["id"])


def upsert_chat(
    conn: sqlite3.Connection,
    name: str,
    *,
    preview: str | None = None,
    badge: str | None = None,
    last_from: str | None = None,
    last_text: str | None = None,
    phone_id: str | None = None,
) -> int:
    person_id = upsert_person(conn, name, phone_id=phone_id)
    existing = conn.execute(
        "SELECT preview, badge, last_from, last_text FROM li_chats WHERE person_id = ?",
        (person_id,),
    ).fetchone()
    preview_v = preview if preview is not None else (existing["preview"] if existing else None)
    badge_v = badge if badge is not None else (existing["badge"] if existing else None)
    last_from_v = last_from if last_from is not None else (existing["last_from"] if existing else None)
    last_text_v = last_text if last_text is not None else (existing["last_text"] if existing else None)
    status = "needs_reply" if (badge_v or "").lower() == "unread" else (existing and existing["last_from"] and "unknown")
    if (badge_v or "").lower() == "unread" or (last_from_v == "them"):
        status = "needs_reply"
    elif last_from_v == "you":
        status = "waiting"
    else:
        status = "unknown"
    now = _now()
    conn.execute(
        """
        INSERT INTO li_chats (person_id, preview, badge, status, last_from, last_text, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(person_id) DO UPDATE SET
            preview = excluded.preview,
            badge = excluded.badge,
            status = excluded.status,
            last_from = excluded.last_from,
            last_text = excluded.last_text,
            updated_at = excluded.updated_at
        """,
        (person_id, preview_v, badge_v, status, last_from_v, last_text_v, now),
    )
    return person_id


def replace_thread(conn: sqlite3.Connection, person_id: int, messages: list[tuple[str, str]]) -> None:
    conn.execute("DELETE FROM li_messages WHERE person_id = ?", (person_id,))
    now = _now()
    cleaned: list[tuple[str, str]] = []
    for seq, (side, body) in enumerate(messages):
        body = (body or "").strip()
        if not body:
            continue
        cleaned.append((side, body))
        conn.execute(
            "INSERT INTO li_messages (person_id, side, body, captured_at) VALUES (?, ?, ?, ?)",
            (person_id, side, body, f"{now}#{seq:04d}"),
        )
    if cleaned:
        last_side, last_body = cleaned[-1]
        conn.execute(
            "UPDATE li_chats SET last_from = ?, last_text = ? WHERE person_id = ?",
            (last_side, last_body, person_id),
        )


def list_people(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT p.name, p.phone_id, c.badge, c.status, c.last_from, c.last_text, c.preview,
                   c.updated_at,
                   (SELECT COUNT(*) FROM li_messages m WHERE m.person_id = p.id) AS message_count
            FROM li_people p
            LEFT JOIN li_chats c ON c.person_id = p.id
            ORDER BY
                CASE c.status
                    WHEN 'needs_reply' THEN 0
                    WHEN 'unknown' THEN 1
                    WHEN 'waiting' THEN 2
                    ELSE 5
                END,
                p.name COLLATE NOCASE
            """
        )
    )


def list_thread(conn: sqlite3.Connection, name: str, phone_id: str | None = None) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT m.side, m.body, m.captured_at
            FROM li_messages m
            JOIN li_people p ON p.id = m.person_id
            WHERE p.name = ? AND p.phone_id = ?
            ORDER BY m.id
            """,
            (name, _scope_phone(phone_id)),
        )
    )


def get_person(conn: sqlite3.Connection, name: str, phone_id: str | None = None) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT p.name, p.phone_id, c.status, c.last_from, c.last_text, c.preview
        FROM li_people p
        LEFT JOIN li_chats c ON c.person_id = p.id
        WHERE p.name = ? COLLATE NOCASE AND p.phone_id = ?
        """,
        (name, _scope_phone(phone_id)),
    ).fetchone()
