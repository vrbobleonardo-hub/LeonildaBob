from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

import requests

from .settings import settings


ROOT_DIR = Path(__file__).resolve().parents[1]
_PROCESS: subprocess.Popen[Any] | None = None


def _bridge_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.whatsapp_qr_bridge_token:
        headers["X-QR-Bridge-Token"] = settings.whatsapp_qr_bridge_token
    return headers


def _bridge_is_local() -> bool:
    parsed = urlparse(settings.whatsapp_qr_bridge_url)
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _bridge_request(method: str, path: str, *, payload: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    response = requests.request(
        method,
        f"{settings.whatsapp_qr_bridge_url}{path}",
        json=payload,
        headers=_bridge_headers(),
        timeout=timeout,
    )
    try:
        data = response.json() if response.content else {}
    except ValueError:
        data = {}
    if response.status_code >= 400:
        reason = str(data.get("error") or response.text or f"qr_bridge_http_{response.status_code}")
        raise RuntimeError(reason[:500])
    return data if isinstance(data, dict) else {}


def _unavailable(reason: str = "") -> dict[str, Any]:
    return {
        "enabled": settings.whatsapp_provider == "qr",
        "bridge_running": False,
        "session": {
            "status": "unavailable" if reason else "disconnected",
            "can_send": False,
            "needs_scan": True,
            "qr_data_url": None,
            "last_error": reason or None,
        },
    }


def _bridge_environment() -> dict[str, str]:
    parsed = urlparse(settings.whatsapp_qr_bridge_url)
    env = os.environ.copy()
    env.setdefault("WHATSAPP_QR_BRIDGE_PORT", str(parsed.port or 3333))
    env.setdefault("WHATSAPP_QR_BRIDGE_HOST", "127.0.0.1")
    env.setdefault("WHATSAPP_QR_AUTH_DIR", str(ROOT_DIR / ".whatsapp-qr-auth" / "default"))
    env.setdefault("WHATSAPP_QR_INBOUND_URL", f"{settings.app_base_url.rstrip('/')}/api/webhooks/whatsapp/qr")
    if settings.whatsapp_qr_bridge_token:
        env.setdefault("WHATSAPP_QR_BRIDGE_TOKEN", settings.whatsapp_qr_bridge_token)
    return env


def ensure_qr_bridge_running() -> dict[str, Any]:
    """Start the bundled bridge only for a local bridge URL.

    A remote bridge is intentionally not spawned by the web app: it must run as a
    dedicated service with persistent storage for the WhatsApp Web session.
    """
    try:
        return _bridge_request("GET", "/status", timeout=1.5)
    except (requests.RequestException, RuntimeError):
        pass

    if not _bridge_is_local() or not settings.whatsapp_qr_bridge_autostart:
        return {"ok": False, "error": "qr_bridge_unreachable"}

    node = shutil.which("node")
    script = ROOT_DIR / "scripts" / "whatsapp_qr_bridge.mjs"
    if not node:
        return {"ok": False, "error": "node_not_found"}
    if not script.exists():
        return {"ok": False, "error": "qr_bridge_script_missing"}

    log_path = ROOT_DIR / "data" / "whatsapp_qr_bridge.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    global _PROCESS
    _PROCESS = subprocess.Popen(
        [node, str(script)],
        cwd=str(ROOT_DIR),
        env=_bridge_environment(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            status = _bridge_request("GET", "/status", timeout=1)
            status["bridge_running"] = True
            return status
        except (requests.RequestException, RuntimeError):
            time.sleep(0.25)
    return {"ok": False, "error": "qr_bridge_start_timeout"}


def qr_session_status(start: bool = False) -> dict[str, Any]:
    if settings.whatsapp_provider != "qr":
        return {
            **_unavailable("qr_provider_not_enabled"),
            "provider": settings.whatsapp_provider,
        }
    if start:
        started = ensure_qr_bridge_running()
        if started.get("error"):
            return _unavailable(str(started["error"]))
    try:
        status = _bridge_request("GET", "/status", timeout=3)
        status["bridge_running"] = True
        return status
    except (requests.RequestException, RuntimeError) as exc:
        return _unavailable(str(exc))


def qr_session_action(action: str, *, force: bool = False) -> dict[str, Any]:
    if settings.whatsapp_provider != "qr":
        return qr_session_status()
    started = ensure_qr_bridge_running()
    if started.get("error"):
        return _unavailable(str(started["error"]))
    normalized = action.strip().lower()
    if normalized in {"status", "refresh"}:
        return qr_session_status()
    if normalized == "connect":
        path, payload = "/connect", {"force": force}
    elif normalized == "disconnect":
        path, payload = "/disconnect", {}
    elif normalized == "reset":
        path, payload = "/reset", {}
    else:
        raise ValueError("Ação de QR inválida.")
    try:
        result = _bridge_request("POST", path, payload=payload, timeout=12)
        result["bridge_running"] = True
        return result
    except (requests.RequestException, RuntimeError) as exc:
        return _unavailable(str(exc))


def send_via_qr_bridge(to: str, text: str) -> dict[str, Any]:
    started = ensure_qr_bridge_running()
    if started.get("error"):
        raise RuntimeError(str(started["error"]))
    return _bridge_request("POST", "/send", payload={"to": to, "text": text}, timeout=15)
