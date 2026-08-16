"""Human-like profile browsing before like/pass — varied per profile."""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import uiautomator2 as u2

from src.gestures import _adb_swipe, _clamp, _frac_to_px, _jitter, tap

log = logging.getLogger(__name__)

# Named browse styles and relative weights. One is sampled per profile.
_STYLES: dict[str, float] = {
    "photos_first": 0.28,   # gallery → bio
    "scroll_first": 0.18,   # bio → maybe photos
    "interleave": 0.18,     # mix taps and scrolls
    "photos_only": 0.12,    # barely open the bio
    "scroll_only": 0.12,    # glance at photo, dive into text
    "skim": 0.08,           # quick glance then decide
    "deep": 0.04,           # linger + reconsider
}


def _pause(lo: float, hi: float, label: str = "look") -> None:
    lo, hi = min(lo, hi), max(lo, hi)
    seconds = random.uniform(lo, hi)
    log.info("%s %.1fs", label, seconds)
    time.sleep(seconds)


def _pick_style(cfg: dict[str, Any]) -> str:
    custom = cfg.get("styles")
    weights = dict(_STYLES)
    if isinstance(custom, dict) and custom:
        weights = {str(k): float(v) for k, v in custom.items() if float(v) > 0}
    names = list(weights.keys())
    vals = [weights[n] for n in names]
    return random.choices(names, weights=vals, k=1)[0]


def _tap_photo(
    device: u2.Device,
    *,
    side: str,
    browse_cfg: dict[str, Any],
) -> None:
    """Tap left/right of the photo to flip gallery images (avoid bottom action buttons)."""
    jitter = float(browse_cfg.get("jitter", 0.02))
    # Vary tap height so it isn't always the same pixel band.
    y_base = float(browse_cfg.get("photo_tap_y", 0.42))
    y = _clamp(_jitter(y_base, 0.06), 0.28, 0.55)
    if side == "right":
        x = float(browse_cfg.get("photo_tap_x_right", 0.78))
        x = _clamp(_jitter(x, 0.04), 0.65, 0.90)
    else:
        x = float(browse_cfg.get("photo_tap_x_left", 0.22))
        x = _clamp(_jitter(x, 0.04), 0.10, 0.35)
    px, py = _frac_to_px(device, x, y, jitter)
    log.info("photo tap %s (%d,%d)", side, px, py)
    tap(device, px, py)


def _scroll_profile(
    device: u2.Device,
    browse_cfg: dict[str, Any],
    *,
    direction: str,
    amount: str | None = None,
) -> None:
    """
    Vertical drag inside the card to scroll bio/prompts.
    amount: short | medium | long — varies how far we drag.
    """
    jitter = float(browse_cfg.get("jitter", 0.02))
    x = float(browse_cfg.get("scroll_x", 0.50))
    amount = amount or random.choice(["short", "medium", "medium", "long"])

    # Different drag lengths (fractions of screen height).
    spans = {
        "short": (0.12, 0.22),
        "medium": (0.28, 0.40),
        "long": (0.45, 0.58),
    }
    span = random.uniform(*spans.get(amount, spans["medium"]))

    # Anchor the drag in slightly different bands of the card.
    if direction == "down":
        y1 = _clamp(random.uniform(0.62, 0.78), 0.55, 0.82)
        y2 = _clamp(y1 - span, 0.28, 0.70)
    else:
        y1 = _clamp(random.uniform(0.32, 0.48), 0.28, 0.55)
        y2 = _clamp(y1 + span, 0.40, 0.82)

    # Tiny horizontal drift only — must stay far from left/right edges.
    x = _clamp(_jitter(x, 0.05), 0.38, 0.62)
    x1, ya = _frac_to_px(device, x, y1, jitter * 0.5)
    x2, yb = _frac_to_px(device, x, y2, jitter * 0.5)
    x2 = x1 + random.randint(-12, 12)

    # Shorter distance → sometimes slower (careful read); long → quicker flick.
    if amount == "short":
        duration = random.randint(420, 780)
    elif amount == "long":
        duration = random.randint(220, 420)
    else:
        duration = random.randint(
            int(browse_cfg.get("scroll_duration_ms_min", 350)),
            int(browse_cfg.get("scroll_duration_ms_max", 650)),
        )

    log.info(
        "scroll %s/%s (%d,%d)->(%d,%d) %dms",
        direction,
        amount,
        x1,
        ya,
        x2,
        yb,
        duration,
    )
    try:
        _adb_swipe(device, x1, ya, x2, yb, duration)
    except Exception:
        device.swipe(x1, ya, x2, yb, duration=duration / 1000.0)


