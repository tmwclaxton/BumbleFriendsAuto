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


def photo_file(name: str, phone_id: str | None = None) -> Path:
    from src.phones import DEFAULT_PHONE_ID, current_phone_id, normalize_phone_id

    pid = normalize_phone_id(phone_id) if phone_id else current_phone_id()
    if pid == "all":
        pid = DEFAULT_PHONE_ID
    nested = avatars_dir() / pid / f"{photo_slug(name)}.jpg"
    nested.parent.mkdir(parents=True, exist_ok=True)
    if nested.is_file() and nested.stat().st_size > 80:
        return nested
    flat = avatars_dir() / f"{photo_slug(name)}.jpg"
    if pid == DEFAULT_PHONE_ID and flat.is_file() and flat.stat().st_size > 80:
        return flat
    return nested


def photo_exists(name: str, phone_id: str | None = None) -> bool:
    path = photo_file(name, phone_id)
    return path.is_file() and path.stat().st_size > 80


def _base_name(name: str) -> str:
    name = (name or "").strip()
    parts = name.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return name


def namesake_photo_names(name: str, conn=None) -> list[str]:
    """Stored avatar slots for this first name: Joshua, Joshua 2, …

    When conn is given, slots without a people row are ignored — list-crop
    jitter used to mint phantom 'Name N' files that then stole threads via
    photo matching (Yelen 9, Gerardo 11).
    """
    base = _base_name(name)
    allowed: set[str] | None = None
    if conn is not None:
        try:
            from src.store import name_aliases

            allowed = {a.casefold() for a in name_aliases(conn, base)}
        except Exception:
            allowed = None
    found: list[str] = []
    for candidate in [base] + [f"{base} {i}" for i in range(2, 12)]:
        if allowed is not None and candidate.casefold() not in allowed:
            continue
        if photo_exists(candidate):
            found.append(candidate)
    return found


def _prep_face(img, *, size: int = 32, inset: float = 0.15):
    from PIL import Image

    img = img.convert("RGB")
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side))
    pad = int(side * inset)
    if side - 2 * pad >= 12:
        img = img.crop((pad, pad, side - pad, side - pad))
    return img.convert("L").resize((size, size), Image.Resampling.LANCZOS)


def face_distance(left, right) -> float:
    """Mean absolute pixel difference 0–255 after a tight grayscale resize. Lower = same face."""
    pa = list(_prep_face(left).getdata())
    pb = list(_prep_face(right).getdata())
    return sum(abs(a - b) for a, b in zip(pa, pb)) / max(1, len(pa))


# Same-file jitter lands ~13–24; different people in the inbox start ~38.
_FACE_MATCH = 28.0
_FACE_DIFFER = 46.0


def faces_match(left, right) -> bool:
    try:
        return face_distance(left, right) <= _FACE_MATCH
    except Exception:
        return False


def faces_differ(left, right) -> bool:
    try:
        return face_distance(left, right) >= _FACE_DIFFER
    except Exception:
        return False


def load_photo(name: str):
    if not photo_exists(name):
        return None
    from PIL import Image

    try:
        return Image.open(photo_file(name)).convert("RGB")
    except Exception:
        return None


def match_face_to_namesakes(face, name: str, conn=None) -> str | None:
    """Return the stored namesake whose avatar matches `face`, or None if unsure."""
    if face is None:
        return None
    best_name = None
    best_dist = 1e9
    second = 1e9
    for candidate in namesake_photo_names(name, conn=conn):
        stored = load_photo(candidate)
        if stored is None:
            continue
        try:
            dist = face_distance(face, stored)
        except Exception:
            continue
        if dist < best_dist:
            second = best_dist
            best_dist = dist
            best_name = candidate
        elif dist < second:
            second = dist
    if best_name is None:
        return None
    if best_dist <= _FACE_MATCH and (second - best_dist) >= 6:
        return best_name
    if best_dist <= _FACE_MATCH and second >= _FACE_DIFFER:
        return best_name
    return None


