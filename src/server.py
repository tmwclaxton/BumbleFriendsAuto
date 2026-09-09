"""Combined ASGI app: inbox dashboard + Streamable HTTP MCP at /mcp."""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from src.config import load_config
from src.dashboard import _load_html, _thread_payload, people_api_payload
from src.mcp_server import mcp
from src.phone_queue import cancel_job, cancel_queued, enqueue, enqueue_many, ensure_worker, queue_snapshot
from src.phones import DEFAULT_PHONE_ID
from src.store import connect as db_connect, db_path_from_config

log = logging.getLogger(__name__)


def _basic_creds() -> tuple[str, str] | None:
    user = (os.environ.get("DASHBOARD_BASIC_USER") or "").strip()
    password = os.environ.get("DASHBOARD_BASIC_PASSWORD") or ""
    if user and password:
        return user, password
    return None


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/api/health":
            return await call_next(request)
        creds = _basic_creds()
        if creds is None:
            return await call_next(request)
        user, password = creds
        header = request.headers.get("authorization") or ""
        ok = False
        if header.startswith("Basic "):
            try:
                raw = base64.b64decode(header[6:].strip()).decode("utf-8")
                got_user, got_pass = raw.split(":", 1)
                ok = got_user == user and got_pass == password
            except Exception:
                ok = False
        if not ok:
            return Response(
                "Authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="lgspipeline"'},
                media_type="text/plain",
            )
        return await call_next(request)


async def homepage(_: Request) -> Response:
    return HTMLResponse(_load_html().decode("utf-8"))


async def swipe_page(_: Request) -> Response:
    path = Path(__file__).with_name("swipe.html")
    return HTMLResponse(path.read_text(encoding="utf-8"))


async def api_health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def api_people(_: Request) -> JSONResponse:
    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        return JSONResponse(people_api_payload(conn))
    finally:
        conn.close()


