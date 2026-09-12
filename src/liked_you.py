"""Liked You tab: parse the list, classify each preview, then Like / Not for me."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from src.device import bring_app_foreground, connect, current_package, dump_hierarchy, wait_idle
from src.gestures import tap
from src.screen import ScreenKind, _bounds_center, classify, find_tab_point
from src.swipe_vision import evaluate_card
from src.unlock import wake_and_unlock

log = logging.getLogger(__name__)

_CHECK_OUT = re.compile(r"check\s+out\s+(.+?)(?:['’]s)?\s+profile", re.I)
_NAME_AGE = re.compile(r"^(.+?),\s*(\d{2})$")


@dataclass(frozen=True)
class LikedYouHit:
    name: str
    age: int | None
    x: int
    y: int
    bounds: str
    key: str


def _parse_bounds(bounds: str) -> tuple[int, int, int, int] | None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not m:
        return None
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


def parse_liked_you_list(xml: str) -> list[LikedYouHit]:
    """Visible Liked You tiles, top to bottom."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []

    hits: list[LikedYouHit] = []
    seen: set[str] = set()
    for node in root.iter():
        desc = (node.attrib.get("content-desc") or "").strip()
        m = _CHECK_OUT.search(desc)
        if not m:
            continue
        name = m.group(1).strip()
        if not name:
            continue
        clickable = node
        parent_bounds = node.attrib.get("bounds") or ""
        # Prefer the clickable ancestor that wraps the tile.
        # Walk is implicit: if this node isn't clickable, use its own bounds;
        # the dump puts clickable on the parent one level up.
        bounds = parent_bounds
        age = None
        # Sibling "Name, 26" lives under the same clickable wrapper in practice.
        clickable = node
        # Use nearest clickable ancestor bounds by scanning parents via tree walk:
        # ElementTree has no parent pointer, so find a clickable node that contains us.
        box = _parse_bounds(bounds)
        if box is None:
            continue
        x1, y1, x2, y2 = box
        for other in root.iter():
            if (other.attrib.get("clickable") or "").lower() != "true":
                continue
            ob = _parse_bounds(other.attrib.get("bounds") or "")
            if not ob:
                continue
            if ob[0] <= x1 and ob[1] <= y1 and ob[2] >= x2 and ob[3] >= y2:
                # Prefer the tightest clickable that still contains the desc node
                # and is not the whole screen.
                ow, oh = ob[2] - ob[0], ob[3] - ob[1]
                if ow < 200 or oh < 80:
                    continue
                if ow > 2000:
                    continue
                bounds = other.attrib.get("bounds") or bounds
                clickable = other
        for child in clickable.iter():
            text = (child.attrib.get("text") or "").strip()
            am = _NAME_AGE.match(text)
            if am:
                name = am.group(1).strip() or name
                age = int(am.group(2))
                break
        center = _bounds_center(bounds)
        if center is None:
            continue
        key = f"{name.casefold()}|{age or ''}"
        if key in seen:
            continue
        seen.add(key)
        hits.append(
            LikedYouHit(
                name=name,
                age=age,
                x=center[0],
                y=center[1],
                bounds=bounds,
                key=key,
            )
        )
    hits.sort(key=lambda h: h.y)
    return hits


def find_liked_you_action(xml: str, *, like: bool) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    want_rid = "myProfilePreview_rightButton" if like else "myProfilePreview_leftButton"
    want_label = "like" if like else "not for me"
    for node in root.iter():
        rid = (node.attrib.get("resource-id") or "").rsplit("/", 1)[-1]
        text = (node.attrib.get("text") or "").strip().lower()
        desc = (node.attrib.get("content-desc") or "").strip().lower()
        if rid == want_rid or text == want_label or desc == want_label:
            return _bounds_center(node.attrib.get("bounds") or "")
    return None


def go_to_liked_you(device, xml: str | None = None) -> bool:
    xml = xml if xml is not None else dump_hierarchy(device)
    point = find_tab_point(xml, "Liked You")
    if point is None:
        info = device.info
        width, height = int(info["displayWidth"]), int(info["displayHeight"])
        # 4th of 5 tabs.
        point = (int(width * 0.70), int(height * 0.96))
        log.info("Liked You tab not found; fallback %s", point)
    else:
        log.info("navigate Liked You @ %s", point)
    tap(device, point[0], point[1])
    wait_idle(device, 1.4)
    return True


def _read(device, package: str):
    wait_idle(device, 0.3)
    xml = dump_hierarchy(device)
    pkg = current_package(device, xml)
    return classify(pkg, xml, expected_package=package), xml


