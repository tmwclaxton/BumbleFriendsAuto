"""LinkedIn inbox model — separate tables from Bumble people/chats/messages."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote_plus

from src.phones import DEFAULT_PHONE_ID, current_phone_id, normalize_phone_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS li_people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE,
    phone_id TEXT NOT NULL DEFAULT 'toby',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    profile_url TEXT,
    lgs_lead_id INTEGER,
    headline TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    about TEXT,
    location TEXT,
    title TEXT,
    posts_json TEXT,
    profile_captured_at TEXT,
    profile_fp TEXT,
    UNIQUE (phone_id, name COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS li_chats (
    person_id INTEGER PRIMARY KEY REFERENCES li_people(id) ON DELETE CASCADE,
    preview TEXT,
    badge TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    last_from TEXT,
    last_text TEXT,
    updated_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    spam TEXT NOT NULL DEFAULT '',
    spam_reason TEXT,
    spam_fp TEXT,
    product TEXT NOT NULL DEFAULT '',
    product_reason TEXT,
    product_fp TEXT,
    draft TEXT,
    draft_status TEXT NOT NULL DEFAULT 'idle',
    draft_error TEXT,
    draft_attempts INTEGER NOT NULL DEFAULT 0,
    draft_pending_fp TEXT,
    draft_updated_at TEXT
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

_IN_URL = re.compile(
    r"https?://(?:[\w.-]+\.)?linkedin\.com/in/[\w%\-.]+/?",
    re.I,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scope_phone(phone_id: str | None = None) -> str:
    pid = normalize_phone_id(phone_id) if phone_id else current_phone_id()
    if pid == "all":
        pid = current_phone_id()
    return pid or DEFAULT_PHONE_ID


_UI_NAMES = {
    "claim",
    "claim offer",
    "view company",
    "trying",
    "active now",
    "search",
    "offer",
    "write a message",
    "write a message…",
    "today",
    "promoted",
    "my network",
    "notifications",
    "jobs",
    "video",
}

_CLOCK_RE = re.compile(r"^\d{1,2}:\d{2}$")
_MOBILE_AGO_RE = re.compile(r"^mobile\b.*\bago\b", re.I)
_ICON_RE = re.compile(r"\s+SYS_ICN\S*", re.I)
_PRONOUN_RE = re.compile(r"\s+\((?:she|he|they)/[^)]+\)\s*$", re.I)
_STAMP_RE = re.compile(r"^(.+?)\s+x\s+[•·]")
_BLEED_BODY = re.compile(
    r"linkedin ads|business\.linkedin\.com|try linkedin|sys_icn_verified|"
    r"write a message|claim offer",
    re.I,
)


def is_linkedin_ui_name(name: str | None) -> bool:
    text = (name or "").strip()
    if not text:
        return False
    low = text.casefold()
    if low in _UI_NAMES:
        return True
    if _CLOCK_RE.match(text) or _MOBILE_AGO_RE.match(text):
        return True
    if "grantgunner.org" in low or low.endswith(".org"):
        return True
    if " ago" in low or "linkedin" in low:
        return True
    return False


def is_linkedin_bleed_thread(messages: list[tuple[str, str]] | None, *texts: str | None) -> bool:
    blob = " ".join(
        [(body or "") for _side, body in (messages or [])] + [t or "" for t in texts]
    )
    return bool(_BLEED_BODY.search(blob))


def _looks_like_person_name(text: str) -> bool:
    from src.linkedin_screen import plausible_person_name

    cand = (text or "").strip()
    if not plausible_person_name(cand) or is_linkedin_ui_name(cand):
        return False
    if any(ch in cand for ch in "?!:;|/"):
        return False
    words = cand.split()
    if not (1 <= len(words) <= 5):
        return False
    if not cand[0].isalpha() or not cand[0].isupper():
        return False
    if any(ch.isdigit() for ch in cand):
        return False
    return True


def recover_linkedin_partner(name: str, messages: list[tuple[str, str]]) -> str | None:
    """Best person name from a LinkedIn thread that was stored under UI chrome."""
    if _looks_like_person_name(name):
        return name.strip()
    stamps: list[str] = []
    headlines: list[str] = []
    for _side, body in messages:
        raw = (body or "").strip()
        stamp = _STAMP_RE.match(raw)
        if stamp:
            cand = stamp.group(1).strip()
            if not cand.casefold().startswith("archie"):
                stamps.append(cand)
        line = raw.splitlines()[0].strip()
        line = _ICON_RE.sub("", line)
        line = _PRONOUN_RE.sub("", line).strip()
        headlines.append(line)
    for cand in stamps + headlines:
        if _looks_like_person_name(cand):
            return cand
    return None


def purge_linkedin_bleed_from_bumble(conn: sqlite3.Connection) -> list[str]:
    """Move real LinkedIn threads out of Bumble people; delete ad/chrome rows."""
    from src.phones import phone_scope
    from src.store import delete_person

    rows = list(
        conn.execute(
            """
            SELECT p.id, p.name, p.phone_id, c.last_text, c.preview
            FROM people p
            LEFT JOIN chats c ON c.person_id = p.id
            WHERE IFNULL(p.channel, 'bumble') != 'linkedin'
            """
        )
    )
    log: list[str] = []
    for row in rows:
        msgs = [
            (str(m["side"]), str(m["body"]))
            for m in conn.execute(
                "SELECT side, body FROM messages WHERE person_id = ? ORDER BY id",
                (int(row["id"]),),
            )
        ]
        name = str(row["name"])
        bleed = is_linkedin_ui_name(name) or is_linkedin_bleed_thread(
            msgs, row["last_text"], row["preview"]
        )
        if not bleed:
            continue
        partner = recover_linkedin_partner(name, msgs)
        phone_id = str(row["phone_id"] or "toby")
        with phone_scope(phone_id):
            if partner and any(
                body.strip() and not _BLEED_BODY.search(body) and body.strip() not in {"Write a message…", "Search"}
                and not _CLOCK_RE.match(body.strip())
                for _side, body in msgs
            ):
                cleaned = [
                    (side, body)
                    for side, body in msgs
                    if body.strip()
                    and body.strip() not in {"Write a message…", "Search"}
                    and not _CLOCK_RE.match(body.strip())
                ]
                last = next((b for _s, b in reversed(cleaned) if b.strip()), "")
                li_id = upsert_chat(
                    conn,
                    partner,
                    preview=row["preview"],
                    last_from=cleaned[-1][0] if cleaned else None,
                    last_text=last,
                    phone_id=phone_id,
                )
                if cleaned:
                    replace_thread(conn, li_id, cleaned)
                log.append(f"{phone_id}:{name} → linkedin {partner}")
            else:
                log.append(f"{phone_id}:{name} deleted")
            delete_person(conn, name)
    return log


def extract_profile_url(*texts: str | None) -> str | None:
    for text in texts:
        match = _IN_URL.search(text or "")
        if match:
            return match.group(0).rstrip(").,;")
    return None


def search_profile_url(name: str) -> str:
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote_plus((name or "").strip())


def public_profile_href(name: str, stored: str | None = None) -> str:
    stored = (stored or "").strip()
    if stored.startswith("http"):
        return stored
    found = extract_profile_url(stored)
    if found:
        return found
    return search_profile_url(name)


def _add_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    cols = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if name in cols:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    except sqlite3.OperationalError as exc:
        # Startup workers can race through this idempotent migration.
        if "duplicate column name" not in str(exc).lower():
            raise


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, name, ddl in (
        ("li_people", "profile_url", "TEXT"),
        ("li_people", "lgs_lead_id", "INTEGER"),
        ("li_people", "headline", "TEXT"),
        ("li_people", "verified", "INTEGER NOT NULL DEFAULT 0"),
        ("li_people", "about", "TEXT"),
        ("li_people", "location", "TEXT"),
        ("li_people", "title", "TEXT"),
        ("li_people", "posts_json", "TEXT"),
        ("li_people", "profile_captured_at", "TEXT"),
        ("li_people", "profile_fp", "TEXT"),
        ("li_chats", "archived", "INTEGER NOT NULL DEFAULT 0"),
        ("li_chats", "spam", "TEXT NOT NULL DEFAULT ''"),
        ("li_chats", "spam_reason", "TEXT"),
        ("li_chats", "spam_fp", "TEXT"),
        ("li_chats", "product", "TEXT NOT NULL DEFAULT ''"),
        ("li_chats", "product_reason", "TEXT"),
        ("li_chats", "product_fp", "TEXT"),
        ("li_chats", "draft", "TEXT"),
        ("li_chats", "draft_status", "TEXT NOT NULL DEFAULT 'idle'"),
        ("li_chats", "draft_error", "TEXT"),
        ("li_chats", "draft_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("li_chats", "draft_pending_fp", "TEXT"),
        ("li_chats", "draft_updated_at", "TEXT"),
    ):
        _add_column(conn, table, name, ddl)
    _move_channel_rows(conn)
    _backfill_profile_urls(conn)
    repair_message_attribution(conn)


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


def _backfill_profile_urls(conn: sqlite3.Connection) -> None:
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(li_people)")}
    if "profile_url" not in cols:
        return
    rows = list(
        conn.execute(
            "SELECT id FROM li_people WHERE profile_url IS NULL OR trim(profile_url) = ''"
        )
    )
    for person in rows:
        texts = [
            str(row[0] or "")
            for row in conn.execute(
                """
                SELECT body FROM li_messages WHERE person_id = ?
                UNION ALL
                SELECT preview FROM li_chats WHERE person_id = ?
                UNION ALL
                SELECT last_text FROM li_chats WHERE person_id = ?
                """,
                (int(person["id"]), int(person["id"]), int(person["id"])),
            )
        ]
        url = extract_profile_url(*texts)
        if url:
            conn.execute("UPDATE li_people SET profile_url = ? WHERE id = ?", (url, int(person["id"])))


def upsert_person(
    conn: sqlite3.Connection,
    name: str,
    *,
    phone_id: str | None = None,
    profile_url: str | None = None,
) -> int:
    name = name.strip()
    now = _now()
    pid = _scope_phone(phone_id)
    url = extract_profile_url(profile_url)
    row = conn.execute(
        "SELECT id, profile_url FROM li_people WHERE name = ? COLLATE NOCASE AND phone_id = ?",
        (name, pid),
    ).fetchone()
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO li_people (name, phone_id, first_seen_at, last_seen_at, profile_url)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, pid, now, now, url),
        )
        return int(cur.lastrowid)
    if url and not (row["profile_url"] or "").strip():
        conn.execute(
            "UPDATE li_people SET last_seen_at = ?, profile_url = ? WHERE id = ?",
            (now, url, int(row["id"])),
        )
    else:
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
    profile_url: str | None = None,
    updated_at: str | None = None,
) -> int:
    person_id = upsert_person(conn, name, phone_id=phone_id, profile_url=profile_url)
    existing = conn.execute(
        "SELECT preview, badge, last_from, last_text, updated_at FROM li_chats WHERE person_id = ?",
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
    text_changed = existing is None or (
        (last_text is not None and last_text_v != existing["last_text"])
        or (preview is not None and preview_v != existing["preview"])
    )
    if updated_at:
        stamp = updated_at
    elif existing and not text_changed and existing["updated_at"]:
        stamp = existing["updated_at"]
    else:
        stamp = now
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
        (person_id, preview_v, badge_v, status, last_from_v, last_text_v, stamp),
    )
    return person_id


def update_person_profile(
    conn: sqlite3.Connection,
    name: str,
    *,
    phone_id: str | None = None,
    headline: str | None = None,
    verified: bool | None = None,
    about: str | None = None,
    location: str | None = None,
    title: str | None = None,
    posts_json: str | None = None,
    captured: bool = False,
    fingerprint: str | None = None,
) -> bool:
    pid = person_id(conn, name, phone_id)
    if pid is None:
        return False
    row = conn.execute("SELECT headline, verified FROM li_people WHERE id = ?", (pid,)).fetchone()
    fresh = (headline or "").strip()
    if fresh:
        old = ""
        if row is not None and "headline" in row.keys():
            old = str(row["headline"] or "").strip()
        if len(fresh) >= len(old):
            conn.execute("UPDATE li_people SET headline = ? WHERE id = ?", (fresh[:400], pid))
    if verified:
        conn.execute("UPDATE li_people SET verified = 1 WHERE id = ?", (pid,))
    if about is not None and about.strip():
        conn.execute("UPDATE li_people SET about = ? WHERE id = ?", (about.strip()[:2500], pid))
    if location is not None and location.strip():
        conn.execute("UPDATE li_people SET location = ? WHERE id = ?", (location.strip()[:200], pid))
    if title is not None and title.strip():
        conn.execute("UPDATE li_people SET title = ? WHERE id = ?", (title.strip()[:240], pid))
    if posts_json is not None and posts_json.strip():
        conn.execute("UPDATE li_people SET posts_json = ? WHERE id = ?", (posts_json.strip()[:8000], pid))
    if captured:
        conn.execute(
            "UPDATE li_people SET profile_captured_at = ?, profile_fp = COALESCE(NULLIF(?, ''), profile_fp) WHERE id = ?",
            (_now(), (fingerprint or "").strip(), pid),
        )
    return True


def _harvest_profile(conn: sqlite3.Connection, person_id: int, texts: list[str]) -> None:
    row = conn.execute("SELECT name, phone_id FROM li_people WHERE id = ?", (person_id,)).fetchone()
    if row is None:
        return
    from src.linkedin_screen import profile_bits_from_text

    headline = ""
    verified = False
    name = str(row["name"])
    for text in texts:
        bits = profile_bits_from_text(text, name)
        if bits.get("headline") and not headline:
            headline = str(bits["headline"])
        if bits.get("verified"):
            verified = True
    if headline or verified:
        update_person_profile(
            conn,
            name,
            phone_id=str(row["phone_id"] or "toby"),
            headline=headline or None,
            verified=verified or None,
        )


def replace_thread(conn: sqlite3.Connection, person_id: int, messages: list[tuple]) -> None:
    from src.linkedin_screen import is_thread_chrome, repair_thread_messages

    partner_row = conn.execute("SELECT name FROM li_people WHERE id = ?", (person_id,)).fetchone()
    partner = str(partner_row["name"]) if partner_row else None
    _harvest_profile(conn, person_id, [item[1] if len(item) > 1 else "" for item in messages])
    kept = repair_thread_messages([(item[0], item[1] if len(item) > 1 else "") for item in messages], partner)
    when_by_body = {
        (item[1] if len(item) > 1 else ""): (item[2] if len(item) > 2 else None)
        for item in messages
        if len(item) > 2
    }
    conn.execute("DELETE FROM li_messages WHERE person_id = ?", (person_id,))
    now = _now()
    cleaned: list[tuple[str, str]] = []
    for seq, item in enumerate(kept):
        side = item[0]
        body = (item[1] if len(item) > 1 else "").strip()
        when = when_by_body.get(body)
        if not body or is_thread_chrome(body, partner):
            continue
        cleaned.append((side, body))
        stamp = (when or "").strip() if isinstance(when, str) else ""
        if not stamp:
            stamp = f"{now}#{seq:04d}"
        conn.execute(
            "INSERT INTO li_messages (person_id, side, body, captured_at) VALUES (?, ?, ?, ?)",
            (person_id, side, body, stamp),
        )
    if cleaned:
        last_side, last_body = cleaned[-1]
        status = "waiting" if last_side == "you" else "needs_reply"
        last_iso = stamp.split("#", 1)[0] if "#" not in stamp else ""
        if last_iso:
            conn.execute(
                """
                UPDATE li_chats SET last_from = ?, last_text = ?, status = ?, updated_at = ?
                WHERE person_id = ?
                """,
                (last_side, last_body, status, last_iso, person_id),
            )
        else:
            conn.execute(
                """
                UPDATE li_chats SET last_from = ?, last_text = ?, status = ?
                WHERE person_id = ?
                """,
                (last_side, last_body, status, person_id),
            )


def repair_message_attribution(conn: sqlite3.Connection) -> None:
    from src.linkedin_screen import is_thread_chrome, polish_message, sender_stamp_side, strip_you_prefix

    people = list(conn.execute("SELECT id, name FROM li_people"))
    for person in people:
        name = str(person["name"])
        rows = list(
            conn.execute(
                "SELECT id, side, body FROM li_messages WHERE person_id = ? ORDER BY id",
                (int(person["id"]),),
            )
        )
        _harvest_profile(conn, int(person["id"]), [str(row["body"] or "") for row in rows])
        pending = None
        last_side = None
        last_body = None
        for row in rows:
            raw = strip_you_prefix(str(row["body"] or ""))
            stamp = sender_stamp_side(raw)
            if stamp is not None or is_thread_chrome(raw, name):
                if stamp is not None:
                    pending = stamp
                conn.execute("DELETE FROM li_messages WHERE id = ?", (int(row["id"]),))
                continue
            side = pending or str(row["side"])
            pending = None
            side, body = polish_message(side, raw, name)
            last_side, last_body = side, body
            if side != row["side"] or body != row["body"]:
                conn.execute(
                    "UPDATE li_messages SET side = ?, body = ? WHERE id = ?",
                    (side, body, int(row["id"])),
                )
        if last_side:
            status = "waiting" if last_side == "you" else "needs_reply"
            conn.execute(
                """
                UPDATE li_chats SET last_from = ?, last_text = ?, status = ?
                WHERE person_id = ?
                """,
                (last_side, last_body, status, int(person["id"])),
            )


def chat_sidebar_stamp(updated_at: str | None, last_real_at: str | None) -> str:
    """Prefer a parsed thread clock over a scan-time chat stamp."""
    real = (last_real_at or "").strip()
    if real:
        return real.split("#", 1)[0]
    return (updated_at or "").strip().split("#", 1)[0]


def list_people(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT p.name, p.phone_id, p.profile_url, p.lgs_lead_id, p.headline, p.verified,
                   p.about, p.location, p.title, p.posts_json, p.profile_captured_at, p.profile_fp,
                   c.badge, c.status, c.last_from, c.last_text, c.preview,
                   c.updated_at, c.archived, c.spam, c.spam_reason, c.spam_fp,
                   c.product, c.product_reason, c.product_fp,
                   c.draft, c.draft_status, c.draft_error, c.draft_attempts, c.draft_pending_fp,
                   (SELECT COUNT(*) FROM li_messages m WHERE m.person_id = p.id) AS message_count,
                   (SELECT m.captured_at FROM li_messages m
                    WHERE m.person_id = p.id AND instr(m.captured_at, '#') = 0
                    ORDER BY m.id DESC LIMIT 1) AS last_real_at
            FROM li_people p
            LEFT JOIN li_chats c ON c.person_id = p.id
            ORDER BY
                CASE WHEN IFNULL(c.archived, 0) = 1 THEN 9 ELSE 0 END,
                CASE WHEN IFNULL(c.spam, '') IN ('spam', 'pitch') THEN 0 ELSE 1 END,
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


def add_message(conn: sqlite3.Connection, person_id: int, side: str, body: str) -> None:
    now = _now()
    conn.execute(
        "INSERT INTO li_messages (person_id, side, body, captured_at) VALUES (?, ?, ?, ?)",
        (person_id, side, (body or "").strip(), now),
    )
    status = "waiting" if side == "you" else "needs_reply"
    conn.execute(
        """
        UPDATE li_chats SET last_from = ?, last_text = ?, status = ?, badge = '', updated_at = ?
        WHERE person_id = ?
        """,
        (side, (body or "").strip(), status, now, person_id),
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
        SELECT p.name, p.phone_id, p.profile_url, p.lgs_lead_id, p.headline, p.verified,
               p.about, p.location, p.title, p.posts_json, p.profile_captured_at, p.profile_fp,
               c.status, c.last_from, c.last_text, c.preview,
               c.archived, c.spam, c.spam_reason, c.spam_fp,
               c.product, c.product_reason, c.product_fp,
               c.draft, c.draft_status, c.draft_error, c.draft_attempts, c.draft_pending_fp
        FROM li_people p
        LEFT JOIN li_chats c ON c.person_id = p.id
        WHERE p.name = ? COLLATE NOCASE AND p.phone_id = ?
        """,
        (name, _scope_phone(phone_id)),
    ).fetchone()


