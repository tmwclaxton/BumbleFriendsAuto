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
from src.phone_queue import cancel_job, cancel_queued, enqueue, enqueue_many, ensure_worker, queue_snapshot
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


def _thread_payload(conn, name: str, phone_id: str | None = None) -> dict:
    from src.phones import DEFAULT_PHONE_ID, current_phone_id

    pid = phone_id or current_phone_id() or DEFAULT_PHONE_ID
    row = conn.execute(
        """
        SELECT p.name, p.phone_id, c.status, c.last_from, c.last_text, c.preview, c.draft, c.message_until,
               c.in_group, c.archived, c.draft_status, c.draft_error, c.draft_attempts, c.draft_pending_fp
        FROM people p
        LEFT JOIN chats c ON c.person_id = p.id
        WHERE p.name = ? AND p.phone_id = ?
        """,
        (name, pid),
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
        return {
            "name": name,
            "phone_id": pid,
            "messages": msgs,
            "status": "unknown",
            "draft": "",
            "message_until": None,
            "in_group": False,
            "archived": False,
            "draft_status": "idle",
            "draft_error": "",
            "draft_attempts": 0,
            "draft_pending": False,
        }
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
    from src.store import auto_draft_fields

    return {
        "name": row["name"] or name,
        "phone_id": row["phone_id"] if "phone_id" in row.keys() else pid,
        "messages": msgs,
        "status": row["status"] or "unknown",
        "draft": row["draft"] or "",
        "message_until": row["message_until"],
        "in_group": bool(row["in_group"]),
        "archived": bool(row["archived"]) if "archived" in row.keys() else False,
        **auto_draft_fields(row),
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
    from src.phones import public_phones
    from src.store import auto_draft_fields, extract_phones, namesake_meta

    labels = namesake_meta(conn)
    phone_meta = {p["id"]: p for p in public_phones(cfg)}
    for row in list_people(conn):
        fresh = is_new_friend(row)
        pid = str(row["phone_id"] if "phone_id" in row.keys() else "toby")
        extra = labels.get(f"{pid}:{row['name']}") or labels.get(str(row["name"])) or {}
        item = {
            "name": row["name"],
            "phone_id": pid,
            "phone_label": (phone_meta.get(pid) or {}).get("label") or pid,
            "phone_device": (phone_meta.get(pid) or {}).get("device") or "",
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
            "lgs_lead_id": row["lgs_lead_id"] if "lgs_lead_id" in row.keys() else None,
            "in_contacts": bool(row["in_contacts"]) if "in_contacts" in row.keys() else False,
            "draft": row["draft"] or "",
            "opener_sent": bool(row["opener_sent"]),
            "new_friend": fresh,
            "message_until": row["message_until"],
            "opener": format_opener(template, str(row["name"])) if fresh else None,
            "photo": photo_exists(str(row["name"]), pid),
            "dismissed": (row["status"] or "") == "dismissed",
            "in_group": bool(row["in_group"]),
            "archived": bool(row["archived"]) if "archived" in row.keys() else False,
            "ethnicity": row["ethnicity"] or "",
            "ethnicity_source": row["ethnicity_source"] or "",
            "phones": list(dict.fromkeys(
                phone
                for blob in (row["last_text"], row["preview"])
                for phone in extract_phones(blob or "")
            )),
            **auto_draft_fields(row),
        }
        people.append(item)
        if fresh:
            new_friends.append(str(row["name"]))
    from src.ethnicity_vision import guess_status
    from src.draft_worker import draft_status
    from src.profile_filters import ETHNICITY_CHOICES

    return {
        "people": people,
        "new_friends": new_friends,
        "opener_template": template,
        "ethnicity_choices": [{"id": cid, "label": label} for cid, label in ETHNICITY_CHOICES],
        "ethnicity_guess": guess_status(),
        "auto_draft": draft_status(),
        "phones": list(phone_meta.values()),
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
            qs = parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0]
            phone_id = (qs.get("phone") or [""])[0].strip() or None
            number = (qs.get("number") or [""])[0].strip() or None
            conn = db_connect(self.server.db_path)  # type: ignore[attr-defined]
            try:
                if not name and number:
                    from src.store import find_person_by_phone_digits

                    person = find_person_by_phone_digits(conn, number)
                    if person is None:
                        self.send_error(404)
                        return
                    name = str(person["name"])
                    phone_id = str(person["phone_id"] or phone_id or "")
                from src.phones import phone_scope

                with phone_scope(phone_id):
                    payload = _thread_payload(conn, name, phone_id)
                payload["phone_id"] = phone_id or payload.get("phone_id") or ""
                self._json(payload)
            finally:
                conn.close()
            return
        if parsed.path == "/api/queue":
            self._json({"jobs": queue_snapshot()})
            return
        if parsed.path == "/api/ethnicity/guess":
            from src.ethnicity_vision import guess_status

            self._json(guess_status())
            return
        if parsed.path == "/api/health":
            from src.phones import public_phones

            phones = public_phones()
            offline = [p["label"] for p in phones if p.get("ready") and not p.get("online")]
            self._json({"ok": True, "phones": phones, "offline": offline})
            return
        if parsed.path == "/api/whatsapp/groups":
            from src.whatsapp import listed_groups

            self._json({"ok": True, "groups": listed_groups()})
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
        if self.path == "/api/in-group":
            data = self._read_json()
            if data is None:
                return
            name = str(data.get("name") or "").strip()
            if not name:
                self._json({"ok": False, "error": "name required"}, 400)
                return
            if "in_group" not in data:
                self._json({"ok": False, "error": "in_group required"}, 400)
                return
            from src.store import set_in_group

            conn = db_connect(self.server.db_path)  # type: ignore[attr-defined]
            try:
                ok = set_in_group(conn, name, bool(data.get("in_group")))
            finally:
                conn.close()
            if not ok:
                self._json({"ok": False, "error": "person not found"}, 404)
                return
            filed = bool(data.get("in_group"))
            self._json(
                {
                    "ok": True,
                    "in_group": filed,
                    "message": f"{name} {'added to group' if filed else 'removed from group'}",
                }
            )
            return
        if self.path == "/api/archive":
            data = self._read_json()
            if data is None:
                return
            name = str(data.get("name") or "").strip()
            if not name:
                self._json({"ok": False, "error": "name required"}, 400)
                return
            if "archived" not in data:
                self._json({"ok": False, "error": "archived required"}, 400)
                return
            from src.phones import phone_scope
            from src.store import set_archived

            conn = db_connect(self.server.db_path)  # type: ignore[attr-defined]
            try:
                with phone_scope(str(data.get("phone_id") or "") or None):
                    ok = set_archived(conn, name, bool(data.get("archived")))
            finally:
                conn.close()
            if not ok:
                self._json({"ok": False, "error": "person not found"}, 404)
                return
            hidden = bool(data.get("archived"))
            self._json(
                {
                    "ok": True,
                    "archived": hidden,
                    "message": f"{name} {'archived' if hidden else 'unarchived'}",
                }
            )
            return
        if self.path == "/api/ethnicity":
            data = self._read_json()
            if data is None:
                return
            name = str(data.get("name") or "").strip()
            if not name:
                self._json({"ok": False, "error": "name required"}, 400)
                return
            from src.store import set_ethnicity

            conn = db_connect(self.server.db_path)  # type: ignore[attr-defined]
            try:
                ok = set_ethnicity(conn, name, str(data.get("ethnicity") or ""), source="manual")
            finally:
                conn.close()
            if not ok:
                self._json({"ok": False, "error": "person or ethnicity not valid"}, 400)
                return
            self._json({"ok": True})
            return
        if self.path == "/api/ethnicity/guess":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                data = self._read_json()
                if data is None:
                    return
            else:
                data = {}
            from src.ethnicity_vision import start_guess

            self._json(
                start_guess(
                    name=str(data.get("name") or ""),
                    force=bool(data.get("force")),
                )
            )
            return
        if self.path == "/api/ethnicity/guess/cancel":
            from src.ethnicity_vision import cancel_guess, guess_status

            cancel_guess()
            self._json({"ok": True, **guess_status()})
            return
        if self.path == "/api/refresh":
            data = self._read_json()
            if data is None:
                return
            name = str(data.get("name") or "").strip()
            if not name:
                self._json({"ok": False, "error": "name required"}, 400)
                return
            job = enqueue("refresh", name, phone_id=str(data.get("phone_id") or "") or None)
            self._json({"ok": True, "queued": True, "job": job, "message": f"queued refresh of {name}"})
            return
        if self.path == "/api/recapture":
            job = enqueue_many("recapture_all", phone_id="all")[0]
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
            job = enqueue_many("grab_photos", phone_id="all")[0]
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
            job = enqueue_many("message_new_friends", phone_id="all")[0]
            self._json(
                {
                    "ok": True,
                    "queued": True,
                    "job": job,
                    "message": "queued opener to all new friends",
                }
            )
            return
        if self.path == "/api/new-friends":
            job = enqueue_many("refresh_new_friends", phone_id="all")[0]
            self._json(
                {
                    "ok": True,
                    "queued": True,
                    "job": job,
                    "message": "queued New friends strip refresh",
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
        if self.path == "/api/draft/retry":
            data = self._read_json()
            if data is None:
                return
            name = str(data.get("name") or "").strip()
            if not name:
                self._json({"ok": False, "error": "name required"}, 400)
                return
            from src.store import retry_auto_draft

            conn = db_connect(self.server.db_path)  # type: ignore[attr-defined]
            try:
                ok = retry_auto_draft(conn, name)
            finally:
                conn.close()
            if not ok:
                self._json({"ok": False, "error": "nothing to retry"}, 400)
                return
            self._json({"ok": True, "queued": True})
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
        if self.path == "/api/queue/cancel-all":
            n = cancel_queued()
            self._json({"ok": True, "cancelled": n})
            return
        if self.path in {"/api/whatsapp/group", "/api/whatsapp/group/add"}:
            data = self._read_json()
            if data is None:
                return
            from src.whatsapp import parse_people

            people = parse_people(data.get("people"))
            if not people:
                self._json({"ok": False, "error": "people with phone numbers required"}, 400)
                return
            title = str(data.get("title") or data.get("name") or "").strip()
            group = str(data.get("group") or "").strip()
            if self.path == "/api/whatsapp/group":
                if not title:
                    self._json({"ok": False, "error": "group title required"}, 400)
                    return
                job = enqueue(
                    "whatsapp_group",
                    title,
                    json.dumps({"title": title, "people": people}),
                    phone_id="toby",
                )
                self._json(
                    {
                        "ok": True,
                        "queued": True,
                        "job": job,
                        "message": f"queued WhatsApp group {title!r}",
                    }
                )
                return
            if not group:
                self._json({"ok": False, "error": "existing group name required"}, 400)
                return
            job = enqueue(
                "whatsapp_add",
                group,
                json.dumps({"group": group, "people": people}),
                phone_id="toby",
            )
            self._json(
                {
                    "ok": True,
                    "queued": True,
                    "job": job,
                    "message": f"queued add to WhatsApp group {group!r}",
                }
            )
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
        job = enqueue(
            "reply",
            name,
            text,
            phone_id=str(data.get("phone_id") or "") or None,
            force=bool(data.get("force")),
        )
        self._json({"ok": True, "queued": True, "job": job, "message": f"queued reply to {name}"})


class InboxServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _serve_worker(host: str, port: int, db_path: Path) -> int:
    ensure_worker()
    try:
        from src.adb_wireless import ensure_all_wireless

        ensure_all_wireless()
    except Exception:
        log.warning("wireless adb attach skipped", exc_info=True)
    from src.draft_worker import ensure_draft_worker
    from src.ethnicity_vision import ensure_backfill

    ensure_draft_worker()
    try:
        ensure_backfill()
    except Exception:
        log.warning("ethnicity photo backfill skipped", exc_info=True)
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
