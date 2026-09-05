#!/usr/bin/env python3
"""Keep swiping until TARGET cards have been decided (like or pass)."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path

log_path = Path("/app/data/swipe_200.log")
progress_path = Path("/app/data/swipe_200_progress.json")
log_path.write_text("")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_path)],
    force=True,
)
log = logging.getLogger("swipe200")

from src.config import load_config
from src.swiper import run_session

TARGET = 200
total = likes = passes = rounds = 0
if progress_path.exists():
    try:
        prev = json.loads(progress_path.read_text())
        total = int(prev.get("total") or 0)
        likes = int(prev.get("likes") or 0)
        passes = int(prev.get("passes") or 0)
        rounds = int(prev.get("rounds") or 0)
        log.info("resuming from progress total=%s", total)
    except Exception:
        pass
log.info("begin swipe marathon target=%s already=%s", TARGET, total)

while total < TARGET:
    rounds += 1
    remaining = TARGET - total
    cfg = load_config()
    cfg["max_swipes"] = remaining
    cfg["like_ratio"] = 1.0
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
    before_len = log_path.stat().st_size
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
    time.sleep(5)

log.info("FINISHED %s", {"total": total, "likes": likes, "passes": passes})
