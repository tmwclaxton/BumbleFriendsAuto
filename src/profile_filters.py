"""Hinge-style ethnicity allowlist for People cards.

Bumble Friends in the UK has no Discover ethnicity picker. This reads
self-declared chips / an Ethnicity row on the card, then likes or passes.
Empty include = filter off.
"""

from __future__ import annotations

import logging
import re
from xml.etree import ElementTree as ET

from src.device import dump_hierarchy, wait_idle

log = logging.getLogger(__name__)

# Same buckets Hinge uses, plus UK census-style aliases.
_CHIP_TO_CANON: dict[str, str] = {
    "white": "white",
    "white / caucasian": "white",
    "white/caucasian": "white",
    "caucasian": "white",
    "white british": "white",
    "white irish": "white",
    "white other": "white",
    "black": "black",
    "black / african descent": "black",
    "black/african descent": "black",
    "african descent": "black",
    "african": "black",
    "black african": "black",
    "black caribbean": "black",
    "caribbean": "black",
    "east asian": "east_asian",
    "east asian descent": "east_asian",
    "chinese": "east_asian",
    "korean": "east_asian",
    "japanese": "east_asian",
    "south asian": "south_asian",
    "south asian descent": "south_asian",
    "indian": "south_asian",
    "pakistani": "south_asian",
    "bangladeshi": "south_asian",
    "sri lankan": "south_asian",
    "southeast asian": "southeast_asian",
    "south east asian": "southeast_asian",
    "southeast asian descent": "southeast_asian",
    "vietnamese": "southeast_asian",
    "thai": "southeast_asian",
    "filipino": "southeast_asian",
    "filipina": "southeast_asian",
    "asian": "asian",
    "asian british": "asian",
    "hispanic / latino": "hispanic",
    "hispanic/latino": "hispanic",
    "hispanic": "hispanic",
    "latino": "hispanic",
    "latina": "hispanic",
    "latinx": "hispanic",
    "middle eastern": "middle_eastern",
    "middle eastern descent": "middle_eastern",
    "arab": "middle_eastern",
    "native american": "native_american",
    "native american descent": "native_american",
    "pacific islander": "pacific_islander",
    "pacific islander descent": "pacific_islander",
    "mixed": "mixed",
    "mixed / other": "mixed",
    "biracial": "mixed",
    "multiracial": "mixed",
    "other": "other",
}

_LABEL_RE = re.compile(r"^(ethnicity|ethnicities|heritage|ethnic background)$", re.I)

# Sidebar / thread picker — same buckets as Hinge, plus Unknown for untagged.
ETHNICITY_CHOICES: list[tuple[str, str]] = [
    ("white", "White"),
    ("black", "Black"),
    ("east_asian", "East Asian"),
    ("south_asian", "South Asian"),
    ("southeast_asian", "Southeast Asian"),
    ("asian", "Asian"),
    ("hispanic", "Hispanic/Latino"),
    ("middle_eastern", "Middle Eastern"),
    ("native_american", "Native American"),
    ("pacific_islander", "Pacific Islander"),
    ("mixed", "Mixed"),
    ("other", "Other"),
]


def ethnicity_label(canon: str | None) -> str:
    key = (canon or "").strip()
    for cid, label in ETHNICITY_CHOICES:
        if cid == key:
            return label
    return key.replace("_", " ").title() if key else ""


def _norm(text: str) -> str:
    t = (text or "").replace("–", "/").replace("—", "/")
    t = re.sub(r"[-_]+", " ", t)
    t = re.sub(r"\s+", " ", t.strip().casefold())
    t = t.replace(" / ", "/").replace("/", " / ")
    return re.sub(r"\s+", " ", t).strip()


def canonicalize(name: str) -> str | None:
    key = _norm(name)
    if not key:
        return None
    if key in _CHIP_TO_CANON:
        return _CHIP_TO_CANON[key]
    key = key.replace(" / ", " ").replace("/", " ")
    key = re.sub(r"\s+", " ", key).strip()
    return _CHIP_TO_CANON.get(key)


def parse_include(raw: object) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        parts = [p for p in re.split(r"[,|]", raw) if p.strip()]
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(p) for p in raw]
    else:
        return frozenset()
    out: set[str] = set()
    for part in parts:
        canon = canonicalize(part)
        if canon:
            out.add(canon)
    return frozenset(out)


def ethnicity_filter_enabled(cfg: dict) -> bool:
    filt = dict((cfg.get("filters") or {}).get("ethnicity") or {})
    return bool(parse_include(filt.get("include") or filt.get("prefer") or []))


def _visible_nodes(xml: str) -> list[str]:
    texts: list[str] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return texts
    for node in root.iter():
        for key in ("text", "content-desc"):
            value = (node.attrib.get(key) or "").strip()
            if value:
                texts.append(value)
    return texts


def ethnicities_on_card(texts: list[str]) -> frozenset[str]:
    """Self-declared chips only — not colour words buried in a bio."""
    found: set[str] = set()
    take_next = False
    for raw in texts:
        if _LABEL_RE.match(raw.strip()):
            take_next = True
            continue
        if take_next:
            take_next = False
            for part in re.split(r"[,&]| and ", raw):
                canon = canonicalize(part)
                if canon:
                    found.add(canon)
            continue
        if len(raw) > 48:
            continue
        canon = canonicalize(raw)
        if canon:
            found.add(canon)
    return frozenset(found)


def _include_hits(found: frozenset[str], include: frozenset[str]) -> bool:
    if found & include:
        return True
    # Bare "Asian" on a UK card matches any Asian family the user listed.
    asian_family = {"east_asian", "south_asian", "southeast_asian", "asian"}
    if "asian" in found and include & asian_family:
        return True
    if found & {"east_asian", "south_asian", "southeast_asian"} and "asian" in include:
        return True
    return False


def ethnicity_allows(texts: list[str], cfg: dict) -> tuple[bool, str]:
    """Return (may_like, reason). Filter off → always True."""
    filt = dict((cfg.get("filters") or {}).get("ethnicity") or {})
    include = parse_include(filt.get("include") or filt.get("prefer") or [])
    if not include:
        return True, "ethnicity filter off"
    if_missing = str(filt.get("if_missing") or "allow").strip().lower()
    if if_missing not in {"allow", "pass"}:
        if_missing = "allow"
    found = ethnicities_on_card(texts)
    if not found:
        if if_missing == "pass":
            return False, "ethnicity missing → pass"
        return True, "ethnicity missing → allow"
    if _include_hits(found, include):
        return True, f"ethnicity {','.join(sorted(found))} matches"
    return False, f"ethnicity {','.join(sorted(found))} outside include"


def collect_card_texts(device, *, extra_scrolls: int = 2) -> list[str]:
    """Dump the card, then scroll so attribute chips below the fold show up."""
    from src.browse import _scroll_profile

    seen: list[str] = []
    for i in range(max(1, extra_scrolls + 1)):
        xml = dump_hierarchy(device)
        seen.extend(_visible_nodes(xml))
        if i < extra_scrolls:
            _scroll_profile(device, {}, direction="down", amount="medium")
            wait_idle(device, 0.45)
    return seen
