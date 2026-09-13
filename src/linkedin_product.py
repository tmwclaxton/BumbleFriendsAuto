"""Tag LinkedIn threads with the product we are selling (Snitch, GrantGunner, Canvassr)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading

from src.config import load_config
from src.linkedin_screen import polish_message
from src.linkedin_store import get_person, list_people, list_thread, set_product
from src.phones import phone_scope
from src.store import connect as db_connect, db_path_from_config

log = logging.getLogger(__name__)

PRODUCTS = ("snitch", "grantgunner", "canvassr")
PRODUCT_LABELS = {
    "snitch": "Snitch",
    "grantgunner": "GrantGunner",
    "canvassr": "Canvassr",
}
PRODUCT_LOGOS = {
    "snitch": "/static/products/snitch.svg",
    "grantgunner": "/static/products/grantgunner.svg?v=binoculars",
    "canvassr": "/static/products/canvassr.png",
}

_SYSTEM = """You classify a LinkedIn DM for Toby / Archie. They sell three products:

- Snitch: competitor social intelligence for local brands, creators, and small agencies — track public posts and remake what wins (snitchsocial.net).
- GrantGunner: AI grant discovery and application drafting for charities, founders, researchers, and community groups (grantgunner.org). Toby is founder.
- Canvassr: bespoke grant consultancy operated by CANVASSR LIMITED (canvassr.org). Archie is a director.

Tag only when the thread is clearly about one of these (they asked, we pitched, or both discussed it).
If more than one appears, pick the primary: most discussed, else most recent, else the one we pitched.
If unsure, or the chat is unrelated / inbound spam selling us something else, leave product empty.
Reply with JSON only: {"product":""|"snitch"|"grantgunner"|"canvassr","reason":"short"}
"""

_HINTS: dict[str, re.Pattern[str]] = {
    "snitch": re.compile(
        r"\bsnitch(?:social)?\b|snitchsocial\.net|remake what wins|competitor social",
        re.I,
    ),
    "grantgunner": re.compile(
        r"grant\s*gunner|grantgunner\.org|funding on autopilot|founder of grantgunner",
        re.I,
    ),
    "canvassr": re.compile(
        r"canvassr|canvassr\.org|archie@canvassr",
        re.I,
    ),
}


def _side_body(item) -> tuple[str, str]:
    side = item[0] if item else ""
    body = item[1] if item and len(item) > 1 else ""
    return str(side), str(body or "")


def parse_product_reply(raw: str) -> tuple[str, str]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            product = str(data.get("product") or "").strip().lower().replace(" ", "")
            if product == "grant-gunner":
                product = "grantgunner"
            if product not in PRODUCTS:
                product = ""
            return product, str(data.get("reason") or "")[:240]
    except json.JSONDecodeError:
        pass
    word = text.split()[0].lower().strip(".,:;\"'") if text else ""
    word = word.replace(" ", "")
    if word in PRODUCTS:
        return word, text[:240]
    return "", ""


def _hits(blob: str) -> list[str]:
    return [key for key, pat in _HINTS.items() if pat.search(blob or "")]


def pick_primary(messages: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Choose the most discussed / most recently mentioned / pitched product."""
    scored: dict[str, dict[str, int]] = {
        key: {"mentions": 0, "last": -1, "you": 0} for key in PRODUCTS
    }
    for idx, item in enumerate(messages):
        side, body = _side_body(item)
        found = _hits(body)
        for key in found:
            scored[key]["mentions"] += 1
            scored[key]["last"] = idx
            if side == "you":
                scored[key]["you"] += 1
    ranked = [key for key in PRODUCTS if scored[key]["mentions"]]
    if not ranked:
        return None
    ranked.sort(
        key=lambda key: (scored[key]["mentions"], scored[key]["last"], scored[key]["you"]),
        reverse=True,
    )
    winner = ranked[0]
    why = "mentioned in thread"
    if scored[winner]["you"]:
        why = "we pitched " + PRODUCT_LABELS[winner]
    return winner, why


def heuristic_product(messages: list[tuple[str, str]]) -> tuple[str, str] | None:
    blob = "\n".join(body for _side, body in (_side_body(item) for item in messages))
    if not blob.strip():
        return None
    return pick_primary(messages)