def crop_open_chat_face(device, xml: str | None = None):
    """Crop the circular avatar in an open conversation toolbar."""
    xml = xml if xml is not None else dump_hierarchy(device)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    title = None
    avatar = None
    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        box = _parse_bounds(node.attrib.get("bounds") or "")
        if box is None:
            continue
        if rid == "com.bumblebff.app:id/chatToolbar_title":
            title = box
        if any(
            rid.endswith(suf)
            for suf in (
                "chatToolbar_avatar",
                "conversation_contact",
                "toolbar_avatar",
                "profileImage",
            )
        ) or "avatar" in rid.lower():
            if box[3] - box[1] >= 48:
                avatar = box
    if avatar is None and title is not None:
        h = max(48, title[3] - title[1])
        left = max(0, title[0] - h - 12)
        avatar = (left, max(0, title[1] - 8), title[0] - 6, title[3] + 8)
    if avatar is None:
        return None
    try:
        img = _screenshot_pil(device)
        return _crop_square(img, avatar, inset=0.08)
    except Exception:
        log.debug("open-chat face crop failed", exc_info=True)
        return None


def closest_alias_by_face(face, name: str, conn=None) -> tuple[str | None, float]:
    """Nearest stored namesake photo to `face`. Distance 1e9 if nothing stored."""
    if face is None:
        return None, 1e9
    best_name = None
    best_dist = 1e9
    for candidate in namesake_photo_names(name, conn=conn):
        stored = load_photo(candidate)
        if stored is None:
            continue
        try:
            dist = face_distance(face, stored)
        except Exception:
            continue
        if dist < best_dist:
            best_dist = dist
            best_name = candidate
    return best_name, best_dist


def open_chat_matches_stored_photo(device, name: str, xml: str | None = None, conn=None) -> bool:
    """True when the open thread's toolbar face is this namesake's stored photo."""
    face = crop_open_chat_face(device, xml)
    if face is None:
        return False
    matched = match_face_to_namesakes(face, name, conn=conn)
    if matched and matched.casefold() == name.casefold():
        log.info("open-chat face matched namesake slot %s", matched)
        return True
    stored = load_photo(name)
    if stored is None:
        log.warning("open-chat face: no stored photo for %s", name)
        return False
    try:
        own = face_distance(face, stored)
    except Exception:
        return False
    alias, other = closest_alias_by_face(face, name, conn=conn)
    log.info(
        "open-chat face vs %s d=%.1f closest=%s d=%.1f matched=%s",
        name,
        own,
        alias,
        other,
        matched,
    )
    if own <= _FACE_MATCH and (alias is None or alias.casefold() == name.casefold() or other - own >= 6):
        log.info("open-chat face matches stored %s (d=%.1f)", name, own)
        return True
    return False


def photos_conflict(left: str, right: str) -> bool:
    """True when both people have avatars and they are clearly different faces."""
    a, b = load_photo(left), load_photo(right)
    if a is None or b is None:
        return False
    return faces_differ(a, b)


def notify_photo_saved(name: str, *, replaced: bool = False) -> None:
    """Guess ethnicity from a newly stored avatar. Manual tags stay put."""
    if not (name or "").strip():
        return
    try:
        from src.ethnicity_vision import schedule_guess
        from src.phones import current_phone_id

        schedule_guess(name=name, phone_id=current_phone_id(), force=replaced)
    except Exception:
        log.debug("ethnicity guess after photo skip", exc_info=True)