def _look_ranges(cfg: dict[str, Any], style: str) -> tuple[float, float, float, float]:
    look_min = float(cfg.get("look_min", 1.5))
    look_max = float(cfg.get("look_max", 4.0))
    read_min = float(cfg.get("read_min", 1.2))
    read_max = float(cfg.get("read_max", 3.5))

    if style == "skim":
        return look_min * 0.4, look_max * 0.55, read_min * 0.4, read_max * 0.55
    if style == "deep":
        return look_min * 1.1, look_max * 1.35, read_min * 1.15, read_max * 1.4
    if style == "photos_only":
        return look_min, look_max, read_min * 0.5, read_max * 0.7
    if style == "scroll_only":
        return look_min * 0.5, look_max * 0.7, read_min, read_max
    return look_min, look_max, read_min, read_max


def _photo_count(cfg: dict[str, Any], style: str) -> int:
    lo = int(cfg.get("photo_taps_min", 0))
    hi = int(cfg.get("photo_taps_max", 3))
    hi = max(lo, hi)
    if style == "skim":
        return random.choice([0, 0, 1])
    if style == "scroll_only":
        return random.choice([0, 0, 1])
    if style == "photos_only":
        return random.randint(max(lo, 1), hi)
    if style == "deep":
        return random.randint(max(lo, 2), hi + 1)
    if style == "photos_first":
        return random.randint(max(lo, 1), hi)
    return random.randint(lo, hi)


def _scroll_count(cfg: dict[str, Any], style: str) -> int:
    lo = int(cfg.get("scrolls_min", 1))
    hi = int(cfg.get("scrolls_max", 3))
    hi = max(lo, hi)
    if style == "skim":
        return random.choice([0, 0, 1])
    if style == "photos_only":
        return random.choice([0, 0, 1])
    if style == "scroll_only":
        return random.randint(max(lo, 1), hi)
    if style == "deep":
        return random.randint(max(lo, 2), hi + 1)
    if style == "scroll_first":
        return random.randint(max(lo, 1), hi)
    return random.randint(lo, hi)


def _do_photos(
    device: u2.Device,
    cfg: dict[str, Any],
    count: int,
    look_min: float,
    look_max: float,
) -> int:
    """Advance through gallery with varied pacing / occasional backtracks."""
    done = 0
    i = 0
    while i < count:
        # Rare hesitation: pause without tapping.
        if done > 0 and random.random() < 0.12:
            _pause(0.4, 1.2, "hesitate")

        _tap_photo(device, side="right", browse_cfg=cfg)
        done += 1
        i += 1

        # Variable look time; sometimes a quick flick to the next photo.
        if random.random() < 0.18 and i < count:
            _pause(0.25, 0.7, "quick glance")
        else:
            _pause(look_min * 0.65, look_max * 0.95, f"look photo {done + 1}")

        # Mid-gallery backtrack.
        if done >= 2 and random.random() < float(cfg.get("back_photo_chance", 0.25)):
            _tap_photo(device, side="left", browse_cfg=cfg)
            _pause(0.5, 1.6, "glance back")
            # Sometimes advance again (re-check).
            if random.random() < 0.45 and i < count:
                _tap_photo(device, side="right", browse_cfg=cfg)
                done += 1
                i += 1
                _pause(look_min * 0.5, look_max * 0.8, "re-check photo")

        # Occasional double-advance (skip a photo fast).
        if i < count and random.random() < 0.15:
            _tap_photo(device, side="right", browse_cfg=cfg)
            done += 1
            i += 1
            _pause(0.3, 0.9, "skip photo")

    return done


def _do_scrolls(
    device: u2.Device,
    cfg: dict[str, Any],
    count: int,
    read_min: float,
    read_max: float,
) -> int:
    """Scroll bio with mixed short/medium/long drags and read pauses."""
    done = 0
    for i in range(count):
        amount = random.choices(
            ["short", "medium", "long"],
            weights=[0.25, 0.50, 0.25],
            k=1,
        )[0]
        _scroll_profile(device, cfg, direction="down", amount=amount)
        done += 1

        # Short nudge sometimes gets a short read; long flick → longer settle.
        if amount == "short":
            _pause(read_min * 0.5, read_max * 0.75, f"skim section {i + 1}")
        elif amount == "long":
            _pause(read_min, read_max * 1.1, f"read section {i + 1}")
        else:
            _pause(read_min, read_max, f"read section {i + 1}")

        # Tiny corrective scroll up mid-profile.
        if random.random() < 0.18:
            _scroll_profile(device, cfg, direction="up", amount="short")
            _pause(0.4, 1.1, "nudge back")

    return done


