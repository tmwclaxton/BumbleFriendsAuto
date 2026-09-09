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
Sound like Toby: warm, short, casual ("tbf", "wee group", "sweett", occasional 👀).
Voice rules learned from Toby's real sent messages:
- Do not start every bubble with "Awesome". Aiden's thread is the anti-pattern (Awesome! four times in a row). Rotate reaction words Toby actually uses: Sweet / Sweett / Fantastic / Nice one / Sound / Dead on / Lovely / Ah nice / Class / Yea. Use Awesome at most once in a thread, and never if the last Toby bubble already started with it. Often skip the reaction word and just answer.
- 1-2 short sentences max — Toby sends small bubbles, not paragraphs.
- NEVER use their name in the reply (no "Hi Mathu,", no "No worries Lee,") — Toby only uses names in the very first intro message.
- Toby's smiley is the text ":)" — never 😊 😄 🙌 or similar. 👀 is fine for an invite hook.
- NEVER use em/en dashes (— or –) — Toby punctuates with commas or short sentences. Dashes read as AI.
- Transcript artifact: Bumble's UI hides emojis from our capture, so old messages show ".." where a real emoji (👀 😂) was sent. NEVER copy that ".." — when the moment calls for an emoji, use the real emoji. Never replace an emoji with ".." or any punctuation.
- NEVER say "no pressure" (or "no worries if not", "if you're up for it" style hedges) — Toby just asks the question and lets them answer.
- NEVER re-pitch the intro ("I'm putting together a wee group...") to anyone who has already replied — answer what they actually said instead.
- If they ask how Toby is: one short honest line (work, R&D, life) + bounce it back ("you?"), then any next step.
- Do NOT paste the itinerary Google Doc for a yes, "sounds good", or a general "what's the plan". Keep that as day + place + 1–2 activities in the bubble. Only paste the hub itinerary link from Events if they ask for specifics beyond that — what time, meet point, how long, exact activity order, or "send me the details / itinerary / doc". Only invented links are banned.
- Pitch events as settled plans: "we're planning an escape room and board games" — never tentative "thinking an escape room". It's a group ("we"), not just Toby.
- Don't tack "you interested?" onto every invite — if the bubble ends with the plan + 👀, that's the question.
- The goal of every chat is their phone number for the WhatsApp group. Once they've shown any interest, steer toward it ("drop me your number and I'll add you to the WhatsApp group"). If there's no sendable event date for their hub, give the Instagram (@letsgosocialuk) to keep them warm AND still ask for the number — never leave the chat at a dead "I'll keep you posted".
- When they have just sent their number: thank them only — "I'll get u added shortly" or "Looking forward to meeting you :)" (or both in one short bubble). Do not re-pitch the event, do not paste the itinerary, do not ask another question.
- If the event they were invited to or asked about has already passed (including you only seeing their reply after the day): draft only "Ah sorry just seeing this now". Do not say the plan changed, do not ask whereabouts, do not invite a replacement date in that bubble. Never write lines like "That one's changed tbf, whereabouts are you based?".
One next step only. Plain text only — no markdown, no quotes wrapping the whole reply, no analysis.
If Events has no sendable upcoming row for their hub and they have not already been invited to a date that passed, do not invent an event; ask whereabouts or keep the chat warm without a date.
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


def _recent_you_openers(conn, name: str) -> list[str]:
    openers: list[str] = []
    for row in list_thread(conn, name):
        if row["side"] != "you":
            continue
        body = (row["body"] or "").strip()
        if not body:
            continue
        openers.append(body.split()[0].strip("!,.:)"))
    return openers[-6:]


def _opener_hint(conn, name: str) -> str:
    recent = _recent_you_openers(conn, name)
    if not recent:
        return ""
    awesome_n = sum(1 for w in recent if w.casefold() == "awesome")
    last = recent[-1]
    extra = ""
    if last.casefold() == "awesome" or awesome_n:
        extra = (
            " Do not start this draft with Awesome. Pick Sweet, Sweett, Fantastic, "
            "Nice one, Sound, or just go straight into the next question."
        )
    return (
        f"Toby's recent first words in this thread: {', '.join(recent)}."
        f"{extra}\n\n"
    )


def _composer_block(composer_text: str) -> str:
    extra = (composer_text or "").strip()
    if not extra:
        return ""
    return (
        "\n\n## Current composer text\n"
        "Toby already typed the following in the inbox. Treat any instruction in it "
        "(tone, ask for their number, shorter, mention the event, etc.) as an order. "
        "Keep usable draft lines he already wrote unless the instruction says to replace them.\n\n"
        f"{extra}\n\n"
        "Write only the next reply bubble Toby should send."
    )