def person_id(conn: sqlite3.Connection, name: str, phone_id: str | None = None) -> int | None:
    row = conn.execute(
        "SELECT id FROM li_people WHERE name = ? COLLATE NOCASE AND phone_id = ?",
        (name, _scope_phone(phone_id)),
    ).fetchone()
    return int(row["id"]) if row else None


def set_spam(
    conn: sqlite3.Connection,
    name: str,
    label: str,
    reason: str = "",
    *,
    phone_id: str | None = None,
    fingerprint: str | None = None,
) -> bool:
    pid = person_id(conn, name, phone_id)
    if pid is None:
        return False
    flag = (label or "").strip().lower()
    if flag not in {"", "ok", "spam", "pitch"}:
        flag = "ok"
    fp = (fingerprint or "").strip()
    conn.execute(
        """
        INSERT INTO li_chats (person_id, status, updated_at, spam, spam_reason, spam_fp)
        VALUES (?, 'unknown', ?, ?, ?, ?)
        ON CONFLICT(person_id) DO UPDATE SET
            spam = excluded.spam,
            spam_reason = excluded.spam_reason,
            spam_fp = CASE WHEN excluded.spam_fp != '' THEN excluded.spam_fp ELSE li_chats.spam_fp END
        """,
        (pid, _now(), flag, (reason or "").strip()[:240], fp),
    )
    return True


