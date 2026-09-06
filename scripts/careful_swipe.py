#!/usr/bin/env python3
"""Careful, self-auditing swipe run.

For each card:
  1. Confirm we're on a clean card screen (dismiss overlays/popups).
  2. Screenshot and ask the vision model if the card is clearly VISIBLE.
     If not visible after a couple of retries -> abort (don't swipe blind).
  3. Get the vision decision (gender/ethnicity/crazy) and apply the rules.
  4. Save the screenshot + decision for review.
  5. Swipe, then verify the card actually advanced (retry, else abort).

Aborts (exit 2) with a clear reason if anything looks wrong, rather than
swiping on a card it can't see or that didn't move.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import uiautomator2 as u2

from src.config import load_config
from src import swipe_vision as sv
from src.gestures import swipe
from src.screen import ScreenKind, classify, find_dismiss_point, find_tab_point
from src.swiper import _card_identity, _screen_size

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("careful")

OUT = Path("/app/data/careful_run")
OUT.mkdir(parents=True, exist_ok=True)

SERIAL = "29081FDH200GZ8"
PKG = "com.bumblebff.app"
TARGET = 10


def tap_point(d, pt):
    d.click(pt[0], pt[1])


def clean_screen(d) -> str:
    """Dismiss popups/overlays and land on a card. Returns screen kind."""
    for _ in range(6):
        xml = d.dump_hierarchy()
        st = classify(PKG, xml, expected_package=PKG)
        kind = st.kind.value
        if kind == "card":
            return kind
        if st.kind == ScreenKind.MATCH:
            pt = find_dismiss_point(xml, _screen_size(d))
            log.info("dismiss overlay/match @ %s", pt)
            if pt:
                tap_point(d, pt)
                time.sleep(1.2)
                continue
        if kind in {"chats", "other_tab"}:
            pt = find_tab_point(xml, "People") or (540, 2304)
            log.info("nav to People @ %s", pt)
            tap_point(d, pt)
            time.sleep(2.0)
            continue
        # loading/unknown/etc — wait and retry
        log.info("screen=%s — waiting", kind)
        time.sleep(1.5)
    return classify(PKG, d.dump_hierarchy(), expected_package=PKG).kind.value


def _scroll_to_top(d) -> None:
    """Scroll the profile back to the top so the main photo is fully visible."""
    w, h = d.window_size()
    for _ in range(3):
        # drag downward to scroll content up to the top
        d.swipe(int(w * 0.5), int(h * 0.25), int(w * 0.5), int(h * 0.75), 0.25)
        time.sleep(0.4)


def _wait_for_settled_card(d, timeout: float = 6.0) -> str:
    """Wait until the card identity is stable across two reads (animation done)."""
    deadline = time.time() + timeout
    last = ""
    stable = 0
    while time.time() < deadline:
        cur = _card_identity(d)
        if cur and cur == last:
            stable += 1
            if stable >= 2:
                return cur
        else:
            stable = 0
        last = cur
        time.sleep(0.5)
    return last


def main() -> int:
    cfg = load_config()
    # vision rules
    f = dict(cfg.get("filters") or {})
    v = dict(f.get("swipe_vision") or {})
    v.update(
        {
            "enabled": True,
            "men_include": ["white", "east_asian", "southeast_asian"],
            "men_exclude": ["black", "south_asian"],
            "men_if_missing": "pass",
        }
    )
    f["swipe_vision"] = v
    cfg["filters"] = f

    swipe_cfg = dict(cfg["swipe"])
    d = u2.connect(SERIAL)
    log.info("connected %s", d.window_size())

    results = []
    done = 0
    while done < TARGET:
        # Dismiss overlays / navigate back toward the card stack, but treat the
        # vision visibility check (below) as the real gate for "is this a usable
        # card" — the XML classifier is brittle across Bumble layouts.
        clean_screen(d)

        # 1) Screenshot FIRST, then classify that exact frame. The vision model
        # OCRs the name off the screenshot, so identity and vision always refer
        # to the same card. (Reading identity from a fresh hierarchy dump races
        # the render and can return the NEXT card while the screenshot shows the
        # previous one.)
        shot = OUT / f"card_{done:02d}.jpg"
        d.screenshot(str(shot))
        vis = sv.check_card_visible(shot, cfg)
        log.info("card %d visible=%s (%s)", done, vis["visible"], vis["reason"])
        if vis["visible"] != "yes":
            # Could be mid-animation or stuck in a scrolled/expanded view.
            log.info("  not visible (%s) — trying to recover card view", vis["reason"])
            _scroll_to_top(d)
            time.sleep(1.2)
            d.screenshot(str(shot))
            vis = sv.check_card_visible(shot, cfg)
            log.info("  after scroll-to-top visible=%s (%s)", vis["visible"], vis["reason"])
            if vis["visible"] != "yes":
                time.sleep(1.5)
                d.screenshot(str(shot))
                vis = sv.check_card_visible(shot, cfg)
                log.info("  retry visible=%s (%s)", vis["visible"], vis["reason"])
                if vis["visible"] != "yes":
                    log.error("ABORT: model cannot see the card clearly: %s", vis["reason"])
                    return 2

        # 2) Classify the SAME screenshot. Name comes from the image itself.
        try:
            vision = sv.classify_card_image(shot, cfg)
        except Exception as exc:
            log.error("ABORT: vision classify failed: %s", exc)
            return 2
        ident = vision.get("name") or _card_identity(d)
        log.info("  card=%s vision=%s", ident, vision)

        # Cross-check: hierarchy name should match the screenshot name. If not,
        # the frame is stale — re-screenshot once.
        hier = _card_identity(d)
        vname = (vision.get("name") or "").lower()
        if vname and hier and vname not in hier.lower():
            log.info("  stale frame (hier=%s vs shot=%s) — re-screenshot", hier, vname)
            time.sleep(1.2)
            d.screenshot(str(shot))
            vision = sv.classify_card_image(shot, cfg)
            ident = vision.get("name") or hier
            log.info("  re-shot card=%s vision=%s", ident, vision)

        like, reason = sv.decide_swipe(texts=[], vision=vision, cfg=cfg)
        text_gender = vision.get("gender")
        log.info("  decision: %s -> %s (%s)", vision, "LIKE" if like else "PASS", reason)

        results.append(
            {
                "i": done,
                "identity": ident,
                "visible": vis,
                "vision": vision,
                "text_gender": text_gender,
                "like": like,
                "reason": reason,
                "shot": shot.name,
            }
        )

        # 3) swipe + verify advance. Compare the NEXT card's screenshot name
        # against the one we just swiped — screenshots are the reliable signal.
        swipe(d, swipe_cfg, like=like)
        advanced = False
        for attempt in range(6):
            time.sleep(1.4)
            k = classify(PKG, d.dump_hierarchy(), expected_package=PKG).kind.value
            if k != "card":
                advanced = True  # moved to match/loading/etc
                break
            probe = OUT / "_probe.jpg"
            d.screenshot(str(probe))
            try:
                pv = sv.classify_card_image(probe, cfg)
                new_name = (pv.get("name") or "").lower()
            except Exception:
                new_name = ""
            if new_name and vname and new_name != vname:
                advanced = True
                break
            # fallback to hierarchy identity compare
            new_id = _card_identity(d)
            if new_id and ident and new_id != ident:
                advanced = True
                break
            log.info("  swipe did not advance (attempt %d) next=%s; retry", attempt + 1, new_name or new_id)
            swipe(d, swipe_cfg, like=like)
        if not advanced:
            log.error("ABORT: card stuck after retries (id=%s)", ident)
            return 2

        done += 1
        log.info("  action=%s count=%d/%d", "like" if like else "pass", done, TARGET)
        (OUT / "results.json").write_text(json.dumps(results, indent=2))
        time.sleep(1.0)

    log.info("DONE %d cards", done)
    (OUT / "results.json").write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
