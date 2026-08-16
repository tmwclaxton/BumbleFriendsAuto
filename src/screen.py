"""Classify the current Bumble screen from UI hierarchy text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from xml.etree import ElementTree as ET


class ScreenKind(str, Enum):
    CARD = "card"
    MATCH = "match"
    PAYWALL = "paywall"
    VERIFY = "verify"
    EMPTY = "empty"
    PERMISSION = "permission"
    CHATS = "chats"
    OTHER_TAB = "other_tab"
    NOT_BUMBLE = "not_bumble"
    UNKNOWN = "unknown"


# Stop immediately on these (do not swipe through).
STOP_KINDS = frozenset(
    {
        ScreenKind.PAYWALL,
        ScreenKind.VERIFY,
        ScreenKind.EMPTY,
        ScreenKind.PERMISSION,
        ScreenKind.NOT_BUMBLE,
        ScreenKind.UNKNOWN,
    }
)

# Prefer longer / more specific phrases first when scanning.
_VERIFY_PATTERNS = (
    r"verify\s*(your\s*)?(photo|identity|profile)",
    r"photo\s*verification",
    r"take\s*a\s*(selfie|photo)",
    r"confirm\s*(it'?s|its)\s*you",
    r"liveness",
)

_PAYWALL_PATTERNS = (
    r"out\s*of\s*(likes|swipes)",
    r"no\s*likes?\s*left",
    r"you'?ve\s*run\s*out",
    r"get\s*(bumble\s*)?premium",
    r"upgrade\s*to\s*(premium|boost)",
    r"continue\s*with\s*premium",
    r"subscribe\s*now",
)

_MATCH_PATTERNS = (
    r"you'?re\s*friends",
    r"it'?s\s*a\s*match",
    r"you\s*(just\s*)?matched",
    r"both\s*have\s*\d+\s*hours",
    r"start\s*chatting",
    r"send\s*a\s*message\.\.\.",
)

_EMPTY_PATTERNS = (
    r"seen\s+everyone\s+nearby",
    r"adjust\s*(your\s*)?filters",
    r"no\s*(more\s*)?(people|profiles|friends)\s*(nearby|left|around)?",
    r"come\s*back\s*later",
    r"check\s*back\s*(soon|later)",
    r"expand\s*your\s*(distance|filters)",
    r"we'?re\s*out\s*of\s*people",
    r"outside\s*your\s*filters",
)

_PERMISSION_PATTERNS = (
    r"allow\s+.+\s+to\s+(access|take|record)",
    r"while\s*using\s*the\s*app",
    r"only\s*this\s*time",
    r"don'?t\s*allow",
)

_CHATS_PATTERNS = (
    r"new\s*friends",
    r"conversation expires",
    r"get\s*a\s*group\s*together",
    r"conversations?\s*are\s*filtered",
)

_OTHER_TAB_PATTERNS = (
    r"liked\s*you",
    r"people\s*like\s*you",
    r"\bplans\b",
    r"edit\s*profile",
)

# Stronger signals for a swipeable discovery card.
_CARD_HINTS = (
    r"send\s+superswipe",
    r"send\s+a\s+compliment",
    r"main\s+photo",
    r"\babout\s+me\b",
    r"\bmy\s+interests\b",
    r"\blooking\s+for\b",
    r"\bprompts?\b",
    r"\byears?\s*old\b",
    r",\s*\d{2}\b",  # "Name, 23"
    r"\bmi\s+away\b",
    r"\bkm\s+away\b",
    r"\bpass\b",
    r"\bsuper\s*like\b",
    r"\bverified\b",
)


@dataclass(frozen=True)
class ScreenState:
    kind: ScreenKind
    package: str
    texts: tuple[str, ...]
    reason: str = ""


def _collect_texts(xml: str) -> tuple[str, ...]:
    texts: list[str] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        for match in re.finditer(r'(?:text|content-desc)="([^"]*)"', xml):
            value = match.group(1).strip()
            if value:
                texts.append(value)
        return tuple(texts)

    for node in root.iter():
        for attr in ("text", "content-desc"):
            value = (node.attrib.get(attr) or "").strip()
            if value:
                texts.append(value)
    return tuple(texts)


def _joined_lower(texts: tuple[str, ...]) -> str:
    return " ".join(texts).lower()


def _matches_any(blob: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        if re.search(pattern, blob, flags=re.IGNORECASE):
            return pattern
    return None


def _is_bumble_package(package: str, expected_package: str) -> bool:
    if not package:
        return True
    if expected_package and expected_package in package:
        return True
    return package.startswith("com.bumble")


def _bounds_center(bounds: str) -> tuple[int, int] | None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def classify(
    package: str,
    xml: str,
    expected_package: str = "com.bumblebff.app",
) -> ScreenState:
    """Best-effort screen classification. Uncertain → UNKNOWN (stop)."""
    texts = _collect_texts(xml)
    blob = _joined_lower(texts)

    if package and not _is_bumble_package(package, expected_package):
        return ScreenState(
            kind=ScreenKind.NOT_BUMBLE,
            package=package,
            texts=texts,
            reason=f"foreground package is {package!r}",
        )

    if hit := _matches_any(blob, _PERMISSION_PATTERNS):
        return ScreenState(ScreenKind.PERMISSION, package, texts, reason=hit)

    if hit := _matches_any(blob, _VERIFY_PATTERNS):
        return ScreenState(ScreenKind.VERIFY, package, texts, reason=hit)

    # Chats / other tabs before paywall/match so list UI isn't misclassified.
    if hit := _matches_any(blob, _CHATS_PATTERNS):
        return ScreenState(ScreenKind.CHATS, package, texts, reason=hit)

    if hit := _matches_any(blob, _EMPTY_PATTERNS):
        return ScreenState(ScreenKind.EMPTY, package, texts, reason=hit)

    if hit := _matches_any(blob, _MATCH_PATTERNS):
        return ScreenState(ScreenKind.MATCH, package, texts, reason=hit)

    if hit := _matches_any(blob, _PAYWALL_PATTERNS):
        return ScreenState(ScreenKind.PAYWALL, package, texts, reason=hit)

    card_hits = [p for p in _CARD_HINTS if re.search(p, blob, flags=re.IGNORECASE)]
    if card_hits:
        return ScreenState(
            kind=ScreenKind.CARD,
            package=package,
            texts=texts,
            reason=",".join(card_hits[:3]),
        )

    # On People tab with like/pass controls — treat as card.
    has_people_nav = bool(re.search(r"\bpeople\b", blob))
    has_pass_or_like = bool(re.search(r"\b(like|pass|nope)\b", blob))
    if has_people_nav and has_pass_or_like:
        return ScreenState(ScreenKind.CARD, package, texts, reason="nav+like/pass")

    # Avoid treating bottom-nav labels ("Liked You") as the Liked You screen.
    # Require body copy beyond the 5 nav labels.
    nav_only = {"profile", "plans", "people", "liked you", "chats"}
    body = [t for t in texts if t.strip().lower() not in nav_only]
    body_blob = _joined_lower(tuple(body))
    if re.search(r"people\s+like\s+you|see\s+who\s+likes\s+you", body_blob):
        return ScreenState(ScreenKind.OTHER_TAB, package, texts, reason="liked-you-body")
    if re.search(r"\bedit\s+profile\b|\bmy\s+profile\b", body_blob):
        return ScreenState(ScreenKind.OTHER_TAB, package, texts, reason="profile-body")
    if re.search(r"get\s+a\s+group\s+together|\bstart\s+a\s+plan\b", body_blob) and not card_hits:
        # Plans promo can also appear on chats; chats already handled above.
        if re.search(r"\bplans\b", body_blob) and "new friends" not in body_blob:
            return ScreenState(ScreenKind.OTHER_TAB, package, texts, reason="plans-body")

    return ScreenState(
        kind=ScreenKind.UNKNOWN,
        package=package,
        texts=texts,
        reason="no confident match",
    )


def find_tab_point(xml: str, tab_name: str) -> tuple[int, int] | None:
    """Find center of a bottom-nav tab by exact text/content-desc (e.g. People)."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None

    target = tab_name.strip().lower()
    candidates: list[tuple[int, int]] = []

    for node in root.iter():
        text = (node.attrib.get("text") or "").strip().lower()
        desc = (node.attrib.get("content-desc") or "").strip().lower()
        # Exact label only — avoids "3 people like you".
        if text != target and desc != target:
            continue
        bounds = node.attrib.get("bounds")
        if not bounds:
            continue
        center = _bounds_center(bounds)
        if center is None:
            continue
        candidates.append((center[1], center[0]))  # y, x

    if not candidates:
        return None
    candidates.sort(reverse=True)  # prefer bottom nav (largest y)
    y, x = candidates[0]
    return (x, y)


def find_dismiss_point(xml: str, screen_size: tuple[int, int]) -> tuple[int, int] | None:
    """
    Find a tap point to dismiss a match overlay.
    Prefers Close / keep swiping; falls back to upper-left (BFF match X).
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None

    dismiss_re = re.compile(
        r"^(close|dismiss|not\s*now|maybe\s*later|keep\s*swiping|skip|×|x)$",
        re.IGNORECASE,
    )
    width, height = screen_size
    preferred: list[tuple[int, int]] = []

    for node in root.iter():
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        label = text or desc
        if not label or not dismiss_re.match(label):
            continue
        bounds = node.attrib.get("bounds")
        if not bounds:
            continue
        center = _bounds_center(bounds)
        if center:
            preferred.append(center)

    if preferred:
        # Prefer the topmost close control.
        preferred.sort(key=lambda p: p[1])
        return preferred[0]

    # BFF match screen puts X top-left.
    return (int(width * 0.08), int(height * 0.08))