def adopt_photo(src_name: str, dest_name: str, phone_id: str | None = None) -> bool:
    """Move src's file onto dest when dest has none."""
    if not photo_exists(src_name, phone_id) or photo_exists(dest_name, phone_id) or not dest_name:
        return False
    dest = photo_file(dest_name, phone_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    photo_file(src_name, phone_id).replace(dest)
    if dest.is_file():
        notify_photo_saved(dest_name)
        return True
    return False


def next_photo_slot(name: str, aliases: list[str] | None = None) -> str:
    """First namesake without a file, or the next free 'Name N' slot."""
    for alias in aliases or [_base_name(name)]:
        if not photo_exists(alias):
            return alias
    base = _base_name(name)
    for i in range(2, 12):
        candidate = f"{base} {i}"
        if not photo_exists(candidate):
            return candidate
    return _base_name(name)


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


def _crop_square(img, box: tuple[int, int, int, int], *, inset: float = 0.08):
    from PIL import Image

    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    pad_x = int(w * inset)
    pad_y = int(h * inset)
    crop = img.crop((x1 + pad_x, y1 + pad_y, x2 - pad_x, y2 - pad_y))
    if crop.width < 12 or crop.height < 12:
        return None
    side = min(crop.width, crop.height)
    left = (crop.width - side) // 2
    top = (crop.height - side) // 2
    crop = crop.crop((left, top, left + side, top + side))
    return crop.convert("RGB").resize((160, 160), Image.Resampling.LANCZOS)


def _save_square(img, box: tuple[int, int, int, int], dest: Path, *, inset: float = 0.08) -> bool:
    crop = _crop_square(img, box, inset=inset)
    if crop is None:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    crop.save(dest, "JPEG", quality=82)
    return dest.is_file() and dest.stat().st_size > 80


def save_face_image(face, name: str) -> bool:
    if face is None or not name:
        return False
    dest = photo_file(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    existed = dest.is_file() and dest.stat().st_size > 80
    try:
        from PIL import Image

        face.convert("RGB").resize((160, 160), Image.Resampling.LANCZOS).save(
            dest, "JPEG", quality=82
        )
    except Exception:
        return False
    if dest.is_file() and dest.stat().st_size > 80:
        notify_photo_saved(name, replaced=existed)
        return True
    return False


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


def _box_key(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return tuple(round(v / 8) * 8 for v in box)  # type: ignore[return-value]


def list_avatar_boxes(xml: str) -> list[tuple[str, tuple[int, int, int, int]]]:
    """Name + avatar bounds from the Chats list (and New-friends rings).

    Two people with the same first name both stay in the list — they are
    distinguished later by the face in the box, not by collapsing to one name.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    found: list[tuple[str, tuple[int, int, int, int]]] = []
    seen: set[tuple[int, int, int, int]] = set()

    def _add(name: str, box: tuple[int, int, int, int] | None) -> None:
        if not name or box is None:
            return
        key = _box_key(box)
        if key in seen:
            return
        seen.add(key)
        found.append((name, box))

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
        _add(name, ring)

    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        if not any(rid.endswith(suf) for suf in _RING_SUFFIXES):
            continue
        desc = (node.attrib.get("content-desc") or "").strip()
        match = re.match(r"^(.+?),\s*BFF,\s*(?:expired\s+)?match$", desc, re.I)
        if not match:
            continue
        _add(match.group(1).strip(), _parse_bounds(node.attrib.get("bounds") or ""))
    return found


def _box_for_row(
    boxes: list[tuple[str, tuple[int, int, int, int]]],
    name: str,
    y: int | None = None,
    x: int | None = None,
) -> tuple[int, int, int, int] | None:
    want = (name or "").strip().casefold()
    base = _base_name(name).casefold()
    candidates = [
        (n, box)
        for n, box in boxes
        if n.strip().casefold() == want or n.strip().casefold() == base
    ]
    if not candidates:
        return None
    if y is None and x is None:
        return candidates[0][1]

    def _dist(item: tuple[str, tuple[int, int, int, int]]) -> int:
        _n, box = item
        cx, cy = _center(box)
        dx = 0 if x is None else cx - int(x)
        dy = 0 if y is None else cy - int(y)
        return dx * dx + dy * dy

    return min(candidates, key=_dist)[1]


def crop_list_face(device, xml: str, name: str, *, y: int | None = None, x: int | None = None):
    """Crop the list/strip avatar nearest this name and position."""
    box = _box_for_row(list_avatar_boxes(xml), name, y=y, x=x)
    if box is None:
        return None
    try:
        img = _screenshot_pil(device)
    except Exception:
        return None
    return _crop_square(img, box, inset=0.12)


def grab_visible_list_avatars(device, xml: str | None = None) -> int:
    """Crop chat-list faces from one screenshot. Same-name faces go to Joshua / Joshua 2 / …"""
    xml = xml if xml is not None else dump_hierarchy(device)
    boxes = list_avatar_boxes(xml)
    if not boxes:
        return 0
    try:
        img = _screenshot_pil(device)
    except Exception:
        log.warning("list avatar screenshot failed")
        return 0
    aliases_by_base: dict[str, list[str]] = {}
    conn = None
    try:
        from src.config import load_config
        from src.store import connect as db_connect, db_path_from_config, name_aliases

        conn = db_connect(db_path_from_config(load_config()))
        seen_bases: set[str] = set()
        for raw_name, _box in boxes:
            base = _base_name(raw_name)
            if base in seen_bases:
                continue
            seen_bases.add(base)
            aliases_by_base[base] = name_aliases(conn, raw_name) or [raw_name]
    except Exception:
        log.debug("namesake alias lookup failed", exc_info=True)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            conn = None

    saved = 0
    try:
        for name, box in boxes:
            try:
                crop = _crop_square(img, box, inset=0.12)
                if crop is None:
                    continue
                matched = match_face_to_namesakes(crop, name, conn=conn)
                if matched:
                    continue
                aliases = aliases_by_base.get(_base_name(name))
                dest = next_photo_slot(name, aliases)
                # Never mint a phantom 'Name N' slot: only save to the row's own
                # name or an alias that exists as a person row. List-crop jitter
                # otherwise mints endless slots (Gerardo 11) that later steal
                # threads through photo matching.
                known = {a.casefold() for a in (aliases or [name])}
                if dest.casefold() != name.casefold() and dest.casefold() not in known:
                    continue
                stored = load_photo(dest)
                if stored is not None and not faces_differ(crop, stored):
                    continue
                dest_path = photo_file(dest)
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                existed = dest_path.is_file() and dest_path.stat().st_size > 80
                crop.save(dest_path, "JPEG", quality=82)
                if dest_path.is_file() and dest_path.stat().st_size > 80:
                    saved += 1
                    notify_photo_saved(dest, replaced=existed)
                    if dest != name:
                        log.info("list avatar %s stored as %s", name, dest)
            except Exception:
                log.debug("crop failed for %s", name, exc_info=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
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
    max_h = 0
    for node in root.iter():
        box = _parse_bounds(node.attrib.get("bounds") or "")
        if box:
            max_h = max(max_h, box[3])
    toolbar_cut = int(max_h * 0.12) if max_h else 0
    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        text = (node.attrib.get("text") or "").strip()
        if rid == "com.bumblebff.app:id/chatToolbar_title" and text:
            box = _parse_bounds(node.attrib.get("bounds") or "")
            if box:
                return _center(box)
        if want and text.casefold() == want:
            box = _parse_bounds(node.attrib.get("bounds") or "")
            if box and (not toolbar_cut or box[1] < toolbar_cut):
                return _center(box)
    return None


def _hero_box(width: int, height: int) -> tuple[int, int, int, int]:
    top = int(height * 0.07)
    side = min(width, int(height * 0.52))
    left = max(0, (width - side) // 2)
    return left, top, left + side, top + side


def capture_open_profile_photo(device, name: str, *, force: bool = False) -> bool:
    """Crop the first profile photo from an already-open profile screen.

    If this face already belongs to a namesake, keep it on that person instead of
    overwriting the wrong Joshua. ``force`` overwrites this person's file (used
    after rematch, when the stored crop is the grey expired avatar).
    """
    try:
        img = _screenshot_pil(device)
        width, height = img.size
        crop = _crop_square(img, _hero_box(width, height), inset=0.02)
        if crop is None:
            return False
        dest = name
        if not force:
            matched = match_face_to_namesakes(crop, name)
            if matched:
                dest = matched
                if photo_exists(dest):
                    log.info("profile photo already stored as %s", dest)
                    return True
            elif photo_exists(name):
                stored = load_photo(name)
                if stored is not None and faces_differ(crop, stored):
                    dest = next_photo_slot(name)
                elif stored is not None:
                    return True
        dest_path = photo_file(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        existed = dest_path.is_file() and dest_path.stat().st_size > 80
        crop.save(dest_path, "JPEG", quality=82)
        if dest_path.is_file() and dest_path.stat().st_size > 80:
            log.info("saved profile photo for %s", dest)
            notify_photo_saved(dest, replaced=existed or force)
            return True
    except Exception:
        log.warning("profile photo crop failed for %s", name)
    return False


def capture_profile_photo(device, name: str, *, force: bool = False) -> bool:
    """From an open chat, tap into the profile and save the first photo."""
    xml = dump_hierarchy(device)
    if _looks_like_profile(xml):
        return capture_open_profile_photo(device, name, force=force)
    point = _toolbar_profile_tap(xml, name)
    if point is None:
        info = device.info or {}
        width = int(info.get("displayWidth") or 0)
        height = int(info.get("displayHeight") or 0)
        if width < 1 or height < 1:
            return False
        point = (width // 2, int(height * 0.055))
    tap(device, point[0], point[1])
    wait_idle(device, 1.4)
    xml = dump_hierarchy(device)
    if not _looks_like_profile(xml):
        log.debug("profile did not open for %s", name)
        return False
    ok = capture_open_profile_photo(device, name, force=force)
    device.press("back")
    wait_idle(device, 0.9)
    return ok
