"""Local web inbox: read SQLite threads and send replies via the phone."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.config import ROOT, load_config
from src.phone_queue import cancel_job, enqueue, ensure_worker, queue_snapshot
from src.photos import photo_exists, photo_file
from src.store import (
    connect as db_connect,
    db_path_from_config,
    is_match_chrome,
    is_new_friend,
    list_people,
    list_thread,
    parse_hours_left,
)

log = logging.getLogger(__name__)

_HTML_PATH = Path(__file__).with_name("dashboard.html")
_PID_PATH = ROOT / "data" / "dashboard.pid"
_LOG_PATH = ROOT / "data" / "dashboard.log"
_ENV_SUPERVISOR = "BFF_DASHBOARD_SUPERVISOR"
_ENV_WORKER = "BFF_DASHBOARD_WORKER"


def _basic_auth_configured() -> tuple[str, str] | None:
    user = (os.environ.get("DASHBOARD_BASIC_USER") or "").strip()
    password = os.environ.get("DASHBOARD_BASIC_PASSWORD") or ""
    if user and password:
        return user, password
    return None


def _check_basic_auth(handler: BaseHTTPRequestHandler) -> bool:
    creds = _basic_auth_configured()
    if creds is None:
        return True
    user, password = creds
    header = handler.headers.get("Authorization") or ""
    if not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:].strip()).decode("utf-8")
    except Exception:
        return False
    if ":" not in raw:
        return False
    got_user, got_pass = raw.split(":", 1)
    return got_user == user and got_pass == password


def _unauthorized(handler: BaseHTTPRequestHandler) -> None:
    body = b"Authentication required"
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", 'Basic realm="lgspipeline"')
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _norm_body(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _is_day_label(text: str) -> bool:
    return bool(re.match(r"^\d{1,2} [A-Za-z]+ 20\d{2}$", (text or "").strip()))


def _preview_already_in_thread(preview: str, msgs: list[dict]) -> bool:
    """Inbox list often ellipsizes the last bubble — don't append that stub."""
    norm = _norm_body(preview)
    if not norm:
        return True
    stub = _norm_body(preview.rstrip(".").strip()) if preview.rstrip().endswith("...") else norm
    for msg in msgs:
        body = _norm_body(msg["body"])
        if body == norm or body.startswith(stub) or stub.startswith(body):
            return True
    return False


def _thread_payload(conn, name: str) -> dict:
    row = conn.execute(
        """
        SELECT p.name, c.status, c.last_from, c.last_text, c.preview, c.draft, c.message_until
        FROM people p
        LEFT JOIN chats c ON c.person_id = p.id
        WHERE p.name = ?
        """,
        (name,),
    ).fetchone()
    msgs = [
        {"side": r["side"], "body": r["body"], "from_preview": False}
        for r in list_thread(conn, name)
        if not parse_hours_left(r["body"])
        and (r["body"] or "").strip().lower() not in {
            "extend",
            "no messages yet",
            "(no messages yet)",
        }
    ]
    if row is None:
        return {"name": name, "messages": msgs, "status": "unknown", "draft": "", "message_until": None}
    extras: list[str] = []
    for candidate in (row["last_text"], row["preview"]):
        text = (candidate or "").strip()
        if not text or _is_day_label(text) or is_match_chrome(text):
            continue
        if text not in extras:
            extras.append(text)
    for text in extras:
        if _preview_already_in_thread(text, msgs):
            continue
        side = row["last_from"] or "them"
        real = [m for m in msgs if not _is_day_label(m["body"])]
        if real and real[-1]["side"] == "you" and side == "you":
            side = "them"
        msgs.append({"side": side, "body": text, "from_preview": True})
    return {
        "name": row["name"] or name,
        "messages": msgs,
        "status": row["status"] or "unknown",
        "draft": row["draft"] or "",
        "message_until": row["message_until"],
    }


def people_api_payload(conn) -> dict:
    from src.chats import format_opener

    cfg = load_config()
    template = str(
        (cfg.get("messenger") or {}).get(
            "template",
            "Hi {name}, I'm putting together a wee group for hiking / board games / sports. "
            "Does that sound like something you would be interested in?",
        )
    )
    people = []
    new_friends: list[str] = []
    from src.store import namesake_meta

    labels = namesake_meta(conn)
    for row in list_people(conn):
        fresh = is_new_friend(row)
        extra = labels.get(str(row["name"])) or {}
        item = {
            "name": row["name"],
            "display_name": extra.get("display_name") or row["name"],
            "base_name": extra.get("base_name") or row["name"],
            "distinguish": extra.get("distinguish") or "",
            "same_name_count": int(extra.get("same_name_count") or 1),
            "status": row["status"] or "unknown",
            "last_from": row["last_from"],
            "last_text": row["last_text"],
            "preview": row["preview"],
            "location": row["location"],
            "phone_provided": bool(row["phone_provided"]),
            "draft": row["draft"] or "",
            "opener_sent": bool(row["opener_sent"]),
            "new_friend": fresh,
            "message_until": row["message_until"],
            "opener": format_opener(template, str(row["name"])) if fresh else None,
            "photo": photo_exists(str(row["name"])),
        }
        people.append(item)
        if fresh:
            new_friends.append(str(row["name"]))
    return {
        "people": people,
        "new_friends": new_friends,
        "opener_template": template,
    }


