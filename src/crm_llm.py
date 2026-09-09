"""LLM enrichment for the CRM popup. Grounded in the thread — never invents phones."""

from __future__ import annotations

import json
import logging
import re
from datetime import date

from src.crm_extract import extract_crm_fields, match_home_group, parse_event_date
from src.draft_llm import _chat_completion
from src.store import extract_phones

log = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{2,30}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_METHODS = {"email", "phone", "text", "whatsapp", "linkedin", "in_person", "other"}

_SYSTEM = """You fill a Let's Go Social CRM card from a Bumble Friends chat.
Return ONLY JSON (no markdown) with exactly these keys:
name, email, phone, instagram, tiktok, region, address, age, ethnicity,
interests_skills, tags, interested_event, preferred_contact_method,
consent_to_contact, closest_group, notes
Rules:
- Use only facts from THEIR messages or the profile line. If unknown, use null, "" or [].
- Never invent a phone, email, Instagram, TikTok, address, or age.
- name is their first name plus " LGS". Drop Bumble bio text after a bullet or comma.
- region is the town/area THEY said they are based. Do not put Toby's Wycombe unless they live there.
- address only if they gave a street or specific place they live. Usually "".
- interested_event is the specific dated plan they said yes to. Not the generic "wee group" intro. If the only dated plan has already passed, use "".
- tags: 2-6 short labels (e.g. "escape room", "drives", "whatsapp").
- interests_skills: hobbies they mentioned, comma-separated.
- preferred_contact_method: whatsapp if they gave a number to be added, else phone/text/email/null.
- consent_to_contact: true only if they offered a number or asked to be added.
- closest_group: one of bucks, london, glos, or null from where they live.
- notes: 2-4 short factual lines. No invented colour. No pep talk.
"""


def _transcript(messages: list[dict], profile: str) -> str:
    lines = []
    if profile:
        lines.append(f"PROFILE: {profile}")
    for msg in messages:
        side = "TOBY" if msg.get("side") == "you" else "THEM"
        body = re.sub(r"\s+", " ", str(msg.get("body") or "")).strip()
        if body:
            lines.append(f"{side}: {body}")
    return "\n".join(lines)[:8000]


def _parse_json(raw: str) -> dict:
    text = (raw or "").strip()
    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _in_text(hay: str, needle: str) -> bool:
    if not needle or not hay:
        return False
    return needle.casefold() in hay.casefold()


def _digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def _safe_phone(ai_phone: str, allowed: list[str], transcript: str) -> str:
    if not ai_phone:
        return ""
    want = _digits(ai_phone)
    for cand in allowed:
        have = _digits(cand)
        if have and (have == want or have.endswith(want[-10:]) or want.endswith(have[-10:])):
            return cand
    if want and want in _digits(transcript):
        found = extract_phones(ai_phone) or extract_phones(transcript)
        return found[0] if found else ""
    return ""


def _safe_handle(value: str, transcript: str) -> str:
    handle = (value or "").strip().lstrip("@").strip("._")
    if not handle or not _HANDLE_RE.match(handle):
        return ""
    if handle.lower() in {"letsgosocialuk", "instagram", "tiktok", "bumble"}:
        return ""
    if not re.search(rf"(?<![A-Za-z0-9._]){re.escape(handle)}(?![A-Za-z0-9._])", transcript, re.I):
        return ""
    return handle.lower()


def _safe_email(value: str, transcript: str) -> str:
    email = (value or "").strip()
    if not email or not _EMAIL_RE.match(email):
        return ""
    return email if _in_text(transcript, email) else ""


def _safe_place(value: str, transcript: str) -> str:
    place = re.sub(r"\s+", " ", (value or "").strip(" .,"))
    if len(place) < 2 or len(place) > 60:
        return ""
    words = [w for w in re.split(r"\s+", place) if len(w) > 2]
    if not words:
        return ""
    if not any(_in_text(transcript, w) for w in words):
        return ""
    return place


_GENERIC_TAGS = {
    "bumble friends",
    "whatsapp",
    "instagram",
    "tiktok",
    "drives",
    "escape room",
    "board games",
}


def _safe_tags(value: object, hay: str = "") -> list[str]:
    if isinstance(value, str):
        items = [p.strip() for p in value.split(",")]
    elif isinstance(value, list):
        items = [str(p).strip() for p in value]
    else:
        items = []
    out: list[str] = []
    for item in items:
        item = item[:40]
        if not item:
            continue
        key = item.casefold()
        if key in {x.casefold() for x in out}:
            continue
        if key in {"travel", "sports", "hiking"} and not re.search(
            rf"\b{re.escape(item)}\b", hay, re.I
        ):
            continue
        if key == "travel" and re.search(r"willing to travel|happy to travel", hay, re.I):
            continue
        if hay and key not in _GENERIC_TAGS and not _in_text(hay, item):
            continue
        out.append(item)
    return out[:8]


