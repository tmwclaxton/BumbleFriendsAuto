"""Read LGS notes from the Obsidian MCP on admin.grantgunner.org."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from src.config import load_config

log = logging.getLogger(__name__)

_DEFAULT_URL = "http://127.0.0.1:18080/mcp"
_cache_lock = threading.Lock()
_note_cache: dict[str, tuple[float, str]] = {}
_CACHE_TTL_SEC = 60.0


def mcp_url(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    obs = dict(cfg.get("obsidian") or {})
    return (
        os.environ.get("OBSIDIAN_MCP_URL")
        or str(obs.get("mcp_url") or "")
        or _DEFAULT_URL
    ).strip()


def mcp_token(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    obs = dict(cfg.get("obsidian") or {})
    return (
        os.environ.get("OBSIDIAN_MCP_TOKEN")
        or os.environ.get("OBSIDIAN_MCP_API_KEY")
        or str(obs.get("mcp_token") or "")
    ).strip()


def events_note(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    obs = dict(cfg.get("obsidian") or {})
    return str(obs.get("events_note") or "LGS/Events.md").strip()


def run_prompt_note(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    obs = dict(cfg.get("obsidian") or {})
    return str(obs.get("run_prompt_note") or "LGS/Run prompt.md").strip()


def people_folder(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    obs = dict(cfg.get("obsidian") or {})
    return str(obs.get("people_folder") or "LGS/People").strip().rstrip("/")


def _parse_sse(body: str) -> dict[str, Any]:
    data_lines: list[str] = []
    for line in (body or "").splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    raw = "\n".join(data_lines) if data_lines else (body or "").strip()
    if not raw:
        return {}
    return json.loads(raw)


class ObsidianMcpClient:
    """Minimal Streamable-HTTP MCP client for read_note."""

    def __init__(self, url: str, token: str, *, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._session: str | None = None
        self._req_id = 0
        self._lock = threading.Lock()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session:
            headers["mcp-session-id"] = self._session
        return headers

    def _post(self, payload: dict[str, Any], *, expect_body: bool = True) -> dict[str, Any]:
        req = urllib.request.Request(
            self.url if self.url.endswith("/mcp") else f"{self.url}/mcp",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session = sid
                raw = resp.read().decode("utf-8", errors="replace")
                if not expect_body or not raw.strip():
                    return {}
                return _parse_sse(raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"Obsidian MCP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Obsidian MCP unreachable: {exc}") from exc

    def ensure_session(self) -> None:
        with self._lock:
            if self._session:
                return
            self._req_id += 1
            result = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": self._req_id,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "lgspipeline", "version": "0.1"},
                    },
                }
            )
            if "error" in result:
                raise RuntimeError(f"Obsidian MCP initialize failed: {result['error']}")
            self._post(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                expect_body=False,
            )

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        self.ensure_session()
        with self._lock:
            self._req_id += 1
            result = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": self._req_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                }
            )
        if "error" in result:
            raise RuntimeError(f"Obsidian MCP tool error: {result['error']}")
        content = ((result.get("result") or {}).get("content") or [])
        texts = [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        text = "\n".join(t for t in texts if t).strip()
        if not text:
            raise RuntimeError(f"Obsidian MCP empty result for {name}")
        return text

    def read_note(self, path: str) -> str:
        text = self.call_tool("read_note", {"path": path})
        if text.startswith("not found:"):
            raise FileNotFoundError(text)
        return text


_client_lock = threading.Lock()
_client: ObsidianMcpClient | None = None


def _get_client(cfg: dict | None = None) -> ObsidianMcpClient:
    global _client
    cfg = cfg if cfg is not None else load_config()
    url = mcp_url(cfg)
    token = mcp_token(cfg)
    if not token:
        raise RuntimeError("OBSIDIAN_MCP_TOKEN is not set")
    with _client_lock:
        if _client is None or _client.url != (url if url.endswith("/mcp") else f"{url}/mcp") or _client.token != token:
            # Normalize stored URL comparison
            full = url if url.endswith("/mcp") else f"{url.rstrip('/')}/mcp"
            _client = ObsidianMcpClient(full, token)
        return _client


def read_note_cached(path: str, cfg: dict | None = None, *, ttl: float = _CACHE_TTL_SEC) -> str:
    path = (path or "").strip()
    if not path:
        raise ValueError("note path required")
    now = time.time()
    with _cache_lock:
        hit = _note_cache.get(path)
        if hit and (now - hit[0]) < ttl:
            return hit[1]
    text = _get_client(cfg).read_note(path)
    with _cache_lock:
        _note_cache[path] = (now, text)
    return text


def clear_note_cache() -> None:
    with _cache_lock:
        _note_cache.clear()


def load_draft_context(name: str, cfg: dict | None = None) -> dict[str, str]:
    """Fetch Events + Run prompt + optional People note for one person."""
    cfg = cfg if cfg is not None else load_config()
    events = read_note_cached(events_note(cfg), cfg)
    if not (events or "").strip():
        raise RuntimeError("LGS/Events.md is empty — refusing to draft")
    prompt = read_note_cached(run_prompt_note(cfg), cfg)
    person_path = f"{people_folder(cfg)}/{name}.md"
    person = ""
    try:
        person = read_note_cached(person_path, cfg, ttl=15.0)
    except FileNotFoundError:
        log.info("no Obsidian people note for %s", name)
    except RuntimeError as exc:
        # Missing person note is fine; missing events is not.
        if "not found" in str(exc).lower():
            log.info("no Obsidian people note for %s", name)
        else:
            raise
    return {
        "events": events,
        "run_prompt": prompt,
        "person_note": person,
        "person_note_path": person_path,
    }
