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
    "You are screening a Bumble Friends profile card screenshot for a friends app (not dating).\n"
    "Reply with ONE line in this exact format:\n"
    "name=<first name>; pronouns=<she/her|he/him|they/them|none>; gender=<male|female|unknown>; ethnicity=<id>; crazy=<yes|no>\n\n"
    "How to decide gender — in this priority order:\n"
    "1. Read the pronouns printed on the card (e.g. 'she/her' -> female, 'he/him' -> male). OCR the text.\n"
    "2. Read the first NAME printed at the top of the card and infer gender from it "
    "(e.g. Lucy, Priya, Aisha -> female; James, Mohammed, Raj -> male). Use the name, not looks.\n"
    "3. Only if no pronouns AND an ambiguous name, fall back to presentation; else unknown.\n\n"
    "ethnicity id must be ONE of:\n"
    "white, black, east_asian, south_asian, southeast_asian, asian, hispanic, "
    "middle_eastern, native_american, pacific_islander, mixed, other, unknown\n"
    "- Prefer east_asian / south_asian / southeast_asian over bare asian when possible.\n"
    "- south_asian = Indian / Pakistani / Bangladeshi / Sri Lankan look.\n"
    "- crazy=yes only if they look visibly unhinged, aggressive, or unsettling in a way "
    "you would not want to meet for hiking/board games. Normal/attractive/quirky = no.\n"
    "- Read the card text for name/pronouns. No explanation."
)

_LINE_RE = re.compile(
    r"gender\s*=\s*(male|female|unknown).*?"
    r"ethnicity\s*=\s*([a-z_]+).*?"
    r"crazy\s*=\s*(yes|no)",
    re.I | re.S,
)
_NAME_RE = re.compile(r"name\s*=\s*([A-Za-z'’.-]+)", re.I)
_PRON_RE = re.compile(r"pronouns\s*=\s*(she/her|he/him|they/them|none)", re.I)

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


def _gender_from_name(name: str) -> str | None:
    """Best-effort gender from a first name using a small built-in map.

    Only returns male/female for clearly gendered names; else None.
    """
    if not name:
        return None
    n = name.strip().lower().strip("'’.-")
    female = {
        "lucy", "priya", "aisha", "sarah", "emma", "olivia", "sophie", "sophia",
        "chloe", "emily", "hannah", "katie", "laura", "rachel", "rebecca", "amy",
        "anna", "bella", "cara", "diya", "elena", "fatima", "grace", "harpreet",
        "isla", "jasmine", "jessica", "julia", "kavya", "lily", "maya", "mia",
        "natasha", "neha", "nina", "olga", "pooja", "ria", "rosa", "sana", "sara",
        "shreya", "simran", "tara", "zara", "zoe", "anjali", "deepika", "isha",
        "mahnoor", "sulekha", "naveena", "noor", "maryam", "amara",
    }
    male = {
        "james", "mohammed", "mohammad", "raj", "joseph", "daniel", "david",
        "michael", "will", "william", "thomas", "charlie", "harry", "jack",
        "oliver", "george", "leon", "pascal", "edward", "zach", "joshua", "dan",
        "kevin", "lewis", "kartik", "shahzaib", "aaran", "joe", "mikey", "promise",
        "toru", "mac", "naveen", "arjun", "rohan", "vikram", "aditya", "sanjay",
        "ali", "omar", "ahmed", "hassan", "ibrahim", "yusuf", "phillip", "philip",
    }
    if n in female:
        return "female"
    if n in male:
        return "male"
    return None


def _parse_vision_line(text: str) -> dict[str, str]:
    raw = (text or "").strip().strip("`\"'")
    match = _LINE_RE.search(raw)
    name_m = _NAME_RE.search(raw)
    pron_m = _PRON_RE.search(raw)
    name = name_m.group(1) if name_m else ""
    pron = pron_m.group(1).lower() if pron_m else ""
    if match:
        gender = match.group(1).lower()
        eth = parse_guess(match.group(2))
        crazy = "yes" if match.group(3).lower() == "yes" else "no"
    else:
        # Fallback: try to salvage pieces
        gender = "unknown"
        if re.search(r"\bfemale\b", raw, re.I):
            gender = "female"
        elif re.search(r"\bmale\b", raw, re.I):
            gender = "male"
        eth = parse_guess(raw)
        crazy = "yes" if re.search(r"crazy\s*=\s*yes|\bcrazy\b", raw, re.I) else "no"
    # Override gender with pronouns, then name — more reliable than appearance.
    if pron.startswith("she"):
        gender = "female"
    elif pron.startswith("he"):
        gender = "male"
    else:
        ng = _gender_from_name(name)
        if ng:
            gender = ng
    return {"gender": gender, "ethnicity": eth, "crazy": crazy, "name": name, "pronouns": pron}


def _canon_set(raw: object, default: frozenset[str] | None) -> frozenset[str] | None:
    if raw is None:
        return default
    out: set[str] = set()
    for part in raw if isinstance(raw, (list, tuple, set)) else str(raw).split(","):
        canon = canonicalize(str(part)) or str(part).strip().lower().replace(" ", "_")
        if canon:
            out.add(canon)
    return frozenset(out)


def _men_include(cfg: dict) -> frozenset[str]:
    filt = dict((cfg.get("filters") or {}).get("swipe_vision") or {})
    return _canon_set(filt.get("men_include"), _MEN_DEFAULT_INCLUDE) or _MEN_DEFAULT_INCLUDE


