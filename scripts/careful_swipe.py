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
import os
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
GATE_DIR = OUT / "gate"
GATE_DIR.mkdir(parents=True, exist_ok=True)
GATE_TIMEOUT = 900  # seconds to wait for human approval per card

SERIAL = "29081FDH200GZ8"
PKG = "com.bumblebff.app"
TARGET = int(os.environ.get("CAREFUL_TARGET", "10"))
# CAREFUL_GATE=0 -> no human approval gate (unattended run)
GATE = os.environ.get("CAREFUL_GATE", "1") != "0"


def wait_for_approval(i: int, decision: dict) -> bool:
    """Park before the swipe until card_XX.approve (or .reject) appears."""
    (GATE_DIR / f"card_{i:02d}.pending.json").write_text(json.dumps(decision, indent=2))
    approve = GATE_DIR / f"card_{i:02d}.approve"
    reject = GATE_DIR / f"card_{i:02d}.reject"
    log.info("  GATE: awaiting approval card_%02d (touch %s to approve)", i, approve)
    deadline = time.time() + GATE_TIMEOUT
    while time.time() < deadline:
        if approve.exists():
            log.info("  GATE: approved card %d", i)
            return True
        if reject.exists():
            log.error("  GATE: REJECTED card %d", i)
            return False
        time.sleep(2)
    log.error("  GATE: timeout waiting for approval card %d", i)
    return False


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


def capture_visible_card(d, shot, cfg, attempts: int = 4):
    """Screenshot -> visibility gate -> classify, all on the SAME frame.

    Never returns a classification of a frame that failed the visibility
    check (black/loading/mid-transition frames get retried, not classified).
    Returns (vis, vision) or (None, None) if no usable frame appeared.
    """
    for n in range(attempts):
        d.screenshot(str(shot))
        vis = sv.check_card_visible(shot, cfg)
        log.info("  capture %d/%d visible=%s (%s)", n + 1, attempts, vis["visible"], vis["reason"])
        if vis["visible"] == "yes":
            try:
                vision = sv.classify_card_image(shot, cfg)
            except Exception as exc:
                log.warning("  classify failed: %s", exc)
                vision = None
            if vision:
                return vis, vision
        if n < attempts - 1:
            # Harmless on a clean card; recovers scrolled views and gives
            # loading/transition frames time to settle.
            _scroll_to_top(d)
            time.sleep(1.5)
    return None, None


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

        # 1) Capture a visibility-gated frame and classify THAT exact image.
        # The vision model OCRs the name off the screenshot, so identity and
        # vision always refer to the same card. Loading/black/transition frames
        # are retried, never classified.
        shot = OUT / f"card_{done:02d}.jpg"
        vis, vision = capture_visible_card(d, shot, cfg)
        if vision is None:
            log.error("ABORT: no usable card frame after retries")
            return 2
        ident = vision.get("name") or _card_identity(d)
        log.info("  card=%s vision=%s", ident, vision)

        # Cross-check: hierarchy name should match the screenshot name. If not,
        # one of them is stale — re-capture once through the same gate.
        hier = _card_identity(d)
        vname = (vision.get("name") or "").lower()
        if vname and hier and vname not in hier.lower():
            log.info("  stale frame (hier=%s vs shot=%s) — re-capture", hier, vname)
            time.sleep(1.5)
            vis, vision = capture_visible_card(d, shot, cfg)
            if vision is None:
                log.error("ABORT: no usable card frame after re-capture")
                return 2
            ident = vision.get("name") or hier
            vname = (vision.get("name") or "").lower()
            hier2 = _card_identity(d)
            if vname and hier2 and vname not in hier2.lower():
                log.warning(
                    "  hierarchy still disagrees (hier=%s vs shot=%s) — trusting settled screenshot",
                    hier2, vname,
                )
            log.info("  re-shot card=%s vision=%s", ident, vision)

        like, reason = sv.decide_swipe(texts=[], vision=vision, cfg=cfg)
        text_gender = vision.get("gender")
        log.info("  decision: %s -> %s (%s)", vision, "LIKE" if like else "PASS", reason)

        # Second opinion before any male LIKE: the audit caught male ethnicity
        # misclassifications (e.g. a Black man labeled white -> wrong LIKE).
        # Re-classify a fresh frame of the SAME card; if it flips the decision,
        # pass instead and log the flip for review.
        flipped = False
        if like and vision.get("gender") == "male":
            time.sleep(0.8)
            d.screenshot(str(shot))
            vis2 = sv.check_card_visible(shot, cfg)
            if vis2["visible"] == "yes":
                try:
                    vision2 = sv.classify_card_image(shot, cfg)
                except Exception:
                    vision2 = None
                if vision2:
                    name2 = (vision2.get("name") or "").lower()
                    if name2 and vname and name2 == vname:
                        like2, reason2 = sv.decide_swipe(texts=[], vision=vision2, cfg=cfg)
                        log.info("  2nd opinion: %s -> %s", vision2, "LIKE" if like2 else "PASS")
                        if not like2:
                            like = False
                            reason = f"2nd-opinion flip ({reason2}; 1st={vision})"
                            vision = vision2
                            flipped = True
                    else:
                        log.info("  2nd opinion name mismatch (%s vs %s) — keeping 1st decision", name2, vname)

        results.append(
            {
                "i": done,
                "identity": ident,
                "visible": vis,
                "vision": vision,
                "text_gender": text_gender,
                "like": like,
                "reason": reason,
                "flipped_by_second_opinion": flipped,
                "shot": shot.name,
            }
        )

        # 3) Human audit gate — only when GATE is on.
        if GATE:
            if not wait_for_approval(done, results[-1]):
                log.error("ABORT: card %d not approved", done)
                (OUT / "results.json").write_text(json.dumps(results, indent=2))
                return 3

        # 4) swipe + verify advance. Compare the NEXT card's screenshot name
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
            new_name = ""
            try:
                pv_vis = sv.check_card_visible(probe, cfg)
                if pv_vis["visible"] == "yes":
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
    likes = sum(1 for r in results if r["like"])
    flips = sum(1 for r in results if r.get("flipped_by_second_opinion"))
    log.info("summary: %d likes, %d passes, %d second-opinion flips", likes, done - likes, flips)
    (OUT / "results.json").write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
