"""Hinge inbox model — separate tables from Bumble and LinkedIn."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.config import ROOT
from src.phones import DEFAULT_PHONE_ID, current_phone_id, normalize_phone_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS hinge_people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE,
    phone_id TEXT NOT NULL DEFAULT 'toby',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    profile_pic TEXT,
    age INTEGER,
    location TEXT,
    job TEXT,
    school TEXT,
    about TEXT,
    matched_at TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    gender TEXT,
    height TEXT,
    extras_json TEXT,
    ethnicity TEXT,
    UNIQUE (phone_id, name COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS hinge_photos (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES hinge_people(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'extra',
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS hinge_prompts (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES hinge_people(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS hinge_chats (
    person_id INTEGER PRIMARY KEY REFERENCES hinge_people(id) ON DELETE CASCADE,
    preview TEXT,
    badge TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    last_from TEXT,
    last_text TEXT,
    updated_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    draft TEXT,
    draft_status TEXT NOT NULL DEFAULT 'idle',
    draft_error TEXT,
    draft_attempts INTEGER NOT NULL DEFAULT 0,
    draft_pending_fp TEXT,
    draft_updated_at TEXT
);

CREATE TABLE IF NOT EXISTS hinge_messages (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES hinge_people(id) ON DELETE CASCADE,
    side TEXT NOT NULL,
    body TEXT NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hinge_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hinge_messages_person ON hinge_messages(person_id, id);
CREATE INDEX IF NOT EXISTS idx_hinge_photos_person ON hinge_photos(person_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_hinge_prompts_person ON hinge_prompts(person_id, sort_order);
"""

