"""SQLite store for BFF people and chat progress."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
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
    return conn


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


def derive_status(
    *,
    badge: str = "",
    preview: str = "",
    last_from: str | None = None,
    last_text: str = "",
) -> str:
    blob = f"{badge} {preview} {last_text}".lower()
    if "expired" in blob:
        return "expired"
    if last_from == "them":
        return "needs_reply"
    if last_from == "you":
        return "waiting"
    if badge.strip().lower() == "your turn" or "your turn" in blob:
        return "needs_reply"
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
    location: str | None = None,
    distance: str | None = None,
    age: int | None = None,
) -> int:
    person_id = upsert_person(conn, name, location=location, distance=distance, age=age)
    existing = conn.execute(
        "SELECT preview, badge, last_from, last_text, opener_sent FROM chats WHERE person_id = ?",
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
    status = derive_status(
        badge=badge_v or "",
        preview=preview_v or "",
        last_from=last_from_v,
        last_text=last_text_v or "",
    )
    conn.execute(
        """
        INSERT INTO chats (
            person_id, preview, badge, status, last_from, last_text, opener_sent, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(person_id) DO UPDATE SET
            preview = excluded.preview,
            badge = excluded.badge,
            status = excluded.status,
            last_from = excluded.last_from,
            last_text = excluded.last_text,
            opener_sent = excluded.opener_sent,
            updated_at = excluded.updated_at
        """,
        (person_id, preview_v, badge_v, status, last_from_v, last_text_v, sent_v, _now()),
    )
    return person_id


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


def list_people(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT p.name, p.location, p.distance, p.age,
                   c.badge, c.status, c.last_from, c.last_text, c.preview,
                   c.opener_sent, c.updated_at
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
            SELECT p.name, c.badge, c.last_from, c.last_text, c.preview, c.updated_at
            FROM chats c
            JOIN people p ON p.id = c.person_id
            WHERE c.status = 'needs_reply'
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