def build_user_prompt(
    conn,
    name: str,
    context: dict[str, str],
    *,
    composer_text: str = "",
) -> str:
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
        f"{_opener_hint(conn, name)}"
        "Write only the next reply bubble Toby should send."
        + _composer_block(composer_text)
    )


def preview_draft_prompt(
    conn,
    name: str = "",
    *,
    composer_text: str = "",
    cfg: dict | None = None,
) -> dict[str, Any]:
    """The system + user messages the model would see for this chat."""
    cfg = cfg if cfg is not None else load_config()
    name = (name or "").strip()
    extra = (composer_text or "").strip()
    if name:
        context = load_draft_context(name, cfg)
        user = build_user_prompt(conn, name, context, composer_text=extra)
    else:
        from src.obsidian_mcp import events_note, read_note_cached, run_prompt_note

        today = datetime.now(_LONDON).strftime("%A %d %B %Y").replace(" 0", " ")
        events = read_note_cached(events_note(cfg), cfg)
        run_prompt = read_note_cached(run_prompt_note(cfg), cfg)
        user = (
            f"Today (Europe/London): {today}\n\n"
            f"## LGS/Events.md (source of truth for invites)\n{events}\n\n"
            f"## LGS/Run prompt.md (style + flow rules)\n{run_prompt}\n\n"
            "(No chat selected — this is the shared prompt without a person or transcript.)"
            + _composer_block(extra)
        )
    return {
        "ok": True,
        "model": chat_model(cfg),
        "system": _SYSTEM.strip(),
        "user": user,
        "mode": "revise" if extra else "generate",
        "name": name,
    }


_PHONE_RE = re.compile(
    r"(?:\+?\d[\d\s().-]{7,}\d)|(?:whatsapp\.com/)|(?:chat\.whatsapp\.com/)",
    re.I,
)
_URL_RE = re.compile(r"https?://|docs\.google\.com|eventbrite\.co\.uk", re.I)
# The two real LGS itinerary docs — shareable only if they ask for specifics
# (time, meet point, itinerary). Any OTHER link is still treated as invented.
_ALLOWED_LINKS = (
    "https://docs.google.com/document/d/14wjywN2o9TcUgcLK4fkBfXsM_yT4tkGJsbJ2HbbPfq8/edit",
    "https://docs.google.com/document/d/1z1faOPgLaJu9TAyhIZa_lWCRwNG4D7DoFAOxpTU8Rz0/edit?tab=t.0",
)
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
    # Toby never uses em/en dashes — they read as AI. Commas instead.
    raw = re.sub(r"\s*[—–]\s*", ", ", raw)
    raw = re.sub(r",\s*,+", ",", raw).strip()
    # Banned AI-tell phrases — reject so the draft regenerates.
    if re.search(r"\bno pressure\b|\bno worries if not\b", raw, re.I):
        raise ValueError("banned phrase (no pressure / no worries if not)")
    if len(raw) < 2:
        raise ValueError("draft too short")
    if len(raw) > 600:
        raise ValueError("draft too long")
    link_check = raw
    for _link in _ALLOWED_LINKS:
        link_check = link_check.replace(_link, "")
    if _URL_RE.search(link_check):
        raise ValueError("draft must not include links")
    if _PHONE_RE.search(raw):
        raise ValueError("draft must not invent phone/WhatsApp links")
    if _SENT_CLAIM_RE.search(raw):
        raise ValueError("draft claims a message was sent")
    return raw


def _chat_completion(
    messages: list[dict[str, Any]],
    cfg: dict | None = None,
    *,
    temperature: float = 0.7,
    max_tokens: int = 220,
) -> str:
    cfg = cfg if cfg is not None else load_config()
    key = api_key(cfg)
    if not key:
        raise RuntimeError("NANOGPT_API_KEY is not set")
    payload = {
        "model": chat_model(cfg),
        "temperature": temperature,
        "max_tokens": max_tokens,
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


def generate_draft(
    conn,
    name: str,
    cfg: dict | None = None,
    *,
    composer_text: str = "",
) -> str:
    """Build Obsidian context + transcript and return a validated draft string."""
    cfg = cfg if cfg is not None else load_config()
    context = load_draft_context(name, cfg)
    user = build_user_prompt(conn, name, context, composer_text=composer_text)
    text = _chat_completion(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        cfg,
    )
    return validate_draft(text)