_ALTERS = (
    ("hinge_people", "profile_pic", "TEXT"),
    ("hinge_people", "age", "INTEGER"),
    ("hinge_people", "location", "TEXT"),
    ("hinge_people", "job", "TEXT"),
    ("hinge_people", "school", "TEXT"),
    ("hinge_people", "about", "TEXT"),
    ("hinge_people", "matched_at", "TEXT"),
    ("hinge_people", "verified", "INTEGER NOT NULL DEFAULT 0"),
    ("hinge_people", "gender", "TEXT"),
    ("hinge_people", "height", "TEXT"),
    ("hinge_people", "extras_json", "TEXT"),
    ("hinge_people", "ethnicity", "TEXT"),
    ("hinge_chats", "archived", "INTEGER NOT NULL DEFAULT 0"),
    ("hinge_chats", "draft", "TEXT"),
    ("hinge_chats", "draft_status", "TEXT NOT NULL DEFAULT 'idle'"),
    ("hinge_chats", "draft_error", "TEXT"),
    ("hinge_chats", "draft_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("hinge_chats", "draft_pending_fp", "TEXT"),
    ("hinge_chats", "draft_updated_at", "TEXT"),
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scope_phone(phone_id: str | None = None) -> str:
    pid = normalize_phone_id(phone_id) if phone_id else current_phone_id()
    if pid == "all":
        pid = current_phone_id()
    return pid or "toby"


def _add_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    cols = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if name in cols:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, name, ddl in _ALTERS:
        _add_column(conn, table, name, ddl)
    rehome_galaxy_to_toby(conn)


def rehome_galaxy_to_toby(conn: sqlite3.Connection) -> int:
    """Live Galaxy Hinge is Toby's account. Move any Archie-tagged rows."""
    rows = list(
        conn.execute("SELECT id, profile_pic FROM hinge_people WHERE phone_id = 'archie'")
    )
    if not rows:
        return 0
    conn.execute("UPDATE hinge_people SET phone_id = 'toby' WHERE phone_id = 'archie'")
    moved = 0
    old_dir = ROOT / "data" / "hinge_photos" / "archie"
    new_dir = ROOT / "data" / "hinge_photos" / "toby"
    new_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        pic = str(row["profile_pic"] or "")
        if "/hinge_photos/archie/" in pic:
            dest = pic.replace("/hinge_photos/archie/", "/hinge_photos/toby/")
            conn.execute("UPDATE hinge_people SET profile_pic = ? WHERE id = ?", (dest, row["id"]))
            moved += 1
    conn.execute(
        "UPDATE hinge_photos SET path = REPLACE(path, '/hinge_photos/archie/', '/hinge_photos/toby/') "
        "WHERE path LIKE '%/hinge_photos/archie/%'"
    )
    if old_dir.is_dir():
        import shutil

        for src in old_dir.iterdir():
            dest = new_dir / src.name
            if not dest.exists():
                shutil.move(str(src), str(dest))
    return len(rows)


def photos_dir(phone_id: str | None = None) -> Path:
    path = ROOT / "data" / "hinge_photos" / _scope_phone(phone_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def upsert_person(
    conn: sqlite3.Connection,
    name: str,
    *,
    phone_id: str | None = None,
    profile_pic: str | None = None,
    age: int | None = None,
    location: str | None = None,
    job: str | None = None,
    school: str | None = None,
    about: str | None = None,
    matched_at: str | None = None,
    verified: bool | None = None,
    gender: str | None = None,
    height: str | None = None,
    extras_json: str | None = None,
    ethnicity: str | None = None,
) -> int:
    name = (name or "").strip()
    now = _now()
    pid = _scope_phone(phone_id)
    eth = (ethnicity or "").strip()
    if not eth and extras_json:
        try:
            extra = json.loads(extras_json)
        except json.JSONDecodeError:
            extra = {}
        if isinstance(extra, dict):
            eth = str(extra.get("ethnicity") or extra.get("ethnicities") or "").strip()
    row = conn.execute(
        "SELECT * FROM hinge_people WHERE name = ? COLLATE NOCASE AND phone_id = ?",
        (name, pid),
    ).fetchone()
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO hinge_people (
                name, phone_id, first_seen_at, last_seen_at, profile_pic,
                age, location, job, school, about, matched_at,
                verified, gender, height, extras_json, ethnicity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                pid,
                now,
                now,
                profile_pic,
                age,
                location,
                job,
                school,
                about,
                matched_at,
                1 if verified else 0,
                gender,
                height,
                extras_json,
                eth or None,
            ),
        )
        return int(cur.lastrowid)
    person_id = int(row["id"])
    fields: dict[str, object] = {"last_seen_at": now}
    if profile_pic:
        fields["profile_pic"] = profile_pic
    if age is not None:
        fields["age"] = age
    if location:
        fields["location"] = location.strip()[:200]
    if job:
        fields["job"] = job.strip()[:200]
    if school:
        fields["school"] = school.strip()[:200]
    if about:
        fields["about"] = about.strip()[:2500]
    if matched_at:
        fields["matched_at"] = matched_at
    if verified:
        fields["verified"] = 1
    if gender:
        fields["gender"] = gender.strip()[:80]
    if height:
        fields["height"] = height.strip()[:40]
    elif (row["height"] or "") and extras_json:
        try:
            extra = json.loads(extras_json)
        except json.JSONDecodeError:
            extra = {}
        if isinstance(extra, dict) and str(extra.get("school") or "") == str(row["height"] or ""):
            fields["height"] = None
    if extras_json:
        fields["extras_json"] = extras_json[:4000]
    if eth:
        fields["ethnicity"] = eth[:80]
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE hinge_people SET {sets} WHERE id = ?",
        (*fields.values(), person_id),
    )
    return person_id


def upsert_chat(
    conn: sqlite3.Connection,
    name: str,
    *,
    preview: str | None = None,
    badge: str | None = None,
    last_from: str | None = None,
    last_text: str | None = None,
    phone_id: str | None = None,
    **profile,
) -> int:
    person_id = upsert_person(conn, name, phone_id=phone_id, **profile)
    existing = conn.execute(
        "SELECT preview, badge, last_from, last_text FROM hinge_chats WHERE person_id = ?",
        (person_id,),
    ).fetchone()
    preview_v = preview if preview is not None else (existing["preview"] if existing else None)
    badge_v = badge if badge is not None else (existing["badge"] if existing else None)
    last_from_v = last_from if last_from is not None else (existing["last_from"] if existing else None)
    last_text_v = last_text if last_text is not None else (existing["last_text"] if existing else None)
    if (badge_v or "").lower() == "unread" or last_from_v == "them":
        status = "needs_reply"
    elif last_from_v == "you":
        status = "waiting"
    else:
        status = "unknown"
    now = _now()
    conn.execute(
        """
        INSERT INTO hinge_chats (person_id, preview, badge, status, last_from, last_text, updated_at)
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


def replace_prompts(conn: sqlite3.Connection, person_id: int, prompts: list[tuple[str, str]]) -> None:
    conn.execute("DELETE FROM hinge_prompts WHERE person_id = ?", (person_id,))
    for idx, item in enumerate(prompts):
        question = (item[0] or "").strip()
        answer = (item[1] or "").strip()
        if not question and not answer:
            continue
        conn.execute(
            "INSERT INTO hinge_prompts (person_id, question, answer, sort_order) VALUES (?, ?, ?, ?)",
            (person_id, question[:400], answer[:1200], idx),
        )


def replace_photos(
    conn: sqlite3.Connection,
    person_id: int,
    photos: list[tuple[str, str]],
) -> None:
    conn.execute("DELETE FROM hinge_photos WHERE person_id = ?", (person_id,))
    face = None
    for idx, item in enumerate(photos):
        path = (item[0] or "").strip()
        kind = (item[1] or "extra").strip() or "extra"
        if not path:
            continue
        conn.execute(
            "INSERT INTO hinge_photos (person_id, path, kind, sort_order) VALUES (?, ?, ?, ?)",
            (person_id, path, kind, idx),
        )
        if kind == "face" and face is None:
            face = path
    if face:
        conn.execute("UPDATE hinge_people SET profile_pic = ? WHERE id = ?", (face, person_id))


def replace_thread(conn: sqlite3.Connection, person_id: int, messages: list[tuple]) -> None:
    conn.execute("DELETE FROM hinge_messages WHERE person_id = ?", (person_id,))
    now = _now()
    cleaned: list[tuple[str, str]] = []
    for seq, item in enumerate(messages):
        side = item[0]
        body = (item[1] if len(item) > 1 else "").strip()
        when = item[2] if len(item) > 2 else None
        if not body:
            continue
        cleaned.append((side, body))
        stamp = (when or "").strip() if isinstance(when, str) else ""
        if not stamp:
            stamp = f"{now}#{seq:04d}"
        conn.execute(
            "INSERT INTO hinge_messages (person_id, side, body, captured_at) VALUES (?, ?, ?, ?)",
            (person_id, side, body, stamp),
        )
    if cleaned:
        last_side, last_body = cleaned[-1]
        status = "waiting" if last_side == "you" else "needs_reply"
        conn.execute(
            """
            UPDATE hinge_chats SET last_from = ?, last_text = ?, status = ?, updated_at = ?
            WHERE person_id = ?
            """,
            (last_side, last_body, status, now, person_id),
        )


def add_message(conn: sqlite3.Connection, person_id: int, side: str, body: str) -> None:
    now = _now()
    conn.execute(
        "INSERT INTO hinge_messages (person_id, side, body, captured_at) VALUES (?, ?, ?, ?)",
        (person_id, side, (body or "").strip(), now),
    )
    status = "waiting" if side == "you" else "needs_reply"
    conn.execute(
        """
        UPDATE hinge_chats SET last_from = ?, last_text = ?, status = ?, badge = '', updated_at = ?
        WHERE person_id = ?
        """,
        (side, (body or "").strip(), status, now, person_id),
    )


def list_people(conn: sqlite3.Connection, phone_id: str | None = None) -> list[sqlite3.Row]:
    sql = """
        SELECT p.id, p.name, p.phone_id, p.profile_pic, p.age, p.location, p.job, p.school,
               p.about, p.matched_at, p.last_seen_at, p.verified, p.gender, p.height, p.extras_json,
               p.ethnicity,
               c.badge, c.status, c.last_from, c.last_text, c.preview, c.updated_at, c.archived,
               c.draft, c.draft_status, c.draft_error, c.draft_attempts, c.draft_pending_fp,
               (SELECT COUNT(*) FROM hinge_messages m WHERE m.person_id = p.id) AS message_count
        FROM hinge_people p
        LEFT JOIN hinge_chats c ON c.person_id = p.id
    """
    args: tuple = ()
    if phone_id and normalize_phone_id(phone_id) != "all":
        sql += " WHERE p.phone_id = ?"
        args = (_scope_phone(phone_id),)
    sql += """
        ORDER BY
            CASE WHEN IFNULL(c.archived, 0) = 1 THEN 9 ELSE 0 END,
            CASE c.status
                WHEN 'needs_reply' THEN 0
                WHEN 'unknown' THEN 1
                WHEN 'waiting' THEN 2
                ELSE 5
            END,
            p.name COLLATE NOCASE
    """
    return list(conn.execute(sql, args))


def list_thread(conn: sqlite3.Connection, name: str, phone_id: str | None = None) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT m.side, m.body, m.captured_at
            FROM hinge_messages m
            JOIN hinge_people p ON p.id = m.person_id
            WHERE p.name = ? COLLATE NOCASE AND p.phone_id = ?
            ORDER BY m.captured_at, m.id
            """,
            (name, _scope_phone(phone_id)),
        )
    )


