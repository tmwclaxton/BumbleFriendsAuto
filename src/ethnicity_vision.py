"""Guess Hinge-style ethnicity from stored profile photos via NanoGPT vision."""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

from src.config import load_config
from src.photos import photo_exists, photo_file
from src.profile_filters import ETHNICITY_CHOICES, canonicalize
from src.store import connect as db_connect, db_path_from_config, set_ethnicity

log = logging.getLogger(__name__)

_API_URL = "https://nano-gpt.com/api/v1/chat/completions"
_DEFAULT_MODEL = "google/gemini-2.5-flash"
_ALLOWED = {cid for cid, _ in ETHNICITY_CHOICES} | {"unknown"}

_PROMPT = (
    "Look at this dating-app profile photo. Guess the person's most likely "
    "ethnicity using ONE of these exact ids:\n"
    "white, black, east_asian, south_asian, southeast_asian, asian, hispanic, "
    "middle_eastern, native_american, pacific_islander, mixed, other, unknown\n\n"
    "Rules:\n"
    "- Use the photo only.\n"
    "- If you can see a face, pick the closest bucket even if you are not certain.\n"
    "- Prefer a specific Asian bucket (east_asian / south_asian / southeast_asian) "
    "over bare asian.\n"
    "- Use mixed if they clearly look mixed; use unknown ONLY if there is no "
    "visible face or the crop is unusable.\n"
    "- Reply with ONLY the id, no punctuation or explanation."
)

_lock = threading.Lock()
_state: dict = {
    "running": False,
    "cancel": False,
    "done": 0,
    "total": 0,
    "tagged": 0,
    "skipped": 0,
    "failed": 0,
    "last_name": "",
    "message": "",
    "error": None,
}


