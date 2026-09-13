"""Parse Instagram Android feed dumps (Following tab, post age, like button)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from src.screen import _bounds_center

_AGE_UNIT = re.compile(
    r"(?P<n>\d+(?:\.\d+)?)\s*"
    r"(?P<u>s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks|"
    r"mo|mos|month|months|y|yr|yrs|year|years)\b",
    re.I,
)
_JUST_NOW = re.compile(r"\b(just now|now|a moment ago)\b", re.I)
_YESTERDAY = re.compile(r"\byesterday\b", re.I)
_SPONSORED = re.compile(r"\bsponsored\b", re.I)
_WEEK_CAP_HOURS = 7 * 24

_CHROME_HANDLES = frozenset(
    {
        "home",
        "reels",
        "search",
        "shop",
        "profile",
        "following",
        "for you",
        "explore",
        "create",
        "instagram",
        "like",
        "unlike",
        "comment",
        "share",
        "send",
    }
)

_HANDLE_OK = re.compile(r"^[A-Za-z0-9._]{2,30}$")
_HEADER_POSTED = re.compile(
    r"^(?P<handle>[A-Za-z0-9._]+)\s+posted a (?:photo|video|reel|carousel|post)\s+(?P<age>.+)$",
    re.I,
)
_REEL_BY = re.compile(r"(?:reel by|profile picture of)\s+([A-Za-z0-9._]+)", re.I)


@dataclass
class FeedPost:
    handle: str
    age_label: str
    age_hours: float | None
    like_xy: tuple[int, int] | None
    already_liked: bool
    sponsored: bool
    recent: bool

    @property
    def fingerprint(self) -> str:
        return f"{self.handle}|{self.age_label}"


def _bounds_box(bounds: str) -> tuple[int, int, int, int] | None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not m:
        return None
    return tuple(int(p) for p in m.groups())  # type: ignore[return-value]


def _screen_size(xml: str) -> tuple[int, int]:
    width, height = 1080, 2400
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return width, height
    for node in root.iter():
        box = _bounds_box(node.attrib.get("bounds") or "")
        if not box:
            continue
        width = max(width, box[2])
        height = max(height, box[3])
    return width, height


def parse_age_hours(label: str) -> float | None:
    blob = (label or "").strip()
    if not blob:
        return None
    if _SPONSORED.search(blob):
        return None
    if _JUST_NOW.search(blob):
        return 0.0
    if _YESTERDAY.search(blob):
        return 24.0
    match = _AGE_UNIT.search(blob)
    if not match:
        return None
    n = float(match.group("n"))
    unit = match.group("u").lower()
    if unit.startswith("s") and not unit.startswith("sec"):
        if unit in {"s"}:
            return n / 3600.0
    if unit.startswith("sec"):
        return n / 3600.0
    if unit in {"s"}:
        return n / 3600.0
    if unit.startswith("m") and not unit.startswith("mo") and unit not in {"min", "mins", "minute", "minutes", "m"}:
        if unit.startswith("mo"):
            return n * 30 * 24
    if unit in {"m", "min", "mins", "minute", "minutes"}:
        return n / 60.0
    if unit.startswith("h") or unit in {"hr", "hrs"}:
        return n
    if unit.startswith("d"):
        return n * 24
    if unit.startswith("w"):
        return n * 7 * 24
    if unit.startswith("mo"):
        return n * 30 * 24
    if unit.startswith("y"):
        return n * 365 * 24
    return None


def is_recent(age_hours: float | None) -> bool:
    return age_hours is not None and 0 <= age_hours < _WEEK_CAP_HOURS


def looks_like_following_feed(xml: str) -> bool:
    if _action_bar_following(xml):
        return True
    blob = " ".join(_texts(xml)).lower()
    if "for you" in blob and "following" in blob:
        return _following_selected(xml)
    return "following" in blob and ("like" in blob or "row_feed" in xml)


def _action_bar_following(xml: str) -> bool:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False
    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        text = (node.attrib.get("text") or "").strip().casefold()
        desc = (node.attrib.get("content-desc") or "").strip().casefold()
        if rid.endswith("action_bar_title") and "following" in f"{text} {desc}":
            return True
    return False


def _following_selected(xml: str) -> bool:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False
    for node in root.iter():
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        selected = (node.attrib.get("selected") or "").lower() == "true"
        low = f"{text} {desc}".lower()
        if "following" in low and (
            selected or "selected" in low or "chosen" in low
        ):
            return True
    return False


def _texts(xml: str) -> list[str]:
    out: list[str] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for node in root.iter():
        for key in ("text", "content-desc"):
            val = (node.attrib.get(key) or "").strip()
            if val:
                out.append(val)
    return out


def find_label_point(xml: str, *needles: str) -> tuple[int, int] | None:
    want = [n.casefold() for n in needles]
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    hits: list[tuple[int, int, int]] = []
    for node in root.iter():
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        low = f"{text} {desc}".casefold()
        if not any(
            n == text.casefold()
            or n == desc.casefold()
            or desc.casefold().startswith(n + " ")
            or text.casefold().startswith(n + " ")
            for n in want
        ):
            continue
        center = _bounds_center(node.attrib.get("bounds") or "")
        if center:
            hits.append((center[1], center[0], center[1]))
    if not hits:
        return None
    _, x, y = min(hits, key=lambda t: t[0])
    return x, y


def find_back_button(xml: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        desc = (node.attrib.get("content-desc") or "").strip().lower()
        if rid.endswith("action_bar_button_back") or desc in {"back", "navigate up"}:
            center = _bounds_center(node.attrib.get("bounds") or "")
            if center:
                return center
    return find_label_point(xml, "Back", "Navigate up")


def looks_like_home_chrome(xml: str) -> bool:
    return find_home_tab(xml) is not None or looks_like_following_feed(xml)


def looks_like_nested_viewer(xml: str) -> bool:
    blob = " ".join(_texts(xml)).lower()
    return any(mark in blob for mark in ("trending", "use audio", "save audio", "use on edits"))


def find_home_tab(xml: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        desc = (node.attrib.get("content-desc") or "").strip().lower()
        if rid.endswith("feed_tab") or desc == "home":
            center = _bounds_center(node.attrib.get("bounds") or "")
            if center and center[1] >= 1600:
                return center
    point = find_label_point(xml, "Home")
    if point and point[1] >= 1600:
        return point
    return None


def find_feed_mode_button(xml: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for suffix in ("action_bar_title_view", "title_logo", "action_bar_title"):
        for node in root.iter():
            rid = node.attrib.get("resource-id") or ""
            if rid.endswith(suffix):
                center = _bounds_center(node.attrib.get("bounds") or "")
                if center:
                    return center
    return None


def find_following_chip(xml: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    hits: list[tuple[int, int, int]] = []
    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        low = f"{text} {desc}".casefold()
        menu = rid.endswith("context_menu_item") or rid.endswith("context_menu_item_label")
        if text.casefold() != "following" and desc.casefold() not in {"following", "following tab"}:
            if not (menu and "following" in low):
                continue
        box = _bounds_box(node.attrib.get("bounds") or "")
        if not box:
            continue
        if box[1] > 700 and not menu:
            continue
        center = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        hits.append((center[1], center[0], center[1]))
    if not hits:
        return None
    _, x, y = min(hits, key=lambda t: t[0])
    return x, y


def parse_visible_post(xml: str) -> FeedPost | None:
    """The post whose like button is nearest the mid-screen action row."""
    posts = parse_feed_posts(xml)
    if not posts:
        return None
    _, height = _screen_size(xml)
    target = int(height * 0.58)

    def score(post: FeedPost) -> int:
        if not post.like_xy:
            return 10_000
        return abs(post.like_xy[1] - target)

    return min(posts, key=score)


def parse_unseen_post(xml: str, seen: set[str]) -> FeedPost | None:
    """Topmost Following post that this session has not reviewed yet."""
    posts = parse_feed_posts(xml)
    posts.sort(key=lambda post: (post.like_xy or (0, 0))[1])
    for post in posts:
        if post.fingerprint not in seen:
            return post
    return None


def parse_feed_posts(xml: str) -> list[FeedPost]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    names: list[tuple[int, str]] = []
    times: list[tuple[int, str]] = []
    likes: list[tuple[int, tuple[int, int], bool]] = []
    sponsored_ys: list[int] = []
    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        box = _bounds_box(node.attrib.get("bounds") or "")
        if not box:
            continue
        y = (box[1] + box[3]) // 2
        blob = f"{text} {desc}"
        if _SPONSORED.search(blob):
            sponsored_ys.append(y)
        if rid.endswith("row_feed_profile_header"):
            parsed = _HEADER_POSTED.match(desc)
            if parsed:
                handle = _norm_handle(parsed.group("handle"))
                if handle:
                    names.append((y, handle))
                age = parsed.group("age").strip()
                if age:
                    times.append((y, age))
            continue
        if rid.endswith("row_feed_photo_profile_name") or rid.endswith("feed_post_header_author") or rid.endswith(
            "clips_author_username"
        ):
            handle = _norm_handle((text or desc).split()[0] if (text or desc) else "")
            if handle:
                names.append((y, handle))
            continue
        reel = _REEL_BY.search(desc)
        if reel:
            handle = _norm_handle(reel.group(1))
            if handle:
                names.append((y, handle))
        if _SPONSORED.search(f"{text} {desc}"):
            sponsored_ys.append(y)
        if rid.endswith("row_feed_photo_imageview") or "posted a" in desc.lower() or "ago" in desc.lower():
            img_age = _age_from_blob(desc)
            if img_age:
                times.append((y, img_age))
        age_src = desc if _AGE_UNIT.search(desc) or _JUST_NOW.search(desc) or _YESTERDAY.search(desc) else text
        if age_src and (
            _AGE_UNIT.search(age_src) or _JUST_NOW.search(age_src) or _YESTERDAY.search(age_src)
        ):
            if len(age_src) < 48:
                times.append((y, age_src))
        like_hit = _like_button(rid, text, desc, box)
        if like_hit:
            likes.append(like_hit)
    posts: list[FeedPost] = []
    seen: set[str] = set()
    for like_y, like_xy, already in likes:
        handle = _nearest_name(names, like_y, max_gap=2200) or ""
        age_label = _nearest_above(times, like_y, max_gap=1600) or _nearest_name(times, like_y, max_gap=2200) or ""
        if not handle:
            continue
        key = f"{handle}|{age_label}|{like_y}"
        if key in seen:
            continue
        seen.add(key)
        age_hours = parse_age_hours(age_label)
        sponsored = any(abs(sy - like_y) < 1600 for sy in sponsored_ys)
        posts.append(
            FeedPost(
                handle=handle,
                age_label=age_label,
                age_hours=age_hours,
                like_xy=like_xy,
                already_liked=already,
                sponsored=sponsored,
                recent=is_recent(age_hours) and not sponsored,
            )
        )
    return posts


def _like_button(
    rid: str, text: str, desc: str, box: tuple[int, int, int, int]
) -> tuple[int, tuple[int, int], bool] | None:
    low = f"{text} {desc}".lower()
    is_like = rid.endswith("row_feed_button_like") or low in {"like", "unlike", "liked"}
    if not is_like and "like" not in low:
        return None
    if any(w in low for w in ("likes", "liked by", "number of")):
        return None
    if "like" not in low and "unlike" not in low:
        return None
    already = "unlike" in low or low.strip() == "liked" or "liked" == desc.lower()
    if "like" not in low and not already:
        return None
    if rid and not rid.endswith("row_feed_button_like") and low not in {"like", "unlike", "liked"}:
        if "button" not in rid and "like" not in rid:
            return None
    center = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
    return center[1], center, already


def _nearest_name(rows: list[tuple[int, str]], y: int, *, max_gap: int) -> str | None:
    """Reel likes sit above the username; photo likes sit below the header."""
    above = _nearest_above(rows, y, max_gap=max_gap)
    if above:
        return above
    best: tuple[int, str] | None = None
    for row_y, value in rows:
        gap = abs(row_y - y)
        if gap > max_gap:
            continue
        if best is None or gap < best[0]:
            best = (gap, value)
    return best[1] if best else None


def _nearest_above(rows: list[tuple[int, str]], y: int, *, max_gap: int) -> str | None:
    best: tuple[int, str] | None = None
    for row_y, value in rows:
        if row_y > y:
            continue
        gap = y - row_y
        if gap > max_gap:
            continue
        if best is None or gap < best[0]:
            best = (gap, value)
    return best[1] if best else None


def _age_from_blob(blob: str) -> str:
    if not blob:
        return ""
    parsed = _HEADER_POSTED.search(blob)
    if parsed:
        return parsed.group("age").strip()
    match = _AGE_UNIT.search(blob)
    if match and ("ago" in blob.lower() or _JUST_NOW.search(blob) or _YESTERDAY.search(blob)):
        return blob[match.start() :].split(",")[0].strip()
    if _JUST_NOW.search(blob):
        return "just now"
    if _YESTERDAY.search(blob):
        return "yesterday"
    return ""


def _looks_like_age(raw: str) -> bool:
    blob = (raw or "").strip()
    if not blob:
        return False
    if _JUST_NOW.search(blob) or _YESTERDAY.search(blob):
        return True
    return bool(_AGE_UNIT.fullmatch(blob) or _AGE_UNIT.search(blob) and len(blob) < 12)


def _norm_handle(raw: str) -> str:
    handle = (raw or "").strip().lstrip("@").split()[0] if raw else ""
    handle = handle.strip("·•|.")
    if not _HANDLE_OK.match(handle):
        return ""
    if handle.lower() in _CHROME_HANDLES:
        return ""
    if _looks_like_age(handle):
        return ""
    if handle.isdigit() or handle.lower() in {"ad", "ads", "reel", "reels", "photo"}:
        return ""
    if not re.search(r"[A-Za-z]", handle):
        return ""
    return handle
