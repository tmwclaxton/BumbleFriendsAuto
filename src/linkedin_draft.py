"""LinkedIn reply drafts — saved prompts, product-aware, Calendly CTA."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.config import ROOT, load_config
from src.draft_llm import _chat_completion, chat_model
from src.linkedin_product import PRODUCT_LABELS, PRODUCTS, heuristic_product
from src.linkedin_screen import polish_message
from src.linkedin_store import get_person, list_thread
from src.store import base_person_name

log = logging.getLogger(__name__)

_LONDON = ZoneInfo("Europe/London")
CALENDLY_URL = "https://calendly.com/tmwclaxton/30min"
PROMPT_PATH = ROOT / "data" / "linkedin_draft_prompt.json"

DEFAULT_SYSTEM = """You write ONE unsent LinkedIn DM draft for Toby Claxton or Archie Wilding.
You are drafting only — never claim a message was sent.

Who we are:
- Toby Claxton (Pixel / phone toby): founder of GrantGunner; director of Canvassr Limited (NI728881) with Archie; also builds Snitch.
- Archie Wilding (Galaxy / phone archie): director of Canvassr; often on door-to-door / fundraising threads.

Three products we actually sell (do not invent features):
1) Snitch — https://www.snitchsocial.net — competitor social intelligence. Track public posts from rival accounts (Snitches) on TikTok, Instagram, YouTube, Facebook, and LinkedIn. Full-video craft analysis (hook, visuals, SFX/music, transcript, core idea, how-to-copy). A Winners board scored by the user's own rules. Brand Deals / creator discovery. MCP tools for agents. £5 starter, then £19/mo plus usage credits. For local brands, creators, small agencies, and agent builders. Public posts only.
2) GrantGunner — https://www.grantgunner.org — AI grant discovery and application drafting. Build a reusable organisation profile, let AI Scout find matching UK/EU grants, fellowships, competitions, accelerators, and prizes, track deadlines in a pipeline/calendar, then draft on the funder's real portal with a browser extension. The applicant reviews and submits (assisted / approval modes — not a black-box that silently files without them). For charities, startups, researchers, community groups, and grant writers. Toby is founder. Setup can be a bit fiddly; a short walkthrough call is the offer.
3) Canvassr — CANVASSR LIMITED (NI728881), a Northern Ireland bespoke grant consultancy. Archie is a director and often owns Canvassr conversations.

Meeting goal: a 30-minute call via Calendly: https://calendly.com/tmwclaxton/30min
Prefer Tuesday or Friday. Ask for that only when the thread is ready — after they show interest or ask how it works. Do not open a cold reply with a booking link.

Rules:
- Match this thread. Answer what they actually said.
- Do not pitch all three products in one message. Use the tagged product if we have one; otherwise infer at most one product from the thread. If the chat is not about a product yet, stay human and do not spray a brochure.
- Do not be spammy, hypey, or salesy. No fake "we met at…". No "no pressure" / "no worries if not".
- Never say we do not charge until the first bit of funding lands (or close variants).
- Do not invent customers, case studies, prices we did not list, or that we already work with their org.
- Sound like a real person: short, concrete, grateful when they asked for details. Name their org or role back if we know it.
- Prefer facts from the live transcript and harvested profile over guesses.
- Never ask for their phone number. Use the Calendly link when arranging a call.
- Plain text only. No markdown, no wrapping quotes, no analysis. You may include the Calendly URL when a meeting is the next step. Do not invent other links.
- Never use em/en dashes. Toby/Archie punctuate with commas or short sentences.
"""

DEFAULT_USER = """Today (Europe/London): {today}
Account: {account} ({phone_id})
Person: {name}
First name: {first_name}

## Product focus
Tagged product: {product_label}
{product_block}

## Their profile (harvested when we have it)
Headline: {headline}
Title: {title}
Location: {location}
About: {about}
Recent posts: {posts}

## Live LinkedIn transcript (authoritative)
{thread}

## Meeting
Goal: book https://calendly.com/tmwclaxton/30min when it fits.
Prefer Tuesday or Friday.