def api_key(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    nano = dict(cfg.get("nanogpt") or {})
    return (
        os.environ.get("NANOGPT_API_KEY")
        or os.environ.get("NANO_GPT_API_KEY")
        or str(nano.get("api_key") or "")
    ).strip()


def vision_model(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    nano = dict(cfg.get("nanogpt") or {})
    return (
        os.environ.get("NANOGPT_VISION_MODEL")
        or str(nano.get("vision_model") or "")
        or _DEFAULT_MODEL
    ).strip()


def parse_guess(text: str) -> str:
    raw = (text or "").strip().strip("`\"'")
    if not raw:
        return "unknown"
    first = raw.splitlines()[0].strip().strip("`\"'")
    if ":" in first:
        first = first.split(":")[-1].strip()
    parts = first.split()
    candidates = [first]
    if parts:
        candidates.append(parts[0])
    for cand in candidates:
        token = cand.strip(".,;()[]").lower()
        if token in _ALLOWED:
            return token
        canon = canonicalize(token.replace("_", " "))
        if canon:
            return canon
    return "unknown"


def guess_status() -> dict:
    with _lock:
        snap = {
            "running": bool(_state["running"]),
            "done": int(_state["done"] or 0),
            "total": int(_state["total"] or 0),
            "tagged": int(_state["tagged"] or 0),
            "skipped": int(_state["skipped"] or 0),
            "failed": int(_state["failed"] or 0),
            "last_name": str(_state["last_name"] or ""),
            "message": str(_state["message"] or ""),
            "error": _state["error"],
        }
    snap["configured"] = bool(api_key())
    snap["model"] = vision_model()
    return snap


def cancel_guess() -> bool:
    with _lock:
        if not _state["running"]:
            return False
        _state["cancel"] = True
        _state["message"] = "cancelling"
        return True


def _manual_tag(ethnicity: str | None, source: str | None) -> bool:
    eth = (ethnicity or "").strip()
    src = (source or "").strip()
    if src == "manual":
        return True
    return bool(eth) and src != "vision"


def _already_guessed(
    ethnicity: str | None,
    source: str | None,
    *,
    force: bool,
    allow_manual: bool = False,
) -> bool:
    if _manual_tag(ethnicity, source) and not allow_manual:
        return True
    if force:
        return False
    src = (source or "").strip()
    return bool((ethnicity or "").strip()) or src == "vision"


def _people_to_guess(
    conn,
    *,
    name: str = "",
    force: bool = False,
) -> list[str]:
    if name:
        row = conn.execute(
            "SELECT name, ethnicity, ethnicity_source FROM people WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return []
        if not photo_exists(str(row["name"])):
            return []
        if _already_guessed(
            row["ethnicity"],
            row["ethnicity_source"],
            force=force,
            allow_manual=force,
        ):
            return []
        return [str(row["name"])]
    names: list[str] = []
    for row in conn.execute(
        "SELECT name, ethnicity, ethnicity_source FROM people ORDER BY name COLLATE NOCASE"
    ):
        person = str(row["name"])
        if _already_guessed(row["ethnicity"], row["ethnicity_source"], force=force):
            continue
        if not photo_exists(person):
            continue
        names.append(person)
    return names


def classify_photo(path: Path, cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    key = api_key(cfg)
    if not key:
        raise RuntimeError("NANOGPT_API_KEY is not set")
    raw = path.read_bytes()
    if len(raw) < 80:
        return "unknown"
    data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
    payload = {
        "model": vision_model(cfg),
        "temperature": 0,
        "max_tokens": 16,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    req = urllib.request.Request(
        _API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:240]
        raise RuntimeError(f"NanoGPT {exc.code}: {detail}") from exc
    text = ""
    try:
        text = str(body["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("NanoGPT response missing text") from exc
    return parse_guess(text)


def _bump(**fields: object) -> None:
    with _lock:
        _state.update(fields)


def _run_guess(*, name: str = "", force: bool = False) -> None:
    cfg = load_config()
    conn = db_connect(db_path_from_config(cfg))
    tagged = skipped = failed = 0
    last = ""
    try:
        targets = _people_to_guess(conn, name=name, force=force)
        _bump(total=len(targets), message="guessing" if targets else "nothing to guess")
        if not targets:
            return
        for i, person in enumerate(targets, start=1):
            with _lock:
                if _state["cancel"]:
                    _state["message"] = "cancelled"
                    break
            last = person
            _bump(done=i - 1, last_name=person, message=f"guessing {person}")
            path = photo_file(person)
            try:
                guess = classify_photo(path, cfg)
            except Exception as exc:
                log.warning("ethnicity vision failed for %s: %s", person, exc)
                failed += 1
                _bump(failed=failed, error=str(exc))
                continue
            value = "" if guess == "unknown" else guess
            if not set_ethnicity(conn, person, value, source="vision"):
                failed += 1
                _bump(failed=failed)
                continue
            tagged += 1
            _bump(done=i, tagged=tagged, last_name=person)
        else:
            _bump(done=len(targets))
    except Exception as exc:
        log.exception("ethnicity vision job failed")
        _bump(error=str(exc), message="failed")
    finally:
        conn.close()
        with _lock:
            cancelled = bool(_state["cancel"])
            _state["running"] = False
            _state["cancel"] = False
            _state["tagged"] = tagged
            _state["skipped"] = skipped
            _state["failed"] = failed
            _state["last_name"] = last
            if _state["error"] and not cancelled:
                _state["message"] = _state["message"] or "failed"
            elif cancelled:
                _state["message"] = (
                    f"stopped after {tagged} guess(es)"
                    if tagged
                    else "cancelled"
                )
            else:
                _state["message"] = (
                    f"guessed {tagged} from photos"
                    + (f", {failed} failed" if failed else "")
                )


def start_guess(*, name: str = "", force: bool = False) -> dict:
    """Start a background guess. Does not overwrite manual tags."""
    name = (name or "").strip()
    if not api_key():
        snap = guess_status()
        snap["ok"] = False
        snap["error"] = "NANOGPT_API_KEY is not set"
        return snap
    with _lock:
        if _state["running"]:
            snap = dict(_state)
            running = True
        else:
            running = False
            _state.update(
                {
                    "running": True,
                    "cancel": False,
                    "done": 0,
                    "total": 0,
                    "tagged": 0,
                    "skipped": 0,
                    "failed": 0,
                    "last_name": "",
                    "message": "starting",
                    "error": None,
                }
            )
    if running:
        out = guess_status()
        out["ok"] = False
        out["error"] = "guess already running"
        return out
    threading.Thread(
        target=_run_guess,
        kwargs={"name": name, "force": force},
        name="ethnicity-vision",
        daemon=True,
    ).start()
    out = guess_status()
    out["ok"] = True
    out["message"] = "Guessing ethnicity from photos" + (f" for {name}" if name else "")
    return out


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Guess ethnicity from stored avatars")
    parser.add_argument("name", nargs="?", default="", help="One person, or everyone untagged")
    parser.add_argument("--force", action="store_true", help="Re-guess previous vision tags")
    args = parser.parse_args()
    result = start_guess(name=args.name, force=args.force)
    if not result.get("ok"):
        raise SystemExit(result.get("error") or "could not start")
    while True:
        snap = guess_status()
        if not snap["running"]:
            print(snap.get("message") or "done")
            if snap.get("error") and snap.get("failed"):
                raise SystemExit(1)
            return
        threading.Event().wait(1.0)


if __name__ == "__main__":
    main()