def _build_plan(style: str, photo_n: int, scroll_n: int) -> list[str]:
    """Return an ordered list of 'photos' / 'scrolls' blocks for this style."""
    if style == "photos_first":
        plan = (["photos"] if photo_n else []) + (["scrolls"] if scroll_n else [])
    elif style == "scroll_first":
        plan = (["scrolls"] if scroll_n else []) + (["photos"] if photo_n else [])
    elif style == "photos_only":
        plan = ["photos"] if photo_n else (["scrolls"] if scroll_n else [])
    elif style == "scroll_only":
        plan = ["scrolls"] if scroll_n else (["photos"] if photo_n else [])
    elif style == "interleave":
        # Alternate single-step chunks: photo burst halves interleaved with scrolls.
        plan = []
        # Represent as fine-grained steps via markers consumed later.
        p_left, s_left = photo_n, scroll_n
        toggle = random.choice([True, False])  # start with photos or scrolls
        while p_left > 0 or s_left > 0:
            if toggle and p_left > 0:
                chunk = random.randint(1, min(2, p_left))
                plan.extend(["photo_one"] * chunk)
                p_left -= chunk
            elif not toggle and s_left > 0:
                plan.append("scroll_one")
                s_left -= 1
            elif p_left > 0:
                plan.append("photo_one")
                p_left -= 1
            elif s_left > 0:
                plan.append("scroll_one")
                s_left -= 1
            toggle = not toggle
        return plan
    else:  # skim / deep / default
        parts = []
        if photo_n:
            parts.append("photos")
        if scroll_n:
            parts.append("scrolls")
        random.shuffle(parts)
        plan = parts

    return plan or (["photos"] if photo_n else ["scrolls"] if scroll_n else [])


def browse_profile(device: u2.Device, browse_cfg: dict[str, Any] | None = None) -> None:
    """
    Mimic a person looking through a profile before deciding.

    Each profile samples a browse style so scroll/tap patterns aren't identical.
    """
    cfg = dict(browse_cfg or {})
    if not cfg.get("enabled", True):
        return

    style = _pick_style(cfg)
    look_min, look_max, read_min, read_max = _look_ranges(cfg, style)
    photo_n = _photo_count(cfg, style)
    scroll_n = _scroll_count(cfg, style)
    plan = _build_plan(style, photo_n, scroll_n)

    log.info(
        "browse:start style=%s photos=%d scrolls=%d plan=%s",
        style,
        photo_n,
        scroll_n,
        ",".join(plan) or "-",
    )

    # Opening beat — sometimes very short if scroll-first / skim.
    if style in {"scroll_first", "scroll_only", "skim"}:
        _pause(look_min * 0.5, look_max * 0.7, "glance cover")
    else:
        _pause(look_min, look_max, "look photo")

    photos_done = 0
    scrolls_done = 0
    photos_remaining = photo_n
    scrolls_remaining = scroll_n

    for step in plan:
        if step == "photos":
            n = photos_remaining
            photos_done += _do_photos(device, cfg, n, look_min, look_max)
            photos_remaining = 0
        elif step == "scrolls":
            n = scrolls_remaining
            scrolls_done += _do_scrolls(device, cfg, n, read_min, read_max)
            scrolls_remaining = 0
        elif step == "photo_one" and photos_remaining > 0:
            photos_done += _do_photos(device, cfg, 1, look_min, look_max)
            photos_remaining -= 1
        elif step == "scroll_one" and scrolls_remaining > 0:
            scrolls_done += _do_scrolls(device, cfg, 1, read_min, read_max)
            scrolls_remaining -= 1

    # End-of-profile reconsider behaviors (varied).
    reconsider_roll = random.random()
    back_chance = float(cfg.get("scroll_back_chance", 0.35))
    if scrolls_done > 0 and reconsider_roll < back_chance:
        _scroll_profile(
            device,
            cfg,
            direction="up",
            amount=random.choice(["short", "medium"]),
        )
        _pause(0.7, 2.2, "reconsider")
        # Sometimes dive back down after reconsidering.
        if random.random() < 0.3:
            _scroll_profile(device, cfg, direction="down", amount="short")
            _pause(0.5, 1.4, "second look")
    elif photos_done > 0 and reconsider_roll < back_chance + 0.15:
        # Jump back toward earlier photos instead of scrolling.
        backs = random.randint(1, min(2, photos_done))
        for _ in range(backs):
            _tap_photo(device, side="left", browse_cfg=cfg)
            _pause(0.4, 1.3, "flip back")

    # Rare idle stare with no gesture.
    if random.random() < 0.2:
        _pause(0.8, 2.5, "idle stare")

    _pause(
        float(cfg.get("pre_swipe_min", 0.4)),
        float(cfg.get("pre_swipe_max", 1.2)),
        "decide",
    )
    log.info(
        "browse:done style=%s photos=%d scrolls=%d",
        style,
        photos_done,
        scrolls_done,
    )
