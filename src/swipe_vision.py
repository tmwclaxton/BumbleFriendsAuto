"""Live card vision for swipe decisions (gender + ethnicity + vibe)."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from src.config import load_config
from src.ethnicity_vision import api_key, parse_guess, vision_model
from src.profile_filters import canonicalize, ethnicities_on_card

log = logging.getLogger(__name__)

_API_URL = "https://nano-gpt.com/api/v1/chat/completions"

_PROMPT = (
    "You are screening a Bumble Friends profile photo for a friends app (not dating).\n"
    "Reply with ONE line in this exact format:\n"
    "gender=<male|female|unknown>; ethnicity=<id>; crazy=<yes|no>\n\n"
    "ethnicity id must be ONE of:\n"
    "white, black, east_asian, south_asian, southeast_asian, asian, hispanic, "
    "middle_eastern, native_american, pacific_islander, mixed, other, unknown\n\n"
    "Rules:\n"
    "- gender from presentation / face; use unknown only if you truly cannot tell.\n"
    "- Prefer east_asian / south_asian / southeast_asian over bare asian when possible.\n"
    "- south_asian = Indian / Pakistani / Bangladeshi / Sri Lankan look.\n"
    "- crazy=yes only if they look visibly unhinged, aggressive, or unsettling in a way "
    "you would not want to meet for hiking/board games. Normal/attractive/quirky = no.\n"
    "- Photo only. No explanation."
)

_LINE_RE = re.compile(
    r"gender\s*=\s*(male|female|unknown).*?"
    r"ethnicity\s*=\s*([a-z_]+).*?"
    r"crazy\s*=\s*(yes|no)",
    re.I | re.S,
)

_HE = re.compile(r"\bhe\s*/\s*him\b|\bhe\s*/\s*his\b", re.I)
_SHE = re.compile(r"\bshe\s*/\s*her\b|\bshe\s*/\s*hers\b", re.I)

_MEN_DEFAULT_INCLUDE = frozenset({"white", "east_asian", "southeast_asian"})
_MEN_DEFAULT_EXCLUDE = frozenset({"black", "south_asian"})


def swipe_vision_enabled(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else load_config()
    filt = dict((cfg.get("filters") or {}).get("swipe_vision") or {})
    if "enabled" in filt:
        return bool(filt.get("enabled"))
    # Enabled when API key present unless explicitly off.
    return bool(api_key(cfg)) and bool(filt.get("enabled", True))


def gender_from_texts(texts: list[str]) -> str | None:
    blob = "\n".join(texts or [])
    he = bool(_HE.search(blob))
    she = bool(_SHE.search(blob))
    if he and not she:
        return "male"
    if she and not he:
        return "female"
    return None


def _parse_vision_line(text: str) -> dict[str, str]:
    raw = (text or "").strip().strip("`\"'")
    match = _LINE_RE.search(raw)
    if match:
        gender = match.group(1).lower()
        eth = parse_guess(match.group(2))
        crazy = "yes" if match.group(3).lower() == "yes" else "no"
        return {"gender": gender, "ethnicity": eth, "crazy": crazy}
    # Fallback: try to salvage pieces
    gender = "unknown"
    if re.search(r"\bfemale\b", raw, re.I):
        gender = "female"
    elif re.search(r"\bmale\b", raw, re.I):
        gender = "male"
    eth = parse_guess(raw)
    crazy = "yes" if re.search(r"crazy\s*=\s*yes|\bcrazy\b", raw, re.I) else "no"
    return {"gender": gender, "ethnicity": eth, "crazy": crazy}


def _men_include(cfg: dict) -> frozenset[str]:
    filt = dict((cfg.get("filters") or {}).get("swipe_vision") or {})
    raw = filt.get("men_include") or list(_MEN_DEFAULT_INCLUDE)
    out: set[str] = set()
    for part in raw if isinstance(raw, (list, tuple, set)) else str(raw).split(","):
        canon = canonicalize(str(part)) or str(part).strip().lower().replace(" ", "_")
        if canon:
            out.add(canon)
    return frozenset(out) or _MEN_DEFAULT_INCLUDE


def _men_exclude(cfg: dict) -> frozenset[str]:
    filt = dict((cfg.get("filters") or {}).get("swipe_vision") or {})
    raw = filt.get("men_exclude") or list(_MEN_DEFAULT_EXCLUDE)
    out: set[str] = set()
    for part in raw if isinstance(raw, (list, tuple, set)) else str(raw).split(","):
        canon = canonicalize(str(part)) or str(part).strip().lower().replace(" ", "_")
        if canon:
            out.add(canon)
    return frozenset(out) | _MEN_DEFAULT_EXCLUDE


def screenshot_card(device) -> Path:
    """Grab the current screen into a temp JPEG for vision."""
    tmp = tempfile.NamedTemporaryFile(prefix="bff-card-", suffix=".jpg", delete=False)
    path = Path(tmp.name)
    tmp.close()
    # uiautomator2 screenshot
    device.screenshot(str(path))
    return path


def classify_card_image(path: Path, cfg: dict | None = None) -> dict[str, str]:
    cfg = cfg if cfg is not None else load_config()
    key = api_key(cfg)
    if not key:
        raise RuntimeError("NANOGPT_API_KEY is not set")
    raw = path.read_bytes()
    if len(raw) < 80:
        return {"gender": "unknown", "ethnicity": "unknown", "crazy": "no"}
    data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
    payload = {
        "model": vision_model(cfg),
        "temperature": 0,
        "max_tokens": 40,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    req = urllib.request.Request(
        _API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:240]
        raise RuntimeError(f"NanoGPT {exc.code}: {detail}") from exc
    try:
        text = str(body["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("NanoGPT response missing text") from exc
    return _parse_vision_line(text)


def decide_swipe(
    *,
    texts: list[str],
    vision: dict[str, str] | None,
    cfg: dict | None = None,
) -> tuple[bool, str]:
    """Return (like?, reason) from pronouns/chips + optional vision."""
    cfg = cfg if cfg is not None else load_config()
    filt = dict((cfg.get("filters") or {}).get("swipe_vision") or {})
    if_missing_men = str(filt.get("men_if_missing") or "pass").strip().lower()
    if if_missing_men not in {"allow", "pass"}:
        if_missing_men = "pass"

    gender = gender_from_texts(texts) or (vision or {}).get("gender") or "unknown"
    chip_eth = ethnicities_on_card(texts)
    vision_eth = (vision or {}).get("ethnicity") or "unknown"
    ethnicity = next(iter(chip_eth), None) or vision_eth
    crazy = ((vision or {}).get("crazy") or "no").lower() == "yes"

    if gender == "female":
        if crazy:
            return False, f"woman crazy=yes ethnicity={ethnicity} → pass"
        return True, f"woman ethnicity={ethnicity} crazy=no → like"

    # Male or unknown → apply men rules (unknown treated as men = stricter).
    include = _men_include(cfg)
    exclude = _men_exclude(cfg)
    label = "man" if gender == "male" else "unknown-gender"
    if ethnicity in exclude or ethnicity == "south_asian":
        return False, f"{label} ethnicity={ethnicity} excluded → pass"
    if ethnicity in {"black"}:
        return False, f"{label} ethnicity=black → pass"
    if ethnicity in include:
        return True, f"{label} ethnicity={ethnicity} allowed → like"
    if ethnicity in {"unknown", ""}:
        if if_missing_men == "allow":
            return True, f"{label} ethnicity missing → allow"
        return False, f"{label} ethnicity missing → pass"
    # Bare asian / other buckets: not in men allowlist
    return False, f"{label} ethnicity={ethnicity} outside men allowlist → pass"


def evaluate_card(device, texts: list[str], cfg: dict | None = None) -> tuple[bool, str, dict[str, Any]]:
    """Screenshot + vision + decision. Cleans up temp file."""
    cfg = cfg if cfg is not None else load_config()
    meta: dict[str, Any] = {}
    path: Path | None = None
    vision: dict[str, str] | None = None
    try:
        path = screenshot_card(device)
        vision = classify_card_image(path, cfg)
        meta["vision"] = vision
    except Exception as exc:
        log.warning("swipe vision failed: %s", exc)
        meta["vision_error"] = str(exc)
        # Fall back to text-only decision (no vision)
        vision = None
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    like, reason = decide_swipe(texts=texts, vision=vision, cfg=cfg)
    meta["reason"] = reason
    return like, reason, meta
