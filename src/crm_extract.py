"""Pull CRM fields from a Bumble thread plus stored profile bits."""

from __future__ import annotations

import re
from datetime import date

from src.contacts import extract_instagrams, with_lgs_suffix
from src.store import extract_phones

_PLACE = r"([A-Za-z][A-Za-z\s'’-]{1,40}?)"
_PLACE_STOP = r"(?:\s+area|\s+but|\s+so|\s+and|\s+though|\s+\+?\d|[.!?,]|$)"
_FROM_RE = re.compile(
    rf"(?:i(?:['’]?m| am)|im)\s+(?:from|in|based(?:\s+in)?)\s+{_PLACE}{_PLACE_STOP}",
    re.I | re.M,
)
_BASED_RE = re.compile(
    rf"(?:based(?:\s+in)?|live(?:s| in)|from)\s+{_PLACE}{_PLACE_STOP}",
    re.I | re.M,
)
_AGE_RE = re.compile(
    r"(?:i(?:['’]?m| am)\s+)([1-9]\d)(?:\s*(?:years?\s*old|yo))?\b",
    re.I,
)
_TIKTOK_RE = re.compile(
    r"(?:tiktok\.com/@|tiktok\s*(?:is|:)?\s*@?)([A-Za-z0-9._]{2,30})",
    re.I,
)
_INSTA_LOOSE_RE = re.compile(
    r"(?:insta(?:gram)?|ig)\b[^\n@]{0,40}(?:it['’]?s|is|:|@)\s*@?([A-Za-z0-9._]{3,30})",
    re.I,
)
_DRIVE_RE = re.compile(r"\b(?:i\s+)?(?:do\s+)?(?:drive|can drive|willing to travel|happy to travel)\b", re.I)
_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_WA_RE = re.compile(r"\bwhatsapp\b|\badd me\b|\bmy number\b|\btext me\b", re.I)
_INTEREST_KEYS = (
    "hiking",
    "hike",
    "board games",
    "board game",
    "escape room",
    "go kart",
    "karting",
    "padel",
    "football",
    "gym",
    "climbing",
    "running",
    "cycling",
    "photography",
    "music",
    "pub",
    "mini golf",
    "bowling",
    "squash",
    "tennis",
    "film",
    "movies",
    "cooking",
)
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_YES_RE = re.compile(
    r"\b(?:yes|yeah|yep|keen|down|up for|interested|absolutely|sounds good|i['’]?d be)\b",
    re.I,
)
_SKIP_PLACES = {
    "wycombe or battersea",
    "you",
    "there",
    "here",
    "the uk",
    "uk",
    "london and gloucestershire",
}
_HUB_KEYS = {
    "bucks": (
        "wycombe",
        "high wycombe",
        "bucks",
        "buckingham",
        "maidenhead",
        "reading",
        "oxford",
        "leighton buzzard",
        "milton keynes",
        "hertford",
        "hertfordshire",
        "aylesbury",
        "slough",
    ),
    "london": (
        "london",
        "battersea",
        "waterloo",
        "croydon",
        "wembley",
        "hammersmith",
        "maida vale",
        "hayes",
        "clapham",
        "brixton",
        "peckham",
    ),
    "glos": (
        "gloucester",
        "glos",
        "tewkesbury",
        "cotswold",
        "bristol",
        "cheltenham",
    ),
}


def _clean_place(raw: str) -> str:
    place = re.sub(r"\s+", " ", (raw or "").strip(" .!?,")).strip()
    place = re.sub(r"\s+\+?\d[\d\s-]{6,}.*$", "", place)
    place = re.sub(
        r"\s+(?:insta(?:gram)?|ig|tiktok|add me|whatsapp|my number)\b.*$",
        "",
        place,
        flags=re.I,
    )
    place = re.sub(r"\b(area|mate|though|tbh|tbf)\b", "", place, flags=re.I).strip(" .")
    if len(place) < 2 or place.casefold() in _SKIP_PLACES:
        return ""
    if len(place) > 48:
        return ""
    return place


