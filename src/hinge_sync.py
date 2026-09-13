"""Conservative Hinge Matches capture on Archie Galaxy."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from src.config import ROOT, load_config
from src.device import bring_app_foreground, connect, dump_hierarchy, wait_idle
from src.gestures import tap
from src.hinge_screen import (
    PACKAGE,
    Bounds,
    classify_screen,
    find_composer,
    find_nav_point,
    find_send,
    find_tab,
    merge_profiles,
    parse_match_list,
    parse_open_thread,
    parse_profile,
    profile_photo_boxes,
)
from src.hinge_store import (
    photos_dir,
    replace_photos,
    replace_prompts,
    replace_thread,
    upsert_chat,
    upsert_person,
)
from src.hinge_swipe import hinge_live_serial, live_account_id, pixel_blocked
from src.phones import normalize_phone_id
from src.store import connect as db_connect, db_path_from_config
from src.unlock import wake_and_unlock

log = logging.getLogger(__name__)
DUMP_DIR = ROOT / "data" / "hinge_dumps"


def _unlock(serial: str | None):
    device = connect(serial)
    if not wake_and_unlock(device, serial=serial):
        return None, "Galaxy still locked — unlock failed"
    bring_app_foreground(device, PACKAGE)
    wait_idle(device, 1.3)
    return device, ""


def _xml(device, tag: str = "hinge") -> str:
    wait_idle(device, 0.35)
    xml = dump_hierarchy(device)
    try:
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (DUMP_DIR / f"{tag}-{stamp}.xml").write_text(xml, encoding="utf-8")
    except OSError as exc:
        log.warning("could not persist hinge dump: %s", exc)
    return xml


def _open_matches(device) -> str:
    xml = _xml(device)
    if classify_screen(xml) == "matches_list":
        return xml
    if classify_screen(xml) in {"match_chat", "match_profile"}:
        point = find_nav_point(xml, "Back") or find_nav_point(xml, "Matches")
        if point:
            tap(device, *point)
            time.sleep(0.6)
            xml = _xml(device)
    point = find_nav_point(xml, "Matches")
    if point:
        tap(device, *point)
        time.sleep(0.8)
        xml = _xml(device)
    return xml


def _slug(name: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-") or "match"


def _crop_photo_boxes(device, xml: str, name: str, phone_id: str, start: int = 0) -> list[tuple[str, str]]:
    from src.photos import _screenshot_pil

    dest_dir = photos_dir(phone_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    slug = _slug(name)
    saved: list[tuple[str, str]] = []
    try:
        img = _screenshot_pil(device)
    except Exception as exc:
        log.warning("hinge screenshot failed: %s", exc)
        return saved
    boxes = profile_photo_boxes(xml)
    if not boxes:
        if start > 0:
            return saved
        width, height = img.size
        top = int(height * 0.16)
        side = min(width - 80, int(height * 0.42))
        left = max(40, (width - side) // 2)
        boxes = [Bounds(left, top, left + side, top + side)]
    for offset, box in enumerate(boxes):
        crop = img.crop((box.x1, box.y1, box.x2, box.y2)).convert("RGB")
        if crop.width < 80 or crop.height < 80:
            continue
        kind = "face" if start + offset == 0 else "photo"
        dest = dest_dir / f"{slug}-{kind}-{start + offset}.jpg"
        crop.save(dest, "JPEG", quality=84)
        if dest.is_file() and dest.stat().st_size > 80:
            saved.append((str(dest), kind))
    return saved


def _scroll_profile(device) -> None:
    info = device.info or {}
    width = int(info.get("displayWidth") or 1080)
    height = int(info.get("displayHeight") or 1920)
    try:
        device.swipe(width // 2, int(height * 0.78), width // 2, int(height * 0.28), 0.35)
    except Exception as exc:
        log.warning("hinge profile scroll failed: %s", exc)
    time.sleep(0.55)


def _collect_profile(device, xml: str, phone_id: str):
    parts = [parse_profile(xml)]
    photos = _crop_photo_boxes(device, xml, parts[0].name or "match", phone_id)
    for _ in range(3):
        _scroll_profile(device)
        page = _xml(device, "profile-page")
        parsed = parse_profile(page)
        parts.append(parsed)
        photos.extend(
            _crop_photo_boxes(
                device,
                page,
                parsed.name or parts[0].name or "match",
                phone_id,
                start=len(photos),
            )
        )
    return merge_profiles(*parts), photos


def run_scan(*, serial: str | None = None, phone_id: str | None = None, open_first: bool = True) -> tuple[bool, str]:
    pid = live_account_id(phone_id)
    serial = hinge_live_serial(serial)
    blocked = pixel_blocked(pid, serial)
    if blocked:
        return False, blocked
    device, err = _unlock(serial)
    if device is None:
        return False, err
    xml = _open_matches(device)
    hits = parse_match_list(xml)
    if not hits:
        kind = classify_screen(xml)
        dump_name = ""
        try:
            DUMP_DIR.mkdir(parents=True, exist_ok=True)
            dump_name = str(DUMP_DIR / f"matches-empty-{time.strftime('%Y%m%d-%H%M%S')}.xml")
            Path(dump_name).write_text(xml or "", encoding="utf-8")
        except OSError:
            pass
        return False, (
            f"no Hinge matches parsed (screen={kind}, xml_chars={len(xml or '')}, dump={dump_name}). "
            "Open Matches on Galaxy and retry."
        )
    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    stored = 0
    try:
        for hit in hits:
            upsert_chat(
                conn,
                hit.name,
                preview=hit.preview,
                badge="unread" if hit.unread else "",
                last_text=hit.preview,
                last_from="them" if hit.unread else None,
                phone_id=pid,
            )
            stored += 1
        opened: list[str] = []
        if open_first:
            for hit in hits[:4]:
                name = _capture_open_hit(device, conn, hit, pid)
                if name:
                    opened.append(name)
                _back_to_matches(device)
        conn.commit()
    finally:
        conn.close()
    extra = f"; opened {', '.join(opened)}" if opened else ""
    return True, f"captured {stored} Hinge match(es) on {pid}{extra}"


def _back_to_matches(device) -> None:
    xml = _xml(device, "back")
    if classify_screen(xml) == "matches_list":
        return
    point = find_nav_point(xml, "Back") or find_nav_point(xml, "Matches")
    if point:
        tap(device, *point)
        time.sleep(0.7)


def _capture_open_hit(device, conn, hit, pid: str) -> str:
    tap(device, hit.x, hit.y)
    time.sleep(1.0)
    xml = _xml(device, "opened-match")
    if classify_screen(xml, expect_name=hit.name) not in {"match_chat", "match_profile"}:
        try:
            if device(text=hit.name).exists(timeout=1.5):
                device(text=hit.name).click()
                time.sleep(0.9)
                xml = _xml(device, "opened-match-retry")
        except Exception as exc:
            log.warning("name tap retry failed: %s", exc)
    tab = find_tab(xml, "Profile")
    if tab:
        tap(device, *tab)
        time.sleep(0.8)
        xml = _xml(device)
    profile, cropped = _collect_profile(device, xml, pid)
    name = profile.name or hit.name
    person_id = upsert_person(
        conn,
        name,
        phone_id=pid,
        age=profile.age,
        location=profile.location,
        job=profile.job,
        school=profile.school,
        about=profile.about,
        verified=profile.verified,
        gender=profile.gender,
        height=profile.height,
        extras_json=json.dumps(profile.extras) if profile.extras else None,
        ethnicity=profile.extras.get("ethnicity") if profile.extras else None,
        profile_pic=cropped[0][0] if cropped else None,
    )
    if profile.prompts:
        replace_prompts(conn, person_id, profile.prompts)
    if cropped:
        replace_photos(conn, person_id, cropped)
    name2, msgs = parse_open_thread(xml)
    chat_tab = find_tab(xml, "Chat")
    if chat_tab:
        tap(device, *chat_tab)
        time.sleep(0.6)
        name2, msgs = parse_open_thread(_xml(device))
    if msgs:
        replace_thread(conn, person_id, msgs)
    return name2 or name


def refresh_named(name: str, *, serial: str | None = None, phone_id: str | None = None) -> tuple[bool, str]:
    pid = live_account_id(phone_id)
    serial = hinge_live_serial(serial)
    blocked = pixel_blocked(pid, serial)
    if blocked:
        return False, blocked
    device, err = _unlock(serial)
    if device is None:
        return False, err
    xml = _open_matches(device)
    hits = parse_match_list(xml)
    hit = next((h for h in hits if h.name.casefold() == name.casefold()), None)
    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        for row in hits:
            upsert_chat(
                conn,
                row.name,
                preview=row.preview,
                badge="unread" if row.unread else "",
                last_text=row.preview,
                last_from="them" if row.unread else None,
                phone_id=pid,
            )
        opened = ""
        if hit:
            opened = _capture_open_hit(device, conn, hit, pid)
        conn.commit()
    finally:
        conn.close()
    if not hit:
        return False, f"{name} not visible on Matches; stored {len(hits)} list row(s)"
    return True, f"refreshed {opened or name} on {pid}"


def send_named_message(
    name: str,
    text: str,
    *,
    serial: str | None = None,
    phone_id: str | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    pid = live_account_id(phone_id)
    serial = hinge_live_serial(serial)
    blocked = pixel_blocked(pid, serial)
    if blocked:
        return False, blocked
    body = (text or "").strip()
    if not body:
        return False, "empty message"
    if not force:
        return False, "Hinge send is opt-in (pass force=true). Capture/drafts do not send DMs."
    device, err = _unlock(serial)
    if device is None:
        return False, err
    xml = _open_matches(device)
    hit = next((h for h in parse_match_list(xml) if h.name.casefold() == name.casefold()), None)
    if hit is None:
        return False, f"{name} not visible on Matches list"
    tap(device, hit.x, hit.y)
    time.sleep(0.9)
    xml = _xml(device)
    composer = find_composer(xml)
    send = find_send(xml)
    if not composer or not send:
        return False, "composer/send not parsed; not sending"
    from src.input_ime import type_into

    tap(device, *composer)
    time.sleep(0.3)
    type_into(device, body)
    time.sleep(0.3)
    tap(device, *send)
    return True, f"sent Hinge message to {name}"