def list_prompts(conn: sqlite3.Connection, person_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT question, answer, sort_order FROM hinge_prompts WHERE person_id = ? ORDER BY sort_order, id",
            (person_id,),
        )
    )


def list_photos(conn: sqlite3.Connection, person_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT path, kind, sort_order FROM hinge_photos WHERE person_id = ? ORDER BY sort_order, id",
            (person_id,),
        )
    )


def get_person(conn: sqlite3.Connection, name: str, phone_id: str | None = None) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT p.id, p.name, p.phone_id, p.profile_pic, p.age, p.location, p.job, p.school,
               p.about, p.matched_at, p.last_seen_at, p.verified, p.gender, p.height, p.extras_json,
               p.ethnicity,
               c.status, c.last_from, c.last_text, c.preview, c.archived,
               c.draft, c.draft_status, c.draft_error, c.draft_attempts, c.draft_pending_fp
        FROM hinge_people p
        LEFT JOIN hinge_chats c ON c.person_id = p.id
        WHERE p.name = ? COLLATE NOCASE AND p.phone_id = ?
        """,
        (name, _scope_phone(phone_id)),
    ).fetchone()


def person_id(conn: sqlite3.Connection, name: str, phone_id: str | None = None) -> int | None:
    row = conn.execute(
        "SELECT id FROM hinge_people WHERE name = ? COLLATE NOCASE AND phone_id = ?",
        (name, _scope_phone(phone_id)),
    ).fetchone()
    return int(row["id"]) if row else None


def set_archived(conn: sqlite3.Connection, name: str, archived: bool, *, phone_id: str | None = None) -> bool:
    pid = person_id(conn, name, phone_id)
    if pid is None:
        return False
    conn.execute(
        """
        INSERT INTO hinge_chats (person_id, status, archived, updated_at)
        VALUES (?, 'unknown', ?, ?)
        ON CONFLICT(person_id) DO UPDATE SET archived = excluded.archived, updated_at = excluded.updated_at
        """,
        (pid, 1 if archived else 0, _now()),
    )
    conn.commit()
    return True


def set_ethnicity(conn: sqlite3.Connection, name: str, ethnicity: str | None, *, phone_id: str | None = None) -> bool:
    from src.profile_filters import canonicalize

    pid = person_id(conn, name, phone_id)
    if pid is None:
        return False
    raw = (ethnicity or "").strip()
    if not raw or raw.lower() in {"unknown", "none", "clear"}:
        value = None
    else:
        value = canonicalize(raw) or raw.strip().lower().replace(" ", "_")
    conn.execute("UPDATE hinge_people SET ethnicity = ? WHERE id = ?", (value, pid))
    conn.commit()
    return True


def incoming_turn_fingerprint(pairs: list[tuple[str, str]]) -> str:
    blob = "\n".join(f"{side}:{(body or '').strip()}" for side, body in pairs if (body or "").strip())
    if not blob:
        return ""
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def set_draft(conn: sqlite3.Connection, name: str, text: str, *, phone_id: str | None = None) -> bool:
    pid = person_id(conn, name, phone_id)
    if pid is None:
        return False
    conn.execute(
        """
        INSERT INTO hinge_chats (person_id, status, updated_at, draft, draft_status, draft_error, draft_pending_fp, draft_updated_at)
        VALUES (?, 'unknown', ?, ?, 'idle', NULL, NULL, ?)
        ON CONFLICT(person_id) DO UPDATE SET
            draft = excluded.draft,
            draft_status = 'idle',
            draft_error = NULL,
            draft_pending_fp = NULL,
            draft_updated_at = excluded.draft_updated_at
        """,
        (pid, _now(), text, _now()),
    )
    conn.commit()
    return True


def queue_draft(
    conn: sqlite3.Connection,
    name: str,
    fingerprint: str,
    *,
    phone_id: str | None = None,
    composer_text: str = "",
) -> bool:
    pid = person_id(conn, name, phone_id)
    if pid is None:
        return False
    conn.execute(
        """
        INSERT INTO hinge_chats (person_id, status, updated_at, draft, draft_status, draft_error,
                              draft_attempts, draft_pending_fp, draft_updated_at)
        VALUES (?, 'unknown', ?, ?, 'queued', NULL, 0, ?, ?)
        ON CONFLICT(person_id) DO UPDATE SET
            draft = CASE WHEN excluded.draft != '' THEN excluded.draft ELSE hinge_chats.draft END,
            draft_status = 'queued',
            draft_error = NULL,
            draft_attempts = 0,
            draft_pending_fp = excluded.draft_pending_fp,
            draft_updated_at = excluded.draft_updated_at
        """,
        (pid, _now(), composer_text, fingerprint, _now()),
    )
    conn.commit()
    return True


def list_pending_drafts(conn: sqlite3.Connection, *, limit: int = 5) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT p.id AS person_id, p.name, p.phone_id, c.draft AS composer_text,
                   c.draft_pending_fp, c.draft_attempts, c.draft_status
            FROM hinge_chats c
            JOIN hinge_people p ON p.id = c.person_id
            WHERE c.draft_status IN ('queued', 'failed')
              AND IFNULL(c.draft_pending_fp, '') != ''
            ORDER BY c.draft_updated_at
            LIMIT ?
            """,
            (limit,),
        )
    )


