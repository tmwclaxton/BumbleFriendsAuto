"""Local web inbox: read SQLite threads and send replies via the phone."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.config import load_config
from src.store import connect as db_connect, db_path_from_config, list_people, list_thread

log = logging.getLogger(__name__)

_SEND_LOCK = threading.Lock()


def _norm_body(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _is_day_label(text: str) -> bool:
    return bool(re.match(r"^\d{1,2} [A-Za-z]+ 20\d{2}$", (text or "").strip()))


def _thread_payload(conn, name: str) -> dict:
    row = conn.execute(
        """
        SELECT p.name, c.status, c.last_from, c.last_text, c.preview
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
        return {"name": name, "messages": msgs}
    extras: list[str] = []
    for candidate in (row["last_text"], row["preview"]):
        text = (candidate or "").strip()
        if not text or _is_day_label(text):
            continue
        if text not in extras:
            extras.append(text)
    have = {_norm_body(m["body"]) for m in msgs}
    for text in extras:
        if _norm_body(text) in have:
            continue
        side = row["last_from"] or "them"
        real = [m for m in msgs if not _is_day_label(m["body"])]
        # Inbox preview is often newer than the last captured bubble.
        if real and real[-1]["side"] == "you" and side == "you":
            side = "them"
        msgs.append({"side": side, "body": text, "from_preview": True})
        have.add(_norm_body(text))
    return {"name": row["name"] or name, "messages": msgs}


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>BFF inbox</title>
<style>
  :root {
    --bg: #111114;
    --panel: #1b1b21;
    --line: #2c2c34;
    --text: #f2efe8;
    --muted: #9a9588;
    --you: #e8c547;
    --them: #2a2a32;
    --need: #ff8a5b;
    --wait: #7eb8a4;
    --exp: #6b6570;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font: 15px/1.45 "Avenir Next", "Segoe UI", sans-serif;
    background: var(--bg);
    color: var(--text);
    display: grid;
    grid-template-columns: 320px 1fr;
  }
  aside {
    border-right: 1px solid var(--line);
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  header {
    padding: 16px 16px 10px;
    border-bottom: 1px solid var(--line);
  }
  header h1 { margin: 0; font-size: 18px; font-weight: 650; }
  header p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
  #filter {
    width: calc(100% - 24px);
    margin: 10px 12px;
    padding: 8px 10px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--bg);
    color: var(--text);
  }
  #list { overflow: auto; flex: 1; }
  .row {
    display: block;
    width: 100%;
    text-align: left;
    padding: 10px 14px;
    border: 0;
    border-bottom: 1px solid var(--line);
    background: transparent;
    color: inherit;
    text-decoration: none;
    cursor: pointer;
  }
  .row:hover, .row.on { background: #24242c; }
  .row .name { font-weight: 600; }
  .row .snip { color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tag {
    display: inline-block;
    font-size: 10px;
    letter-spacing: .04em;
    text-transform: uppercase;
    padding: 1px 6px;
    border-radius: 999px;
    margin-left: 6px;
  }
  .needs_reply { background: #3a2218; color: var(--need); }
  .waiting { background: #17332c; color: var(--wait); }
  .expired { background: #2a272c; color: var(--exp); }
  .unknown { background: #222; color: var(--muted); }
  main { display: flex; flex-direction: column; min-height: 0; }
  #thread-head {
    padding: 16px 20px;
    border-bottom: 1px solid var(--line);
    min-height: 64px;
  }
  #thread-head h2 { margin: 0; font-size: 20px; }
  #msgs {
    flex: 1;
    overflow: auto;
    padding: 18px 22px 28px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .bubble {
    max-width: 68%;
    padding: 8px 12px;
    border-radius: 14px;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .bubble.you { align-self: flex-end; background: var(--you); color: #1a1508; }
  .bubble.them { align-self: flex-start; background: var(--them); }
  .day {
    align-self: center;
    max-width: none;
    padding: 4px 0;
    background: transparent;
    color: var(--muted);
    font-size: 12px;
  }
  #composer {
    display: flex;
    gap: 8px;
    padding: 12px 16px 16px;
    border-top: 1px solid var(--line);
  }
  #composer textarea {
    flex: 1;
    min-height: 54px;
    resize: vertical;
    padding: 10px 12px;
    border-radius: 10px;
    border: 1px solid var(--line);
    background: var(--panel);
    color: var(--text);
    font: inherit;
  }
  #composer button {
    background: var(--you);
    color: #1a1508;
    border: 0;
    border-radius: 10px;
    padding: 0 16px;
    font-weight: 650;
    cursor: pointer;
  }
  #composer button:disabled { opacity: .45; cursor: default; }
  #status { padding: 0 16px 10px; font-size: 12px; color: var(--muted); min-height: 18px; }
  .empty { margin: auto; color: var(--muted); }
</style>
</head>
<body>
<aside>
  <header>
    <h1>BFF inbox</h1>
    <p id="meta">Loading…</p>
  </header>
  <input id="filter" placeholder="Filter names" autocomplete="off"/>
  <div id="list"></div>
</aside>
<main>
  <div id="thread-head"><h2>Select a chat</h2></div>
  <div id="msgs"><p class="empty">Pick someone on the left.</p></div>
  <div id="status"></div>
  <form id="composer">
    <textarea id="draft" placeholder="Reply — this sends on the phone" disabled></textarea>
    <button type="submit" id="send" disabled>Send</button>
  </form>
</main>
<script>
const listEl = document.getElementById("list");
const msgsEl = document.getElementById("msgs");
const headEl = document.getElementById("thread-head");
const metaEl = document.getElementById("meta");
const draftEl = document.getElementById("draft");
const sendEl = document.getElementById("send");
const statusEl = document.getElementById("status");
let people = [];
let current = null;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }[c]));
}

