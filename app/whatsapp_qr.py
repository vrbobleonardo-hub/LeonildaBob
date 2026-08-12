from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Optional

import requests

from .settings import settings


ROOT_DIR = Path(__file__).resolve().parents[1]
_PROCESS: Optional[subprocess.Popen[Any]] = None


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def _bridge_port() -> int:
    return int(os.getenv("WHATSAPP_QR_BRIDGE_PORT", "3333"))


def _bridge_url(path: str = "") -> str:
    return f"{settings.whatsapp_qr_bridge_url.rstrip('/')}{path}"


def _bridge_headers() -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if settings.whatsapp_qr_bridge_token:
        headers["X-QR-Bridge-Token"] = settings.whatsapp_qr_bridge_token
    return headers


def _bridge_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("WHATSAPP_QR_BRIDGE_PORT", str(_bridge_port()))
    env.setdefault("WHATSAPP_QR_BRIDGE_HOST", "127.0.0.1")
    env.setdefault("WHATSAPP_QR_AUTH_DIR", str(ROOT_DIR / ".whatsapp-qr-auth" / "default"))
    port = os.getenv("PORT", "8000")
    env.setdefault("WHATSAPP_QR_INBOUND_URL", f"http://127.0.0.1:{port}/api/webhooks/whatsapp/qr")
    env["NODE_PATH"] = str(ROOT_DIR / "node_modules")
    if settings.whatsapp_qr_bridge_token:
        env.setdefault("WHATSAPP_QR_BRIDGE_TOKEN", settings.whatsapp_qr_bridge_token)
    return env


def _script_path() -> Path:
    return ROOT_DIR / "scripts" / "whatsapp_qr_bridge.mjs"


def _bridge_request(method: str, path: str, payload: Optional[dict[str, Any]] = None, timeout: float = 4.0) -> dict[str, Any]:
    response = requests.request(method, _bridge_url(path), json=payload, headers=_bridge_headers(), timeout=timeout)
    data = response.json() if response.text else {}
    if response.status_code >= 400:
        reason = data.get("error") if isinstance(data, dict) else response.text
        raise RuntimeError(clean_text(reason) or f"qr_bridge_http_{response.status_code}")
    return data if isinstance(data, dict) else {}


def _qr_is_enabled() -> bool:
    return settings.whatsapp_provider == "qr" and not settings.whatsapp_dry_run


def _unavailable(reason: str = "") -> dict[str, Any]:
    return {
        "enabled": _qr_is_enabled(),
        "bridge_running": False,
        "session": {
            "id": "default",
            "label": "WhatsApp QR Leonilda Bob",
            "status": "unavailable" if reason else "disconnected",
            "phone_wa_id": None,
            "display_name": None,
            "qr_expires_at": None,
            "last_connected_at": None,
            "last_disconnected_at": None,
            "last_error": reason or None,
            "runtime_connected": False,
            "runtime_starting": False,
            "auth_available": False,
            "can_send": False,
            "needs_scan": True,
            "qr_data_url": None,
        },
    }


def ensure_qr_bridge_running() -> dict[str, Any]:
    if not _qr_is_enabled():
        return {"ok": False, "error": "whatsapp_qr_disabled"}

    try:
        data = _bridge_request("GET", "/status", timeout=1.5)
        data["ok"] = True
        return data
    except (requests.RequestException, RuntimeError):
        pass

    # Production uses a dedicated Render service. Never try to start a local
    # subprocess when the configured remote bridge is unavailable.
    if not settings.whatsapp_qr_bridge_autostart:
        return {"ok": False, "error": "qr_bridge_unreachable"}

    node = shutil.which("node")
    if not node:
        return {"ok": False, "error": "node_not_found"}

    script = _script_path()
    if not script.exists():
        return {"ok": False, "error": "qr_bridge_script_missing"}

    # Restore auth from database before starting bridge
    auth_dir = str(ROOT_DIR / ".whatsapp-qr-auth" / "default")
    try:
        from . import db
        restored = db.restore_qr_auth_files(auth_dir)
        if restored:
            import logging
            logging.getLogger(__name__).info(f"[QR] Restored {restored} auth files from database")
    except Exception:
        pass

    data_dir = ROOT_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_file = (data_dir / "whatsapp_qr_bridge.log").open("ab")

    global _PROCESS
    _PROCESS = subprocess.Popen(
        [node, str(script)],
        cwd=str(ROOT_DIR),
        env=_bridge_env(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    deadline = time.monotonic() + 8
    last_error = ""
    while time.monotonic() < deadline:
        try:
            data = _bridge_request("GET", "/status", timeout=1.0)
            data["ok"] = True
            data["bridge_running"] = True
            return data
        except (requests.RequestException, RuntimeError) as exc:
            last_error = clean_text(exc)
            time.sleep(0.25)

    return {"ok": False, "error": last_error or "qr_bridge_start_timeout"}


def qr_session_status(start: bool = False) -> dict[str, Any]:
    if not _qr_is_enabled():
        return {
            **_unavailable(
                "qr_dry_run_enabled"
                if settings.whatsapp_provider == "qr"
                else "qr_provider_not_enabled"
            ),
            "provider": settings.whatsapp_provider,
            "dry_run": settings.whatsapp_dry_run,
        }
    if start:
        started = ensure_qr_bridge_running()
        if started.get("error"):
            return _unavailable(str(started["error"]))
    try:
        status = _bridge_request("GET", "/status", timeout=3)
        session = status.get("session") or {}
        should_autostart = (
            start
            and bool(session.get("auth_available"))
            and not bool(session.get("runtime_connected"))
            and clean_text(session.get("status")) not in {"qr", "connecting"}
        )
        if should_autostart:
            status = _bridge_request("POST", "/connect", {"force": False}, timeout=10.0)
        status["bridge_running"] = True
        return status
    except (requests.RequestException, RuntimeError) as exc:
        return _unavailable(str(exc) if start else "")


def qr_session_action(action: str, *, force: bool = False) -> dict[str, Any]:
    if not _qr_is_enabled():
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
        return {
            "sent": False,
            "status": "error",
            "provider_message_id": None,
            "reason": started["error"],
            "transport": "qr",
        }
    try:
        data = _bridge_request("POST", "/send", {"to": to, "text": text}, timeout=12.0)
    except (requests.RequestException, RuntimeError) as exc:
        return {
            "sent": False,
            "status": "error",
            "provider_message_id": None,
            "reason": clean_text(exc),
            "transport": "qr",
        }
    return {
        "sent": bool(data.get("sent", data.get("ok"))),
        "status": data.get("status", "sent" if data.get("ok") else "error"),
        "provider_message_id": data.get("provider_message_id"),
        "response": data,
        "transport": "qr",
    }


def is_qr_phone_number_id(value: str) -> bool:
    return clean_text(value).lower().startswith("qr:")