def claim_draft(conn: sqlite3.Connection, person_id: int, pending_fp: str) -> bool:
    cur = conn.execute(
        """
        UPDATE hinge_chats SET draft_status = 'running', draft_updated_at = ?
        WHERE person_id = ? AND draft_pending_fp = ? AND draft_status IN ('queued', 'failed')
        """,
        (_now(), person_id, pending_fp),
    )
    conn.commit()
    return cur.rowcount > 0


def complete_draft(
    conn: sqlite3.Connection, name: str, pending_fp: str, text: str, *, phone_id: str | None = None
) -> bool:
    pid = person_id(conn, name, phone_id)
    if pid is None:
        return False
    cur = conn.execute(
        """
        UPDATE hinge_chats
        SET draft = ?, draft_status = 'idle', draft_error = NULL,
            draft_pending_fp = NULL, draft_updated_at = ?
        WHERE person_id = ? AND draft_pending_fp = ?
        """,
        (text, _now(), pid, pending_fp),
    )
    conn.commit()
    return cur.rowcount > 0


def fail_draft(
    conn: sqlite3.Connection,
    person_id: int,
    pending_fp: str,
    error: str,
    attempts: int,
    *,
    give_up: bool = False,
) -> None:
    conn.execute(
        """
        UPDATE hinge_chats
        SET draft_status = ?, draft_error = ?, draft_attempts = ?, draft_updated_at = ?
        WHERE person_id = ? AND draft_pending_fp = ?
        """,
        ("idle" if give_up else "failed", (error or "")[:400], attempts, _now(), person_id, pending_fp),
    )
    if give_up:
        conn.execute("UPDATE hinge_chats SET draft_pending_fp = NULL WHERE person_id = ?", (person_id,))
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM hinge_settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO hinge_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def people_payload(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for row in list_people(conn):
        item = {k: row[k] for k in row.keys()}
        item["prompt_count"] = len(list_prompts(conn, int(row["id"])))
        item["photo_count"] = len(list_photos(conn, int(row["id"])))
        out.append(item)
    return out


def thread_payload(conn: sqlite3.Connection, name: str, phone_id: str | None = None) -> dict:
    person = get_person(conn, name, phone_id)
    if person is None:
        return {"ok": False, "error": "not found"}
    pid = int(person["id"])
    extras = {}
    raw = person["extras_json"] if "extras_json" in person.keys() else None
    if raw:
        try:
            parsed = json.loads(str(raw))
            if isinstance(parsed, dict):
                extras = {str(k): str(v) for k, v in parsed.items() if v}
        except json.JSONDecodeError:
            extras = {}
    return {
        "ok": True,
        "person": {k: person[k] for k in person.keys()},
        "extras": extras,
        "messages": [
            {"side": m["side"], "body": m["body"], "captured_at": m["captured_at"]}
            for m in list_thread(conn, name, phone_id)
        ],
        "prompts": [
            {"question": p["question"], "answer": p["answer"]} for p in list_prompts(conn, pid)
        ],
        "photos": [{"path": p["path"], "kind": p["kind"]} for p in list_photos(conn, pid)],
    }


def prefs_json_default() -> dict:
    from src.hinge_swipe import default_prefs

    return default_prefs()


def load_prefs_blob(conn: sqlite3.Connection | None = None) -> dict:
    from src.hinge_swipe import load_prefs

    return load_prefs()


def dump_setting_json(conn: sqlite3.Connection, key: str, payload: dict) -> None:
    set_setting(conn, key, json.dumps(payload, ensure_ascii=False))