async def api_photo(request: Request) -> Response:
    from src.photos import photo_exists, photo_file

    name = (parse_qs(request.url.query).get("name") or [""])[0]
    phone_id = (parse_qs(request.url.query).get("phone") or [DEFAULT_PHONE_ID])[0]
    if not name or not photo_exists(name, phone_id):
        return Response(status_code=404)
    return FileResponse(
        photo_file(name, phone_id),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def api_thread(request: Request) -> JSONResponse:
    name = (parse_qs(request.url.query).get("name") or [""])[0]
    phone_id = (parse_qs(request.url.query).get("phone") or [DEFAULT_PHONE_ID])[0]
    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        from src.phones import phone_scope

        with phone_scope(phone_id):
            return JSONResponse(_thread_payload(conn, name, phone_id=phone_id))
    finally:
        conn.close()


async def api_queue(_: Request) -> JSONResponse:
    return JSONResponse({"jobs": queue_snapshot()})


async def _read_json(request: Request) -> dict | JSONResponse:
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    return data


async def api_dismiss(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    name = str(data.get("name") or "").strip()
    phone_id = str(data.get("phone_id") or DEFAULT_PHONE_ID).strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    from src.phones import phone_scope
    from src.store import dismiss_needs_reply

    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        with phone_scope(phone_id):
            ok = dismiss_needs_reply(conn, name)
    finally:
        conn.close()
    if not ok:
        return JSONResponse({"ok": False, "error": "person not found"}, status_code=404)
    return JSONResponse({"ok": True, "message": f"dismissed needs-reply for {name}"})


async def api_in_group(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    name = str(data.get("name") or "").strip()
    phone_id = str(data.get("phone_id") or DEFAULT_PHONE_ID).strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    if "in_group" not in data:
        return JSONResponse({"ok": False, "error": "in_group required"}, status_code=400)
    from src.phones import phone_scope
    from src.store import set_in_group

    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        with phone_scope(phone_id):
            ok = set_in_group(conn, name, bool(data.get("in_group")))
    finally:
        conn.close()
    if not ok:
        return JSONResponse({"ok": False, "error": "person not found"}, status_code=404)
    filed = bool(data.get("in_group"))
    return JSONResponse(
        {
            "ok": True,
            "in_group": filed,
            "message": f"{name} {'added to group' if filed else 'removed from group'}",
        }
    )


async def api_ethnicity(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    name = str(data.get("name") or "").strip()
    phone_id = str(data.get("phone_id") or DEFAULT_PHONE_ID).strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    from src.phones import phone_scope
    from src.store import set_ethnicity

    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        with phone_scope(phone_id):
            ok = set_ethnicity(conn, name, str(data.get("ethnicity") or ""), source="manual")
    finally:
        conn.close()
    if not ok:
        return JSONResponse({"ok": False, "error": "person or ethnicity not valid"}, status_code=400)
    return JSONResponse({"ok": True})


async def api_ethnicity_guess(request: Request) -> JSONResponse:
    from src.ethnicity_vision import guess_status, start_guess

    if request.method == "GET":
        return JSONResponse(guess_status())
    data: dict = {}
    if request.headers.get("content-length") not in {None, "0"}:
        parsed = await _read_json(request)
        if isinstance(parsed, JSONResponse):
            return parsed
        data = parsed
    return JSONResponse(
        start_guess(
            name=str(data.get("name") or ""),
            force=bool(data.get("force")),
            phone_id=str(data.get("phone_id") or "").strip() or None,
        )
    )


async def api_ethnicity_guess_cancel(_: Request) -> JSONResponse:
    from src.ethnicity_vision import cancel_guess, guess_status

    cancel_guess()
    return JSONResponse({"ok": True, **guess_status()})


async def api_refresh(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    name = str(data.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    phone_id = str(data.get("phone_id") or "").strip() or None
    job = enqueue("refresh", name, phone_id=phone_id)
    return JSONResponse({"ok": True, "queued": True, "job": job, "message": f"queued refresh of {name}"})


def _phone_from_body(data: dict) -> str:
    return str(data.get("phone_id") or data.get("phone") or "all").strip() or "all"


async def api_recapture(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        data = {}
    jobs = enqueue_many("recapture_all", phone_id=_phone_from_body(data))
    return JSONResponse(
        {"ok": True, "queued": True, "jobs": jobs, "job": jobs[0], "message": f"queued recapture on {len(jobs)} phone(s)"}
    )


async def api_fast_scan(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        data = {}
    jobs = enqueue_many("fast_scan", phone_id=_phone_from_body(data))
    return JSONResponse(
        {"ok": True, "queued": True, "jobs": jobs, "job": jobs[0], "message": f"queued fast scan on {len(jobs)} phone(s)"}
    )


async def api_photos(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        data = {}
    jobs = enqueue_many("grab_photos", phone_id=_phone_from_body(data))
    return JSONResponse(
        {"ok": True, "queued": True, "jobs": jobs, "job": jobs[0], "message": f"queued photo grab on {len(jobs)} phone(s)"}
    )


async def api_message_new_friends(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        data = {}
    jobs = enqueue_many("message_new_friends", phone_id=_phone_from_body(data))
    return JSONResponse(
        {"ok": True, "queued": True, "jobs": jobs, "job": jobs[0], "message": f"queued openers on {len(jobs)} phone(s)"}
    )


async def api_new_friends(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        data = {}
    jobs = enqueue_many("refresh_new_friends", phone_id=_phone_from_body(data))
    return JSONResponse(
        {"ok": True, "queued": True, "jobs": jobs, "job": jobs[0], "message": f"queued strip refresh on {len(jobs)} phone(s)"}
    )


def _bulk_expired_text(data: dict) -> str:
    if bool(data.get("new_friends_only")):
        return json.dumps({"new_friends_only": True})
    return ""


async def api_unmatch(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    name = str(data.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    phone_id = str(data.get("phone_id") or "").strip() or None
    job = enqueue("unmatch", name, phone_id=phone_id)
    return JSONResponse({"ok": True, "queued": True, "job": job, "message": f"queued unmatch of {name}"})


async def api_unmatch_expired(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        data = {}
    new_only = bool(data.get("new_friends_only"))
    jobs = enqueue_many(
        "unmatch_expired",
        text=_bulk_expired_text(data),
        phone_id=_phone_from_body(data),
    )
    scope = " expired new friends" if new_only else " expired"
    return JSONResponse(
        {
            "ok": True,
            "queued": True,
            "jobs": jobs,
            "job": jobs[0],
            "message": f"queued unmatch{scope} on {len(jobs)} phone(s)",
        }
    )


async def api_rematch(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    name = str(data.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    phone_id = str(data.get("phone_id") or "").strip() or None
    job = enqueue("rematch", name, phone_id=phone_id)
    return JSONResponse({"ok": True, "queued": True, "job": job, "message": f"queued rematch of {name}"})


async def api_rematch_expired(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        data = {}
    new_only = bool(data.get("new_friends_only"))
    jobs = enqueue_many(
        "rematch_expired",
        text=_bulk_expired_text(data),
        phone_id=_phone_from_body(data),
    )
    scope = " expired new friends" if new_only else " expired"
    return JSONResponse(
        {
            "ok": True,
            "queued": True,
            "jobs": jobs,
            "job": jobs[0],
            "message": f"queued rematch{scope} on {len(jobs)} phone(s)",
        }
    )


async def api_crm_draft(request: Request) -> JSONResponse:
    name = (parse_qs(request.url.query).get("name") or [""])[0]
    phone_id = (parse_qs(request.url.query).get("phone") or [DEFAULT_PHONE_ID])[0]
    from src.crm import crm_draft

    return JSONResponse(crm_draft(name, phone_id=phone_id))


async def api_crm_lead(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    from src.crm import create_lead

    result = create_lead(data)
    status = 200
    if not result.get("ok"):
        status = int(result.get("status") or 502)
        if status < 400:
            status = 502
    elif result.get("created"):
        status = 201
    return JSONResponse(result, status_code=status)


async def api_swipe(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        data = {}
    extra = dict(data) if isinstance(data, dict) else {}
    extra.pop("phone_id", None)
    extra.pop("phone", None)
    text = json.dumps(extra) if extra else ""
    jobs = enqueue_many("swipe", text=text, phone_id=_phone_from_body(data))
    return JSONResponse(
        {"ok": True, "queued": True, "jobs": jobs, "job": jobs[0], "message": f"queued swipe on {len(jobs)} phone(s)"}
    )


async def api_swipe_prefs(request: Request) -> JSONResponse:
    from src.swipe_desk import load_prefs, save_prefs

    if request.method == "GET":
        return JSONResponse({"ok": True, "prefs": load_prefs()})
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    return JSONResponse({"ok": True, "prefs": save_prefs(data)})


async def api_swipe_desk(_: Request) -> JSONResponse:
    from src.swipe_desk import snapshot

    return JSONResponse(snapshot())


async def api_swipe_start(request: Request) -> JSONResponse:
    from src.swipe_desk import job_payload, save_prefs

    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    prefs = save_prefs(data)
    jobs = enqueue_many("swipe", text=job_payload(prefs), phone_id=prefs["phone_id"])
    return JSONResponse(
        {
            "ok": True,
            "queued": True,
            "prefs": prefs,
            "jobs": jobs,
            "job": jobs[0],
            "message": f"queued swipe on {prefs['phone_id']}",
        }
    )


async def api_swipe_decide(request: Request) -> JSONResponse:
    from src.swipe_desk import snapshot, submit_decision

    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    ok = submit_decision(str(data.get("action") or ""))
    return JSONResponse({"ok": ok, **snapshot()}, status_code=200 if ok else 400)


async def api_swipe_stop(_: Request) -> JSONResponse:
    from src.phone_queue import queue_snapshot
    from src.swipe_desk import snapshot, submit_decision

    submit_decision("stop")
    for job in queue_snapshot():
        if job.get("kind") == "swipe" and job.get("status") in {"queued", "running"}:
            cancel_job(int(job["id"]))
    return JSONResponse({"ok": True, **snapshot()})


async def api_swipe_card(_: Request) -> Response:
    from src.swipe_desk import CARD_PATH

    if not CARD_PATH.is_file() or CARD_PATH.stat().st_size < 80:
        return Response(status_code=404)
    return FileResponse(CARD_PATH, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def api_draft(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    name = str(data.get("name") or "").strip()
    text = str(data.get("text") or "")
    phone_id = str(data.get("phone_id") or DEFAULT_PHONE_ID).strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    from src.phones import phone_scope
    from src.store import set_draft

    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        with phone_scope(phone_id):
            ok = set_draft(conn, name, text)
    finally:
        conn.close()
    if not ok:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    return JSONResponse({"ok": True})


async def api_draft_prompt(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    name = str(data.get("name") or "").strip()
    phone_id = str(data.get("phone_id") or DEFAULT_PHONE_ID).strip()
    composer = str(data.get("text") or "")
    from src.draft_llm import preview_draft_prompt
    from src.phones import phone_scope

    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        with phone_scope(phone_id):
            return JSONResponse(preview_draft_prompt(conn, name, composer_text=composer, cfg=cfg))
    except Exception as exc:
        log.warning("draft prompt preview failed: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    finally:
        conn.close()


async def api_draft_generate(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    name = str(data.get("name") or "").strip()
    phone_id = str(data.get("phone_id") or DEFAULT_PHONE_ID).strip()
    composer = str(data.get("text") or "")
    if not name:
        return JSONResponse({"ok": False, "error": "open a chat first"}, status_code=400)
    from src.draft_llm import generate_draft
    from src.phones import phone_scope
    from src.store import set_draft

    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        with phone_scope(phone_id):
            text = generate_draft(conn, name, cfg, composer_text=composer)
            set_draft(conn, name, text)
    except Exception as exc:
        log.warning("manual draft failed for %s: %s", name, exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    finally:
        conn.close()
    return JSONResponse({
        "ok": True,
        "text": text,
        "mode": "revise" if composer.strip() else "generate",
    })


async def api_draft_retry(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    name = str(data.get("name") or "").strip()
    phone_id = str(data.get("phone_id") or DEFAULT_PHONE_ID).strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    from src.phones import phone_scope
    from src.store import retry_auto_draft

    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        with phone_scope(phone_id):
            ok = retry_auto_draft(conn, name)
    finally:
        conn.close()
    if not ok:
        return JSONResponse({"ok": False, "error": "nothing to retry"}, status_code=400)
    return JSONResponse({"ok": True, "queued": True})


async def api_cancel(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    try:
        job_id = int(data.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "id required"}, status_code=400)
    ok = cancel_job(job_id)
    return JSONResponse({"ok": ok, "error": None if ok else "cannot cancel"})


async def api_cancel_all(_request: Request) -> JSONResponse:
    n = cancel_queued()
    return JSONResponse({"ok": True, "cancelled": n})


async def api_reply(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    name = str(data.get("name") or "").strip()
    text = str(data.get("text") or "").strip()
    if not name or not text:
        return JSONResponse({"ok": False, "error": "name and text required"}, status_code=400)
    phone_id = str(data.get("phone_id") or "").strip() or None
    job = enqueue("reply", name, text, phone_id=phone_id)
    return JSONResponse({"ok": True, "queued": True, "job": job, "message": f"queued reply to {name}"})


def build_app() -> Starlette:
    ensure_worker()
    from src.draft_worker import ensure_draft_worker

    ensure_draft_worker()
    mcp_app = mcp.streamable_http_app()
    routes = [
        Route("/", homepage),
        Route("/index.html", homepage),
        Route("/swipe", swipe_page),
        Route("/swipe.html", swipe_page),
        Route("/api/health", api_health),
        Route("/api/people", api_people),
        Route("/api/photo", api_photo),
        Route("/api/thread", api_thread),
        Route("/api/queue", api_queue),
        Route("/api/dismiss", api_dismiss, methods=["POST"]),
        Route("/api/in-group", api_in_group, methods=["POST"]),
        Route("/api/ethnicity", api_ethnicity, methods=["POST"]),
        Route("/api/ethnicity/guess", api_ethnicity_guess, methods=["GET", "POST"]),
        Route("/api/ethnicity/guess/cancel", api_ethnicity_guess_cancel, methods=["POST"]),
        Route("/api/refresh", api_refresh, methods=["POST"]),
        Route("/api/recapture", api_recapture, methods=["POST"]),
        Route("/api/fast-scan", api_fast_scan, methods=["POST"]),
        Route("/api/photos", api_photos, methods=["POST"]),
        Route("/api/message-new-friends", api_message_new_friends, methods=["POST"]),
        Route("/api/new-friends", api_new_friends, methods=["POST"]),
        Route("/api/swipe", api_swipe, methods=["POST"]),
        Route("/api/unmatch", api_unmatch, methods=["POST"]),
        Route("/api/unmatch-expired", api_unmatch_expired, methods=["POST"]),
        Route("/api/rematch", api_rematch, methods=["POST"]),
        Route("/api/rematch-expired", api_rematch_expired, methods=["POST"]),
        Route("/api/crm/draft", api_crm_draft),
        Route("/api/crm/lead", api_crm_lead, methods=["POST"]),
        Route("/api/swipe/prefs", api_swipe_prefs, methods=["GET", "POST"]),
        Route("/api/swipe/desk", api_swipe_desk),
        Route("/api/swipe/start", api_swipe_start, methods=["POST"]),
        Route("/api/swipe/decide", api_swipe_decide, methods=["POST"]),
        Route("/api/swipe/stop", api_swipe_stop, methods=["POST"]),
        Route("/api/swipe/card", api_swipe_card),
        Route("/api/draft", api_draft, methods=["POST"]),
        Route("/api/draft/prompt", api_draft_prompt, methods=["POST"]),
        Route("/api/draft/generate", api_draft_generate, methods=["POST"]),
        Route("/api/draft/retry", api_draft_retry, methods=["POST"]),
        Route("/api/queue/cancel", api_cancel, methods=["POST"]),
        Route("/api/queue/cancel-all", api_cancel_all, methods=["POST"]),
        Route("/api/reply", api_reply, methods=["POST"]),
        # FastMCP already registers path /mcp — do not Mount("/mcp") or it becomes /mcp/mcp.
        *list(mcp_app.routes),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(BasicAuthMiddleware)
    # Propagate MCP lifespan (session manager) if present.
    if getattr(mcp_app, "router", None) is not None and mcp_app.router.lifespan_context is not None:
        app.router.lifespan_context = mcp_app.router.lifespan_context
    return app


def serve_combined(host: str, port: int, db_path: Path) -> int:
    import uvicorn

    os.environ.setdefault("DB_PATH", str(db_path))
    app = build_app()
    log.info("Inbox+MCP at http://%s:%s/  mcp=/mcp  (db %s)", host, port, db_path)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0
