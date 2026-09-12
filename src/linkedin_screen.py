"""Parse LinkedIn Android feed and messaging hierarchies."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from src.screen import _bounds_center

PACKAGE = "com.linkedin.android"

_UNREAD = re.compile(r"\b\d+\s+unread\b|\bunread message", re.I)
_NAME_PREVIEW = re.compile(
    r"^(?:conversation with\s+)?(.+?)(?:[:,–—-]\s+|\.\s+)(.+)$",
    re.I,
)
_LONDON = ZoneInfo("Europe/London")
_SELF_SENDERS = {"you", "toby claxton", "archie wilding"}
_SENDER_STAMP = re.compile(
    r"^(?P<who>.+?)\s+x\s+[•·∙‧\u2022\u2219\-]?\s*(?P<time>\d{1,2}:\d{2}\s*[ap]m)$",
    re.I,
)
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})\s*([ap]m)?", re.I)
_PROFILE_BADGE = re.compile(
    r"(system_verified|verified_small|\+icon\b|sys_icn|logos_bugs|"
    r"premium_inbug|premium_v2|xxsmall\+premium)",
    re.I,
)
_HEADLINE_ROLE = re.compile(
    r"\b(co-?founders?|founders?|ceo|cto|cfo|coo|investors?|engineers?|"
    r"directors?|managers?|head of|authors?|consultants?|advisors?|"
    r"partners?|presidents?|owners?|specialists?|recruiters?)\b",
    re.I,
)
_MONTH = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_SKIP_NAMES = {
    "message",
    "messages",
    "button",
    "cover",
    "search",
    "home",
    "back",
    "compose",
    "like",
    "view",
    "more",
    "follow",
    "share",
}


@dataclass(frozen=True)
class LiListHit:
    name: str
    preview: str
    unread: bool
    x: int
    y: int
    bounds: str


def _rid(node) -> str:
    return (node.attrib.get("resource-id") or "").rsplit("/", 1)[-1]


def _parse_bounds(bounds: str) -> tuple[int, int, int, int] | None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def is_linkedin_package(package: str | None) -> bool:
    return bool(package) and "linkedin" in package.lower()


def looks_like_messaging(xml: str) -> bool:
    blob = xml.lower()
    return any(
        s in blob
        for s in (
            "messaging_conversation_list_item",
            "conversation_list_container",
            "search messages",
            "pill_inbox_search",
            "conversation with",
        )
    )


def looks_like_feed(xml: str) -> bool:
    blob = xml.lower()
    return ("tab_feed" in blob or "home_messaging" in blob or "home 1 of 5" in blob) and (
        "cover photo" not in blob or "tab_feed" in blob
    )


def looks_like_profile(xml: str) -> bool:
    blob = xml.lower()
    if looks_like_messaging(xml) or looks_like_feed(xml):
        return False
    return "cover photo" in blob or ("connections" in blob and "back button" in blob)


def looks_like_share_sheet(xml: str) -> bool:
    blob = xml.lower()
    return "copy link" in blob and ("save link" in blob or "mail" in blob)


def looks_like_in_app_web(xml: str) -> bool:
    blob = xml.lower()
    return "web_view_container" in blob or "cookie consent" in blob or "stcm-banner" in blob


def looks_like_blocker(xml: str) -> bool:
    return looks_like_share_sheet(xml) or looks_like_in_app_web(xml)


def hierarchy_is_linkedin(xml: str) -> bool:
    """True when the dump is LinkedIn, not Bumble For Friends."""
    blob = xml or ""
    if "com.linkedin.android" in blob:
        return True
    return looks_like_messaging(blob) or looks_like_feed(blob) or looks_like_blocker(blob)


def find_dismiss_point(xml: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        desc = (node.attrib.get("content-desc") or "").strip().lower()
        text = (node.attrib.get("text") or "").strip().lower()
        if desc in {"close", "close button"} or text == "close":
            return _bounds_center(node.attrib.get("bounds") or "")
    return None


def find_inbox_search(xml: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        rid = _rid(node)
        desc = (node.attrib.get("content-desc") or "").strip().lower()
        text = (node.attrib.get("text") or "").strip().lower()
        if rid in {"pill_inbox_search_box", "pill_inbox_search_box_container"}:
            return _bounds_center(node.attrib.get("bounds") or "")
        if desc == "search messages" or text == "search messages":
            return _bounds_center(node.attrib.get("bounds") or "")
    return None


def find_messaging_entry(xml: str) -> tuple[int, int] | None:
    """Header inbox icon — LinkedIn has no bottom Messaging tab."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        if _rid(node) == "home_messaging":
            return _bounds_center(node.attrib.get("bounds") or "")
    for node in root.iter():
        desc = (node.attrib.get("content-desc") or "").strip().lower()
        if desc.startswith("messaging") and (node.attrib.get("clickable") or "").lower() == "true":
            return _bounds_center(node.attrib.get("bounds") or "")
    return None