def llm_crm_fields(
    messages: list[dict],
    *,
    inbox_name: str,
    display_name: str = "",
    profile_location: str = "",
    profile_age: int | None = None,
    ethnicity: str = "",
    cfg: dict | None = None,
) -> dict:
    profile = " · ".join(
        p
        for p in (
            display_name or inbox_name,
            f"age {profile_age}" if profile_age else "",
            profile_location,
            ethnicity,
        )
        if p
    )
    user = (
        f"Inbox name: {inbox_name}\n"
        f"Display: {display_name or inbox_name}\n"
        f"{_transcript(messages, profile)}\n"
        "JSON:"
    )
    raw = _chat_completion(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        cfg,
        temperature=0.15,
        max_tokens=500,
    )
    return _parse_json(raw)


def merge_crm_fields(base: dict, ai: dict, *, messages: list[dict], groups: list[dict] | None = None) -> dict:
    transcript = "\n".join(str(m.get("body") or "") for m in messages)
    them = "\n".join(str(m.get("body") or "") for m in messages if m.get("side") == "them")
    out = dict(base)
    phones = list(base.get("phone_candidates") or [])
    ai_phone = _safe_phone(str(ai.get("phone") or ""), phones, them or transcript)
    if ai_phone:
        out["phone"] = ai_phone
    elif phones:
        out["phone"] = phones[0]

    email = _safe_email(str(ai.get("email") or ""), transcript) or str(base.get("email") or "")
    out["email"] = email

    insta = _safe_handle(str(ai.get("instagram") or ""), them or transcript)
    if insta and not out.get("instagram"):
        out["instagram"] = insta
    tiktok = _safe_handle(str(ai.get("tiktok") or ""), them or transcript)
    if tiktok and not out.get("tiktok"):
        out["tiktok"] = tiktok

    region = _safe_place(str(ai.get("region") or ""), them or transcript)
    if region and not out.get("hometown"):
        out["hometown"] = region
        out["region"] = region
    elif out.get("hometown"):
        out["region"] = out["hometown"]
    elif region:
        out["region"] = region

    address = _safe_place(str(ai.get("address") or ""), them)
    if address and re.search(r"\d", address):
        out["address"] = address

    interests = str(ai.get("interests_skills") or "").strip()
    if interests:
        kept = [
            w.strip()
            for w in re.split(r"[,/]", interests)
            if len(w.strip()) > 2 and _in_text(them or transcript, w.strip())
        ]
        if kept:
            out["interests_skills"] = ", ".join(kept)
    event = re.sub(r"[—–]", "-", str(ai.get("interested_event") or "")).strip()
    when = parse_event_date(event)
    if when is not None and when < date.today():
        event = ""
    if event and len(event) < 180 and (
        _in_text(transcript, event[:24])
        or any(_in_text(event, w) for w in ("escape", "saturday", "september", "hike", "kart"))
    ):
        current = str(out.get("interested_event") or "")
        if not current or len(event) < len(current):
            out["interested_event"] = event

    tags = _safe_tags(ai.get("tags"), them)
    if tags:
        out["tags"] = _safe_tags(list(out.get("tags") or []) + tags, them or "ok")

    method = str(ai.get("preferred_contact_method") or "").strip()
    if method in _METHODS:
        out["preferred_contact_method"] = method
    if ai.get("consent_to_contact") is True or out.get("phone"):
        out["consent_to_contact"] = True

    hub = str(ai.get("closest_group") or "").strip().lower()
    if hub in {"bucks", "london", "glos"}:
        out["hub"] = hub
        group_id = match_home_group(groups or [], hub, str(out.get("hometown") or ""))
        if group_id:
            out["home_lgs_group_id"] = group_id
            out["closest_lgs_group_id"] = group_id

    notes = str(ai.get("notes") or "").strip()
    if notes and len(notes) < 800:
        existing = str(out.get("suggested_notes") or "")
        if len(notes) > 20:
            out["suggested_notes"] = notes if not existing else existing + "\n" + notes

    return out


def enrich_crm_fields(
    messages: list[dict],
    *,
    inbox_name: str,
    display_name: str = "",
    profile_location: str = "",
    profile_age: int | None = None,
    ethnicity: str = "",
    phone_id: str = "toby",
    groups: list[dict] | None = None,
    cfg: dict | None = None,
) -> dict:
    base = extract_crm_fields(
        messages,
        inbox_name=inbox_name,
        display_name=display_name,
        profile_location=profile_location,
        profile_age=profile_age,
        ethnicity=ethnicity,
        phone_id=phone_id,
        groups=groups,
    )
    try:
        ai = llm_crm_fields(
            messages,
            inbox_name=inbox_name,
            display_name=display_name,
            profile_location=profile_location,
            profile_age=profile_age,
            ethnicity=ethnicity,
            cfg=cfg,
        )
    except Exception as exc:
        log.warning("CRM LLM extract failed, using regex only: %s", exc)
        return base
    if not ai:
        return base
    return merge_crm_fields(base, ai, messages=messages, groups=groups)
