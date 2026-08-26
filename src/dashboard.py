"""Local web inbox: read SQLite threads and send replies via the phone."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.config import ROOT, load_config
from src.store import connect as db_connect, db_path_from_config, list_people, list_thread

log = logging.getLogger(__name__)

_HTML_PATH = Path(__file__).with_name("dashboard.html")
_PID_PATH = ROOT / "data" / "dashboard.pid"
_LOG_PATH = ROOT / "data" / "dashboard.log"
_QUEUE_PATH = ROOT / "data" / "action_queue.json"
_ENV_SUPERVISOR = "BFF_DASHBOARD_SUPERVISOR"
_ENV_WORKER = "BFF_DASHBOARD_WORKER"

_jobs: list[dict] = []
_jobs_lock = threading.Lock()
_jobs_wake = threading.Event()
_job_seq = 0


def _queue_snapshot() -> list[dict]:
    with _jobs_lock:
        return [dict(j) for j in _jobs]


def _persist_queue() -> None:
    pending = [j for j in _jobs if j.get("status") in {"queued", "running"}]
    _QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _QUEUE_PATH.write_text(json.dumps(pending, indent=2), encoding="utf-8")


def _load_queue() -> None:
    global _job_seq
    if not _QUEUE_PATH.exists():
        return
    try:
        raw = json.loads(_QUEUE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    restored: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        job = dict(item)
        if job.get("status") == "running":
            job["status"] = "queued"
            job["error"] = None
        if job.get("status") != "queued":
            continue
        restored.append(job)
    with _jobs_lock:
        _jobs[:] = restored
        _job_seq = max([int(j.get("id") or 0) for j in _jobs] + [_job_seq])
    if restored:
        log.info("restored %d queued phone action(s)", len(restored))
        _jobs_wake.set()


def _enqueue(kind: str, name: str, text: str = "") -> dict:
    global _job_seq
    with _jobs_lock:
        _job_seq += 1
        job = {
            "id": _job_seq,
            "kind": kind,
            "name": name,
            "text": text,
            "status": "queued",
            "error": None,
            "message": None,
        }
        _jobs.append(job)
        _persist_queue()
        snap = dict(job)
    _jobs_wake.set()
    return snap


def _cancel_job(job_id: int) -> bool:
    with _jobs_lock:
        for job in _jobs:
            if int(job["id"]) == job_id and job["status"] == "queued":
                job["status"] = "cancelled"
                snap = dict(job)
                _persist_queue()
                break
        else:
            return False
    if snap.get("kind") == "reply":
        from src.store import set_draft

        conn = db_connect(db_path_from_config(load_config()))
        try:
            set_draft(conn, str(snap.get("name") or ""), str(snap.get("text") or ""))
        finally:
            conn.close()
    return True


def _update_job(job_id: int, **fields: object) -> None:
    with _jobs_lock:
        for job in _jobs:
            if int(job["id"]) == job_id:
                job.update(fields)
                done = [j for j in _jobs if j["status"] in {"done", "error", "cancelled"}]
                if len(done) > 40:
                    keep_done = done[-20:]
                    keep_ids = {id(j) for j in keep_done}
                    _jobs[:] = [
                        j
                        for j in _jobs
                        if j["status"] in {"queued", "running"} or id(j) in keep_ids
                    ]
                _persist_queue()
                return


def _next_queued() -> dict | None:
    with _jobs_lock:
        for job in _jobs:
            if job["status"] == "queued":
                return dict(job)
    return None


def _run_job(job: dict) -> tuple[bool, str]:
    kind = job["kind"]
    name = job["name"]
    if kind == "reply":
        from src.messenger import send_named_message

        return send_named_message(name, str(job.get("text") or ""))
    if kind == "refresh":
        from src.sync_chats import refresh_named_chat

        return refresh_named_chat(name)
    return False, f"unknown action {kind}"


def _queue_worker() -> None:
    log.info("phone action queue ready")
    while True:
        job = _next_queued()
        if job is None:
            _jobs_wake.wait(timeout=1.0)
            _jobs_wake.clear()
            continue
        _update_job(job["id"], status="running")
        log.info("queue run #%s %s %s", job["id"], job["kind"], job["name"])
        try:
            ok, message = _run_job(job)
        except Exception as exc:
            log.exception("queue job failed")
            ok, message = False, str(exc)
        _update_job(
            job["id"],
            status="done" if ok else "error",
            message=message,
            error=None if ok else message,
        )
        if job.get("kind") == "reply":
            from src.store import set_draft

            conn = db_connect(db_path_from_config(load_config()))
            try:
                person = str(job.get("name") or "")
                if ok:
                    set_draft(conn, person, "")
                else:
                    set_draft(conn, person, str(job.get("text") or ""))
            except Exception:
                log.exception("could not save draft after reply job")
            finally:
                conn.close()
        log.info("queue #%s %s — %s", job["id"], "ok" if ok else "fail", message)


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
        SELECT p.name, c.status, c.last_from, c.last_text, c.preview, c.draft
        FROM people p
        LEFT JOIN chats c ON c.person_id = p.id
        WHERE p.name = ?
        """,
        (name,),
    ).fetchone()
    msgs = [
        {"side": r["side"], "body": r["body"], "from_preview": False}
        for r in list_thread(conn, name)
    ]
    if row is None:
        return {"name": name, "messages": msgs, "status": "unknown", "draft": ""}
    extras: list[str] = []
    for candidate in (row["last_text"], row["preview"]):
        text = (candidate or "").strip()
        if not text or _is_day_label(text):
            continue
        if text not in extras:
            extras.append(text)
    have = {_norm_body(m["body"]) for m in msgs}
    for text in extras:
        if _preview_already_in_thread(text, msgs):
            continue
        side = row["last_from"] or "them"
        real = [m for m in msgs if not _is_day_label(m["body"])]
        # Inbox preview is often newer than the last captured bubble.
        if real and real[-1]["side"] == "you" and side == "you":
            side = "them"
        msgs.append({"side": side, "body": text, "from_preview": True})
        have.add(_norm_body(text))
    return {
        "name": row["name"] or name,
        "messages": msgs,
        "status": row["status"] or "unknown",
        "draft": row["draft"] or "",
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

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._html()
            return
        if parsed.path == "/api/people":
            conn = db_connect(self.server.db_path)  # type: ignore[attr-defined]
            try:
                people = []
                for row in list_people(conn):
                    people.append(
                        {
                            "name": row["name"],
                            "status": row["status"] or "unknown",
                            "last_from": row["last_from"],
                            "last_text": row["last_text"],
                            "preview": row["preview"],
                            "location": row["location"],
                            "phone_provided": bool(row["phone_provided"]),
                            "draft": row["draft"] or "",
                        }
                    )
                self._json({"people": people})
            finally:
                conn.close()
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
            self._json({"jobs": _queue_snapshot()})
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
            job = _enqueue("refresh", name)
            self._json({"ok": True, "queued": True, "job": job, "message": f"queued refresh of {name}"})
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
            ok = _cancel_job(job_id)
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
        job = _enqueue("reply", name, text)
        self._json({"ok": True, "queued": True, "job": job, "message": f"queued reply to {name}"})


class InboxServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _serve_worker(host: str, port: int, db_path: Path) -> int:
    _load_queue()
    threading.Thread(target=_queue_worker, name="phone-queue", daemon=True).start()
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