def find_home_tab(xml: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        if _rid(node) == "tab_feed":
            return _bounds_center(node.attrib.get("bounds") or "")
    for node in root.iter():
        desc = (node.attrib.get("content-desc") or "").strip().lower()
        text = (node.attrib.get("text") or "").strip().lower()
        if text == "home" or desc.startswith("home "):
            return _bounds_center(node.attrib.get("bounds") or "")
    return None


def find_back_point(xml: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        desc = (node.attrib.get("content-desc") or "").strip().lower()
        if desc in {"back", "back button", "navigate up"}:
            return _bounds_center(node.attrib.get("bounds") or "")
    return None


def find_nav_point(xml: str, label: str) -> tuple[int, int] | None:
    target = label.strip().lower()
    if target == "messaging":
        return find_messaging_entry(xml)
    if target == "home":
        return find_home_tab(xml)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    hits: list[tuple[int, int, int]] = []
    for node in root.iter():
        text = (node.attrib.get("text") or "").strip().lower()
        desc = (node.attrib.get("content-desc") or "").strip().lower()
        if text != target and desc != target and not desc.startswith(target + " "):
            continue
        bounds = node.attrib.get("bounds") or ""
        center = _bounds_center(bounds)
        box = _parse_bounds(bounds)
        if center is None or box is None:
            continue
        hits.append((box[1], center[0], center[1]))
    if not hits:
        return None
    hits.sort(reverse=True)
    _, x, y = hits[0]
    return (x, y)


def parse_messaging_list(xml: str) -> list[LiListHit]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    rows = _parse_conversation_rows(root)
    if rows:
        return rows
    if looks_like_messaging(xml):
        return _parse_generic_list(root)
    return []


def _parse_conversation_rows(root: ET.Element) -> list[LiListHit]:
    hits: list[LiListHit] = []
    seen: set[str] = set()
    for node in root.iter():
        if _rid(node) != "messaging_conversation_list_item_container":
            continue
        name = ""
        preview = ""
        unread = False
        for child in node.iter():
            rid = _rid(child)
            text = (child.attrib.get("text") or "").strip()
            if rid == "messaging_conversation_list_item_title" and text:
                name = text
            elif rid == "messaging_conversation_summary" and text:
                preview = text
            elif rid == "messaging_conversation_unread_count" and text:
                unread = True
        desc = (node.attrib.get("content-desc") or "").strip()
        if _UNREAD.search(desc):
            unread = True
        if not name:
            name, preview = _split_row_desc(desc, preview)
        if not name or name.casefold() in _SKIP_NAMES:
            continue
        center = _bounds_center(node.attrib.get("bounds") or "")
        if center is None:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        hits.append(
            LiListHit(
                name=name,
                preview=preview,
                unread=unread,
                x=center[0],
                y=center[1],
                bounds=node.attrib.get("bounds") or "",
            )
        )
    hits.sort(key=lambda h: h.y)
    return hits


def _split_row_desc(desc: str, preview: str) -> tuple[str, str]:
    """'Button, Ada Lovelace, Loved the hiking note, Sunday' → name + preview."""
    parts = [p.strip() for p in desc.split(",") if p.strip()]
    if parts and parts[0].casefold() == "button":
        parts = parts[1:]
    if not parts:
        return "", preview
    name = parts[0]
    if not preview and len(parts) > 1:
        preview = parts[1]
    return name, preview


def _parse_generic_list(root: ET.Element) -> list[LiListHit]:
    hits: list[LiListHit] = []
    seen: set[str] = set()
    for node in root.iter():
        if (node.attrib.get("clickable") or "").lower() != "true":
            continue
        desc = (node.attrib.get("content-desc") or "").strip()
        text = (node.attrib.get("text") or "").strip()
        blob = desc or text
        if not blob or len(blob) < 2:
            continue
        low = blob.lower()
        if any(
            k in low
            for k in (
                "tab bar",
                "bottom navigation",
                "compose",
                "search",
                "home",
                "my network",
                "jobs",
                "notifications",
                "cover photo",
                "profile",
                "follow ",
                "pending",
                "verifications",
            )
        ):
            continue
        unread = bool(_UNREAD.search(blob))
        name, preview = _split_name_preview(blob)
        if not name or name.casefold() in _SKIP_NAMES:
            continue
        center = _bounds_center(node.attrib.get("bounds") or "")
        if center is None:
            continue
        box = _parse_bounds(node.attrib.get("bounds") or "")
        if box and (box[3] - box[1]) < 80:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        hits.append(
            LiListHit(
                name=name,
                preview=preview,
                unread=unread,
                x=center[0],
                y=center[1],
                bounds=node.attrib.get("bounds") or "",
            )
        )
    hits.sort(key=lambda h: h.y)
    return hits


def _split_name_preview(blob: str) -> tuple[str, str]:
    blob = re.sub(r"\s+", " ", blob).strip()
    m = _NAME_PREVIEW.match(blob)
    if m:
        name = m.group(1).strip(" ·,.-")
        preview = m.group(2).strip()
        if 1 < len(name) < 80:
            return name, preview
    parts = blob.split(" ", 3)
    if len(parts) >= 2 and parts[0][:1].isalpha():
        return parts[0], " ".join(parts[1:])[:160]
    return "", ""


_PROFILE_VIEW = re.compile(r"^View\s+(.+?)(?:['’]s)\s+profile$", re.I)
_COMPANY_VIEW = re.compile(r"^View company:\s*(.+)$", re.I)
_REACT_STATE = re.compile(r"reaction button state:\s*(.+)", re.I)


@dataclass(frozen=True)
class FeedPost:
    actor: str
    kind: str
    text: str
    already: bool
    promoted: bool
    like_x: int
    like_y: int


def parse_feed_posts(xml: str) -> list[FeedPost]:
    """Visible Home posts with a Like control we can tap or long-press."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    parent = {child: node for node in root.iter() for child in list(node)}
    actors: list[tuple[int, str, str]] = []
    texts: list[tuple[int, str]] = []
    promoted_ys: list[int] = []
    likes: list[tuple[int, int, int, bool]] = []
    for node in root.iter():
        desc = (node.attrib.get("content-desc") or "").strip()
        text = (node.attrib.get("text") or "").strip()
        center = _bounds_center(node.attrib.get("bounds") or "")
        if center is None:
            continue
        low = desc.lower()
        if "promoted" in low:
            promoted_ys.append(center[1])
        prof = _PROFILE_VIEW.match(desc)
        if prof:
            actors.append((center[1], prof.group(1).strip(), "person"))
            continue
        comp = _COMPANY_VIEW.match(desc)
        if comp:
            actors.append((center[1], comp.group(1).strip(), "company"))
            continue
        state = _REACT_STATE.search(desc)
        if state:
            host = node
            while host in parent:
                host = parent[host]
                clickable = (host.attrib.get("clickable") or "").lower() == "true"
                if clickable:
                    hc = _bounds_center(host.attrib.get("bounds") or "")
                    if hc:
                        already = "no reaction" not in state.group(1).strip().lower()
                        likes.append((hc[1], hc[0], hc[1], already))
                    break
            continue
        if (node.attrib.get("clickable") or "").lower() == "true" and len(desc) > 40:
            if any(
                k in low
                for k in (
                    "view company",
                    "view more options",
                    "’s profile",
                    "'s profile",
                    "repost",
                    "notification",
                    "search for",
                )
            ) or low.startswith(("comment", "like ", "follow ")):
                continue
            texts.append((center[1], desc.replace("\n", " ").strip()))
    likes.sort(key=lambda row: row[0])
    posts: list[FeedPost] = []
    seen: set[tuple[int, int]] = set()
    for _y, lx, ly, already in likes:
        if (lx, ly) in seen:
            continue
        seen.add((lx, ly))
        above = [(ay, name, akind) for ay, name, akind in actors if 0 < ly - ay < 2000]
        if above:
            ay, actor, kind = max(above, key=lambda row: row[0])
        else:
            ay, actor, kind = 0, "", "unknown"
        body = ""
        for ty, t in texts:
            if ty >= ly or ly - ty > 1600:
                continue
            if ay and not (ay < ty < ly):
                continue
            if len(t) > len(body):
                body = t
        promoted = any(ay <= py <= ly for py in promoted_ys) if ay else any(abs(py - ly) < 400 for py in promoted_ys)
        if not actor and not body:
            continue
        posts.append(
            FeedPost(
                actor=actor or "Unknown",
                kind=kind,
                text=body[:400],
                already=already,
                promoted=promoted,
                like_x=lx,
                like_y=ly,
            )
        )
    return posts


def find_reaction_tray(xml: str) -> dict[str, tuple[int, int]]:
    """Emoji tray after a long-press on Like."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {}
    keys = ("like", "celebrate", "support", "love", "insightful", "funny")
    out: dict[str, tuple[int, int]] = {}
    for node in root.iter():
        label = ((node.attrib.get("content-desc") or "") + " " + (node.attrib.get("text") or "")).strip().lower()
        center = _bounds_center(node.attrib.get("bounds") or "")
        if center is None or not label:
            continue
        for key in keys:
            if label == key or label.startswith(key + " "):
                out.setdefault(key, center)
    return out


def find_reaction_points(xml: str) -> dict[str, tuple[int, int]]:
    """Like / Celebrate (clap) tap points on a visible feed item."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {}
    out: dict[str, tuple[int, int]] = {}
    for node in root.iter():
        label = ((node.attrib.get("content-desc") or "") + " " + (node.attrib.get("text") or "")).strip().lower()
        center = _bounds_center(node.attrib.get("bounds") or "")
        if center is None or not label:
            continue
        if label in {"like", "likes"} or label.startswith("like "):
            out.setdefault("like", center)
        elif "celebrate" in label or "clap" in label:
            out.setdefault("celebrate", center)
        elif "insightful" in label:
            out.setdefault("insightful", center)
    return out


_INMAIL_PREFIX = re.compile(r"^(inmail|sponsored)\b", re.I)
_INMAIL_BULLET = re.compile(r"\b(inmail|sponsored)\s*[•·∙‧\u2022]", re.I)
_INMAIL_PROMO = re.compile(
    r"grow your hiring|expand your (?:bd|b\.?d\.?|pipeline)|"
    r"hiring pipeline|linkedin member|"
    r"try linkedin (?:ads|premium|recruiter)",
    re.I,
)


def looks_like_inmail_promo(text: str | None) -> bool:
    """Inbox chrome for InMail / Sponsored / LinkedIn hiring ads — not a 1:1 bubble."""
    raw = _fold_ui(text)
    if not raw:
        return False
    if _INMAIL_PREFIX.match(raw) or _INMAIL_BULLET.search(raw):
        return True
    if len(raw) < 160 and _INMAIL_PROMO.search(raw):
        return True
    return False


def inmail_kind(*texts: str | None) -> str:
    """'sponsored', 'inmail', or '' from inbox preview / last_text."""
    kinds: list[str] = []
    for text in texts:
        if not looks_like_inmail_promo(text):
            continue
        low = _fold_ui(text).casefold()
        if low.startswith("sponsored") or "sponsored •" in low or "sponsored ·" in low:
            kinds.append("sponsored")
        else:
            kinds.append("inmail")
    if "sponsored" in kinds:
        return "sponsored"
    return kinds[0] if kinds else ""


def should_open_row(hit: LiListHit, stored_preview: str | None) -> bool:
    if looks_like_inmail_promo(hit.preview):
        return False
    if hit.unread:
        return True
    stored = (stored_preview or "").strip()
    if stored and hit.preview and hit.preview.strip() != stored:
        return True
    return False


def plausible_person_name(name: str | None) -> bool:
    text = (name or "").strip()
    if not text or text.casefold() in _SKIP_NAMES:
        return False
    low = text.casefold()
    if low in {"active now", "today"} or low.startswith("mobile"):
        return False
    if " ago" in low or "grantgunner" in low:
        return False
    if "." in text and " " not in text:
        return False
    return 1 < len(text) < 80


def sender_is_self(sender: str | None) -> bool:
    head = (sender or "").split(" x")[0].split("•")[0].strip().casefold()
    if not head:
        return False
    if head.startswith("you"):
        return True
    return head in _SELF_SENDERS


def strip_you_prefix(text: str) -> str:
    body = (text or "").strip()
    if body[:4].casefold() == "you:":
        return body[4:].strip()
    return body


def _fold_ui(text: str) -> str:
    return re.sub(r"[\s\u00a0\u202f\u2007\u2009]+", " ", text or "").strip()


def sender_stamp_side(text: str) -> str | None:
    raw = _fold_ui(text)
    match = _SENDER_STAMP.match(raw)
    if not match:
        return None
    return "you" if sender_is_self(match.group("who")) else "them"


_DATE_ONLY = re.compile(
    r"^(today|yesterday|"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\s+\d{1,2}(?:,?\s+\d{4})?)$",
    re.I,
)
_TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}(?:\s*[ap]m)?$", re.I)
_WEEKDAY = {
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
}
_SELF_MARKERS = (
    "grantgunner",
    "canvassr",
    "i am the founder",
    "i'm the founder",
    "founder of grantgunner",
    "thanks for connecting",
    "floating this back",
    "leave it there",
    "my co-founder",
    "demo video on my linkedin",
    "apply for 10 grants",
    "went to the pub",
    "donate a full week",
    "donate a week of our time",
    "week of free fundraising",
    "week of our time",
    "pro bono",
    "first application within a week",
    "door-to-door campaign",
)
_THEM_GREET = re.compile(
    r"\b(morning|hi|hey|hello|thanks|thank you|cheers)\s+(toby|archie)\b",
    re.I,
)
_SELF_SLOTS = re.compile(
    r"\bare you free\b.+\b(mon|tues|wednes|thurs|fri|sat|sun|morning|afternoon|evening|any time)\b",
    re.I,
)


def looks_like_profile_badge(text: str, partner: str | None = None) -> bool:
    raw = _fold_ui(text)
    if not raw:
        return False
    if _PROFILE_BADGE.search(raw):
        return True
    if not partner:
        return False
    name = _fold_ui(partner)
    if not raw.casefold().startswith(name.casefold()):
        return False
    rest = raw[len(name) :].strip()
    if not rest:
        return False
    if _PROFILE_BADGE.search(rest):
        return True
    token = rest.replace("+", " ").replace("_", " ")
    return bool(token) and token.replace(" ", "").isalnum() and rest.upper() == rest


def looks_like_headline(text: str, partner: str | None = None) -> bool:
    raw = _fold_ui(text)
    if len(raw) < 12 or len(raw) > 400:
        return False
    if looks_like_profile_badge(raw, partner) or sender_stamp_side(raw):
        return False
    if infer_them(raw, partner) or infer_self_pitch(raw, partner):
        return False
    if re.search(r"[?!]|\b(hi|hey|hello|thanks|thank you|morning|cheers)\b", raw, re.I):
        return False
    roles = _HEADLINE_ROLE.findall(raw)
    if len(roles) >= 2 and ("," in raw or " of " in raw.casefold()):
        return True
    if roles and (" | " in raw or " · " in raw or raw.count("|") >= 1):
        return True
    return False


def profile_bits_from_text(text: str, partner: str | None = None) -> dict:
    raw = _fold_ui(text)
    bits: dict = {}
    if looks_like_profile_badge(raw, partner):
        bits["verified"] = True
    if looks_like_headline(raw, partner):
        bits["headline"] = raw
    return bits


def is_thread_chrome(text: str, partner: str | None = None) -> bool:
    raw = _fold_ui(text)
    if not raw:
        return True
    if sender_stamp_side(raw) is not None:
        return True
    if looks_like_profile_badge(raw, partner) or looks_like_headline(raw, partner):
        return True
    low = raw.casefold()
    if looks_like_inmail_promo(raw):
        return True
    if "sys_icn" in low or "download brochure" in low:
        return True
    if _DATE_ONLY.match(raw) or _TIME_ONLY.match(raw):
        return True
    if low in _WEEKDAY:
        return True
    if low in {
        "write a message",
        "write a message…",
        "messaging",
        "send",
        "active now",
        "view my services",
        "view profile",
    }:
        return True
    if partner and raw.casefold() == partner.casefold():
        return True
    return False


def parse_thread_profile(xml: str) -> dict:
    """Headline / verification from the open-thread profile card, not from bubbles."""
    out = {"headline": "", "verified": False}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    name = None
    for node in root.iter():
        if _rid(node) == "messaging_toolbar_title":
            cand = (node.attrib.get("text") or "").strip()
            if plausible_person_name(cand):
                name = cand
                break
    for node in root.iter():
        rid = _rid(node)
        text = _fold_ui(node.attrib.get("text") or "")
        desc = _fold_ui(node.attrib.get("content-desc") or "")
        if rid == "one_on_one_occupation" and text:
            out["headline"] = text
        if looks_like_profile_badge(text, name) or looks_like_profile_badge(desc, name):
            out["verified"] = True
        bits = profile_bits_from_text(text, name)
        if bits.get("headline") and not out["headline"]:
            out["headline"] = bits["headline"]
        if bits.get("verified"):
            out["verified"] = True
    return out


def infer_them(body: str, partner: str | None) -> bool:
    text = (body or "").strip()
    if not text:
        return False
    if _THEM_GREET.search(text):
        return True
    if re.search(r"\bkr\.?\s*$", text, re.I):
        return True
    first = ((partner or "").strip().split() or [""])[0]
    if first and re.search(rf"\bi['’]m\s+{re.escape(first)}\b", text, re.I):
        return True
    return False


def infer_self_pitch(body: str, partner: str | None) -> bool:
    text = (body or "").strip()
    if not text or infer_them(text, partner):
        return False
    low = text.casefold()
    if any(token in low for token in _SELF_MARKERS):
        return True
    first = ((partner or "").strip().split() or [""])[0]
    if first and re.match(rf"^(hi|hey|hello|hi again)\s+{re.escape(first)}\b", low):
        if any(token in low for token in ("co-founder", "our time", "fundraising", "canvassr", "grant")):
            return True
    if _SELF_SLOTS.search(text):
        return True
    return False


def polish_message(side: str, body: str, partner: str | None = None) -> tuple[str, str]:
    text = strip_you_prefix(body)
    if infer_them(text, partner):
        return "them", text
    if infer_self_pitch(text, partner) or (body or "").strip()[:4].casefold() == "you:":
        return "you", text
    who = "you" if side == "you" else (side or "them")
    return who, text


def repair_thread_messages(items: list[tuple[str, str]], partner: str | None = None) -> list[tuple[str, str]]:
    """Drop chrome/sender stamps and flip obvious self/partner bubbles."""
    out: list[tuple[str, str]] = []
    pending: str | None = None
    for item in items:
        side = item[0]
        body = item[1] if len(item) > 1 else ""
        raw = strip_you_prefix(body)
        stamp = sender_stamp_side(raw)
        if stamp:
            pending = stamp
            continue
        if is_thread_chrome(raw, partner):
            continue
        who = pending or side
        pending = None
        who, cleaned = polish_message(who, raw, partner)
        if not cleaned:
            continue
        if out and out[-1] == (who, cleaned):
            continue
        out.append((who, cleaned))
    return out


def _parse_header_date(text: str, now: datetime) -> datetime | None:
    raw = (text or "").strip()
    low = raw.casefold()
    if low == "today":
        return now.replace(hour=12, minute=0, second=0, microsecond=0)
    if low == "yesterday":
        return (now - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    match = re.match(r"([a-z]{3})\s+(\d{1,2})(?:,?\s*(\d{4}))?", low)
    if not match or match.group(1) not in _MONTH:
        return None
    year = int(match.group(3) or now.year)
    try:
        return now.replace(
            year=year,
            month=_MONTH[match.group(1)],
            day=int(match.group(2)),
            hour=12,
            minute=0,
            second=0,
            microsecond=0,
        )
    except ValueError:
        return None


def _parse_clock(text: str) -> tuple[int, int] | None:
    match = _CLOCK.search(text or "")
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    ampm = (match.group(3) or "").casefold()
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _stamp_iso(day: datetime, sender: str) -> str | None:
    clock = _parse_clock(sender)
    if clock is None:
        return None
    local = day.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_open_thread(xml: str) -> tuple[str | None, list[tuple]]:
    """Return (partner name, [(side, body), ...]) from an open conversation."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None, []
    name = None
    for node in root.iter():
        if _rid(node) == "messaging_toolbar_title":
            text = (node.attrib.get("text") or "").strip()
            if plausible_person_name(text):
                name = text
                break
    if name is None:
        for node in root.iter():
            rid = _rid(node).lower()
            desc = (node.attrib.get("content-desc") or "").strip()
            text = (node.attrib.get("text") or "").strip()
            if rid in {"toolbar_title", "conversation_title"} and plausible_person_name(text):
                name = text
            if desc.lower().startswith("conversation with") and not name:
                cand = desc.split("with", 1)[-1].strip()
                if plausible_person_name(cand):
                    name = cand
    msgs = _parse_thread_bodies(root, name)
    if not msgs:
        msgs = _parse_thread_bodies_generic(root, name)
    polished: list[tuple] = []
    for item in msgs:
        side, body = item[0], item[1]
        when = item[2] if len(item) > 2 else None
        side, body = polish_message(side, body, name)
        polished.append((side, body, when) if when else (side, body))
    cleaned = repair_thread_messages([(item[0], item[1]) for item in polished], name)
    when_by_body = {item[1]: item[2] for item in polished if len(item) > 2}
    out: list[tuple] = []
    for side, body in cleaned:
        when = when_by_body.get(body)
        out.append((side, body, when) if when else (side, body))
    return name, out


def fold_thread_chunk(
    thread: list[tuple],
    seen: set[tuple[str, str]],
    chunk: list[tuple],
    *,
    prepend: bool,
) -> int:
    """Merge overlapping screens while preserving timestamps and repeated bodies."""
    incoming = [item for item in chunk if len(item) > 1 and str(item[1] or "").strip()]
    if not incoming:
        return 0

    def key(item: tuple) -> tuple:
        # Header dates can be incomplete on one overlapping screen, so body/side
        # define overlap. Repeated messages inside a single screen remain intact.
        return (str(item[0]), str(item[1]))

    if not thread:
        thread.extend(incoming)
        seen.update((str(item[0]), str(item[1])) for item in incoming)
        return len(incoming)

    overlap = 0
    if prepend:
        limit = min(len(incoming), len(thread))
        for size in range(limit, 0, -1):
            if [key(item) for item in incoming[-size:]] == [key(item) for item in thread[:size]]:
                overlap = size
                break
        unseen = incoming[:-overlap] if overlap else incoming
        thread[:0] = unseen
    else:
        limit = min(len(incoming), len(thread))
        for size in range(limit, 0, -1):
            if [key(item) for item in thread[-size:]] == [key(item) for item in incoming[:size]]:
                overlap = size
                break
        unseen = incoming[overlap:]
        thread.extend(unseen)
    seen.update((str(item[0]), str(item[1])) for item in unseen)
    return len(unseen)


def _parse_thread_bodies(root: ET.Element, partner: str | None = None) -> list[tuple]:
    now = datetime.now(_LONDON)
    day = now.replace(hour=12, minute=0, second=0, microsecond=0)
    msgs: list[tuple] = []
    for node in root.iter():
        rid = _rid(node)
        text = (node.attrib.get("text") or "").strip()
        if rid == "messaging_header_time" and text:
            parsed = _parse_header_date(text, now)
            if parsed is not None:
                day = parsed
            continue
        if rid != "message_list_item_container":
            continue
        sender = ""
        body = ""
        for child in node.iter():
            child_rid = _rid(child)
            child_text = (child.attrib.get("text") or "").strip()
            if child_rid == "sender_name" and child_text:
                sender = child_text
            elif child_rid == "body" and child_text:
                body = child_text
        if not body or is_thread_chrome(body, partner):
            continue
        side = "you" if sender_is_self(sender) else "them"
        side, body = polish_message(side, body, partner)
        when = _stamp_iso(day, sender)
        msgs.append((side, body, when) if when else (side, body))
    return msgs


def _parse_thread_bodies_generic(root: ET.Element, name: str | None) -> list[tuple[str, str]]:
    msgs: list[tuple[str, str]] = []
    pending: str | None = None
    for node in root.iter():
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        rid = _rid(node).lower()
        if not text or len(text) > 2000:
            continue
        if text.lower() in {"messaging", "write a message", "write a message…", "send", "home", "search messages"}:
            continue
        if "toolbar" in rid or rid.endswith("title") or "conversation_title" in rid:
            continue
        if rid in {
            "messaging_conversation_timestamp",
            "swipe_action_text",
            "messaging_header_time",
            "one_on_one_occupation",
            "participant_name",
            "messaging_toolbar_subtitle",
            "messaging_premium_custom_button_banner_text",
            "messaging_mercado_smart_quick_reply",
            "sender_name",
        }:
            stamp = sender_stamp_side(text)
            if stamp:
                pending = stamp
            continue
        stamp = sender_stamp_side(text)
        if stamp:
            pending = stamp
            continue
        if is_thread_chrome(text, name):
            continue
        if name and text == name:
            continue
        if not plausible_person_name(text) and len(text) < 20:
            continue
        side = pending or "them"
        pending = None
        blob = f"{desc} {rid}"
        if any(k in blob.lower() for k in ("you sent", "outgoing", "from_me", "sent_by_me", "self")):
            side = "you"
        if text.lower().startswith("you:") or sender_is_self(text):
            side = "you"
        side, text = polish_message(side, text, name)
        msgs.append((side, text))
    return repair_thread_messages(msgs, name)


def names_match(left: str, right: str) -> bool:
    a = " ".join((left or "").split()).casefold()
    b = " ".join((right or "").split()).casefold()
    if not a or not b:
        return False
    if a == b:
        return True
    at, bt = a.split(), b.split()
    return len(at) >= 2 and len(bt) >= 2 and at[0] == bt[0] and at[-1] == bt[-1]


def find_more_options(xml: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        if _rid(node) == "messaging_toolbar_detail_option":
            return _bounds_center(node.attrib.get("bounds") or "")
    for node in root.iter():
        desc = (node.attrib.get("content-desc") or "").strip().lower()
        if desc in {"more options", "more option", "overflow"}:
            return _bounds_center(node.attrib.get("bounds") or "")
    return None


def find_archive_action(xml: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        label = ((node.attrib.get("content-desc") or "") + " " + (node.attrib.get("text") or "")).strip().lower()
        if label in {"archive", "archive conversation", "archive chat"} or label.startswith("archive "):
            return _bounds_center(node.attrib.get("bounds") or "")
    return None


def find_conversation_hit(xml: str, name: str) -> LiListHit | None:
    hits = [h for h in parse_messaging_list(xml) if names_match(h.name, name)]
    if len(hits) == 1:
        return hits[0]
    return None


def find_send_point(xml: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        rid = _rid(node).lower()
        desc = (node.attrib.get("content-desc") or "").strip().lower()
        text = (node.attrib.get("text") or "").strip().lower()
        if any(skip in rid for skip in ("receipt", "sent", "voice")):
            continue
        clickable = (node.attrib.get("clickable") or "").lower() == "true"
        if desc in {"send", "send message"} or text == "send":
            return _bounds_center(node.attrib.get("bounds") or "")
        if clickable and "send" in rid:
            return _bounds_center(node.attrib.get("bounds") or "")
    return None


def message_visible(xml: str, message: str) -> bool:
    needle = " ".join((message or "").split()).casefold()
    if not needle:
        return False
    return needle in " ".join((xml or "").split()).casefold()


def find_thread_profile_point(xml: str, partner: str | None = None) -> tuple[int, int] | None:
    """Open the person profile from an open DM (toolbar name / View profile)."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    want = _fold_ui(partner or "").casefold()
    for node in root.iter():
        rid = _rid(node)
        desc = _fold_ui(node.attrib.get("content-desc") or "")
        text = _fold_ui(node.attrib.get("text") or "")
        clickable = (node.attrib.get("clickable") or "").lower() == "true"
        low_desc = desc.casefold()
        if rid == "messaging_toolbar_title" and clickable:
            return _bounds_center(node.attrib.get("bounds") or "")
        if low_desc.startswith("view") and "profile" in low_desc:
            return _bounds_center(node.attrib.get("bounds") or "")
        if clickable and want and text.casefold() == want:
            return _bounds_center(node.attrib.get("bounds") or "")
    return None


_LOCATION_HINT = re.compile(
    r"\b("
    r"united kingdom|great britain|england|scotland|wales|northern ireland|"
    r"greater london|london|belfast|manchester|birmingham|edinburgh|glasgow|"
    r"dublin|leeds|bristol|cardiff|uk\b|ireland|europe|remote"
    r")\b",
    re.I,
)
_CHROME_PROFILE = re.compile(
    r"^(message|connect|follow|more|share|about|activity|experience|education|"
    r"featured|skills|interests|contact info|open to|services|highlights|"
    r"people also viewed|view my services)$",
    re.I,
)
_AGO = re.compile(r"\b(\d+\s*(m|h|d|w|mo|yr|year|hour|minute|day|week)s?\s+ago|just now)\b", re.I)


def parse_person_profile(xml: str, partner: str | None = None) -> dict:
    """About / location / title / a few recent post texts from a profile hierarchy."""
    out: dict = {"headline": "", "about": "", "location": "", "title": "", "posts": []}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    texts: list[str] = []
    for node in root.iter():
        raw = _fold_ui(node.attrib.get("text") or "")
        if not raw or _CHROME_PROFILE.match(raw):
            continue
        if looks_like_profile_badge(raw, partner):
            continue
        texts.append(raw)
    seen: set[str] = set()
    unique: list[str] = []
    for text in texts:
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(text)
    about_idx = next((i for i, t in enumerate(unique) if t.casefold() == "about"), -1)
    if about_idx >= 0:
        chunks = []
        for text in unique[about_idx + 1 : about_idx + 4]:
            if text.casefold() in {"activity", "experience", "education", "featured"}:
                break
            if len(text) >= 20:
                chunks.append(text)
        if chunks:
            out["about"] = " ".join(chunks)[:2000]
    name = _fold_ui(partner or "")
    for text in unique:
        if not out["headline"] and looks_like_headline(text, partner):
            out["headline"] = text
        if not out["location"] and 3 < len(text) < 80 and _LOCATION_HINT.search(text):
            if "http" not in text.casefold():
                out["location"] = text
        if (
            not out["title"]
            and 8 < len(text) < 160
            and _HEADLINE_ROLE.search(text)
            and text.casefold() != (out["headline"] or "").casefold()
            and (name and not text.casefold().startswith(name.casefold()) or not name)
        ):
            out["title"] = text
        if (
            len(out["posts"]) < 3
            and len(text) >= 40
            and _AGO.search(text)
            and text.casefold() != (out["about"] or "").casefold()
        ):
            body = _AGO.sub("", text).strip(" ·•-,")
            if len(body) >= 24:
                out["posts"].append(body[:400])
    if not out["posts"]:
        for text in unique:
            if len(out["posts"]) >= 3:
                break
            if len(text) < 50 or text in {out["about"], out["headline"], out["title"]}:
                continue
            if looks_like_headline(text, partner) or _LOCATION_HINT.search(text):
                continue
            if re.search(r"\b(followers?|connections?)\b", text, re.I):
                continue
            out["posts"].append(text[:400])
    if not out["title"] and out["headline"]:
        out["title"] = out["headline"]
    return out
