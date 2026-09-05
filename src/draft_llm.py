"""Generate Bumble Friends reply drafts via NanoGPT (GPT-5.6 Sol)."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.config import load_config
from src.obsidian_mcp import load_draft_context
from src.store import base_person_name, list_thread

log = logging.getLogger(__name__)

_API_URL = "https://nano-gpt.com/api/v1/chat/completions"
_DEFAULT_CHAT_MODEL = "openai/gpt-5.6-sol"
_LONDON = ZoneInfo("Europe/London")

_SYSTEM = """You write ONE unsent Bumble Friends reply draft for Toby running Let's Go Social.
You are drafting only — never claim a message was sent, never invent events/dates/links/phone numbers/WhatsApp invites.
Sound like Toby: warm, short, casual ("Awesome", "tbf", "wee group", "sweett", occasional 👀).
One next step only. Plain text only — no markdown, no quotes wrapping the whole reply, no analysis.
If Events has no sendable upcoming row for their hub, do not invent an event; ask whereabouts or keep the chat warm without a date.
Prefer facts from the live SQLite transcript over the People note when they disagree.
"""


def api_key(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    nano = dict(cfg.get("nanogpt") or {})
    return (
        os.environ.get("NANOGPT_API_KEY")
        or os.environ.get("NANO_GPT_API_KEY")
        or str(nano.get("api_key") or "")
    ).strip()


def chat_model(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    nano = dict(cfg.get("nanogpt") or {})
    return (
        os.environ.get("NANOGPT_CHAT_MODEL")
        or str(nano.get("chat_model") or "")
        or _DEFAULT_CHAT_MODEL
    ).strip()


def _first_name(name: str) -> str:
    base = base_person_name(name)
    return (base.split() or [base])[0]


def _format_thread(conn, name: str) -> str:
    lines: list[str] = []
    for row in list_thread(conn, name):
        side = "Toby" if row["side"] == "you" else name
        body = (row["body"] or "").strip()
        if not body:
            continue
        lines.append(f"{side}: {body}")
    return "\n".join(lines) if lines else "(no messages)"


def build_user_prompt(conn, name: str, context: dict[str, str]) -> str:
    today = datetime.now(_LONDON).strftime("%A %d %B %Y").replace(" 0", " ")
    person = (context.get("person_note") or "").strip() or "(no people note yet)"
    return (
        f"Today (Europe/London): {today}\n"
        f"Person inbox name: {name}\n"
        f"First name to use: {_first_name(name)}\n\n"
        f"## LGS/Events.md (source of truth for invites)\n{context['events']}\n\n"
        f"## LGS/Run prompt.md (style + flow rules)\n{context['run_prompt']}\n\n"
        f"## {context.get('person_note_path') or 'People note'}\n{person}\n\n"
        f"## Live Bumble transcript (authoritative)\n{_format_thread(conn, name)}\n\n"
        "Write only the next reply bubble Toby should send."
    )


_PHONE_RE = re.compile(
    r"(?:\+?\d[\d\s().-]{7,}\d)|(?:whatsapp\.com/)|(?:chat\.whatsapp\.com/)",
    re.I,
)
_URL_RE = re.compile(r"https?://|docs\.google\.com|eventbrite\.co\.uk", re.I)
_SENT_CLAIM_RE = re.compile(
    r"\b(i('ve| have)? sent|just sent|message (has been )?sent|already sent)\b",
    re.I,
)


def validate_draft(text: str) -> str:
    """Normalize and reject unsafe model output."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty draft")
    # Strip common wrappers
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        raw = raw[1:-1].strip()
    # Keep a single short bubble — take first paragraph if the model rambling
    parts = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
    if parts:
        raw = parts[0]
    raw = " ".join(raw.split())
    if len(raw) < 2:
        raise ValueError("draft too short")
    if len(raw) > 600:
        raise ValueError("draft too long")
    if _URL_RE.search(raw):
        raise ValueError("draft must not include links")
    if _PHONE_RE.search(raw):
        raise ValueError("draft must not invent phone/WhatsApp links")
    if _SENT_CLAIM_RE.search(raw):
        raise ValueError("draft claims a message was sent")
    return raw


def _chat_completion(messages: list[dict[str, Any]], cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    key = api_key(cfg)
    if not key:
        raise RuntimeError("NANOGPT_API_KEY is not set")
    payload = {
        "model": chat_model(cfg),
        "temperature": 0.7,
        "max_tokens": 220,
        "messages": messages,
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
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:240]
        raise RuntimeError(f"NanoGPT {exc.code}: {detail}") from exc
    try:
        return str(body["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("NanoGPT response missing text") from exc


def generate_draft(conn, name: str, cfg: dict | None = None) -> str:
    """Build Obsidian context + transcript and return a validated draft string."""
    cfg = cfg if cfg is not None else load_config()
    context = load_draft_context(name, cfg)
    user = build_user_prompt(conn, name, context)
    text = _chat_completion(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        cfg,
    )
    return validate_draft(text)
