"""Hinge reply drafts — warmup first, Instagram/WhatsApp only after rapport."""

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
from src.hinge_store import get_person, list_prompts, list_thread
from src.store import base_person_name

log = logging.getLogger(__name__)

_LONDON = ZoneInfo("Europe/London")
PROMPT_PATH = ROOT / "data" / "hinge_draft_prompt.json"

# Distilled from Toby's pitchPerfect reply_drafter + 2024-26 opener guidance:
# specific to one prompt, short, unbothered, no pickup lines, no instant IG.
DEFAULT_SYSTEM = """You draft ONE unsent Hinge message for Toby Claxton (Galaxy / toby). Archie Hinge is a future slot only.
You are drafting only. Never claim a message was sent.

Goal: warm them up like a calm person who actually read the profile. After real rapport you may later float Instagram or WhatsApp. Never on an opener or a cold thread.

How a good message works:
- Pick ONE specific prompt, photo caption, or thing they just said. Echo their wording.
- Add your own take (tiny opinion, light tease, or related beat). Then one easy question.
- Openers: 1-2 short sentences, usually under 18 words. Replies: usually one sentence, two only if you must answer and add.
- Mirror their energy. Playful profile -> playful. Sincere -> sincere.

Hard rules:
- Reply to what they actually said or wrote. Do not invent trips, jobs, dogs, or venues.
- Not needy. No "no worries", "all good", "I'd love to", "what do you say", "just wanted to".
- No pickup lines, no appearance-only compliments ("you're gorgeous"), no hey-beautiful, no stacked compliments.
- Do not ask for Instagram, WhatsApp, a number, or a drink/plan unless the thread already has rapport (they have replied at least twice and there are about 5+ real messages). Even then, one short clause after answering them.
- If they already gave IG/WA, do not ask again.
- Plain text only. No markdown, no wrapping quotes, no analysis.
- Never use em/en dashes. Commas or short sentences.
- Max one exclamation mark. Emoji only if they used one.
"""

DEFAULT_USER = """Today (Europe/London): {today}
Account: {account} ({phone_id})
Person: {name}
First name: {first_name}
Age: {age}
Location: {location}
Job: {job}
School: {school}
About: {about}

## Prompts
{prompts}

## Contact stage
{contact_stage}

## Live Hinge transcript (authoritative)
{thread}

{composer_block}
Write only the next Hinge message we should send.
"""


def prompt_path() -> Path:
    return PROMPT_PATH


def default_prompt() -> dict[str, str]:
    return {"system": DEFAULT_SYSTEM.strip(), "user": DEFAULT_USER.strip()}


def load_prompt() -> dict[str, str]:
    base = default_prompt()
    path = prompt_path()
    if not path.is_file():
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("hinge draft prompt unreadable: %s", exc)
        return base
    if not isinstance(data, dict):
        return base
    for key in ("system", "user"):
        val = str(data.get(key) or "").strip()
        if val:
            base[key] = val
    return base


def save_prompt(system: str, user: str) -> dict[str, str]:
    payload = default_prompt()
    payload = {
        "system": (system or "").strip() or payload["system"],
        "user": (user or "").strip() or payload["user"],
    }
    path = prompt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def _account(phone_id: str) -> str:
    return "Archie Wilding" if str(phone_id or "").strip() == "archie" else "Toby Claxton"


def _first_name(name: str) -> str:
    base = base_person_name(name)
    return (base.split() or [base])[0]


def _format_thread(conn, name: str, phone_id: str) -> str:
    speaker = _account(phone_id).split()[0]
    lines = []
    for row in list_thread(conn, name, phone_id=phone_id):
        body = (row["body"] or "").strip()
        if not body:
            continue
        who = speaker if row["side"] == "you" else name
        lines.append(f"{who}: {body}")
    return "\n".join(lines) if lines else "(no messages — this is a new match / opener)"