def _them_blob(messages: list[dict]) -> str:
    return "\n".join(str(m.get("body") or "") for m in messages if m.get("side") == "them")


def extract_hometown(them: str, profile_location: str = "") -> str:
    profile = (profile_location or "").strip()
    for match in _FROM_RE.finditer(them or ""):
        place = _clean_place(match.group(1))
        if place:
            return place
    for match in _BASED_RE.finditer(them or ""):
        place = _clean_place(match.group(1))
        if place:
            return place
    hint = re.search(
        r"\b(milton keynes|hertfordshire|reading|high wycombe|wycombe|wycomb|battersea|gloucester)\b",
        them or "",
        re.I,
    )
    if hint:
        place = hint.group(1)
        if place.lower() in {"wycombe", "wycomb"}:
            place = "High Wycombe"
        else:
            place = place.title() if place.lower() != "milton keynes" else "Milton Keynes"
        return place
    return profile


def extract_age(them: str, profile_age: int | None = None) -> int | None:
    match = _AGE_RE.search(them or "")
    if match:
        age = int(match.group(1))
        if 16 <= age <= 80:
            return age
    if profile_age and 16 <= int(profile_age) <= 80:
        return int(profile_age)
    return None


def extract_tiktok(text: str) -> str:
    match = _TIKTOK_RE.search(text or "")
    if not match:
        return ""
    handle = match.group(1).strip("._").lower()
    if handle in {"letsgosocialuk", "tiktok"}:
        return ""
    return handle


def extract_instagram_them(them: str) -> list[str]:
    found = extract_instagrams(them)
    skip = {"letsgosocialuk", "bumble", "instagram", "interested"}
    for match in _INSTA_LOOSE_RE.finditer(them or ""):
        handle = match.group(1).strip(".").lower()
        if handle in skip or handle in found or len(handle) < 3:
            continue
        if handle.startswith("http"):
            continue
        found.append(handle)
    return found


def guess_hub(hometown: str, blob: str = "") -> str | None:
    text = f"{hometown} {blob}".casefold()
    scores: dict[str, int] = {}
    for hub, keys in _HUB_KEYS.items():
        scores[hub] = sum(1 for key in keys if key.strip() and key.casefold() in text)
    best = max(scores, key=scores.get)
    return best if scores[best] else None