async function loadPeople() {
  const res = await fetch("/api/people");
  const data = await res.json();
  people = data.people || [];
  const n = people.filter(p => p.status === "needs_reply").length;
  metaEl.textContent = people.length + " people · " + n + " need a reply";
  renderList();
}

function chatFromUrl() {
  return new URLSearchParams(location.search).get("chat") || "";
}

function setChatUrl(name, push) {
  const url = new URL(location.href);
  if (name) url.searchParams.set("chat", name);
  else url.searchParams.delete("chat");
  const next = url.pathname + url.search + url.hash;
  if (next === location.pathname + location.search + location.hash) return;
  if (push) history.pushState({chat: name}, "", next);
  else history.replaceState({chat: name}, "", next);
}

function renderList() {
  const q = document.getElementById("filter").value.trim().toLowerCase();
  listEl.innerHTML = people
    .filter(p => !q || p.name.toLowerCase().includes(q))
    .map(p => {
      const on = current === p.name ? " on" : "";
      const snip = esc(p.last_text || p.preview || "");
      const st = p.status || "unknown";
      const href = "?chat=" + encodeURIComponent(p.name);
      return `<a class="row${on}" href="${href}" data-name="${esc(p.name)}">
        <div><span class="name">${esc(p.name)}</span><span class="tag ${st}">${esc(st.replace("_"," "))}</span></div>
        <div class="snip">${snip}</div>
      </a>`;
    }).join("");
}

function isDayLabel(body) {
  return /^[0-9]{1,2} [A-Za-z]+ 20[0-9]{2}$/.test(String(body || "").trim());
}

async function openThread(name, opts) {
  const push = !opts || opts.push !== false;
  current = name;
  setChatUrl(name, push);
  renderList();
  headEl.innerHTML = `<h2>${esc(name)}</h2>`;
  draftEl.disabled = false;
  sendEl.disabled = false;
  const res = await fetch("/api/thread?name=" + encodeURIComponent(name));
  const data = await res.json();
  const msgs = data.messages || [];
  if (!msgs.length) {
    msgsEl.innerHTML = '<p class="empty">No transcript stored.</p>';
    return;
  }
  msgsEl.innerHTML = msgs.map(m => {
    if (isDayLabel(m.body)) return `<div class="day">${esc(m.body)}</div>`;
    const side = m.side === "you" ? "you" : "them";
    const extra = m.from_preview ? " title='from inbox preview (not in captured thread)'" : "";
    return `<div class="bubble ${side}"${extra}>${esc(m.body)}</div>`;
  }).join("");
  msgsEl.scrollTop = msgsEl.scrollHeight;
}

listEl.addEventListener("click", e => {
  const link = e.target.closest("a[data-name]");
  if (!link) return;
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
  e.preventDefault();
  openThread(link.dataset.name);
});
document.getElementById("filter").addEventListener("input", renderList);
window.addEventListener("popstate", () => {
  const name = chatFromUrl();
  if (name) openThread(name, {push: false});
});

document.getElementById("composer").addEventListener("submit", async e => {
  e.preventDefault();
  if (!current) return;
  const text = draftEl.value.trim();
  if (!text) return;
  sendEl.disabled = true;
  statusEl.textContent = "Sending on the phone… keep it unlocked.";
  const res = await fetch("/api/reply", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name: current, text}),
  });
  const data = await res.json();
  statusEl.textContent = data.ok ? data.message : ("Failed: " + (data.error || res.status));
  sendEl.disabled = false;
  if (data.ok) {
    draftEl.value = "";
    await loadPeople();
    await openThread(current, {push: false});
  }
});

loadPeople().then(() => {
  const name = chatFromUrl();
  if (name) return openThread(name, {push: false});
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "BffInbox/1.0"

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s " + fmt, self.address_string(), *args)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self) -> None:
        body = _PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/reply":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json({"ok": False, "error": "invalid json"}, 400)
            return
        name = str(data.get("name") or "").strip()
        text = str(data.get("text") or "").strip()
        if not name or not text:
            self._json({"ok": False, "error": "name and text required"}, 400)
            return
        if not _SEND_LOCK.acquire(blocking=False):
            self._json({"ok": False, "error": "already sending another reply"}, 409)
            return
        try:
            from src.messenger import send_named_message

            ok, message = send_named_message(name, text)
            self._json({"ok": ok, "message": message, "error": None if ok else message}, 200 if ok else 500)
        except Exception as exc:
            log.exception("reply failed")
            self._json({"ok": False, "error": str(exc)}, 500)
        finally:
            _SEND_LOCK.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local BFF inbox dashboard")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config(args.config)
    db_path = db_path_from_config(cfg)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.db_path = db_path  # type: ignore[attr-defined]
    url = f"http://{args.host}:{args.port}/"
    log.info("Inbox at %s  (db %s)", url, db_path)
    log.info("Replies send on the connected phone — keep it unlocked")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