def _men_exclude(cfg: dict) -> frozenset[str]:
    filt = dict((cfg.get("filters") or {}).get("swipe_vision") or {})
    if "men_include" in filt and filt.get("men_exclude") in (None, [], ()):
        return frozenset()
    raw = filt.get("men_exclude")
    if raw is None:
        raw = list(_MEN_DEFAULT_EXCLUDE)
    return _canon_set(raw, _MEN_DEFAULT_EXCLUDE) or frozenset()


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


_VISIBILITY_PROMPT = (
    "Look at this phone screenshot for a friends-swiping app.\n"
    "Reply with ONE line in this exact format:\n"
    "visible=<yes|no>; reason=<short>\n\n"
    "This is a check for whether the app is showing a usable profile card RIGHT NOW — "
    "NOT a judgement of the person's photo quality.\n\n"
    "visible=yes if a profile card is on screen and you can make out the person, EVEN IF "
    "their photo is a soft-focus selfie, slightly blurry, dark, or they look away. "
    "Photo quality / blurriness / lighting of the PERSON does NOT matter — only whether "
    "the card itself is shown and readable.\n\n"
    "visible=no ONLY when the app is NOT showing a usable card, i.e.:\n"
    "- A popup, dialog, notification shade, match screen, or overlay is covering the card.\n"
    "- The screen is the phone home screen, a different app, black, or loading.\n"
    "- The card is mid-swipe / mid-transition so two cards or a partial frame is shown.\n"
    "- There is no person/card visible at all.\n\n"
    "When in doubt between yes/no: if you can see one person's profile card and read their "
    "name, answer yes. No explanation beyond the short reason."
)

_VISIBILITY_RE = re.compile(r"visible\s*=\s*(yes|no)", re.I)


def check_card_visible(path: Path, cfg: dict | None = None) -> dict[str, str]:
    """Ask the vision model whether the card is clearly visible / unobstructed.

    Returns {"visible": "yes"|"no", "reason": str}. On any error, returns
    visible=no so the caller can abort safely rather than swipe blind.
    """
    cfg = cfg if cfg is not None else load_config()
    key = api_key(cfg)
    if not key:
        return {"visible": "no", "reason": "no api key"}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {"visible": "no", "reason": f"read error {exc}"}
    if len(raw) < 80:
        return {"visible": "no", "reason": "screenshot too small"}
    data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
    payload = {
        "model": vision_model(cfg),
        "temperature": 0,
        "max_tokens": 30,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISIBILITY_PROMPT},
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
        text = str(body["choices"][0]["message"]["content"] or "")
    except Exception as exc:
        return {"visible": "no", "reason": f"api error {exc}"}
    m = _VISIBILITY_RE.search(text or "")
    visible = m.group(1).lower() if m else "no"
    reason = "clear" if visible == "yes" else (text.strip()[:80] or "not visible")
    return {"visible": visible, "reason": reason}


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

    pass_crazy = filt.get("pass_crazy", True)
    if isinstance(pass_crazy, str):
        pass_crazy = pass_crazy.strip().lower() not in {"0", "false", "off", "no"}
    if crazy and pass_crazy:
        return False, f"{gender or 'unknown'} crazy=yes ethnicity={ethnicity} → pass"

    if gender == "female":
        if "women_include" in filt:
            women = _canon_set(filt.get("women_include"), default=None)
            if women is None:
                women = frozenset()
            if_missing_women = str(filt.get("women_if_missing") or "allow").strip().lower()
            if if_missing_women not in {"allow", "pass"}:
                if_missing_women = "allow"
            if ethnicity in {"unknown", ""}:
                if if_missing_women == "allow":
                    return True, f"woman ethnicity missing → allow"
                return False, f"woman ethnicity missing → pass"
            if ethnicity in women:
                return True, f"woman ethnicity={ethnicity} allowed → like"
            return False, f"woman ethnicity={ethnicity} excluded → pass"
        return True, f"woman ethnicity={ethnicity} crazy=no → like"

    # Male or unknown → apply men rules (unknown treated as men = stricter).
    include = _men_include(cfg)
    exclude = _men_exclude(cfg)
    label = "man" if gender == "male" else "unknown-gender"
    if ethnicity in exclude or (ethnicity == "south_asian" and "men_include" not in filt):
        return False, f"{label} ethnicity={ethnicity} excluded → pass"
    if ethnicity in {"black"} and "men_include" not in filt:
        return False, f"{label} ethnicity=black → pass"
    if ethnicity in include:
        return True, f"{label} ethnicity={ethnicity} allowed → like"
    if ethnicity in {"unknown", ""}:
        if if_missing_men == "allow":
            return True, f"{label} ethnicity missing → allow"
        return False, f"{label} ethnicity missing → pass"
    # Bare asian / other buckets: not in men allowlist
    return False, f"{label} ethnicity={ethnicity} excluded → pass"


def evaluate_card(device, texts: list[str], cfg: dict | None = None) -> tuple[bool, str, dict[str, Any]]:
    """Screenshot + vision + decision. Cleans up temp file."""
    cfg = cfg if cfg is not None else load_config()
    meta: dict[str, Any] = {}
    path: Path | None = None
    vision: dict[str, str] | None = None
    try:
        path = screenshot_card(device)
        try:
            from src.swipe_desk import save_card_photo

            meta["photo_seq"] = save_card_photo(path)
        except Exception:
            log.debug("swipe desk photo skip", exc_info=True)
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
