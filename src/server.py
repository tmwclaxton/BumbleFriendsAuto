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
from src.phone_queue import cancel_job, cancel_queued, enqueue, ensure_worker, queue_snapshot
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
    if not name or not photo_exists(name):
        return Response(status_code=404)
    return FileResponse(
        photo_file(name),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def api_thread(request: Request) -> JSONResponse:
    name = (parse_qs(request.url.query).get("name") or [""])[0]
    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        return JSONResponse(_thread_payload(conn, name))
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
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    from src.store import dismiss_needs_reply

    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
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
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    if "in_group" not in data:
        return JSONResponse({"ok": False, "error": "in_group required"}, status_code=400)
    from src.store import set_in_group

    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
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


async def api_refresh(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    name = str(data.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    job = enqueue("refresh", name)
    return JSONResponse({"ok": True, "queued": True, "job": job, "message": f"queued refresh of {name}"})


async def api_recapture(_: Request) -> JSONResponse:
    job = enqueue("recapture_all", "")
    return JSONResponse(
        {"ok": True, "queued": True, "job": job, "message": "queued full inbox recapture"}
    )


async def api_photos(_: Request) -> JSONResponse:
    job = enqueue("grab_photos", "")
    return JSONResponse(
        {"ok": True, "queued": True, "job": job, "message": "queued inbox thumbnail grab"}
    )


async def api_message_new_friends(_: Request) -> JSONResponse:
    job = enqueue("message_new_friends", "")
    return JSONResponse(
        {"ok": True, "queued": True, "job": job, "message": "queued opener to all new friends"}
    )


async def api_draft(request: Request) -> JSONResponse:
    data = await _read_json(request)
    if isinstance(data, JSONResponse):
        return data
    name = str(data.get("name") or "").strip()
    text = str(data.get("text") or "")
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    from src.store import set_draft

    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    try:
        ok = set_draft(conn, name, text)
    finally:
        conn.close()
    if not ok:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    return JSONResponse({"ok": True})


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
    job = enqueue("reply", name, text)
    return JSONResponse({"ok": True, "queued": True, "job": job, "message": f"queued reply to {name}"})


def build_app() -> Starlette:
    ensure_worker()
    mcp_app = mcp.streamable_http_app()
    routes = [
        Route("/", homepage),
        Route("/index.html", homepage),
        Route("/api/health", api_health),
        Route("/api/people", api_people),
        Route("/api/photo", api_photo),
        Route("/api/thread", api_thread),
        Route("/api/queue", api_queue),
        Route("/api/dismiss", api_dismiss, methods=["POST"]),
        Route("/api/in-group", api_in_group, methods=["POST"]),
        Route("/api/refresh", api_refresh, methods=["POST"]),
        Route("/api/recapture", api_recapture, methods=["POST"]),
        Route("/api/photos", api_photos, methods=["POST"]),
        Route("/api/message-new-friends", api_message_new_friends, methods=["POST"]),
        Route("/api/draft", api_draft, methods=["POST"]),
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