def set_product(
    conn: sqlite3.Connection,
    name: str,
    product: str,
    reason: str = "",
    *,
    phone_id: str | None = None,
    fingerprint: str | None = None,
) -> bool:
    pid = person_id(conn, name, phone_id)
    if pid is None:
        return False
    flag = (product or "").strip().lower().replace(" ", "")
    if flag not in {"", "snitch", "grantgunner", "canvassr"}:
        flag = ""
    fp = (fingerprint or "").strip()
    conn.execute(
        """
        INSERT INTO li_chats (person_id, status, updated_at, product, product_reason, product_fp)
        VALUES (?, 'unknown', ?, ?, ?, ?)
        ON CONFLICT(person_id) DO UPDATE SET
            product = excluded.product,
            product_reason = excluded.product_reason,
            product_fp = CASE WHEN excluded.product_fp != '' THEN excluded.product_fp ELSE li_chats.product_fp END
        """,
        (pid, _now(), flag, (reason or "").strip()[:240], fp),
    )
    return True


def set_archived(
    conn: sqlite3.Connection,
    name: str,
    archived: bool,
    *,
    phone_id: str | None = None,
) -> bool:
    pid = person_id(conn, name, phone_id)
    if pid is None:
        return False
    conn.execute(
        """
        INSERT INTO li_chats (person_id, status, updated_at, archived)
        VALUES (?, 'unknown', ?, ?)
        ON CONFLICT(person_id) DO UPDATE SET archived = excluded.archived, updated_at = excluded.updated_at
        """,
        (pid, _now(), 1 if archived else 0),
    )
    return True