def _dismiss_preview(device, package: str) -> None:
    try:
        device.press("back")
    except Exception:
        info = device.info
        tap(device, int(info["displayWidth"]) // 2, int(int(info["displayHeight"]) * 0.12))
    wait_idle(device, 0.8)
    state, xml = _read(device, package)
    if state.kind == ScreenKind.LIKED_YOU_CARD:
        try:
            device.press("back")
        except Exception:
            pass
        wait_idle(device, 0.6)


def _unlock(cfg: dict, serial: str | None):
    package = str(cfg["package"])
    device = connect(serial)
    unlocked = False
    for attempt in range(4):
        try:
            unlocked = wake_and_unlock(device, serial=serial)
            break
        except Exception as exc:
            log.warning("unlock attempt %d dropped (%s)", attempt + 1, exc)
            time.sleep(2)
            try:
                device = connect(serial)
            except Exception:
                time.sleep(2)
    if not unlocked:
        return None, "phone still locked — unlock failed"
    if cfg.get("bring_to_foreground", True):
        bring_app_foreground(device, package)
    return device, ""


def run_scan(cfg: dict, serial: str | None = None) -> tuple[bool, str]:
    """Open Liked You, classify each visible preview, fill the desk review list."""
    from src.phone_queue import check_cancel
    from src.swipe_desk import (
        liked_you_add,
        liked_you_begin_scan,
        liked_you_finish,
        liked_you_set_message,
        save_liked_thumb,
    )

    phone_id = str(cfg.get("phone_id") or "toby")
    package = str(cfg["package"])
    liked_you_begin_scan(phone_id=phone_id)
    device, err = _unlock(cfg, serial)
    if device is None:
        liked_you_finish(err)
        return False, err

    try:
        state, xml = _read(device, package)
        if state.kind == ScreenKind.PAYWALL:
            liked_you_finish("paywall on Liked You")
            return False, "paywall"
        if state.kind != ScreenKind.LIKED_YOU:
            go_to_liked_you(device, xml)
            state, xml = _read(device, package)
        if state.kind == ScreenKind.PAYWALL:
            liked_you_finish("paywall on Liked You")
            return False, "paywall"
        if state.kind != ScreenKind.LIKED_YOU:
            liked_you_finish(f"could not open Liked You ({state.kind.value})")
            return False, f"not liked you: {state.kind.value}"

        seen: set[str] = set()
        stagnant = 0
        while stagnant < 3:
            check_cancel()
            state, xml = _read(device, package)
            if state.kind == ScreenKind.PAYWALL:
                liked_you_add(
                    {
                        "id": f"paywall-{len(seen)}",
                        "name": "?",
                        "proposed": "skip",
                        "decision": "skip",
                        "status": "skipped",
                        "skip_reason": "paywall",
                        "reason": "paywall",
                    }
                )
                break
            if state.kind != ScreenKind.LIKED_YOU:
                go_to_liked_you(device, xml)
                continue
            hits = parse_liked_you_list(xml)
            new_hits = [h for h in hits if h.key not in seen]
            if not new_hits:
                info = device.info
                w, h = int(info["displayWidth"]), int(info["displayHeight"])
                # Scroll the list.
                try:
                    device.swipe(w // 2, int(h * 0.78), w // 2, int(h * 0.38), 0.4)
                except Exception:
                    pass
                wait_idle(device, 0.8)
                stagnant += 1
                continue
            stagnant = 0
            for hit in new_hits:
                check_cancel()
                seen.add(hit.key)
                liked_you_set_message(f"Opening {hit.name}")
                tap(device, hit.x, hit.y)
                wait_idle(device, 1.2)
                state, xml = _read(device, package)
                if state.kind == ScreenKind.PAYWALL:
                    liked_you_add(
                        {
                            "id": hit.key,
                            "name": hit.name,
                            "age": hit.age,
                            "proposed": "skip",
                            "decision": "skip",
                            "status": "skipped",
                            "skip_reason": "paywall",
                            "reason": "paywall",
                        }
                    )
                    _dismiss_preview(device, package)
                    continue
                if state.kind != ScreenKind.LIKED_YOU_CARD:
                    liked_you_add(
                        {
                            "id": hit.key,
                            "name": hit.name,
                            "age": hit.age,
                            "proposed": "skip",
                            "decision": "skip",
                            "status": "skipped",
                            "skip_reason": "unopenable",
                            "reason": f"opened as {state.kind.value}",
                        }
                    )
                    _dismiss_preview(device, package)
                    continue
                like, reason, meta = evaluate_card(device, list(state.texts), cfg)
                vision = dict(meta.get("vision") or {})
                proposed = "like" if like else "pass"
                thumb = ""
                try:
                    from src.swipe_vision import screenshot_card

                    shot = screenshot_card(device)
                    thumb = save_liked_thumb(shot, hit.key)
                    shot.unlink(missing_ok=True)
                except Exception:
                    log.debug("liked you thumb skip", exc_info=True)
                liked_you_add(
                    {
                        "id": hit.key,
                        "name": vision.get("name") or hit.name,
                        "age": hit.age,
                        "gender": vision.get("gender") or "",
                        "ethnicity": vision.get("ethnicity") or "",
                        "crazy": vision.get("crazy") or "no",
                        "proposed": proposed,
                        "decision": proposed,
                        "reason": reason,
                        "status": "pending",
                        "thumb": thumb,
                    }
                )
                _dismiss_preview(device, package)
        liked_you_finish(f"scanned {len(seen)} Liked You")
        return True, f"scanned {len(seen)}"
    except Exception as exc:
        from src.phone_queue import QueueCancelled

        if isinstance(exc, QueueCancelled):
            liked_you_finish("stopped")
            raise
        log.exception("liked you scan failed")
        liked_you_finish(str(exc))
        return False, str(exc)


def run_go(cfg: dict, serial: str | None = None) -> tuple[bool, str]:
    """Apply desk decisions (like/pass) in list order on the Liked You tab."""
    from src.phone_queue import check_cancel
    from src.swipe_desk import liked_you_finish, liked_you_items, liked_you_mark, liked_you_set_running

    phone_id = str(cfg.get("phone_id") or "toby")
    package = str(cfg["package"])
    items = [i for i in liked_you_items() if i.get("decision") in {"like", "pass"} and i.get("status") == "pending"]
    liked_you_set_running(phone_id=phone_id, message=f"Going through {len(items)}")
    device, err = _unlock(cfg, serial)
    if device is None:
        liked_you_finish(err)
        return False, err

    done = 0
    try:
        state, xml = _read(device, package)
        if state.kind != ScreenKind.LIKED_YOU:
            go_to_liked_you(device, xml)

        for item in items:
            check_cancel()
            name = str(item.get("name") or "")
            decision = str(item.get("decision") or "pass")
            liked_you_set_running(phone_id=phone_id, message=f"{decision} {name}")
            state, xml = _read(device, package)
            if state.kind == ScreenKind.PAYWALL:
                liked_you_mark(item["id"], status="skipped", skip_reason="paywall")
                continue
            if state.kind != ScreenKind.LIKED_YOU:
                go_to_liked_you(device, xml)
                state, xml = _read(device, package)
            hit = _find_hit(xml, name, item.get("id") or "")
            if hit is None:
                # After each action the next person is often the new top card.
                hits = parse_liked_you_list(xml)
                hit = hits[0] if hits else None
            if hit is None:
                liked_you_mark(item["id"], status="error", reason="not on Liked You list")
                continue
            tap(device, hit.x, hit.y)
            wait_idle(device, 1.1)
            state, xml = _read(device, package)
            if state.kind == ScreenKind.PAYWALL:
                liked_you_mark(item["id"], status="skipped", skip_reason="paywall")
                _dismiss_preview(device, package)
                continue
            if state.kind != ScreenKind.LIKED_YOU_CARD:
                liked_you_mark(item["id"], status="error", reason=f"opened as {state.kind.value}")
                _dismiss_preview(device, package)
                continue
            point = find_liked_you_action(xml, like=decision == "like")
            if point is None:
                liked_you_mark(item["id"], status="error", reason="no Like/Not for me")
                _dismiss_preview(device, package)
                continue
            tap(device, point[0], point[1])
            wait_idle(device, 1.2)
            state, xml = _read(device, package)
            if state.kind == ScreenKind.LIKE_CONFIRM:
                from src.swiper import confirm_like_prompt

                confirm_like_prompt(device, xml)
                wait_idle(device, 0.8)
                state, xml = _read(device, package)
            if state.kind == ScreenKind.MATCH:
                from src.swiper import dismiss_match

                dismiss_match(device, xml)
                wait_idle(device, 0.8)
            liked_you_mark(item["id"], status="done")
            done += 1
            if state.kind == ScreenKind.LIKED_YOU_CARD:
                _dismiss_preview(device, package)
        liked_you_finish(f"applied {done}/{len(items)}")
        return True, f"applied {done}/{len(items)}"
    except Exception as exc:
        from src.phone_queue import QueueCancelled

        if isinstance(exc, QueueCancelled):
            liked_you_finish("stopped")
            raise
        log.exception("liked you go failed")
        liked_you_finish(str(exc))
        return False, str(exc)


def _find_hit(xml: str, name: str, key: str) -> LikedYouHit | None:
    want = (name or "").strip().casefold()
    key_cf = (key or "").casefold()
    for hit in parse_liked_you_list(xml):
        if key_cf and hit.key.casefold() == key_cf:
            return hit
        if want and hit.name.casefold() == want:
            return hit
    return None