def classify_messages(messages: list[tuple[str, str]], name: str, *, cfg: dict | None = None) -> tuple[str, str]:
    guessed = heuristic_product(messages)
    blob = "\n".join(f"{side}: {body}" for side, body in (_side_body(item) for item in messages[-12:]))
    if not blob.strip():
        return "", "no messages"
    from src.draft_llm import _chat_completion

    try:
        raw = _chat_completion(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Partner: {name}\n{blob}\nJSON:"},
            ],
            cfg,
            temperature=0.1,
            max_tokens=80,
        )
        product, reason = parse_product_reply(raw)
        if product:
            return product, reason
        if guessed:
            return guessed
        return "", reason or "not a product thread"
    except Exception as exc:
        log.warning("product model failed for %s (%s)", name, exc)
        return guessed or ("", "classifier unavailable")


def message_product_fp(pairs: list[tuple[str, str]]) -> str:
    blob = "\n".join(
        f"{side}:{body.strip()}"
        for side, body in (_side_body(item) for item in pairs)
        if (body or "").strip()
    )
    if not blob:
        return ""
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def needs_product_check(stored_fp: str | None, pairs: list[tuple[str, str]]) -> bool:
    fp = message_product_fp(pairs)
    if not fp:
        return False
    return (stored_fp or "") != fp


def schedule_product_check(name: str, phone_id: str, *, force: bool = False) -> None:
    def run() -> None:
        try:
            classify_person_product(name, phone_id, force=force)
        except Exception:
            log.warning("background product check failed for %s", name, exc_info=True)

    threading.Thread(target=run, name=f"li-product-{phone_id}-{name[:20]}", daemon=True).start()


def _thread_pairs(conn, name: str, phone_id: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for row in list_thread(conn, name, phone_id=phone_id):
        side, body = polish_message(str(row["side"]), str(row["body"] or ""), name)
        if body:
            pairs.append((side, body))
    return pairs


def classify_person_product(name: str, phone_id: str, *, cfg: dict | None = None, force: bool = False) -> dict:
    cfg = cfg or load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        with phone_scope(phone_id):
            row = get_person(conn, name, phone_id)
            if row is None:
                return {"ok": False, "error": "person not found", "name": name, "phone_id": phone_id}
            existing = str(row["product"] or "") if "product" in row.keys() else ""
            stored_fp = str(row["product_fp"] or "") if "product_fp" in row.keys() else ""
            pairs = _thread_pairs(conn, name, phone_id)
            fp = message_product_fp(pairs)
            if not force and existing and stored_fp and stored_fp == fp:
                return {
                    "ok": True,
                    "name": name,
                    "phone_id": phone_id,
                    "product": existing,
                    "reason": str(row["product_reason"] or "") if "product_reason" in row.keys() else "",
                    "cached": True,
                }
            if not force and stored_fp and stored_fp == fp:
                return {
                    "ok": True,
                    "name": name,
                    "phone_id": phone_id,
                    "product": existing,
                    "reason": str(row["product_reason"] or "") if "product_reason" in row.keys() else "",
                    "cached": True,
                }
            if not fp and not force:
                return {
                    "ok": True,
                    "name": name,
                    "phone_id": phone_id,
                    "product": existing,
                    "reason": "no messages",
                    "cached": True,
                }
            product, reason = classify_messages(pairs, name, cfg=cfg)
            set_product(conn, name, product, reason, phone_id=phone_id, fingerprint=fp)
            conn.commit()
            return {
                "ok": True,
                "name": name,
                "phone_id": phone_id,
                "product": product,
                "reason": reason,
                "cached": False,
            }
    finally:
        conn.close()


def classify_unscanned_products(*, limit: int = 40, force: bool = False) -> dict:
    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    results = []
    try:
        people = list_people(conn)
    finally:
        conn.close()
    scanned = 0
    for row in people:
        if scanned >= limit:
            break
        stored_fp = str(row["product_fp"] or "") if "product_fp" in row.keys() else ""
        pid = str(row["phone_id"] or "toby")
        if not force:
            scan_conn = db_connect(db_path_from_config(cfg))
            try:
                with phone_scope(pid):
                    pairs = _thread_pairs(scan_conn, str(row["name"]), pid)
            finally:
                scan_conn.close()
            if not needs_product_check(stored_fp, pairs):
                continue
        result = classify_person_product(str(row["name"]), pid, cfg=cfg, force=force)
        results.append(result)
        scanned += 1
    tagged = [r for r in results if r.get("product")]
    return {
        "ok": True,
        "scanned": scanned,
        "tagged": len(tagged),
        "results": results,
        "message": f"scanned {scanned}, tagged {len(tagged)}",
    }
