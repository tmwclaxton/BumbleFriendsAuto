"""Load and merge YAML config with CLI overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = ROOT / "config.example.yaml"

DEFAULTS: dict[str, Any] = {
    "package": "com.bumblebff.app",
    "max_swipes": 30,
    "like_ratio": 1.0,
    "delay_min": 2.5,
    "delay_max": 7.0,
    "browse": {
        "enabled": True,
        "look_min": 1.8,
        "look_max": 4.5,
        "photo_taps_min": 0,
        "photo_taps_max": 4,
        "back_photo_chance": 0.28,
        "scrolls_min": 0,
        "scrolls_max": 3,
        "read_min": 1.5,
        "read_max": 4.0,
        "scroll_back_chance": 0.35,
        "pre_swipe_min": 0.5,
        "pre_swipe_max": 1.4,
        "photo_tap_x_right": 0.78,
        "photo_tap_x_left": 0.22,
        "photo_tap_y": 0.42,
        "scroll_x": 0.50,
        "scroll_y_start": 0.72,
        "scroll_y_end": 0.38,
        "scroll_duration_ms_min": 350,
        "scroll_duration_ms_max": 650,
        "jitter": 0.02,
    },
    "swipe": {
        "start_x": 0.22,
        "start_y": 0.50,
        "end_x_like": 0.92,
        "end_x_pass": 0.08,
        "end_y": 0.50,
        "duration_ms_min": 180,
        "duration_ms_max": 280,
        "jitter": 0.02,
    },
    "bring_to_foreground": True,
    "dump_dir": "dumps",
    "db_path": "data/friends.db",
    "filters": {
        "ethnicity": {
            "include": [],
            "if_missing": "allow",
        },
    },
    "messenger": {
        "template": (
            "Hi {name}, I'm putting together a wee group for hiking / board games / sports. "
            "Does that sound like something you would be interested in?"
        ),
        "max_messages": 20,
        "delay_min": 2.5,
        "delay_max": 5.0,
        "type_pause": 0.8,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load config from YAML, falling back to example then built-in defaults."""
    import os

    cfg = dict(DEFAULTS)
    candidates = []
    if path is not None:
        candidates.append(path)
    else:
        candidates.extend([DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG_PATH])

    for candidate in candidates:
        if candidate.is_file():
            with candidate.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                raise ValueError(f"Config at {candidate} must be a mapping")
            cfg = _deep_merge(cfg, data)
            break

    # Environment overrides for container / server deploys.
    serial = (os.environ.get("SERIAL") or os.environ.get("PIXEL_SERIAL") or "").strip()
    if serial:
        cfg["serial"] = serial
    db_path = (os.environ.get("DB_PATH") or "").strip()
    if db_path:
        cfg["db_path"] = db_path
    return cfg
