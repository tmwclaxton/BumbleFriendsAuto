"""Review Toby's Instagram following on the Pixel. Propose unfollows; do not act.

REVIEW-ONLY until the parent says the list is approved: never tap Unfollow,
never confirm an unfollow dialog, never like posts.

Later (not this run), after the user approves the unfollow list AND those
unfollows are finished: walk the Following feed on the Pixel and like each
person's new posts at human pace, skipping ads/suggested, stopping if
Instagram action-blocks. Do not implement or run that like pass here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from src.config import ROOT
from src.device import bring_app_foreground, connect, dump_artifacts, dump_hierarchy, wait_idle
from src.gestures import tap
from src.phones import DEFAULT_PHONE_ID, serial_for
from src.screen import _bounds_center
from src.unlock import wake_and_unlock

log = logging.getLogger(__name__)

PACKAGE = "com.instagram.android"
PIXEL_SERIAL = "29081FDH200GZ8"
GALAXY_MARK = "192.168.0.168"
MAX_MINUTES = 45
INSPECT_PAUSE = (1.2, 3.2)
STUCK_REPEAT = 2
REVIEW_ONLY = True
NEVER_REVISIT = frozenset(
    {
        "closetshare",
        "closet_share",
        "closet.share",
        "closetshare_",
        "closetshared",
    }
)

ALWAYS_KEEP = frozenset(
    {
        "letsgosocialuk",
        "letsgosocial",
        "grantgunner",
        "grantgunner_official",
        "canvassr",
        "vidgaze",
        "rapidresearch.ai",
        "rapidresearch",
        "tobyclaxton",
        "tobyc1laxton",
        "tmwclaxton",
        "archiewilding",
        "lgsbucks",
        "lgslondon",
    }
)

_KEEP_SUBSTR = (
    "letsgosocial",
    "grantgunner",
    "canvassr",
    "lgsuk",
    "lgs_",
)

_CHROME = frozenset(
    {
        "following",
        "followers",
        "follow",
        "unfollow",
        "requested",
        "search",
        "profile",
        "home",
        "reels",
        "shop",
        "store",
        "messages",
        "activity",
        "options",
        "more options",
        "sort",
        "least interacted with",
        "most shown first",
        "default",
        "suggestions",
        "see all",
        "edit profile",
        "share profile",
        "add",
        "new",
        "posts",
        "stories",
        "highlights",
        "cancel",
        "not now",
        "ok",
        "done",
        "close",
        "back",
        "next",
        "skip",
        "allow",
        "deny",
        "instagram",
        "create",
        "explore",
        "search and explore",
        "your story",
        "menu",
        "settings",
        "notifications",
        "suggested for you",
        "people you follow",
        "categories",
        "accounts",
        "hashtags",
        "places",
        "audio",
    }
)

_BRAND_WORDS = re.compile(
    r"\b("
    r"official|memes?|shitpost|relatable|quote(?:s| of the day)|daily memes?|"
    r"meme page|fan page|dump account|"
    r"breaking news|\bnews\b|media outlet|magazine|newspaper|broadcast|"
    r"football\s?club|\bfc\b|premier league|\bnba\b|\bnfl\b|\bufc\b|espn|sky sports|"
    r"flagship store|\bshop\b|\bstore\b|outlet|cosmetics brand|clothing brand|"
    r"ai art|midjourney|\bnft\b|giveaway page|promo codes?|"
    r"podcast network|netflix|disney\+|spotify official"
    r")\b",
    re.I,
)

_HANDLE_BRAND = re.compile(
    r"(memes?|shitpost|relatable|dailymeme|quote(?:s|oftheday)|viralvids|"
    r"official|newsroom|newstoday|fanpage|fcofficial|theathletic|"
    r"shop|store|brand|podcastclip|aiart|midjourney)",
    re.I,
)

_HANDLE_OK = re.compile(r"^[A-Za-z0-9._]{2,30}$")
_HUMAN_NAME = re.compile(r"^(?!The |A |An )[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}$")
_HUMAN_HANDLE = re.compile(
    r"^[a-z]{2,15}[._]?[a-z]{2,15}\d{0,3}$",
    re.I,
)
_BLOCKED = re.compile(
    r"try again later|action blocked|we restrict certain activity|"
    r"we suspend accounts|checkpoint|unusual activity|confirm it.?s you|"
    r"log in to instagram|enter your password|two-factor|challenge required|"
    r"we detected unusual|temporarily blocked",
    re.I,
)

_FOLLOWER_COUNT = re.compile(
    r"([\d,.]+)\s*(k|m|b)?\s*followers?",
    re.I,
)


@dataclass
class FollowRow:
    handle: str
    display: str
    row_xy: tuple[int, int]
    follow_xy: tuple[int, int] | None


def progress_path(phone_id: str) -> Path:
    pid = (phone_id or DEFAULT_PHONE_ID).strip() or DEFAULT_PHONE_ID
    return ROOT / "data" / f"instagram_prune_{pid}.json"


def review_path(phone_id: str) -> Path:
    pid = (phone_id or DEFAULT_PHONE_ID).strip() or DEFAULT_PHONE_ID
    return ROOT / "data" / f"instagram_review_{pid}.json"


def load_progress(phone_id: str) -> dict:
    path = progress_path(phone_id)
    empty = {"decided": {}, "proposed": [], "unfollowed": [], "skipped": [], "kept": [], "log": []}
    if not path.is_file():
        return empty
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(raw, dict):
        return empty
    raw.setdefault("decided", {})
    raw.setdefault("proposed", [])
    raw.setdefault("unfollowed", [])
    raw.setdefault("skipped", [])
    raw.setdefault("kept", [])
    raw.setdefault("log", [])
    return raw


def save_progress(phone_id: str, state: dict) -> None:
    path = progress_path(phone_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    review = {
        "review_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "proposed": [
            {
                "handle": str(item.get("handle") or ""),
                "display": str(item.get("display") or ""),
                "reason": str(item.get("reason") or ""),
            }
            for item in state.get("proposed") or []
            if isinstance(item, dict)
        ],
        "unsure": [
            {
                "handle": str(item.get("handle") or ""),
                "display": str(item.get("display") or ""),
                "reason": str(item.get("reason") or ""),
            }
            for item in state.get("skipped") or []
            if isinstance(item, dict)
        ],
        "unfollowed": [],
        "summary": (
            f"review-only decided={len(state.get('decided') or {})} "
            f"proposed={len(state.get('proposed') or [])} "
            f"unsure={len(state.get('skipped') or [])} unfollowed=0; "
            f"last_run={str(state.get('summary') or 'not recorded')}"
        ),
    }
    review_path(phone_id).write_text(
        json.dumps(review, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _norm_handle(raw: str) -> str:
    return (raw or "").strip().lstrip("@").strip().lower()


def _known_people() -> tuple[set[str], set[str]]:
    handles: set[str] = set(ALWAYS_KEEP)
    names: set[str] = set()
    try:
        from src.config import load_config
        from src.store import connect as db_connect, db_path_from_config

        conn = db_connect(db_path_from_config(load_config()))
        try:
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(people)")}
            extra = ", p.instagram" if "instagram" in cols else ""
            rows = conn.execute(f"SELECT p.name{extra} FROM people p").fetchall()
            for row in rows:
                name = str(row[0] or "").strip()
                if name:
                    names.add(name.casefold())
                    first = name.split()[0].casefold()
                    if len(first) >= 3:
                        names.add(first)
                if extra and len(row) > 1 and row[1]:
                    handles.add(_norm_handle(str(row[1])))
            try:
                crm_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(crm)")}
            except Exception:
                crm_cols = set()
            if "instagram" in crm_cols:
                for (ig,) in conn.execute(
                    "SELECT instagram FROM crm WHERE instagram IS NOT NULL AND instagram != ''"
                ):
                    handles.add(_norm_handle(str(ig)))
        finally:
            conn.close()
    except Exception as exc:
        log.info("crm keep-list unavailable: %s", exc)
    return handles, names


def classify_account(
    handle: str,
    display: str = "",
    *,
    bio: str = "",
    followers: int | None = None,
    known_handles: set[str] | None = None,
    known_names: set[str] | None = None,
) -> tuple[str, str]:
    """Return (keep|propose|skip, reason). Skip when unsure. Never acts."""
    h = _norm_handle(handle)
    display = (display or "").strip()
    disp_cf = display.casefold()
    blob = f"{h} {display} {bio}".strip()
    if not h:
        return "skip", "no handle"
    compact = h.replace("_", "").replace(".", "")
    if h in NEVER_REVISIT or "closetshare" in compact or "closet share" in disp_cf:
        return "skip", "Closet Share — skip, never revisit"
    if h in ALWAYS_KEEP or (known_handles and h in known_handles):
        return "keep", "known person / LGS"
    if any(part in h for part in _KEEP_SUBSTR):
        return "keep", "LGS-related handle"
    if known_names:
        if disp_cf in known_names:
            return "keep", "matches CRM name"
        first = disp_cf.split()[0] if disp_cf else ""
        if first and first in known_names and _HUMAN_NAME.match(display):
            return "keep", "matches known first name"
    if any(part in disp_cf for part in ("let's go social", "lets go social", "grantgunner", "canvassr")):
        return "keep", "LGS-related name"

    brand_hit = _BRAND_WORDS.search(blob) or _HANDLE_BRAND.search(h)
    huge = followers is not None and followers >= 80_000
    if brand_hit and (huge or _HANDLE_BRAND.search(h) or re.search(r"\b(memes?|shitpost|daily|quotes?)\b", blob, re.I)):
        return "propose", f"not a person ({brand_hit.group(0) if brand_hit else 'brand'})"
    if brand_hit and re.search(r"\b(official|fc|news|magazine|store|shop|brand)\b", blob, re.I):
        return "propose", f"org/brand ({brand_hit.group(0)})"
    if re.search(r"\b(daily memes|meme page|quote page|ai art|fan page)\b", blob, re.I):
        return "propose", "meme/quote/fan page"
    if re.match(r"^the\s+", display, re.I) and not _HUMAN_NAME.match(display):
        return "propose", "page/band (The …), not a person"
    if h.endswith("fc") and not _HUMAN_NAME.match(display):
        if re.search(r"(united|city|town|rovers|athletic|wanderers|hotspur|arsenal|chelsea|liverpool)", h, re.I):
            return "propose", "football club"

    if _HUMAN_NAME.match(display) and not brand_hit:
        return "keep", "looks like a real name"
    if _HUMAN_HANDLE.match(h) and display and not brand_hit and (followers is None or followers < 50_000):
        if not re.search(r"\d{4,}", h):
            return "keep", "looks like a person handle"

    if brand_hit:
        return "skip", f"brand-ish but unsure ({brand_hit.group(0)})"
    if huge and not display:
        return "skip", "huge account, no name"
    return "skip", "unsure — leaving followed"


def parse_follower_count(text: str) -> int | None:
    m = _FOLLOWER_COUNT.search(text or "")
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        num = float(raw)
    except ValueError:
        return None
    suffix = (m.group(2) or "").lower()
    mul = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suffix]
    return int(num * mul)


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


def _blob(xml: str) -> str:
    return "\n".join(_texts(xml))


def looks_blocked(xml: str) -> bool:
    return bool(_BLOCKED.search(_blob(xml)))


def looks_like_login(xml: str) -> bool:
    blob = _blob(xml).lower()
    return any(
        s in blob
        for s in (
            "log in to instagram",
            "sign up with email",
            "create new account",
            "i already have an account",
            "phone number, username or email",
        )
    )


def looks_like_own_profile(xml: str) -> bool:
    blob = _blob(xml).lower()
    mine = "tobyc1laxton" in blob or "edit profile" in blob
    header = "row_profile_header" in xml or "profile_header_following" in xml
    return (
        mine
        and ("edit profile" in blob or "share profile" in blob or header)
        and ("followers" in blob or "profile_header_followers" in xml)
        and "following" in blob
    )


def looks_like_following_list(xml: str) -> bool:
    if looks_like_own_profile(xml):
        return False
    blob = _blob(xml).lower()
    if "tobyc1laxton" not in blob:
        return False
    if re.search(r"\b(2 mutual|suggested)\b", blob) and "1,053 following" not in blob and "1053 following" not in blob:
        if "follow_list_username" not in xml:
            return False
    rows = parse_following_rows(xml)
    if "follow_list_username" in xml or "follow_list_container" in xml:
        return bool(rows)
    has_search = "search" in blob
    has_title = "following" in blob
    return has_title and has_search and len(rows) >= 1


def _parse_bounds(bounds: str) -> tuple[int, int, int, int] | None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def parse_following_rows(xml: str) -> list[FollowRow]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    rows: list[FollowRow] = []
    seen: set[str] = set()
    for node in root.iter():
        rid = (node.attrib.get("resource-id") or "").rsplit("/", 1)[-1]
        if rid != "follow_list_container":
            continue
        handle = ""
        display = ""
        follow_xy = None
        row_xy = _bounds_center(node.attrib.get("bounds") or "")
        for child in node.iter():
            crid = (child.attrib.get("resource-id") or "").rsplit("/", 1)[-1]
            text = (child.attrib.get("text") or "").strip()
            if crid == "follow_list_username" and text:
                handle = _norm_handle(text)
                center = _bounds_center(child.attrib.get("bounds") or "")
                if center:
                    row_xy = center
            elif crid == "follow_list_subtitle" and text:
                if text.casefold() not in {"1 new post", "new post"} and not text.casefold().endswith("new post"):
                    display = text
            elif crid == "follow_list_row_large_follow_button":
                follow_xy = _bounds_center(child.attrib.get("bounds") or "")
        if not handle or handle in seen or handle in _CHROME:
            continue
        seen.add(handle)
        rows.append(
            FollowRow(handle=handle, display=display, row_xy=row_xy or (0, 0), follow_xy=follow_xy)
        )
    if rows:
        return rows

    buttons: list[tuple[int, int, int, int, tuple[int, int]]] = []
    labels: list[tuple[int, int, int, int, str, str]] = []
    for node in root.iter():
        bounds = _parse_bounds(node.attrib.get("bounds") or "")
        if not bounds:
            continue
        x1, y1, x2, y2 = bounds
        if y2 - y1 < 20 or x2 - x1 < 20:
            continue
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        rid = (node.attrib.get("resource-id") or "").lower()
        if "profile_header" in rid or "row_profile_header" in rid:
            continue
        label = text or desc
        low = label.lower()
        if re.search(r"[\d,.]+\s*following", low) or re.fullmatch(r"[\d,.]+\s*following", low.replace(" ", "")):
            continue
        if low in {"following", "requested"}:
            if "followers" in low:
                continue
            center = _bounds_center(node.attrib.get("bounds") or "")
            if center and x1 > 520:
                buttons.append((x1, y1, x2, y2, center))
            continue
        if not text and not desc:
            continue
        if low in _CHROME:
            continue
        if re.fullmatch(r"\d[\d,.]*[kmb]?", text):
            continue
        kind = "handle" if ("username" in rid or _HANDLE_OK.match(text.lstrip("@"))) else "display"
        if kind == "handle" and not _HANDLE_OK.match(text.lstrip("@")):
            if _HANDLE_OK.match(desc.lstrip("@")):
                labels.append((x1, y1, x2, y2, _norm_handle(desc), "handle"))
            continue
        labels.append((x1, y1, x2, y2, text.lstrip("@"), kind))

    for bx1, by1, bx2, by2, bxy in buttons:
        mid_y = (by1 + by2) / 2
        handle = ""
        display = ""
        row_xy = (int((bx1 + 80)), int(mid_y))
        for x1, y1, x2, y2, text, kind in labels:
            if abs(((y1 + y2) / 2) - mid_y) > 70:
                continue
            if x2 > bx1 - 8:
                continue
            if kind == "handle" or _HANDLE_OK.match(text):
                if not handle:
                    handle = _norm_handle(text)
                    row_xy = ((x1 + x2) // 2, (y1 + y2) // 2)
            elif not display:
                display = text
        if not handle or handle in seen or handle in _CHROME:
            continue
        seen.add(handle)
        rows.append(FollowRow(handle=handle, display=display, row_xy=row_xy, follow_xy=bxy))
    return rows


def find_label_point(xml: str, *needles: str, prefer_bottom: bool = False) -> tuple[int, int] | None:
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
        if not any(n == text.casefold() or n == desc.casefold() or n in low for n in want):
            continue
        center = _bounds_center(node.attrib.get("bounds") or "")
        if not center:
            continue
        hits.append((center[1], center[0], center[1]))
    if not hits:
        return None
    hits.sort(key=lambda t: t[0], reverse=prefer_bottom)
    _, x, y = hits[0] if prefer_bottom else min(hits, key=lambda t: t[0])
    if not prefer_bottom:
        y0, x0, _ = min(hits, key=lambda t: t[0])
        return x0, y0
    return x, y


def find_following_stat(xml: str) -> tuple[int, int] | None:
    """Tap the Following count on a profile — never Followers."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    prefer: list[tuple[int, int]] = []
    hits: list[tuple[int, int]] = []
    for node in root.iter():
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        rid = (node.attrib.get("resource-id") or "").lower()
        if "follower" in rid and "following" not in rid:
            continue
        if "follower" in desc.lower() and "following" not in desc.lower():
            continue
        center = _bounds_center(node.attrib.get("bounds") or "")
        if not center:
            continue
        if "profile_header_following" in rid or "familiar_following" in rid:
            prefer.append(center)
            continue
        if re.search(r"[\d,.]+\s*following", desc, re.I) or desc.lower().endswith("following"):
            hits.append(center)
        elif text.casefold() == "following" and "profile_header" in rid:
            hits.append(center)
    pool = prefer or hits
    if not pool:
        return None
    pool.sort(key=lambda p: (p[1], -p[0]))
    return pool[0]


