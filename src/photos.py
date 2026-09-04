"""Grab and serve wee profile thumbnails for the inbox sidebar."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from src.config import ROOT, load_config
from src.device import dump_hierarchy, take_screenshot, wait_idle
from src.gestures import tap

log = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")
_RING_SUFFIXES = ("connectionItem_ringView", "connectionsItem_ringView")


def avatars_dir() -> Path:
    cfg = load_config()
    raw = cfg.get("db_path") or str(ROOT / "data" / "friends.db")
    db = Path(raw)
    if not db.is_absolute():
        db = ROOT / db
    path = db.parent / "avatars"
    path.mkdir(parents=True, exist_ok=True)
    return path


def photo_slug(name: str) -> str:
    slug = _SLUG_RE.sub("-", (name or "").strip()).strip("-").lower()
    return slug or "unknown"


def photo_file(name: str) -> Path:
    return avatars_dir() / f"{photo_slug(name)}.jpg"


def photo_exists(name: str) -> bool:
    path = photo_file(name)
    return path.is_file() and path.stat().st_size > 80


def _parse_bounds(bounds: str) -> tuple[int, int, int, int] | None:
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not match:
        return None
    x1, y1, x2, y2 = map(int, match.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _center(box: tuple[int, int, int, int]) -> tuple[int, int]:
    x1, y1, x2, y2 = box
    return (x1 + x2) // 2, (y1 + y2) // 2


def _save_square(img, box: tuple[int, int, int, int], dest: Path, *, inset: float = 0.08) -> bool:
    from PIL import Image

    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    pad_x = int(w * inset)
    pad_y = int(h * inset)
    crop = img.crop((x1 + pad_x, y1 + pad_y, x2 - pad_x, y2 - pad_y))
    if crop.width < 12 or crop.height < 12:
        return False
    side = min(crop.width, crop.height)
    left = (crop.width - side) // 2
    top = (crop.height - side) // 2
    crop = crop.crop((left, top, left + side, top + side))
    crop = crop.convert("RGB").resize((160, 160), Image.Resampling.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    crop.save(dest, "JPEG", quality=82)
    return dest.is_file() and dest.stat().st_size > 80


def _screenshot_pil(device):
    try:
        img = device.screenshot()
        if img is not None:
            return img
    except Exception:
        log.debug("device.screenshot() failed", exc_info=True)
    tmp = avatars_dir() / "_screen.png"
    take_screenshot(device, tmp)
    from PIL import Image

    return Image.open(tmp)


def list_avatar_boxes(xml: str) -> list[tuple[str, tuple[int, int, int, int]]]:
    """Name + avatar bounds from the Chats list (and New-friends rings)."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    found: list[tuple[str, tuple[int, int, int, int]]] = []
    seen: set[str] = set()

    for item in root.iter():
        rid = item.attrib.get("resource-id") or ""
        if not rid.endswith(("connectionItem", "connectionsItem")):
            continue
        name = ""
        ring = None
        for node in item.iter():
            child = node.attrib.get("resource-id") or ""
            if child.endswith("personName"):
                name = (node.attrib.get("text") or "").strip()
            elif any(child.endswith(suf) for suf in _RING_SUFFIXES):
                ring = _parse_bounds(node.attrib.get("bounds") or "")
        if not name or ring is None:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append((name, ring))

    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        if not any(rid.endswith(suf) for suf in _RING_SUFFIXES):
            continue
        desc = (node.attrib.get("content-desc") or "").strip()
        match = re.match(r"^(.+?),\s*BFF,\s*(?:expired\s+)?match$", desc, re.I)
        if not match:
            continue
        name = match.group(1).strip()
        box = _parse_bounds(node.attrib.get("bounds") or "")
        if not name or box is None:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append((name, box))
    return found


def grab_visible_list_avatars(device, xml: str | None = None) -> int:
    """Crop chat-list faces from one screenshot. Cheap — call while scrolling."""
    xml = xml if xml is not None else dump_hierarchy(device)
    boxes = [(name, box) for name, box in list_avatar_boxes(xml) if not photo_exists(name)]
    if not boxes:
        return 0
    try:
        img = _screenshot_pil(device)
    except Exception:
        log.warning("list avatar screenshot failed")
        return 0
    saved = 0
    for name, box in boxes:
        try:
            if _save_square(img, box, photo_file(name), inset=0.12):
                saved += 1
        except Exception:
            log.debug("crop failed for %s", name, exc_info=True)
    if saved:
        log.info("saved %d list avatar(s)", saved)
    return saved


def _looks_like_profile(xml: str) -> bool:
    blob = xml.lower()
    return (
        "unmatch and report" in blob
        or "block and report" in blob
        or "my location" in blob
        or "profile_details" in blob
    )


def _toolbar_profile_tap(xml: str, name: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    want = (name or "").strip().casefold()
    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        text = (node.attrib.get("text") or "").strip()
        if rid == "com.bumblebff.app:id/chatToolbar_title" and text:
            box = _parse_bounds(node.attrib.get("bounds") or "")
            if box:
                return _center(box)
        if want and text.casefold() == want:
            box = _parse_bounds(node.attrib.get("bounds") or "")
            if box and box[1] < 280:
                return _center(box)
    return None


def _hero_box(width: int, height: int) -> tuple[int, int, int, int]:
    top = int(height * 0.07)
    side = min(width, int(height * 0.52))
    left = max(0, (width - side) // 2)
    return left, top, left + side, top + side


def capture_open_profile_photo(device, name: str) -> bool:
    """Crop the first profile photo from an already-open profile screen."""
    try:
        img = _screenshot_pil(device)
        width, height = img.size
        if _save_square(img, _hero_box(width, height), photo_file(name), inset=0.02):
            log.info("saved profile photo for %s", name)
            return True
    except Exception:
        log.warning("profile photo crop failed for %s", name)
    return False


def capture_profile_photo(device, name: str) -> bool:
    """From an open chat, tap into the profile and save the first photo."""
    xml = dump_hierarchy(device)
    if _looks_like_profile(xml):
        return capture_open_profile_photo(device, name)
    point = _toolbar_profile_tap(xml, name)
    if point is None:
        info = device.info or {}
        point = (int(info.get("displayWidth") or 1080) // 2, int((info.get("displayHeight") or 2400) * 0.055))
    tap(device, point[0], point[1])
    wait_idle(device, 1.4)
    xml = dump_hierarchy(device)
    if not _looks_like_profile(xml):
        log.debug("profile did not open for %s", name)
        return False
    ok = capture_open_profile_photo(device, name)
    device.press("back")
    wait_idle(device, 0.9)
    return ok
