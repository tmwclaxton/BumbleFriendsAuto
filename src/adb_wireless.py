"""Keep Archie's Galaxy attached over wireless debugging (TLS ADB)."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger(__name__)

_STATE_NAME = "archie_adb.txt"


def _adb(*args: str, timeout: float = 12) -> str:
    cmd = ["adb", *args]
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        detail = getattr(exc, "output", None) or exc
        log.warning("adb %s failed: %s", args[:3], detail)
        return ""


def _state_path() -> Path:
    from src.config import ROOT

    return ROOT / "data" / _STATE_NAME


def _save_endpoint(endpoint: str) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(endpoint.strip() + "\n", encoding="utf-8")


def _saved_endpoint() -> str:
    path = _state_path()
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _is_wireless_phone(row: dict) -> bool:
    pid = str(row.get("id") or "").strip().casefold()
    device = str(row.get("device") or "").strip().casefold()
    return pid in {"archie", "galaxy"} or device in {"galaxy", "archie"}


def wireless_endpoint(row: dict | None) -> str:
    if not row:
        return ""
    # ARCHIE_ADB is Galaxy-only. Applying it to every phone made Pixel jobs
    # connect to 192.168.0.168 when USB ADB was empty.
    if _is_wireless_phone(row):
        env = (
            os.environ.get("ARCHIE_ADB")
            or os.environ.get("GALAXY_ADB")
            or os.environ.get("ARCHIE_WIRELESS")
            or ""
        ).strip()
        if env:
            return env
    for key in ("adb", "wireless", "wireless_adb"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    if _is_wireless_phone(row):
        return _saved_endpoint()
    return ""


def devices_text() -> str:
    return _adb("devices", "-l")


def line_is_online(line: str, serial: str) -> bool:
    if not serial or serial not in line:
        return False
    parts = line.split()
    return len(parts) >= 2 and parts[1] == "device"


def serial_is_online(serial: str) -> bool:
    if not serial:
        return False
    return any(line_is_online(line, serial) for line in devices_text().splitlines())


def adb_transport(hint: str) -> str:
    """ADB serial string uiautomator2 can open (IP:port or USB id)."""
    hint = (hint or "").strip()
    if not hint:
        return ""
    for line in devices_text().splitlines():
        if line_is_online(line, hint):
            return line.split()[0]
    return ""


def transport_for_row(row: dict | None) -> str:
    if not row:
        return ""
    ensure_wireless(row)
    endpoint = wireless_endpoint(row)
    serial = str(row.get("serial") or "").strip()
    for hint in (endpoint, serial):
        found = adb_transport(hint)
        if found:
            return found
    return endpoint or serial


def _probe(ip: str, port: int, timeout: float = 0.18) -> bool:
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def discover_port(ip: str) -> str:
    """Find a listening wireless-debug port on a known Galaxy IP."""
    if ":" in ip:
        return ip
    found: list[int] = []

    def one(port: int) -> int | None:
        return port if _probe(ip, port) else None

    with ThreadPoolExecutor(100) as pool:
        for port in pool.map(one, list(range(30000, 50000)) + [5555, 5556]):
            if port:
                found.append(port)
    if not found:
        return ""
    return f"{ip}:{found[0]}"


def adb_connect(endpoint: str) -> bool:
    endpoint = (endpoint or "").strip()
    if not endpoint or ":" not in endpoint:
        return False
    out = _adb("connect", endpoint)
    ok = "connected to" in out.lower() or "already connected" in out.lower()
    if ok:
        _save_endpoint(endpoint)
        log.info("wireless adb %s", out.strip() or endpoint)
    else:
        log.warning("wireless adb connect failed: %s", out.strip() or endpoint)
    return ok


def ensure_wireless(row: dict | None) -> bool:
    """Attach this phone over wireless ADB if configured. Safe no-op for USB Pixel."""
    if not row:
        return False
    serial = str(row.get("serial") or "").strip()
    if serial and serial_is_online(serial):
        return True
    endpoint = wireless_endpoint(row)
    if not endpoint:
        return bool(serial and serial_is_online(serial))
    if ":" not in endpoint:
        found = discover_port(endpoint)
        if found:
            endpoint = found
        else:
            log.warning("no wireless debug port on %s", endpoint)
            return False
    host, _, port = endpoint.partition(":")
    if not serial_is_online(serial) and not serial_is_online(endpoint):
        if not adb_connect(endpoint):
            found = discover_port(host)
            if found and found != endpoint:
                adb_connect(found)
                endpoint = found
    time.sleep(0.2)
    return serial_is_online(serial) or serial_is_online(endpoint)


def ensure_all_wireless(cfg: dict | None = None) -> dict[str, bool]:
    from src.phones import list_phones

    results = {}
    for row in list_phones(cfg):
        if not wireless_endpoint(row):
            continue
        results[str(row["id"])] = ensure_wireless(row)
    return results


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = ensure_all_wireless()
    print(devices_text())
    return 0 if (not results or any(results.values())) else 1


if __name__ == "__main__":
    raise SystemExit(main())