def incoming_turn_fingerprint(pairs: list[tuple[str, str]]) -> str:
    import hashlib

    blob = "\n".join(
        f"{side}:{body.strip()}"
        for side, body in pairs
        if (body or "").strip()
    )
    if not blob:
        return ""
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def set_draft(conn: sqlite3.Connection, name: str, text: str, *, phone_id: str | None = None) -> bool:
    pid = person_id(conn, name, phone_id)
    if pid is None:
        return False
    conn.execute(
        """
        INSERT INTO li_chats (person_id, status, updated_at, draft, draft_status, draft_error, draft_pending_fp, draft_updated_at)
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
        INSERT INTO li_chats (person_id, status, updated_at, draft, draft_status, draft_error,
                              draft_attempts, draft_pending_fp, draft_updated_at)
        VALUES (?, 'unknown', ?, ?, 'queued', NULL, 0, ?, ?)
        ON CONFLICT(person_id) DO UPDATE SET
            draft = CASE WHEN excluded.draft != '' THEN excluded.draft ELSE li_chats.draft END,
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
            FROM li_chats c
            JOIN li_people p ON p.id = c.person_id
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
        UPDATE li_chats SET draft_status = 'running', draft_updated_at = ?
        WHERE person_id = ? AND draft_pending_fp = ? AND draft_status IN ('queued', 'failed')
        """,
        (_now(), person_id, pending_fp),
    )
    conn.commit()
    return cur.rowcount > 0


def complete_draft(conn: sqlite3.Connection, name: str, pending_fp: str, text: str, *, phone_id: str | None = None) -> bool:
    pid = person_id(conn, name, phone_id)
    if pid is None:
        return False
    cur = conn.execute(
        """
        UPDATE li_chats
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
        UPDATE li_chats
        SET draft_status = ?, draft_error = ?, draft_attempts = ?, draft_updated_at = ?
        WHERE person_id = ? AND draft_pending_fp = ?
        """,
        ("idle" if give_up else "failed", (error or "")[:400], attempts, _now(), person_id, pending_fp),
    )
    if give_up:
        conn.execute(
            "UPDATE li_chats SET draft_pending_fp = NULL WHERE person_id = ?",
            (person_id,),
        )
    conn.commit()
