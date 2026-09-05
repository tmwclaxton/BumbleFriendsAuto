#!/usr/bin/env python3
"""Swipe until 200 decisions with vision ethnicity filter. Fast browse."""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path

from src.config import load_config
from src.swiper import run_session

TARGET = 200
progress_path = Path("/app/data/swipe_200_progress.json")
log_path = Path("/app/data/swipe_200.log")
stdout_path = Path("/app/data/swipe_200_stdout.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path, encoding="utf-8"),
    ],
)
log = logging.getLogger("swipe200")


def _count_actions(text: str) -> tuple[int, int, int]:
    likes = len(re.findall(r"action=like count=", text))
    passes = len(re.findall(r"action=pass count=", text))
    return likes + passes, likes, passes


def load_progress() -> dict:
    if progress_path.exists():
        try:
            return json.loads(progress_path.read_text())
        except Exception:
            pass
    if stdout_path.exists():
        total, likes, passes = _count_actions(stdout_path.read_text(errors="replace"))
        return {"total": total, "likes": likes, "passes": passes, "rounds": 0}
    return {"total": 0, "likes": 0, "passes": 0, "rounds": 0}


def main() -> int:
    prog = load_progress()
    total = int(prog.get("total") or 0)
    likes = int(prog.get("likes") or 0)
    passes = int(prog.get("passes") or 0)
    rounds = int(prog.get("rounds") or 0)
    log.info("resuming from progress total=%s", total)
    log.info("begin swipe marathon target=%s already=%s", TARGET, total)

    while total < TARGET:
        rounds += 1
        remaining = TARGET - total
        cfg = load_config()
        cfg["max_swipes"] = remaining
        cfg["like_ratio"] = 1.0
        cfg["delay_min"] = 0.8
        cfg["delay_max"] = 1.8
        browse = dict(cfg.get("browse") or {})
        browse.update(
            {
                "enabled": True,
                "look_min": 0.35,
                "look_max": 0.9,
                "photo_taps_min": 0,
                "photo_taps_max": 1,
                "scrolls_min": 0,
                "scrolls_max": 1,
                "read_min": 0.25,
                "read_max": 0.7,
                "pre_swipe_min": 0.15,
                "pre_swipe_max": 0.4,
            }
        )
        cfg["browse"] = browse
        filt = dict(cfg.get("filters") or {})
        vision = dict(filt.get("swipe_vision") or {})
        vision.update(
            {
                "enabled": True,
                "men_include": ["white", "east_asian", "southeast_asian"],
                "men_exclude": ["black", "south_asian"],
                "men_if_missing": "pass",
            }
        )
        filt["swipe_vision"] = vision
        cfg["filters"] = filt
        log.info("round %s remaining=%s", rounds, remaining)
        before_len = log_path.stat().st_size if log_path.exists() else 0
        try:
            rc = run_session(cfg)
        except Exception:
            log.exception("run_session crashed")
            rc = 99
        text = log_path.read_text(errors="replace")[before_len:]
        match = re.search(r"session done swipes=(\d+) likes=(\d+) passes=(\d+)", text)
        if match:
            rs, rl, rp = int(match.group(1)), int(match.group(2)), int(match.group(3))
        else:
            rs = len(re.findall(r"action=(?:like|pass) count=", text))
            rl = len(re.findall(r"action=like count=", text))
            rp = len(re.findall(r"action=pass count=", text))
        total += rs
        likes += rl
        passes += rp
        progress = {
            "total": total,
            "target": TARGET,
            "likes": likes,
            "passes": passes,
            "rounds": rounds,
            "last_rc": rc,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        progress_path.write_text(json.dumps(progress) + "\n")
        log.info("progress %s", progress)
        if total >= TARGET:
            break
        if rs == 0:
            log.warning("zero swipes this round rc=%s; cool down 8s", rc)
            time.sleep(8)
    log.info("DONE total=%s likes=%s passes=%s", total, likes, passes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