def _load_html() -> bytes:
    return _HTML_PATH.read_bytes()


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _watch_stamp() -> float:
    latest = 0.0
    for path in Path(__file__).parent.glob("*.py"):
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            pass
    return latest


def _write_pid(pid: int) -> None:
    _PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PID_PATH.write_text(str(pid), encoding="utf-8")


def _read_pid() -> int | None:
    try:
        return int(_PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _stop(host: str, port: int) -> int:
    pid = _read_pid()
    if pid and _pid_alive(pid):
        log.info("stopping dashboard pid %s", pid)
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for _ in range(30):
            if not _pid_alive(pid) and not _port_open(host, port):
                break
            time.sleep(0.1)
        if _pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
    elif _port_open(host, port):
        log.warning("port %s is in use but pid file is stale", port)
    if _PID_PATH.exists():
        _PID_PATH.unlink()
    return 0


class Handler(BaseHTTPRequestHandler):
    server_version = "BffInbox/1.0"

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s " + fmt, self.address_string(), *args)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self) -> None:
        body = _load_html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_auth(self) -> bool:
        if _check_basic_auth(self):
            return True
        _unauthorized(self)
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._html()
            return
        if parsed.path == "/api/people":
            conn = db_connect(self.server.db_path)  # type: ignore[attr-defined]
            try:
                self._json(people_api_payload(conn))
            finally:
                conn.close()
            return
        if parsed.path == "/api/photo":
            name = (parse_qs(parsed.query).get("name") or [""])[0]
            path = photo_file(name)
            if not name or not photo_exists(name):
                self.send_error(404)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/thread":
            name = (parse_qs(parsed.query).get("name") or [""])[0]
            conn = db_connect(self.server.db_path)  # type: ignore[attr-defined]
            try:
                self._json(_thread_payload(conn, name))
            finally:
                conn.close()
            return
        if parsed.path == "/api/queue":
            self._json({"jobs": queue_snapshot()})
            return
        if parsed.path == "/api/health":
            self._json({"ok": True})
            return
        self.send_error(404)

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json({"ok": False, "error": "invalid json"}, 400)
            return None
        if not isinstance(data, dict):
            self._json({"ok": False, "error": "invalid json"}, 400)
            return None
        return data

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        if self.path == "/api/dismiss":
            data = self._read_json()
            if data is None:
                return
            name = str(data.get("name") or "").strip()
            if not name:
                self._json({"ok": False, "error": "name required"}, 400)
                return
            from src.store import dismiss_needs_reply

            conn = db_connect(self.server.db_path)  # type: ignore[attr-defined]
            try:
                ok = dismiss_needs_reply(conn, name)
            finally:
                conn.close()
            if not ok:
                self._json({"ok": False, "error": "person not found"}, 404)
                return
            self._json({"ok": True, "message": f"dismissed needs-reply for {name}"})
            return
        if self.path == "/api/refresh":
            data = self._read_json()
            if data is None:
                return
            name = str(data.get("name") or "").strip()
            if not name:
                self._json({"ok": False, "error": "name required"}, 400)
                return
            job = enqueue("refresh", name)
            self._json({"ok": True, "queued": True, "job": job, "message": f"queued refresh of {name}"})
            return
        if self.path == "/api/recapture":
            job = enqueue("recapture_all", "")
            self._json(
                {
                    "ok": True,
                    "queued": True,
                    "job": job,
                    "message": "queued full inbox recapture",
                }
            )
            return
        if self.path == "/api/photos":
            job = enqueue("grab_photos", "")
            self._json(
                {
                    "ok": True,
                    "queued": True,
                    "job": job,
                    "message": "queued inbox thumbnail grab",
                }
            )
            return
        if self.path == "/api/message-new-friends":
            job = enqueue("message_new_friends", "")
            self._json(
                {
                    "ok": True,
                    "queued": True,
                    "job": job,
                    "message": "queued opener to all new friends",
                }
            )
            return
        if self.path == "/api/draft":
            data = self._read_json()
            if data is None:
                return
            name = str(data.get("name") or "").strip()
            text = str(data.get("text") or "")
            if not name:
                self._json({"ok": False, "error": "name required"}, 400)
                return
            from src.store import set_draft

            conn = db_connect(self.server.db_path)  # type: ignore[attr-defined]
            try:
                ok = set_draft(conn, name, text)
            finally:
                conn.close()
            if not ok:
                self._json({"ok": False, "error": "name required"}, 400)
                return
            self._json({"ok": True})
            return
        if self.path == "/api/queue/cancel":
            data = self._read_json()
            if data is None:
                return
            try:
                job_id = int(data.get("id"))
            except (TypeError, ValueError):
                self._json({"ok": False, "error": "id required"}, 400)
                return
            ok = cancel_job(job_id)
            self._json({"ok": ok, "error": None if ok else "cannot cancel"})
            return
        if self.path != "/api/reply":
            self.send_error(404)
            return
        data = self._read_json()
        if data is None:
            return
        name = str(data.get("name") or "").strip()
        text = str(data.get("text") or "").strip()
        if not name or not text:
            self._json({"ok": False, "error": "name and text required"}, 400)
            return
        job = enqueue("reply", name, text)
        self._json({"ok": True, "queued": True, "job": job, "message": f"queued reply to {name}"})


class InboxServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _serve_worker(host: str, port: int, db_path: Path) -> int:
    ensure_worker()
    # Combined ASGI app (dashboard + MCP) when available; else classic HTTP only.
    if os.environ.get("BFF_COMBINED_SERVER", "1") == "1":
        try:
            from src.server import serve_combined

            return serve_combined(host, port, db_path)
        except Exception:
            log.exception("combined server failed; falling back to classic dashboard")
    httpd = InboxServer((host, port), Handler)
    httpd.db_path = db_path  # type: ignore[attr-defined]
    log.info("Inbox at http://%s:%s/  (db %s)", host, port, db_path)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("stopped")
    finally:
        httpd.server_close()
    return 0


def _run_supervisor(host: str, port: int, extra: list[str]) -> int:
    _write_pid(os.getpid())
    log.info("dashboard supervisor pid %s", os.getpid())
    while True:
        stamp = _watch_stamp()
        env = os.environ.copy()
        env[_ENV_WORKER] = "1"
        env.pop(_ENV_SUPERVISOR, None)
        cmd = [sys.executable, "-m", "src.dashboard", "--host", host, "--port", str(port), *extra]
        worker = subprocess.Popen(cmd, env=env, cwd=str(ROOT))
        while True:
            time.sleep(0.5)
            code = worker.poll()
            if code is not None:
                log.warning("dashboard worker exited %s — restarting", code)
                time.sleep(0.4)
                break
            if _watch_stamp() > stamp:
                log.info("code changed — restarting dashboard worker")
                worker.terminate()
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.kill()
                break


def _detach_supervisor(host: str, port: int, argv: list[str]) -> int:
    if _port_open(host, port):
        log.info("Inbox already running at http://%s:%s/", host, port)
        return 0
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env[_ENV_SUPERVISOR] = "1"
    log_f = open(_LOG_PATH, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.dashboard", *argv],
        env=env,
        cwd=str(ROOT),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    _write_pid(proc.pid)
    for _ in range(50):
        if _port_open(host, port):
            log.info("Inbox at http://%s:%s/  (detached pid %s, log %s)", host, port, proc.pid, _LOG_PATH)
            return 0
        time.sleep(0.1)
    log.error("dashboard did not bind %s:%s — see %s", host, port, _LOG_PATH)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local BFF inbox dashboard")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--stop", action="store_true", help="Stop the detached dashboard")
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Run in this terminal (no detach / auto-restart)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.stop:
        return _stop(args.host, args.port)

    extra: list[str] = []
    if args.config:
        extra.extend(["--config", str(args.config)])

    if os.environ.get(_ENV_WORKER) == "1":
        cfg = load_config(args.config)
        return _serve_worker(args.host, args.port, db_path_from_config(cfg))

    if os.environ.get(_ENV_SUPERVISOR) == "1":
        return _run_supervisor(args.host, args.port, extra)

    if args.foreground:
        cfg = load_config(args.config)
        return _serve_worker(args.host, args.port, db_path_from_config(cfg))

    forwarded: list[str] = ["--host", args.host, "--port", str(args.port)]
    if args.config:
        forwarded.extend(["--config", str(args.config)])
    return _detach_supervisor(args.host, args.port, forwarded)


if __name__ == "__main__":
    sys.exit(main())