def match_home_group(groups: list[dict], hub: str | None, hometown: str) -> int | None:
    if not groups:
        return None
    hay = f"{hub or ''} {hometown or ''}".casefold()

    def score(group: dict) -> int:
        blob = " ".join(
            str(group.get(k) or "") for k in ("slug", "name", "town", "region")
        ).casefold()
        n = 0
        if hub and hub in blob:
            n += 4
        if hometown:
            for word in hometown.casefold().split():
                if len(word) > 3 and word in blob:
                    n += 2
        if hub == "bucks" and any(w in blob for w in ("wycombe", "bucks", "buckingham")):
            n += 3
        if hub == "london" and "london" in blob:
            n += 3
        if hub == "glos" and any(w in blob for w in ("glos", "gloucester", "cotswold")):
            n += 3
        return n

    ranked = sorted(groups, key=score, reverse=True)
    if ranked and score(ranked[0]) > 0:
        try:
            return int(ranked[0]["id"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _short_invite(body: str) -> str:
    text = re.sub(r"\s+", " ", body or "").strip()
    text = re.split(
        r"what['’]?s your number|i['’]?ll add u|add u to the",
        text,
        maxsplit=1,
        flags=re.I,
    )[0].strip(" .")
    planned = re.search(
        r"((?:we(?:'re| are) planning|next \w+ one['’]?s|this (?:saturday|sunday)).+)$",
        text,
        re.I,
    )
    if planned:
        text = planned.group(1).strip(" .")
    return text[:160]


def extract_event(messages: list[dict], today: date | None = None) -> str:
    today = today or date.today()
    last_invite = ""
    for msg in messages:
        body = str(msg.get("body") or "")
        if msg.get("side") != "you":
            if last_invite and _YES_RE.search(body):
                return last_invite[:160]
            continue
        if re.search(r"putting together a wee group", body, re.I):
            continue
        if re.search(r"what['’]?s your number|add u to the whatsapp", body, re.I) and not re.search(
            r"escape room|mini golf|saturday|september", body, re.I
        ):
            continue
        if not re.search(
            r"september|october|november|december|january|saturday|sunday|"
            r"escape room|mini golf|go kart|padel|hike|bowling|pub|"
            r"\b\d{1,2}(?:st|nd|rd|th)\b",
            body,
            re.I,
        ):
            continue
        invite = _short_invite(body)
        when = parse_event_date(invite, today)
        if when is not None and when < today:
            continue
        last_invite = invite
    return last_invite


def extract_email(text: str) -> str:
    match = _EMAIL_RE.search(text or "")
    return (match.group(1) if match else "").strip()


def extract_interests(text: str) -> str:
    found: list[str] = []
    blob = (text or "").casefold()
    for key in _INTEREST_KEYS:
        if key in blob and key not in found:
            label = "board games" if key.startswith("board game") else key
            if label == "hike":
                label = "hiking"
            if label not in found:
                found.append(label)
    return ", ".join(found)


def parse_event_date(event: str, today: date | None = None) -> date | None:
    today = today or date.today()
    if not event:
        return None
    match = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(january|february|march|april|may|june|"
        r"july|august|september|october|november|december|jan|feb|mar|apr|jun|"
        r"jul|aug|sep|sept|oct|nov|dec)\b",
        event,
        re.I,
    )
    if not match:
        return None
    day = int(match.group(1))
    month = _MONTHS[match.group(2).lower()]
    try:
        return date(today.year, month, day)
    except ValueError:
        return None


def event_follow_up_date(event: str, today: date | None = None) -> str:
    today = today or date.today()
    when = parse_event_date(event, today)
    if when is None or when < today:
        return ""
    return when.isoformat()


def suggested_crm_name(inbox_name: str, display_name: str | None = None) -> str:
    raw = re.split(r"[•·|,—–]", display_name or inbox_name or "", maxsplit=1)[0].strip()
    raw = raw.split(",")[0].strip()
    if not raw or len(raw) > 40:
        raw = inbox_name
    first = (raw.split() or [raw])[0]
    if first and first.casefold() == (inbox_name or "").split()[0].casefold():
        raw = inbox_name.split()[0] if inbox_name else first
    return with_lgs_suffix(raw)


def extract_crm_fields(
    messages: list[dict],
    *,
    inbox_name: str,
    display_name: str = "",
    profile_location: str = "",
    profile_age: int | None = None,
    ethnicity: str = "",
    phone_id: str = "toby",
    groups: list[dict] | None = None,
    today: date | None = None,
) -> dict:
    them = _them_blob(messages)
    everyone = "\n".join(str(m.get("body") or "") for m in messages)
    hometown = extract_hometown(them, profile_location)
    insta = extract_instagram_them(them)
    phones = extract_phones(them) or extract_phones(everyone)
    age = extract_age(them, profile_age)
    tiktok = extract_tiktok(them)
    event = extract_event(messages, today=today)
    hub = guess_hub(hometown, them)
    if re.search(r"\bbattersea\b", them, re.I) and not re.search(
        r"\b(?:high )?wycombe\b", them, re.I
    ):
        hub = "london"
    group_id = match_home_group(groups or [], hub, hometown)
    drives = bool(_DRIVE_RE.search(them))
    email = extract_email(them) or extract_email(everyone)
    interests = extract_interests(them)
    if event:
        extra = extract_interests(event)
        if extra:
            merged = [p.strip() for p in interests.split(",") if p.strip()]
            for part in extra.split(","):
                part = part.strip()
                if part and part not in merged:
                    merged.append(part)
            interests = ", ".join(merged)
    follow_up = event_follow_up_date(event, today=today)
    gave_phone = bool(phones)
    method = "whatsapp" if gave_phone or _WA_RE.search(them) else ""
    tags: list[str] = ["bumble friends"]
    if event:
        if re.search(r"escape room", event, re.I):
            tags.append("escape room")
        if re.search(r"board game", event, re.I):
            tags.append("board games")
        if re.search(r"hike|hiking", event, re.I):
            tags.append("hiking")
    if drives:
        tags.append("drives")
    if insta:
        tags.append("instagram")
    notes = []
    who = "Toby" if (phone_id or "toby") != "archie" else "Archie"
    notes.append(f"Bumble Friends / {who}")
    if hometown:
        notes.append(f"Region: {hometown}")
    if age:
        notes.append(f"Age: {age}")
    if event:
        notes.append(f"Event: {event}")
    if drives:
        notes.append("Travels / drives")
    if insta:
        notes.append("ig @" + ", @".join(insta))
    if tiktok:
        notes.append("tiktok @" + tiktok)
    return {
        "suggested_contact_name": suggested_crm_name(inbox_name, display_name),
        "phone": phones[0] if phones else "",
        "phone_candidates": phones,
        "email": email,
        "instagram": insta[0] if insta else "",
        "instagram_candidates": insta,
        "tiktok": tiktok,
        "hometown": hometown,
        "region": hometown,
        "address": "",
        "age": age,
        "ethnicity": (ethnicity or "").strip(),
        "interested_event": event,
        "interests_skills": interests,
        "tags": tags,
        "status": "prospect",
        "source": "bumble_friends",
        "preferred_contact_method": method,
        "consent_to_contact": gave_phone,
        "next_follow_up_at": follow_up,
        "hub": hub,
        "home_lgs_group_id": group_id,
        "closest_lgs_group_id": group_id,
        "suggested_notes": " · ".join(notes),
    }


def crm_has_location(fields: dict) -> bool:
    for key in ("hometown", "region"):
        if str(fields.get(key) or "").strip():
            return True
    return bool(str(fields.get("hub") or "").strip())


def crm_ready_to_save(fields: dict) -> bool:
    """Name + phone + a rough base (said in chat, profile town, or hub)."""
    name = str(fields.get("suggested_contact_name") or fields.get("name") or "").strip()
    phone = str(fields.get("phone") or "").strip()
    return bool(name and phone and crm_has_location(fields))


def _crm_digits(value: object) -> str:
    return re.sub(r"\D+", "", str(value or ""))[-10:]


def _crm_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def crm_should_update(existing: dict | None, fields: dict) -> bool:
    """True when the thread has a fact the CRM card is missing or has stale."""
    if not existing:
        return True
    checks = (
        (_crm_digits(existing.get("phone")), _crm_digits(fields.get("phone"))),
        (
            _crm_text(existing.get("hometown") or existing.get("region")),
            _crm_text(fields.get("hometown") or fields.get("region")),
        ),
        (
            _crm_text(str(existing.get("instagram") or "").lstrip("@")),
            _crm_text(str(fields.get("instagram") or "").lstrip("@")),
        ),
        (
            _crm_text(str(existing.get("tiktok") or "").lstrip("@")),
            _crm_text(str(fields.get("tiktok") or "").lstrip("@")),
        ),
        (_crm_text(existing.get("email")), _crm_text(fields.get("email"))),
        (
            _crm_text(existing.get("interested_event")),
            _crm_text(fields.get("interested_event")),
        ),
        (_crm_text(existing.get("age")), _crm_text(fields.get("age"))),
        (
            _crm_text(existing.get("interests_skills")),
            _crm_text(fields.get("interests_skills")),
        ),
    )
    for old, new in checks:
        if new and new != old:
            return True
    old_g = existing.get("closest_lgs_group_id") or existing.get("home_lgs_group_id")
    new_g = fields.get("closest_lgs_group_id") or fields.get("home_lgs_group_id")
    try:
        if new_g and int(new_g) != int(old_g or 0):
            return True
    except (TypeError, ValueError):
        pass
    return False
