"""Main swipe loop: like/pass with stop conditions."""

from __future__ import annotations

import argparse
import logging
import random
import sys
from pathlib import Path

from src.browse import browse_profile
from src.config import load_config
from src.device import bring_app_foreground, connect, current_package, dump_hierarchy, wait_idle
from src.gestures import sleep_between_swipes, swipe, tap
from src.screen import STOP_KINDS, ScreenKind, classify, find_dismiss_point, find_tab_point

log = logging.getLogger(__name__)


def _screen_size(device) -> tuple[int, int]:
    info = device.info
    return int(info["displayWidth"]), int(info["displayHeight"])


def _read_state(device, package: str):
    wait_idle(device, 0.5)
    xml = dump_hierarchy(device)
    pkg = current_package(device, xml)
    return classify(pkg, xml, expected_package=package), xml


def dismiss_match(device, xml: str) -> bool:
    point = find_dismiss_point(xml, _screen_size(device))
    if point is None:
        return False
    tap(device, point[0], point[1])
    wait_idle(device, 1.0)
    return True


def go_to_people(device, xml: str) -> bool:
    point = find_tab_point(xml, "People")
    if point is None:
        # Fallback: center bottom nav on 1280-wide screens (~5 tabs).
        width, height = _screen_size(device)
        point = (width // 2, int(height * 0.96))
        log.info("People tab not found in hierarchy; tapping fallback %s", point)
    else:
        log.info("navigate People tab @ %s", point)
    tap(device, point[0], point[1])
    wait_idle(device, 1.5)
    return True


def run_session(cfg: dict, serial: str | None = None) -> int:
    package = str(cfg["package"])
    max_swipes = int(cfg["max_swipes"])
    like_ratio = float(cfg["like_ratio"])
    delay_min = float(cfg["delay_min"])
    delay_max = float(cfg["delay_max"])
    swipe_cfg = dict(cfg["swipe"])

    device = connect(serial)
    if cfg.get("bring_to_foreground", True):
        bring_app_foreground(device, package)

    swipes = 0
    likes = 0
    passes = 0
    matches_dismissed = 0
    nav_attempts = 0

    log.info(
        "session start max_swipes=%d like_ratio=%.2f delay=%.1f–%.1fs package=%s",
        max_swipes,
        like_ratio,
        delay_min,
        delay_max,
        package,
    )

    try:
        while swipes < max_swipes:
            state, xml = _read_state(device, package)
            log.info(
                "screen=%s package=%s reason=%s",
                state.kind.value,
                state.package,
                state.reason or "-",
            )

            if state.kind in {ScreenKind.CHATS, ScreenKind.OTHER_TAB}:
                if nav_attempts >= 3:
                    log.warning("stop:could_not_reach_people")
                    return 2
                nav_attempts += 1
                go_to_people(device, xml)
                continue

            if state.kind == ScreenKind.MATCH:
                log.info("dismiss_match")
                if not dismiss_match(device, xml):
                    log.warning("stop:match_undismissable")
                    return 2
                matches_dismissed += 1
                continue

            if state.kind in STOP_KINDS:
                log.warning("stop:%s (%s)", state.kind.value, state.reason or "detected")
                return 2

            if state.kind != ScreenKind.CARD:
                log.warning("stop:unexpected (%s)", state.kind.value)
                return 2

            nav_attempts = 0
            browse_profile(device, cfg.get("browse") or {})
            # Re-check after browsing — scrolling can hit empty/paywall overlays rarely.
            state_after, xml_after = _read_state(device, package)
            if state_after.kind == ScreenKind.MATCH:
                log.info("dismiss_match")
                if not dismiss_match(device, xml_after):
                    log.warning("stop:match_undismissable")
                    return 2
                matches_dismissed += 1
                continue
            if state_after.kind in STOP_KINDS:
                log.warning(
                    "stop:%s (%s)",
                    state_after.kind.value,
                    state_after.reason or "after browse",
                )
                return 2
            if state_after.kind in {ScreenKind.CHATS, ScreenKind.OTHER_TAB}:
                nav_attempts += 1
                go_to_people(device, xml_after)
                continue

            do_like = random.random() < like_ratio
            swipe(device, swipe_cfg, like=do_like)
            swipes += 1
            if do_like:
                likes += 1
                log.info("action=like count=%d/%d", swipes, max_swipes)
            else:
                passes += 1
                log.info("action=pass count=%d/%d", swipes, max_swipes)

            wait_idle(device, 0.9)
            post, post_xml = _read_state(device, package)
            if post.kind == ScreenKind.MATCH:
                log.info("dismiss_match")
                if dismiss_match(device, post_xml):
                    matches_dismissed += 1
                else:
                    log.warning("stop:match_undismissable")
                    return 2
            elif post.kind in {
                ScreenKind.PAYWALL,
                ScreenKind.VERIFY,
                ScreenKind.EMPTY,
                ScreenKind.PERMISSION,
                ScreenKind.NOT_BUMBLE,
            }:
                log.warning("stop:%s (%s)", post.kind.value, post.reason or "detected")
                return 2

            if swipes < max_swipes:
                sleep_between_swipes(delay_min, delay_max)

    except KeyboardInterrupt:
        log.info("interrupted (Ctrl+C)")
        return 130

    log.info(
        "session done swipes=%d likes=%d passes=%d matches_dismissed=%d",
        swipes,
        likes,
        passes,
        matches_dismissed,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bumble Friends ADB auto-swiper")
    parser.add_argument("--serial", help="ADB device serial (optional)")
    parser.add_argument("--config", type=Path, help="Path to config.yaml")
    parser.add_argument("--max-swipes", type=int, help="Override max swipes for this session")
    parser.add_argument(
        "--like-ratio",
        type=float,
        help="Probability of liking each card (0.0–1.0)",
    )
    parser.add_argument("--delay-min", type=float, help="Min delay between swipes (seconds)")
    parser.add_argument("--delay-max", type=float, help="Max delay between swipes (seconds)")
    parser.add_argument(
        "--no-foreground",
        action="store_true",
        help="Do not bring Bumble to the foreground at start",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)
    if args.max_swipes is not None:
        cfg["max_swipes"] = args.max_swipes
    if args.like_ratio is not None:
        if not 0.0 <= args.like_ratio <= 1.0:
            parser.error("--like-ratio must be between 0.0 and 1.0")
        cfg["like_ratio"] = args.like_ratio
    if args.delay_min is not None:
        cfg["delay_min"] = args.delay_min
    if args.delay_max is not None:
        cfg["delay_max"] = args.delay_max
    if args.no_foreground:
        cfg["bring_to_foreground"] = False

    if float(cfg["delay_min"]) > float(cfg["delay_max"]):
        parser.error("delay_min must be <= delay_max")

    return run_session(cfg, serial=args.serial)


if __name__ == "__main__":
    sys.exit(main())
