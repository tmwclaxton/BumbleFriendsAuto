"""Parse LinkedIn Android feed and messaging hierarchies."""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from src.screen import _bounds_center

PACKAGE = "com.linkedin.android"

_UNREAD = re.compile(r"\b\d+\s+unread\b|\bunread message", re.I)
_NAME_PREVIEW = re.compile(
    r"^(?:conversation with\s+)?(.+?)(?:[:,–—-]\s+|\.\s+)(.+)$",
    re.I,
)
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
    return _parse_generic_list(root)


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


def should_open_row(hit: LiListHit, stored_preview: str | None) -> bool:
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


def parse_open_thread(xml: str) -> tuple[str | None, list[tuple[str, str]]]:
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
    msgs = _parse_thread_bodies(root)
    if not msgs:
        msgs = _parse_thread_bodies_generic(root, name)
    return name, msgs[-40:]


def _parse_thread_bodies(root: ET.Element) -> list[tuple[str, str]]:
    msgs: list[tuple[str, str]] = []
    for node in root.iter():
        if _rid(node) != "message_list_item_container":
            continue
        sender = ""
        body = ""
        for child in node.iter():
            rid = _rid(child)
            text = (child.attrib.get("text") or "").strip()
            if rid == "sender_name" and text:
                sender = text
            elif rid == "body" and text:
                body = text
        if not body:
            continue
        side = "you" if sender.casefold().startswith("you") else "them"
        msgs.append((side, body))
    return msgs


def _parse_thread_bodies_generic(root: ET.Element, name: str | None) -> list[tuple[str, str]]:
    msgs: list[tuple[str, str]] = []
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
        }:
            continue
        if name and text == name:
            continue
        if not plausible_person_name(text) and len(text) < 20:
            continue
        side = "them"
        blob = f"{desc} {rid}"
        if any(k in blob.lower() for k in ("you sent", "outgoing", "from_me", "sent_by_me", "self")):
            side = "you"
        if text.lower().startswith("you:"):
            side = "you"
        msgs.append((side, text))
    cleaned: list[tuple[str, str]] = []
    for side, body in msgs:
        if cleaned and cleaned[-1] == (side, body):
            continue
        cleaned.append((side, body))
    return cleaned
