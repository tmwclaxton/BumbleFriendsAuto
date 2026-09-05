"""Classify the current Bumble screen from UI hierarchy text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from xml.etree import ElementTree as ET


class ScreenKind(str, Enum):
    CARD = "card"
    MATCH = "match"
    LIKE_CONFIRM = "like_confirm"
    PAYWALL = "paywall"
    VERIFY = "verify"
    EMPTY = "empty"
    PERMISSION = "permission"
    CHATS = "chats"
    OTHER_TAB = "other_tab"
    LOADING = "loading"
    NOT_BUMBLE = "not_bumble"
    UNKNOWN = "unknown"


# Hard stops — do not swipe through. UNKNOWN/LOADING are retried in the loop.
STOP_KINDS = frozenset(
    {
        ScreenKind.PAYWALL,
        ScreenKind.VERIFY,
        ScreenKind.EMPTY,
        ScreenKind.PERMISSION,
        ScreenKind.NOT_BUMBLE,
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
    r"keep\s*swiping",
    r"say\s*hello",
    r"crossed\s*paths",
    r"you'?ve\s+crossed",
    r"great\s+news",
)

# First right-swipe confirm modal on Bumble Friends.
_LIKE_CONFIRM_PATTERNS = (
    r"\binterested\?\b",
    r"swiping\s+a\s+profile\s+to\s+the\s+right",
    r"means\s+that\s+you\s+want\s+to\s+connect",
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
    r"\bfilters\b",
    r"\bhe/him\b",
    r"\bshe/her\b",
)

_CARD_RIDS = (
    "profile_details_badgeSuperSwipe",
    "toolbar_filter",
)

_CHATS_RIDS = (
    "connectionItem",
    "connections_expiringConnectionsTitle",
    "connections_connectionsTitleValue",
    "connectionsItem_personName",
)

_NAV_LABELS = frozenset({"profile", "plans", "people", "liked you", "chats"})


@dataclass(frozen=True)
class ScreenState:
    kind: ScreenKind
    package: str
    texts: tuple[str, ...]
    reason: str = ""


def _collect_resource_ids(xml: str) -> frozenset[str]:
    ids: set[str] = set()
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        for match in re.finditer(r'resource-id="([^"]+)"', xml):
            rid = match.group(1).rsplit("/", 1)[-1]
            if rid:
                ids.add(rid)
        return frozenset(ids)
    for node in root.iter():
        rid = (node.attrib.get("resource-id") or "").rsplit("/", 1)[-1]
        if rid:
            ids.add(rid)
    return frozenset(ids)


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


def _is_sparse_dump(xml: str, texts: tuple[str, ...], rids: frozenset[str]) -> bool:
    """True when hierarchy is mostly chrome (status bar / nav) during a transition."""
    if "progressBar" in "".join(rids) or any("progress" in r.lower() for r in rids):
        return True
    if len(xml) < 4000:
        return True
    body = [
        t
        for t in texts
        if t.strip().lower() not in _NAV_LABELS
        and not re.fullmatch(r"\d{1,2}:\d{2}", t.strip())
        and "percent" not in t.lower()
        and "notification" not in t.lower()
        and "battery" not in t.lower()
        and "signal" not in t.lower()
        and "k/s" not in t.lower()
        and "b/s" not in t.lower()
    ]
    bumble_rids = [r for r in rids if not r.startswith("status") and r not in {"clock", "speed"}]
    return len(body) <= 2 and not any(r in rids for r in _CARD_RIDS) and len(bumble_rids) < 8


def classify(
    package: str,
    xml: str,
    expected_package: str = "com.bumblebff.app",
) -> ScreenState:
    """Best-effort screen classification. Uncertain → UNKNOWN (retry, don't swipe)."""
    texts = _collect_texts(xml)
    rids = _collect_resource_ids(xml)
    blob = _joined_lower(texts)

    if package and not _is_bumble_package(package, expected_package):
        # Transition dumps are often systemui-only while BFF is still focused.
        if _is_sparse_dump(xml, texts, rids):
            return ScreenState(ScreenKind.LOADING, package, texts, reason="sparse-systemui")
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
    if any(rid in rids for rid in _CHATS_RIDS) or (hit := _matches_any(blob, _CHATS_PATTERNS)):
        return ScreenState(
            ScreenKind.CHATS,
            package,
            texts,
            reason="chats-rid" if any(rid in rids for rid in _CHATS_RIDS) else hit,
        )

    if hit := _matches_any(blob, _EMPTY_PATTERNS):
        return ScreenState(ScreenKind.EMPTY, package, texts, reason=hit)

    if hit := _matches_any(blob, _MATCH_PATTERNS):
        return ScreenState(ScreenKind.MATCH, package, texts, reason=hit)

    if hit := _matches_any(blob, _LIKE_CONFIRM_PATTERNS):
        return ScreenState(ScreenKind.LIKE_CONFIRM, package, texts, reason=hit)

    if hit := _matches_any(blob, _PAYWALL_PATTERNS):
        return ScreenState(ScreenKind.PAYWALL, package, texts, reason=hit)

    card_rid_hits = [rid for rid in _CARD_RIDS if rid in rids]
    if card_rid_hits:
        return ScreenState(ScreenKind.CARD, package, texts, reason=",".join(card_rid_hits[:2]))

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
    body = [t for t in texts if t.strip().lower() not in _NAV_LABELS]
    body_blob = _joined_lower(tuple(body))
    if re.search(r"people\s+like\s+you|see\s+who\s+likes\s+you", body_blob):
        return ScreenState(ScreenKind.OTHER_TAB, package, texts, reason="liked-you-body")
    if re.search(r"\bedit\s+profile\b|\bmy\s+profile\b", body_blob):
        return ScreenState(ScreenKind.OTHER_TAB, package, texts, reason="profile-body")
    if re.search(r"get\s+a\s+group\s+together|\bstart\s+a\s+plan\b", body_blob) and not card_hits:
        # Plans promo can also appear on chats; chats already handled above.
        if re.search(r"\bplans\b", body_blob) and "new friends" not in body_blob:
            return ScreenState(ScreenKind.OTHER_TAB, package, texts, reason="plans-body")

    # Match overlay is often just Close + a photo, no "You're friends" in the dump.
    if re.search(r"\b(close|dismiss|keep swiping)\b", blob) and "filters" not in blob:
        return ScreenState(ScreenKind.MATCH, package, texts, reason="close-overlay")

    if _is_sparse_dump(xml, texts, rids):
        return ScreenState(ScreenKind.LOADING, package, texts, reason="sparse-hierarchy")

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


def find_like_confirm_yes(xml: str, screen_size: tuple[int, int]) -> tuple[int, int] | None:
    """Tap point for the first-like 'Interested?' YES button."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    width, height = screen_size
    yes_re = re.compile(r"^(yes|ok|continue|confirm)$", re.I)
    hits: list[tuple[int, int]] = []
    for node in root.iter():
        label = ((node.attrib.get("text") or "") or (node.attrib.get("content-desc") or "")).strip()
        if not label or not yes_re.match(label):
            continue
        center = _bounds_center(node.attrib.get("bounds") or "")
        if center:
            hits.append(center)
    if hits:
        # Prefer lower / right-hand YES.
        hits.sort(key=lambda p: (p[1], p[0]), reverse=True)
        return hits[0]
    return (int(width * 0.72), int(height * 0.58))
