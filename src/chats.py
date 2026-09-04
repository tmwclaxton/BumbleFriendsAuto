"""New-friends chat helpers (Chats tab circles)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)

_MATCH_DESC_RE = re.compile(r"^(.+?),\s*BFF,\s*(?:expired\s+)?match$", re.IGNORECASE)
_EMPTY_CHAT_RE = re.compile(
    r"(hours?\s+left\s+to\s+message|start\s+the\s+chat|say\s+hello|send\s+a\s+message)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NewFriend:
    name: str
    x: int
    y: int
    bounds: str


def _bounds_center(bounds: str) -> tuple[int, int] | None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def _name_from_match_desc(desc: str) -> str | None:
    m = _MATCH_DESC_RE.match(desc.strip())
    if not m:
        return None
    # First token of display name ("Lauren Mizaela" → keep full first+last for greeting,
    # but template uses first name only).
    return m.group(1).strip()


def first_name(full_name: str) -> str:
    return full_name.strip().split()[0] if full_name.strip() else full_name


def list_new_friends(xml: str) -> list[NewFriend]:
    """
    Circles under the Chats → New friends row.

    These are clickable connectionItem_ringView nodes with desc like
    "Abhinav, BFF, match". Chat-list avatars use the same id but are not clickable.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []

    friends: list[NewFriend] = []
    seen: set[str] = set()

    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        if rid != "com.bumblebff.app:id/connectionItem_ringView":
            continue
        if (node.attrib.get("clickable") or "").lower() != "true":
            continue
        desc = (node.attrib.get("content-desc") or "").strip()
        name = _name_from_match_desc(desc)
        if not name:
            continue
        # Skip the "people like you" beeline stack.
        if "like you" in desc.lower():
            continue
        bounds = node.attrib.get("bounds") or ""
        center = _bounds_center(bounds)
        if center is None:
            continue
        key = bounds or f"{name.lower()}@{center[0]}"
        if key in seen:
            continue
        seen.add(key)
        friends.append(NewFriend(name=name, x=center[0], y=center[1], bounds=bounds))

    # Left-to-right as shown in the row.
    friends.sort(key=lambda f: f.x)
    return friends


def chat_partner_name(xml: str) -> str | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        if rid == "com.bumblebff.app:id/chatToolbar_title":
            text = (node.attrib.get("text") or "").strip()
            if text:
                return text
    return None


def is_empty_outbound_chat(xml: str) -> bool:
    """
    True if this looks like a New-friends chat with no messages yet
    (they haven't written, and neither have we).
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False

    has_composer = False
    has_empty_hint = False
    has_message_bubble = False
    blob_parts: list[str] = []

    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        if text:
            blob_parts.append(text)
        if desc:
            blob_parts.append(desc)

        if rid == "com.bumblebff.app:id/chatInput_text":
            has_composer = True

        if rid == "com.bumblebff.app:id/chatEmpty_v2_user_expiry_timer_or_display_message":
            has_empty_hint = True
        elif text and _EMPTY_CHAT_RE.search(text):
            has_empty_hint = True

        # Real chat bubbles only — ignore icebreaker / input chrome.
        if rid.startswith("com.bumblebff.app:id/chatMessage") or "ChatMessage" in rid:
            if text:
                has_message_bubble = True
        if desc.lower() in {"incoming message", "outgoing message"}:
            has_message_bubble = True

    blob = " ".join(blob_parts).lower()
    if re.search(r"\d+\s*hours?\s+left\s+to\s+message", blob):
        has_empty_hint = True

    if has_message_bubble:
        return False
    return has_composer and has_empty_hint


def dismiss_icebreaker_if_present(xml: str) -> tuple[int, int] | None:
    """Tap Continue on Bumble's 'break the ice' coach mark if shown."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        text = (node.attrib.get("text") or "").strip().lower()
        if text != "continue":
            continue
        if (node.attrib.get("clickable") or "").lower() != "true":
            continue
        bounds = node.attrib.get("bounds") or ""
        return _bounds_center(bounds)
    return None


def format_opener(template: str, full_name: str) -> str:
    name = first_name(full_name)
    try:
        return template.format(name=name)
    except (KeyError, ValueError):
        return template.replace("{name}", name)


def find_send_button(xml: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        desc = (node.attrib.get("content-desc") or "").strip().lower()
        if rid == "com.bumblebff.app:id/chatInput_button_send" or desc == "send message":
            bounds = node.attrib.get("bounds") or ""
            return _bounds_center(bounds)
    return None


def find_back_button(xml: str) -> tuple[int, int] | None:
    """Prefer the top-left chat toolbar Back (ignore stray lower matches)."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    candidates: list[tuple[int, int, int]] = []  # y, x, prefer_score
    for node in root.iter():
        desc = (node.attrib.get("content-desc") or "").strip().lower()
        if desc != "back":
            continue
        if (node.attrib.get("clickable") or "").lower() != "true":
            continue
        bounds = node.attrib.get("bounds") or ""
        center = _bounds_center(bounds)
        if center is None:
            continue
        x, y = center
        # Chat toolbar back is top-left; ignore overflow / call buttons.
        if y > 500 or x > 280:
            continue
        cls = node.attrib.get("class") or ""
        score = 0 if "ImageButton" in cls else 1
        candidates.append((score, y, x))
    if not candidates:
        return None
    candidates.sort()
    _, y, x = candidates[0]
    return (x, y)
