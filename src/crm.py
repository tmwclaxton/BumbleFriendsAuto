"""Push Bumble inbox people into the Let's Go Social CRM (pipeline leads API)."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from src.config import load_config
from src.contacts import contact_preview
from src.phones import phone_scope
from src.store import connect as db_connect, db_path_from_config, set_lgs_lead_id

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
    return preview


def create_lead(payload: dict) -> dict:
    name = str(payload.get("bumble_inbox_name") or payload.get("name") or "").strip()
    phone_id = str(payload.get("bumble_phone_id") or "").strip() or None
    result = _request("POST", "/api/pipeline/leads", payload)
    if result.get("ok") and name:
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