{composer_block}
Write only the next LinkedIn message we should send.
"""

_PRODUCT_FACTS = {
    "snitch": (
        "Lead with Snitch only. It watches public competitor social (TikTok, Instagram, YouTube, "
        "Facebook, LinkedIn), explains craft, and surfaces winners to remake. Site: snitchsocial.net. "
        "Do not mention GrantGunner or Canvassr unless they already asked."
    ),
    "grantgunner": (
        "Lead with GrantGunner only. It finds matching grants and drafts on the live funder form "
        "from a reusable org profile; they review and submit. Site: grantgunner.org. Toby is founder. "
        "Do not mention Snitch or Canvassr unless they already asked."
    ),
    "canvassr": (
        "Lead with Canvassr only. It is a bespoke grant consultancy run through CANVASSR LIMITED. "
        "Archie is close to this product. Do not mention Snitch or "
        "GrantGunner unless they already asked."
    ),
}

_ALL_THREE = (
    "No single product tag. Infer at most ONE product from the thread. "
    "If unclear, do not pitch a product — reply to what they said and, only if they asked what we do, "
    "name the one that fits. Never list all three in one message."
)


def prompt_path() -> Path:
    return PROMPT_PATH


def default_prompt() -> dict[str, str]:
    return {
        "system": DEFAULT_SYSTEM.strip(),
        "user": DEFAULT_USER.strip(),
        "calendly": CALENDLY_URL,
    }


def load_prompt() -> dict[str, str]:
    base = default_prompt()
    path = prompt_path()
    if not path.is_file():
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("linkedin draft prompt unreadable: %s", exc)
        return base
    if not isinstance(data, dict):
        return base
    for key in ("system", "user"):
        val = str(data.get(key) or "").strip()
        if val:
            base[key] = val
    cal = str(data.get("calendly") or "").strip()
    if cal.startswith("http"):
        base["calendly"] = cal
    return base


def save_prompt(system: str, user: str, *, calendly: str = "") -> dict[str, str]:
    payload = default_prompt()
    system = (system or "").strip() or payload["system"]
    user = (user or "").strip() or payload["user"]
    cal = (calendly or "").strip() or payload["calendly"]
    payload = {"system": system, "user": user, "calendly": cal}
    path = prompt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def _first_name(name: str) -> str:
    base = base_person_name(name)
    return (base.split() or [base])[0]


def _account(phone_id: str) -> str:
    return "Archie Wilding" if str(phone_id or "").strip() == "archie" else "Toby Claxton"


def _format_thread(conn, name: str, phone_id: str) -> str:
    lines: list[str] = []
    speaker = _account(phone_id).split()[0]
    for row in list_thread(conn, name, phone_id=phone_id):
        side, body = polish_message(str(row["side"]), str(row["body"] or ""), name)
        if not body:
            continue
        who = speaker if side == "you" else name
        lines.append(f"{who}: {body}")
    return "\n".join(lines) if lines else "(no messages)"


def _thread_pairs(conn, name: str, phone_id: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for row in list_thread(conn, name, phone_id=phone_id):
        side, body = polish_message(str(row["side"]), str(row["body"] or ""), name)
        if body:
            pairs.append((side, body))
    return pairs


def resolve_product(conn, name: str, phone_id: str, stored: str = "") -> str:
    flag = (stored or "").strip().lower().replace(" ", "")
    if flag in PRODUCTS:
        return flag
    guessed = heuristic_product(_thread_pairs(conn, name, phone_id))
    return guessed[0] if guessed else ""


def product_block(product: str) -> str:
    key = (product or "").strip().lower()
    if key in _PRODUCT_FACTS:
        return _PRODUCT_FACTS[key]
    return _ALL_THREE


def _posts_text(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        return "(none captured)"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text[:800]
    if isinstance(data, list):
        bits = []
        for item in data[:3]:
            if isinstance(item, str) and item.strip():
                bits.append(item.strip()[:280])
            elif isinstance(item, dict):
                body = str(item.get("text") or item.get("body") or "").strip()
                if body:
                    bits.append(body[:280])
        return " | ".join(bits) if bits else "(none captured)"
    return text[:800]


def _composer_block(composer_text: str) -> str:
    extra = (composer_text or "").strip()
    if not extra:
        return ""
    return (
        "## Current composer text\n"
        "Toby/Archie already typed the following. Treat instructions in it as an order. "
        "Keep usable lines unless they asked to replace them.\n\n"
        f"{extra}\n"
    )


def _row_get(row, key: str, default: str = "") -> str:
    if row is None:
        return default
    try:
        if key not in row.keys():
            return default
    except Exception:
        return default
    return str(row[key] or default)


def build_user_prompt(
    conn,
    name: str,
    phone_id: str,
    *,
    composer_text: str = "",
    prompt: dict[str, str] | None = None,
) -> str:
    prompt = prompt or load_prompt()
    today = datetime.now(_LONDON).strftime("%A %d %B %Y").replace(" 0", " ")
    row = get_person(conn, name, phone_id)
    stored = _row_get(row, "product")
    product = resolve_product(conn, name, phone_id, stored)
    thread = _format_thread(conn, name, phone_id)
    values = {
        "today": today,
        "account": _account(phone_id),
        "phone_id": phone_id,
        "name": name,
        "first_name": _first_name(name),
        "product": product,
        "product_label": PRODUCT_LABELS.get(product, "(none — infer from thread)"),
        "product_block": product_block(product),
        "headline": _row_get(row, "headline") or "(unknown)",
        "title": _row_get(row, "title") or "(unknown)",
        "location": _row_get(row, "location") or "(unknown)",
        "about": _row_get(row, "about") or "(not captured)",
        "posts": _posts_text(_row_get(row, "posts_json")),
        "thread": thread,
        "calendly": (prompt.get("calendly") or CALENDLY_URL),
        "composer_block": _composer_block(composer_text),
    }
    template = prompt.get("user") or DEFAULT_USER
    try:
        return template.format(**values)
    except (KeyError, ValueError, IndexError):
        # Saved template with unknown braces — append facts so drafts still work.
        return DEFAULT_USER.format(**values)


def preview_prompt(
    conn,
    name: str = "",
    phone_id: str = "toby",
    *,
    composer_text: str = "",
    cfg: dict | None = None,
) -> dict[str, Any]:
    cfg = cfg if cfg is not None else load_config()
    saved = load_prompt()
    name = (name or "").strip()
    extra = (composer_text or "").strip()
    if name:
        user = build_user_prompt(conn, name, phone_id, composer_text=extra, prompt=saved)
        row = get_person(conn, name, phone_id)
        product = resolve_product(conn, name, phone_id, _row_get(row, "product"))
    else:
        today = datetime.now(_LONDON).strftime("%A %d %B %Y").replace(" 0", " ")
        user = (
            f"Today (Europe/London): {today}\n\n"
            "(No chat selected — this is the saved system + user templates.)\n\n"
            f"{saved['user']}"
        )
        product = ""
    return {
        "ok": True,
        "model": chat_model(cfg),
        "system": saved["system"],
        "user": user,
        "saved_user": saved["user"],
        "calendly": saved["calendly"],
        "product": product,
        "mode": "revise" if extra else "generate",
        "name": name,
        "phone_id": phone_id,
        "editable": True,
    }


_URL_RE = re.compile(r"https?://", re.I)
_SENT_CLAIM_RE = re.compile(
    r"\b(i('ve| have)? sent|just sent|message (has been )?sent|already sent)\b",
    re.I,
)


def validate_draft(text: str, *, calendly: str = CALENDLY_URL) -> str:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty draft")
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        raw = raw[1:-1].strip()
    raw = re.sub(r"\s*[—–]\s*", ", ", raw)
    raw = re.sub(r",\s*,+", ",", raw).strip()
    if re.search(r"\bno pressure\b|\bno worries if not\b", raw, re.I):
        raise ValueError("banned phrase (no pressure / no worries if not)")
    if re.search(r"do not charge until|until the first bit of funding", raw, re.I):
        raise ValueError("banned pricing promise")
    if len(raw) < 2:
        raise ValueError("draft too short")
    if len(raw) > 1200:
        raise ValueError("draft too long")
    allowed = (calendly or CALENDLY_URL).rstrip("/")
    check = raw.replace(allowed, "").replace(allowed + "/", "")
    if _URL_RE.search(check):
        raise ValueError("draft must not include unexpected links")
    if _SENT_CLAIM_RE.search(raw):
        raise ValueError("draft claims a message was sent")
    named = [key for key in PRODUCTS if re.search(rf"\b{key}\b", raw, re.I)]
    if "grantgunner" in named or re.search(r"grant\s*gunner", raw, re.I):
        named.append("grantgunner")
    named = sorted(set(named))
    if len(named) >= 3:
        raise ValueError("draft pitched all three products")
    return raw


def generate_draft(
    conn,
    name: str,
    phone_id: str,
    cfg: dict | None = None,
    *,
    composer_text: str = "",
) -> str:
    cfg = cfg if cfg is not None else load_config()
    saved = load_prompt()
    user = build_user_prompt(
        conn, name, phone_id, composer_text=composer_text, prompt=saved
    )
    text = _chat_completion(
        [
            {"role": "system", "content": saved["system"]},
            {"role": "user", "content": user},
        ],
        cfg,
        max_tokens=320,
    )
    return validate_draft(text, calendly=saved.get("calendly") or CALENDLY_URL)