def find_unfollow_confirm(xml: str) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        low = f"{text} {desc}".lower()
        if any(bad in low for bad in ("block", "restrict", "report", "cancel", "mute")):
            continue
        if text.casefold() == "unfollow" or desc.casefold() == "unfollow" or low.startswith("unfollow @"):
            center = _bounds_center(node.attrib.get("bounds") or "")
            if center:
                return center
    return None


def _xml(device) -> str:
    wait_idle(device, 0.35)
    return dump_hierarchy(device)


def _pause(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def _check_cancel() -> None:
    from src.phone_queue import check_cancel

    check_cancel()


def _unlock(serial: str | None):
    device = connect(serial)
    unlocked = False
    for _ in range(4):
        try:
            unlocked = wake_and_unlock(device, serial=serial)
            break
        except Exception as exc:
            log.warning("unlock dropped: %s", exc)
            time.sleep(2)
            try:
                device = connect(serial)
            except Exception:
                time.sleep(2)
    if not unlocked:
        return None, "phone still locked — unlock failed"
    bring_app_foreground(device, PACKAGE)
    wait_idle(device, 1.6)
    return device, ""


def _dismiss_popups(device, xml: str) -> str:
    for _ in range(6):
        if looks_blocked(xml) or looks_like_login(xml):
            return xml
        point = find_label_point(xml, "Not now", "Cancel", "Don't allow")
        if point is None:
            return xml
        tap(device, point[0], point[1])
        wait_idle(device, 0.8)
        xml = _xml(device)
    return xml


def _open_own_following(device) -> tuple[str, str]:
    xml = _dismiss_popups(device, _xml(device))
    for _ in range(2):
        if looks_like_following_list(xml) or looks_like_own_profile(xml) or looks_like_login(xml):
            break
        _press_back(device)
        xml = _xml(device)
    if looks_like_following_list(xml):
        return xml, ""
    if looks_like_login(xml):
        return xml, "instagram login wall"
    if not looks_like_own_profile(xml):
        profile = find_label_point(xml, "Profile", prefer_bottom=True)
        if profile:
            tap(device, profile[0], profile[1])
            wait_idle(device, 1.4)
            xml = _dismiss_popups(device, _xml(device))
        if not looks_like_own_profile(xml):
            info = device.info
            w, h = int(info["displayWidth"]), int(info["displayHeight"])
            tap(device, int(w * 0.90), int(h * 0.96))
            wait_idle(device, 1.4)
            xml = _dismiss_popups(device, _xml(device))
    if looks_like_following_list(xml):
        return xml, ""
    try:
        node = device(resourceId="com.instagram.android:id/profile_header_following_stacked_familiar")
        if node.exists:
            node.click()
            wait_idle(device, 2.0)
            xml = _dismiss_popups(device, _xml(device))
            if looks_like_following_list(xml):
                return xml, ""
    except Exception:
        pass
    stat = find_following_stat(xml)
    if stat:
        tap(device, stat[0], stat[1])
        wait_idle(device, 2.0)
        xml = _dismiss_popups(device, _xml(device))
    if looks_like_following_list(xml):
        return xml, ""
    return xml, "could not open Following list"


def _scroll_list(device) -> None:
    try:
        lst = device(resourceId="android:id/list")
        if lst.exists:
            try:
                lst.scroll.vert.forward(steps=16)
            except Exception:
                lst.fling(direction="forward")
            wait_idle(device, 0.8)
            return
    except Exception:
        pass
    info = device.info
    w, h = int(info["displayWidth"]), int(info["displayHeight"])
    x = int(w * random.uniform(0.36, 0.48))
    y1 = int(h * random.uniform(0.74, 0.80))
    y2 = int(h * random.uniform(0.36, 0.44))
    try:
        from src.gestures import _adb_swipe

        _adb_swipe(device, x, y1, x, y2, random.randint(420, 700))
    except Exception:
        device.swipe(x, y1, x, y2, duration=0.5)
    wait_idle(device, 0.9)


def _press_back(device) -> None:
    try:
        device.press("back")
    except Exception:
        pass
    wait_idle(device, 0.8)


def _dump_stuck(device, tag: str) -> None:
    dump_dir = ROOT / "data"
    try:
        dump_artifacts(device, dump_dir, prefix=f"ig-prune-{tag}")
    except Exception as exc:
        log.warning("stuck dump failed: %s", exc)


def _profile_signals(xml: str) -> tuple[int | None, str]:
    blob = _blob(xml)
    followers = parse_follower_count(blob)
    bio_bits = []
    for line in _texts(xml):
        low = line.strip().lower()
        if low in _CHROME or low in {"following", "follow", "message", "email"}:
            continue
        if _FOLLOWER_COUNT.search(line) or re.search(r"\bfollowers?\b|\bfollowing\b|\bposts?\b", line, re.I):
            continue
        if _HANDLE_OK.match(line.lstrip("@")) and len(line) < 32:
            continue
        if len(line) >= 8:
            bio_bits.append(line)
    return followers, " | ".join(bio_bits[:6])


def _confirm_unfollow(device, xml: str) -> tuple[bool, str]:
    if REVIEW_ONLY:
        log.warning("review-only: refusing unfollow confirm")
        return False, "review-only"
    xml = _dismiss_popups(device, xml)
    if looks_blocked(xml):
        return False, "rate-limited"
    point = find_unfollow_confirm(xml)
    if point is None:
        # Row tap sometimes toggles without a dialog on some builds.
        if "unfollow" in _blob(xml).lower():
            return False, "no confirm dialog"
        return True, "toggled following"
    tap(device, point[0], point[1])
    wait_idle(device, 1.0)
    xml = _xml(device)
    if looks_blocked(xml):
        return False, "rate-limited"
    return True, "unfollowed"


def _unfollow_from_list(device, row: FollowRow) -> tuple[bool, str]:
    if REVIEW_ONLY:
        log.warning("review-only: refusing unfollow tap")
        return False, "review-only"
    if row.follow_xy:
        tap(device, row.follow_xy[0], row.follow_xy[1])
    else:
        return False, "no following button"
    wait_idle(device, 0.9)
    return _confirm_unfollow(device, _xml(device))


def _inspect_profile(device, row: FollowRow) -> tuple[str, str, int | None, str]:
    tap(device, row.row_xy[0], row.row_xy[1])
    wait_idle(device, 1.2)
    xml = _dismiss_popups(device, _xml(device))
    if looks_blocked(xml):
        return "stop", "rate-limited", None, ""
    followers, bio = _profile_signals(xml)
    return "ok", "", followers, bio


def run_prune(*, serial: str | None = None, phone_id: str = "toby") -> tuple[bool, str]:
    pid = (phone_id or DEFAULT_PHONE_ID).strip() or DEFAULT_PHONE_ID
    if pid not in {"toby", "pixel"}:
        return False, "instagram prune is Pixel / Toby only"
    serial = serial or serial_for("toby")
    if serial and GALAXY_MARK in str(serial):
        return False, "refusing Galaxy / Archie serial"
    if serial and PIXEL_SERIAL not in str(serial) and "29081FDH200GZ8" not in str(serial):
        log.warning("unexpected serial %s — continuing only if it is the Pixel", serial)

    device, err = _unlock(serial)
    if device is None:
        return False, err
    xml0 = _xml(device)
    if "closet share" in _blob(xml0).lower() or "closetshare" in _blob(xml0).lower():
        log.warning("on Closet Share — force-stopping Instagram and leaving")
        try:
            device.app_stop(PACKAGE)
        except Exception:
            pass
        wait_idle(device, 0.6)
        bring_app_foreground(device, PACKAGE)
        wait_idle(device, 1.6)

    known_handles, known_names = _known_people()
    state = load_progress(pid)
    decided: dict = state["decided"]
    state["proposed"] = []
    state["kept"] = []
    state["skipped"] = []
    state["unfollowed"] = []
    for handle, rec in list(decided.items()):
        if not isinstance(rec, dict):
            continue
        action, reason = classify_account(
            handle,
            str(rec.get("display") or ""),
            known_handles=known_handles,
            known_names=known_names,
        )
        if action == "unfollow":
            action = "propose"
        rec["action"] = action
        rec["reason"] = reason
        decided[handle] = rec
        if action == "propose":
            state["proposed"].append(rec)
        elif action == "keep":
            state["kept"].append(rec)
        else:
            state["skipped"].append(rec)
    save_progress(pid, state)
    started = time.time()
    inspected = 0
    proposed_n = 0
    skipped = 0
    kept = 0
    rate_limited = False
    stop_reason = ""
    same_sig = ""
    same_n = 0
    recovered_once = False
    empty_scrolls = 0

    xml, nav_err = _open_own_following(device)
    if nav_err:
        _dump_stuck(device, "nav")
        return False, nav_err
    if looks_blocked(xml) or looks_like_login(xml):
        return False, "instagram blocked or login wall before start"

    while time.time() - started < MAX_MINUTES * 60:
        _check_cancel()
        xml = _dismiss_popups(device, _xml(device))
        if looks_blocked(xml):
            rate_limited = True
            stop_reason = "rate-limited"
            _dump_stuck(device, "blocked")
            break
        if looks_like_login(xml):
            stop_reason = "login wall"
            _dump_stuck(device, "login")
            break
        if not looks_like_following_list(xml):
            xml, nav_err = _open_own_following(device)
            if nav_err:
                stop_reason = nav_err
                _dump_stuck(device, "lost-list")
                break

        rows = parse_following_rows(xml)
        sig = hashlib.sha1(
            (";".join(r.handle for r in rows[:6]) or _blob(xml)[:400]).encode("utf-8", "replace")
        ).hexdigest()
        pending = [
            r
            for r in rows
            if r.handle not in decided
            and r.handle not in NEVER_REVISIT
            and "closetshare" not in r.handle.replace("_", "").replace(".", "")
        ]
        if sig == same_sig and not pending:
            same_n += 1
        elif sig != same_sig:
            same_sig = sig
            same_n = 1
        else:
            same_n = 0
        if same_n >= STUCK_REPEAT:
            log.warning("same names/xml twice — bounded recovery, no swipe loop")
            _dump_stuck(device, "repeat")
            if not recovered_once:
                recovered_once = True
                try:
                    device.app_stop(PACKAGE)
                    wait_idle(device, 0.8)
                    bring_app_foreground(device, PACKAGE)
                    wait_idle(device, 1.6)
                except Exception:
                    pass
                xml, nav_err = _open_own_following(device)
                if nav_err:
                    stop_reason = nav_err
                    break
                same_n = 0
                _scroll_list(device)
                continue
            stop_reason = "stuck repeating the same following rows"
            break

        if not pending:
            empty_scrolls += 1
            if empty_scrolls >= 18:
                stop_reason = "reached end of following list"
                break
            _scroll_list(device)
            continue
        empty_scrolls = 0

        row = pending[0]
        inspected += 1
        _pause(*INSPECT_PAUSE)
        action, reason = classify_account(
            row.handle,
            row.display,
            known_handles=known_handles,
            known_names=known_names,
        )
        if action == "unfollow":
            action = "propose"
        followers = None
        rec = {
            "handle": row.handle,
            "display": row.display,
            "action": action,
            "reason": reason,
            "followers": followers,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        decided[row.handle] = rec
        if action == "propose":
            state.setdefault("proposed", []).append(rec)
            proposed_n += 1
        elif action == "keep":
            state["kept"].append(rec)
            kept += 1
        else:
            state["skipped"].append(rec)
            skipped += 1
        state["decided"] = decided
        state["inspected"] = int(state.get("inspected") or 0) + 1
        state["unfollowed"] = []
        state["log"] = (state.get("log") or [])[-40:] + [
            f"{rec['action']} @{row.handle} — {rec['reason']}"
        ]
        save_progress(pid, state)
        log.info("%s @%s — %s", rec["action"], row.handle, rec["reason"])
        if len(pending) <= 1:
            _scroll_list(device)

    if not stop_reason:
        stop_reason = "time budget" if time.time() - started >= MAX_MINUTES * 60 else "pass complete"

    summary = (
        f"instagram review inspected={inspected} proposed={proposed_n} "
        f"kept={kept} skipped={skipped} unfollowed=0 rate_limited={rate_limited} stop={stop_reason}"
    )
    log.info(summary)
    state["summary"] = summary
    save_progress(pid, state)
    return not rate_limited and "login" not in stop_reason and "locked" not in stop_reason, summary
