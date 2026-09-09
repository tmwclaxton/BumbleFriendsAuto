"""Push Bumble inbox people into the Let's Go Social CRM (pipeline leads API)."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from src.config import load_config
from src.contacts import contact_preview
from src.crm_llm import enrich_crm_fields
from src.phones import phone_scope
from src.store import connect as db_connect, db_path_from_config, list_thread, set_lgs_lead_id

log = logging.getLogger(__name__)


def _lgs_settings(cfg: dict | None = None) -> tuple[str, str]:
    cfg = cfg or load_config()
    block = cfg.get("lgs") if isinstance(cfg.get("lgs"), dict) else {}
    url = (
        os.environ.get("LGS_API_URL")
        or str(block.get("api_url") or "")
        or "http://127.0.0.1:8099"
    ).rstrip("/")
    token = os.environ.get("LGS_PIPELINE_TOKEN") or str(block.get("pipeline_token") or "")
    return url, token


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    url, token = _lgs_settings()
    if not token:
        return {"ok": False, "error": "LGS_PIPELINE_TOKEN is not set on this inbox"}
    body = None if payload is None else json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode()
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                return {"ok": False, "error": "unexpected CRM response"}
            data.setdefault("ok", True)
            return data
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        try:
            parsed = json.loads(detail)
            if isinstance(parsed, dict) and parsed.get("error"):
                return {"ok": False, "error": str(parsed["error"]), "status": exc.code}
            if isinstance(parsed, dict) and parsed.get("message"):
                return {"ok": False, "error": str(parsed["message"]), "status": exc.code}
        except json.JSONDecodeError:
            pass
        return {"ok": False, "error": f"CRM HTTP {exc.code}: {detail}", "status": exc.code}
    except Exception as exc:
        log.warning("LGS CRM request failed: %s", exc)
        return {"ok": False, "error": f"could not reach Let's Go Social ({exc})"}


def list_groups() -> dict:
    return _request("GET", "/api/pipeline/groups")


def _as_age(value: object) -> int | None:
    try:
        age = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return age if 16 <= age <= 80 else None


def get_lead(lead_id: int) -> dict:
    result = _request("GET", f"/api/pipeline/contacts/{int(lead_id)}")
    if result.get("ok"):
        return result
    return _request("GET", f"/api/pipeline/leads/{int(lead_id)}")


def crm_draft(name: str, phone_id: str | None = None) -> dict:
    preview = contact_preview(name, phone_id=phone_id)
    if not preview.get("ok"):
        return preview
    groups = list_groups()
    if not groups.get("ok"):
        preview["groups"] = []
        preview["groups_error"] = groups.get("error")
    else:
        preview["groups"] = groups.get("groups") or []
        preview["groups_error"] = None
    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        with phone_scope(phone_id or preview.get("phone_id")):
            messages = [
                {"side": row["side"], "body": row["body"]} for row in list_thread(conn, name)
            ]
    finally:
        conn.close()
    extracted = enrich_crm_fields(
        messages,
        inbox_name=name,
        display_name=str(preview.get("display_name") or name),
        profile_location=str(preview.get("location") or ""),
        profile_age=_as_age(preview.get("age")),
        ethnicity=str(preview.get("ethnicity") or ""),
        phone_id=str(preview.get("phone_id") or phone_id or "toby"),
        groups=preview.get("groups") or [],
        cfg=cfg,
    )
    preview.update(extracted)
    lead_id = preview.get("lgs_lead_id")
    if lead_id:
        existing = get_lead(int(lead_id))
        card = existing.get("contact") or existing.get("lead")
        if existing.get("ok") and isinstance(card, dict):
            preview["existing_lead"] = card
            preview["existing_contact"] = card
    return preview


def _crm_payload(payload: dict) -> dict:
    tags = payload.get("tags")
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",") if part.strip()]
    group_id = payload.get("closest_lgs_group_id") or payload.get("home_lgs_group_id")
    body = {
        "name": payload.get("name"),
        "phone": payload.get("phone"),
        "email": payload.get("email") or None,
        "address": payload.get("address") or None,
        "region": payload.get("region") or payload.get("hometown") or None,
        "hometown": payload.get("hometown") or payload.get("region") or None,
        "instagram": payload.get("instagram") or None,
        "tiktok": payload.get("tiktok") or None,
        "age": _as_age(payload.get("age")),
        "ethnicity": payload.get("ethnicity") or None,
        "interested_event": payload.get("interested_event") or None,
        "interests_skills": payload.get("interests_skills") or None,
        "notes": payload.get("notes") or None,
        "tags": tags or None,
        "status": payload.get("status") or "prospect",
        "source": payload.get("source") or "bumble_friends",
        "preferred_contact_method": payload.get("preferred_contact_method") or None,
        "consent_to_contact": bool(payload.get("consent_to_contact", True)),
        "attach_bumble_logs": bool(payload.get("attach_bumble_logs", True)),
        "next_follow_up_at": payload.get("next_follow_up_at") or None,
        "closest_lgs_group_id": int(group_id) if group_id else None,
        "home_lgs_group_id": int(group_id) if group_id else None,
        "bumble_inbox_name": payload.get("bumble_inbox_name"),
        "bumble_phone_id": payload.get("bumble_phone_id"),
    }
    return {key: value for key, value in body.items() if value is not None}


def create_lead(payload: dict) -> dict:
    name = str(payload.get("bumble_inbox_name") or payload.get("name") or "").strip()
    phone_id = str(payload.get("bumble_phone_id") or "").strip() or None
    body = _crm_payload(payload)
    result = _request("POST", "/api/pipeline/contacts", body)
    if not result.get("ok") and int(result.get("status") or 0) in {404, 405}:
        result = _request("POST", "/api/pipeline/leads", body)
    if result.get("ok") and name:
        lead = result.get("contact") if isinstance(result.get("contact"), dict) else {}
        if not lead:
            lead = result.get("lead") if isinstance(result.get("lead"), dict) else {}
        lead_id = lead.get("id")
        if lead_id:
            cfg = load_config()
            conn = db_connect(db_path_from_config(cfg))
            try:
                with phone_scope(phone_id):
                    set_lgs_lead_id(conn, name, int(lead_id))
            finally:
                conn.close()
    return result
