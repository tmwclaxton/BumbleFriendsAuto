"""Flag inbound LinkedIn sales / spam. Our GrantGunner / Canvassr outreach is not spam."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading

from src.config import load_config
from src.linkedin_screen import looks_like_inmail_promo, polish_message
from src.linkedin_store import list_people, list_thread, set_spam
from src.phones import phone_scope
from src.store import connect as db_connect, db_path_from_config

log = logging.getLogger(__name__)

_SYSTEM = """You classify a LinkedIn DM thread for Toby (GrantGunner) or Archie (Canvassr / Let's Go Social).
Their outbound pitches — grants, Canvassr, donating a week, asking for a call — are NOT spam.
Flag when the OTHER person is selling US something: SEO, web design, insurance, courses, lead-gen,
recruiting, crypto, generic InMail, "book a demo of our tool", agency retainers.
Reply with JSON only: {"label":"ok"|"spam"|"pitch","reason":"short"}
Use pitch when they are trying to sell us. Use spam for junk / scams. Use ok otherwise.
"""

_INBOUND_SELL = re.compile(
    r"\b("
    r"book a demo|our (?:platform|agency|retainer)|seo\b|web design|lead.?gen|"
    r"would you be open to a (?:quick )?call|i help (?:companies|founders|businesses)|"
    r"scale your (?:pipeline|revenue)|limited time offer|sponsored|"
    r"grow your hiring|hiring pipeline|calendly|"
    r"we(?:['’]re| are) an award.?winning"
    r")\b",
    re.I,
)


def parse_spam_reply(raw: str) -> tuple[str, str]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            label = str(data.get("label") or "ok").strip().lower()
            if label not in {"ok", "spam", "pitch"}:
                label = "ok"
            return label, str(data.get("reason") or "")[:240]
    except json.JSONDecodeError:
        pass
    word = text.split()[0].lower().strip(".,:;\"'") if text else "ok"
    if word in {"spam", "pitch", "ok"}:
        return word, text[:240]
    return "ok", ""


def _side_body(item) -> tuple[str, str]:
    side = item[0] if item else ""
    body = item[1] if item and len(item) > 1 else ""
    return str(side), str(body or "")


def heuristic_spam(messages: list[tuple[str, str]]) -> tuple[str, str] | None:
    them = " ".join(body for side, body in (_side_body(item) for item in messages) if side == "them")
    if not them.strip():
        return None
    if looks_like_inmail_promo(them):
        return "pitch", "Sponsored InMail / hiring promo"
    if _INBOUND_SELL.search(them):
        return "pitch", "Inbound sales language"
    return None


def classify_messages(messages: list[tuple[str, str]], name: str, *, cfg: dict | None = None) -> tuple[str, str]:
    guessed = heuristic_spam(messages)
    blob = "\n".join(f"{side}: {body}" for side, body in (_side_body(item) for item in messages[-12:]))
    if not blob.strip():
        return "ok", "no messages"
    from src.draft_llm import _chat_completion

    try:
        raw = _chat_completion(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Partner: {name}\n{blob}\nJSON:"},
            ],
            cfg,
            temperature=0.1,
            max_tokens=80,
        )
        label, reason = parse_spam_reply(raw)
        if label == "ok" and guessed:
            return guessed
        return label, reason
    except Exception as exc:
        log.warning("spam model failed for %s (%s)", name, exc)
        return guessed or ("ok", "classifier unavailable")


def inbound_from_stranger(pairs: list[tuple[str, str]]) -> bool:
    """True only if they messaged us first (new inbound / they opened the thread)."""
    saw_you = False
    first_is_them = False
    saw_them = False
    for side, body in (_side_body(item) for item in pairs):
        if not (body or "").strip():
            continue
        if side == "them":
            saw_them = True
            if not saw_you:
                first_is_them = True
        elif side == "you":
            saw_you = True
    return bool(saw_them and first_is_them)


def message_spam_fp(pairs: list[tuple[str, str]]) -> str:
    them = "\n".join(
        body.strip()
        for side, body in (_side_body(item) for item in pairs)
        if side == "them" and (body or "").strip()
    )
    if not them:
        return ""
    return hashlib.sha256(them.encode("utf-8")).hexdigest()[:16]


def needs_spam_check(stored_fp: str | None, pairs: list[tuple[str, str]]) -> bool:
    if not inbound_from_stranger(pairs):
        return False
    fp = message_spam_fp(pairs)
    if not fp:
        return False
    return (stored_fp or "") != fp


def schedule_spam_check(name: str, phone_id: str, *, force: bool = False) -> None:
    def run() -> None:
        try:
            classify_person(name, phone_id, force=force)
        except Exception:
            log.warning("background spam check failed for %s", name, exc_info=True)

    threading.Thread(target=run, name=f"li-spam-{phone_id}-{name[:20]}", daemon=True).start()


def _thread_pairs(conn, name: str, phone_id: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for row in list_thread(conn, name, phone_id=phone_id):
        side, body = polish_message(str(row["side"]), str(row["body"] or ""), name)
        if body:
            pairs.append((side, body))
    if pairs:
        return pairs
    from src.linkedin_store import get_person

    person = get_person(conn, name, phone_id)
    if person is None:
        return pairs
    for candidate in (person["last_text"], person["preview"]):
        text = (candidate or "").strip()
        if not text:
            continue
        side, body = polish_message(str(person["last_from"] or "them"), text, name)
        if body:
            pairs.append((side, body))
            break
    return pairs


def classify_person(name: str, phone_id: str, *, cfg: dict | None = None, force: bool = False) -> dict:
    cfg = cfg or load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        with phone_scope(phone_id):
            from src.linkedin_store import get_person

            row = get_person(conn, name, phone_id)
            if row is None:
                return {"ok": False, "error": "person not found", "name": name, "phone_id": phone_id}
            existing = str(row["spam"] or "") if "spam" in row.keys() else ""
            stored_fp = str(row["spam_fp"] or "") if "spam_fp" in row.keys() else ""
            pairs = _thread_pairs(conn, name, phone_id)
            fp = message_spam_fp(pairs)
            if not inbound_from_stranger(pairs):
                return {
                    "ok": True,
                    "name": name,
                    "phone_id": phone_id,
                    "spam": existing or "ok",
                    "reason": "not inbound from stranger",
                    "cached": True,
                }
            if not force and existing and stored_fp and stored_fp == fp:
                return {
                    "ok": True,
                    "name": name,
                    "phone_id": phone_id,
                    "spam": existing,
                    "reason": str(row["spam_reason"] or "") if "spam_reason" in row.keys() else "",
                    "cached": True,
                }
            if not force and existing and not stored_fp:
                set_spam(conn, name, existing, str(row["spam_reason"] or "") if "spam_reason" in row.keys() else "", phone_id=phone_id, fingerprint=fp)
                conn.commit()
                return {
                    "ok": True,
                    "name": name,
                    "phone_id": phone_id,
                    "spam": existing,
                    "reason": str(row["spam_reason"] or "") if "spam_reason" in row.keys() else "",
                    "cached": True,
                }
            if not fp and not force:
                return {
                    "ok": True,
                    "name": name,
                    "phone_id": phone_id,
                    "spam": existing or "ok",
                    "reason": "no inbound messages",
                    "cached": True,
                }
            label, reason = classify_messages(pairs, name, cfg=cfg)
            set_spam(conn, name, label, reason, phone_id=phone_id, fingerprint=fp)
            conn.commit()
            from src.linkedin_profile import maybe_after_spam_label

            maybe_after_spam_label(name, phone_id, label)
            return {
                "ok": True,
                "name": name,
                "phone_id": phone_id,
                "spam": label,
                "reason": reason,
                "cached": False,
            }
    finally:
        conn.close()


def classify_unscanned(*, limit: int = 40, force: bool = False) -> dict:
    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    results = []
    try:
        people = list_people(conn)
    finally:
        conn.close()
    scanned = 0
    for row in people:
        if scanned >= limit:
            break
        stored_fp = str(row["spam_fp"] or "") if "spam_fp" in row.keys() else ""
        if stored_fp and not force:
            continue
        pid = str(row["phone_id"] or "toby")
        scan_conn = db_connect(db_path_from_config(cfg))
        try:
            with phone_scope(pid):
                pairs = _thread_pairs(scan_conn, str(row["name"]), pid)
        finally:
            scan_conn.close()
        if not inbound_from_stranger(pairs):
            continue
        if not force and not needs_spam_check(stored_fp, pairs):
            continue
        result = classify_person(str(row["name"]), pid, cfg=cfg, force=force)
        results.append(result)
        scanned += 1
    flagged = [r for r in results if r.get("spam") in {"spam", "pitch"}]
    return {
        "ok": True,
        "scanned": scanned,
        "flagged": len(flagged),
        "results": results,
        "message": f"scanned {scanned}, flagged {len(flagged)} as spam/pitch",
    }


def is_flagged(label: str | None) -> bool:
    return (label or "") in {"spam", "pitch"}


def archive_in_crm(name: str, reason: str = "", lead_id: int | None = None) -> dict:
    from src.crm import _request, update_lead

    note = f"Archived from LinkedIn inbox as spam/pitch. {reason}".strip()
    if lead_id:
        result = update_lead(
            int(lead_id),
            {
                "status": "archived",
                "archived": True,
                "tags": "linkedin_spam",
                "notes": note,
                "bumble_inbox_name": name,
                "source": "linkedin",
            },
        )
        result.setdefault("via", "lead_id")
        return result
    found = _request("GET", f"/api/pipeline/contacts?q={name.replace(' ', '+')}")
    cards = found.get("contacts") or found.get("leads") or []
    if isinstance(cards, list):
        for card in cards:
            if not isinstance(card, dict):
                continue
            if str(card.get("name") or "").strip().casefold() == name.casefold():
                cid = card.get("id")
                if cid:
                    return archive_in_crm(name, reason, lead_id=int(cid))
    return {"ok": True, "skipped": True, "error": None, "message": "no LGS contact to archive"}