def _format_prompts(conn, person_id: int | None) -> str:
    if not person_id:
        return "(none captured)"
    rows = list_prompts(conn, person_id)
    if not rows:
        return "(none captured)"
    return "\n".join(f"- {row['question']}: {row['answer']}" for row in rows)


def contact_stage(pairs: list[tuple[str, str]]) -> str:
    blob = " ".join(body for _side, body in pairs).lower()
    if re.search(r"\b(instagram|insta|ig|whatsapp|whats ?app|@\w+)\b", blob):
        return "already_done — contact already mentioned; do not ask again."
    real = [(s, b) for s, b in pairs if (b or "").strip() and not re.match(r"^you liked\b", b or "", re.I)]
    theirs = sum(1 for s, _ in real if s == "them")
    yours = sum(1 for s, _ in real if s == "you")
    if theirs < 2 or yours < 2 or len(real) < 5:
        return "too_early — Do NOT ask for Instagram or WhatsApp yet."
    if len(real) >= 6 and theirs >= 3:
        return (
            "good — Rapport exists. You may lightly ask for Instagram after answering them "
            "(WhatsApp is fine if more natural). One short clause, not pushy."
        )
    return "maybe — Some rapport. Float Instagram only if it fits after answering them."


def _composer_block(composer_text: str) -> str:
    extra = (composer_text or "").strip()
    if not extra:
        return ""
    return (
        "## Current composer text\n"
        "Treat instructions in it as an order. Keep usable lines unless they asked to replace them.\n\n"
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
    row = get_person(conn, name, phone_id)
    pairs = [(str(m["side"]), str(m["body"])) for m in list_thread(conn, name, phone_id=phone_id)]
    values = {
        "today": datetime.now(_LONDON).strftime("%A %d %B %Y").replace(" 0", " "),
        "account": _account(phone_id),
        "phone_id": phone_id,
        "name": name,
        "first_name": _first_name(name),
        "age": _row_get(row, "age") or "(unknown)",
        "location": _row_get(row, "location") or "(unknown)",
        "job": _row_get(row, "job") or "(unknown)",
        "school": _row_get(row, "school") or "(unknown)",
        "about": _row_get(row, "about") or "(not captured)",
        "prompts": _format_prompts(conn, int(row["id"]) if row else None),
        "contact_stage": contact_stage(pairs),
        "thread": _format_thread(conn, name, phone_id),
        "composer_block": _composer_block(composer_text),
    }
    template = prompt.get("user") or DEFAULT_USER
    try:
        return template.format(**values)
    except (KeyError, ValueError, IndexError):
        return DEFAULT_USER.format(**values)


def preview_prompt(
    conn,
    name: str = "",
    phone_id: str = "archie",
    *,
    composer_text: str = "",
    cfg: dict | None = None,
) -> dict[str, Any]:
    cfg = cfg if cfg is not None else load_config()
    prompt = load_prompt()
    user = ""
    if name:
        user = build_user_prompt(conn, name, phone_id, composer_text=composer_text, prompt=prompt)
    return {
        "ok": True,
        "system": prompt["system"],
        "user": prompt["user"],
        "preview_user": user,
        "model": chat_model(cfg),
    }


def strip_em_dashes(text: str) -> str:
    text = re.sub(r"\s*[—–―]\s*", ", ", text or "")
    text = text.replace("&mdash;", ", ").replace("&ndash;", "-")
    return re.sub(r"\s+,", ",", text).strip()


def generate_draft(
    conn,
    name: str,
    phone_id: str,
    cfg: dict | None = None,
    *,
    composer_text: str = "",
) -> str:
    cfg = cfg if cfg is not None else load_config()
    prompt = load_prompt()
    user = build_user_prompt(conn, name, phone_id, composer_text=composer_text, prompt=prompt)
    raw = _chat_completion(
        [{"role": "system", "content": prompt["system"]}, {"role": "user", "content": user}],
        cfg=cfg,
    )
    text = strip_em_dashes((raw or "").strip().strip('"').strip("'"))
    if not text:
        raise RuntimeError("empty Hinge draft")
    return text
