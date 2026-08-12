from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .settings import settings


HTTP = requests.Session()


class EvolutionRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


def _enabled() -> bool:
    return settings.whatsapp_provider == "evolution" and not settings.whatsapp_dry_run


def _endpoint(path: str) -> str:
    return f"{settings.evolution_api_url.rstrip('/')}/{path.lstrip('/')}"


def _headers() -> dict[str, str]:
    return {"apikey": settings.evolution_api_key, "Content-Type": "application/json"}


def _response_json(response: requests.Response) -> dict[str, Any]:
    try:
        value = response.json() if response.content else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _error_message(response: requests.Response, data: dict[str, Any]) -> str:
    error = data.get("error") or data.get("message")
    if isinstance(error, dict):
        error = error.get("message") or error.get("error")
    return str(error or response.text or f"Erro HTTP {response.status_code}")[:500]


def _request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    if not settings.evolution_api_url or not settings.evolution_api_key:
        raise EvolutionRequestError("Evolution API não configurada.")
    try:
        response = HTTP.request(
            method,
            _endpoint(path),
            headers=_headers(),
            json=payload,
            timeout=timeout or settings.evolution_request_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise EvolutionRequestError("A Evolution API não respondeu.") from exc
    data = _response_json(response)
    if response.status_code >= 400:
        raise EvolutionRequestError(_error_message(response, data), status_code=response.status_code)
    if data.get("error") is True:
        raise EvolutionRequestError(
            str(data.get("message") or "A Evolution API recusou a operação.")[:500],
            status_code=response.status_code,
        )
    return data


def _is_missing_instance_error(exc: EvolutionRequestError) -> bool:
    if exc.status_code == 404:
        return True
    message = str(exc).lower()
    markers = ("not found", "does not exist", "não encontrada", "nao encontrada")
    return "instance" in message and any(marker in message for marker in markers)


def _session(
    *,
    status: str,
    api_reachable: bool,
    qr_data_url: str | None = None,
    error: str | None = None,
    phone: str | None = None,
) -> dict[str, Any]:
    connected = status == "connected"
    return {
        "enabled": _enabled(),
        "provider": settings.whatsapp_provider,
        "transport": "evolution",
        "bridge_running": api_reachable,
        "session": {
            "id": settings.evolution_instance,
            "label": f"Evolution API · {settings.evolution_instance}",
            "status": status,
            "phone_wa_id": phone,
            "display_name": None,
            "qr_expires_at": None,
            "last_connected_at": None,
            "last_disconnected_at": None,
            "last_error": error,
            "runtime_connected": connected,
            "runtime_starting": status == "connecting",
            "auth_available": connected,
            "can_send": connected,
            "needs_scan": not connected,
            "qr_data_url": qr_data_url,
        },
    }


def _disabled_session() -> dict[str, Any]:
    if settings.whatsapp_provider == "evolution" and settings.whatsapp_dry_run:
        reason = "evolution_dry_run_enabled"
    elif settings.whatsapp_provider != "evolution":
        reason = "evolution_provider_not_enabled"
    else:
        reason = "evolution_not_configured"
    return _session(status="unavailable", api_reachable=False, error=reason)


def _connection_state(data: dict[str, Any]) -> str:
    instance = data.get("instance")
    if isinstance(instance, dict):
        return str(instance.get("state") or instance.get("connectionStatus") or "").strip().lower()
    return str(data.get("state") or data.get("connectionStatus") or "").strip().lower()


def _state_session(data: dict[str, Any], *, qr_data_url: str | None = None) -> dict[str, Any]:
    state = _connection_state(data)
    status = "connected" if state in {"open", "connected"} else "qr" if qr_data_url else "disconnected"
    instance = data.get("instance") if isinstance(data.get("instance"), dict) else {}
    phone = str(instance.get("ownerJid") or instance.get("number") or "") or None
    if phone and "@" in phone:
        phone = phone.split("@", 1)[0].split(":", 1)[0]
    return _session(status=status, api_reachable=True, qr_data_url=qr_data_url, phone=phone)


def _find_qr(value: Any) -> str | None:
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.startswith("data:image/"):
            return candidate
        if len(candidate) > 200 and re.fullmatch(r"[A-Za-z0-9+/=\r\n]+", candidate):
            return f"data:image/png;base64,{candidate}"
        return None
    if isinstance(value, dict):
        for key in ("base64", "qrcode", "qr", "code"):
            found = _find_qr(value.get(key))
            if found:
                return found
        for nested in value.values():
            found = _find_qr(nested)
            if found:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _find_qr(nested)
            if found:
                return found
    return None


def _configure_webhook() -> None:
    webhook: dict[str, Any] = {
        "enabled": True,
        "url": f"{settings.app_base_url.rstrip('/')}/api/webhooks/whatsapp/evolution",
        "byEvents": False,
        "base64": True,
        "events": ["MESSAGES_UPSERT", "MESSAGES_UPDATE", "CONNECTION_UPDATE", "QRCODE_UPDATED"],
    }
    if settings.evolution_webhook_token:
        webhook["headers"] = {"x-evolution-webhook-token": settings.evolution_webhook_token}
    instance = quote(settings.evolution_instance, safe="")
    _request("POST", f"/webhook/set/{instance}", payload={"webhook": webhook})


def _create_instance() -> dict[str, Any]:
    return _request(
        "POST",
        "/instance/create",
        payload={
            "instanceName": settings.evolution_instance,
            "integration": "WHATSAPP-BAILEYS",
            "qrcode": True,
            "groupsIgnore": True,
            "alwaysOnline": False,
            "readMessages": False,
            "readStatus": False,
            "syncFullHistory": False,
        },
    )


def evolution_session_status(start: bool = False) -> dict[str, Any]:
    if not _enabled():
        return _disabled_session()
    instance = quote(settings.evolution_instance, safe="")
    try:
        data = _request("GET", f"/instance/connectionState/{instance}")
    except EvolutionRequestError as exc:
        if _is_missing_instance_error(exc):
            return _session(status="disconnected", api_reachable=True)
        return _session(status="unavailable", api_reachable=False, error=str(exc))
    return _state_session(data)


def evolution_session_action(action: str, *, force: bool = False) -> dict[str, Any]:
    del force  # Evolution owns the session state; never force a competing reconnect.
    if not _enabled():
        return _disabled_session()
    normalized = action.strip().lower()
    if normalized in {"status", "refresh"}:
        return evolution_session_status()
    instance = quote(settings.evolution_instance, safe="")
    try:
        if normalized in {"disconnect", "reset"}:
            try:
                _request("DELETE", f"/instance/logout/{instance}")
            except EvolutionRequestError as exc:
                if not _is_missing_instance_error(exc):
                    raise
            return evolution_session_status()
        if normalized != "connect":
            raise ValueError("Ação de conexão inválida.")
        try:
            current = _request("GET", f"/instance/connectionState/{instance}")
            if not _connection_state(current):
                _create_instance()
        except EvolutionRequestError as exc:
            if not _is_missing_instance_error(exc):
                raise
            _create_instance()
        _configure_webhook()
        connection = _request("GET", f"/instance/connect/{instance}")
        qr_data_url = _find_qr(connection)
        try:
            status = evolution_session_status()
        except EvolutionRequestError:
            status = _state_session(connection, qr_data_url=qr_data_url)
        if qr_data_url:
            status["session"]["status"] = "qr"
            status["session"]["qr_data_url"] = qr_data_url
            status["session"]["needs_scan"] = True
        return status
    except EvolutionRequestError as exc:
        return _session(status="unavailable", api_reachable=False, error=str(exc))


def _destination(to: str) -> str:
    digits = re.sub(r"\D+", "", to or "")
    if len(digits) in {10, 11}:
        digits = f"55{digits}"
    if not re.fullmatch(r"55\d{10,11}", digits):
        raise ValueError("Telefone inválido para a Evolution API.")
    return digits


def _message_id(data: dict[str, Any]) -> str | None:
    candidates: list[Any] = [data]
    nested = data.get("data")
    if isinstance(nested, dict):
        candidates.append(nested)
    for candidate in candidates[:100]:
        key = candidate.get("key") if isinstance(candidate, dict) else None
        if isinstance(key, dict) and key.get("id"):
            return str(key["id"])
        if isinstance(candidate, dict):
            for field in ("messageId", "message_id", "id"):
                if candidate.get(field):
                    return str(candidate[field])
    return None


def send_via_evolution(to: str, text: str) -> dict[str, Any]:
    if not _enabled():
        return {"sent": False, "status": "error", "reason": "evolution_not_enabled", "transport": "evolution"}
    instance = quote(settings.evolution_instance, safe="")
    try:
        response = _request(
            "POST",
            f"/message/sendText/{instance}",
            payload={"number": _destination(to), "text": text},
        )
    except (EvolutionRequestError, ValueError) as exc:
        return {"sent": False, "status": "error", "reason": str(exc), "transport": "evolution"}
    return {
        "sent": True,
        "status": "sent",
        "provider_message_id": _message_id(response),
        "response": response,
        "transport": "evolution",
    }


def send_via_evolution_media(
    to: str,
    *,
    file_path: Path,
    media_type: str,
    mime_type: str,
    filename: str,
    caption: str,
) -> dict[str, Any]:
    if not _enabled():
        return {"sent": False, "status": "error", "reason": "evolution_not_enabled", "transport": "evolution"}
    if not file_path.exists() or not file_path.is_file():
        return {"sent": False, "status": "error", "reason": "arquivo_não_encontrado", "transport": "evolution"}
    if file_path.stat().st_size > settings.max_upload_bytes:
        return {"sent": False, "status": "error", "reason": "arquivo_excede_limite", "transport": "evolution"}
    instance = quote(settings.evolution_instance, safe="")
    try:
        encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
        response = _request(
            "POST",
            f"/message/sendMedia/{instance}",
            payload={
                "number": _destination(to),
                "mediatype": media_type,
                "mimetype": mime_type,
                "media": encoded,
                "fileName": filename[:240],
                "caption": caption[:1024],
            },
            timeout=max(settings.evolution_request_timeout_seconds, 60),
        )
    except (EvolutionRequestError, ValueError, OSError) as exc:
        return {"sent": False, "status": "error", "reason": str(exc), "transport": "evolution"}
    return {
        "sent": True,
        "status": "sent",
        "provider_message_id": _message_id(response),
        "response": response,
        "transport": "evolution",
    }


def _unwrap_message(message: dict[str, Any]) -> dict[str, Any]:
    current = message
    for _depth in range(4):
        nested: dict[str, Any] | None = None
        for wrapper in (
            "ephemeralMessage",
            "viewOnceMessage",
            "viewOnceMessageV2",
            "viewOnceMessageV2Extension",
            "documentWithCaptionMessage",
        ):
            body = current.get(wrapper)
            if isinstance(body, dict) and isinstance(body.get("message"), dict):
                nested = body["message"]
                break
        if nested is None:
            return current
        current = nested
    return current


def _content_text(message: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(message, dict):
        return "", "text"
    direct = str(message.get("conversation") or "").strip()
    if direct:
        return direct, "text"
    choices = (
        ("extendedTextMessage", "text", "text"),
        ("imageMessage", "caption", "image"),
        ("videoMessage", "caption", "video"),
        ("documentMessage", "caption", "document"),
        ("buttonsResponseMessage", "selectedDisplayText", "text"),
        ("listResponseMessage", "title", "text"),
    )
    for container, field, message_type in choices:
        body = message.get(container)
        if isinstance(body, dict):
            text = str(body.get(field) or "").strip()
            return text or f"Mensagem de {message_type} recebida.", message_type
    if isinstance(message.get("audioMessage"), dict):
        return "Mensagem de áudio recebida.", "audio"
    if isinstance(message.get("stickerMessage"), dict):
        return "Figurinha recebida.", "sticker"
    return "Mensagem recebida.", "text"


def _media_details(
    raw_message: dict[str, Any], message: dict[str, Any], message_type: str
) -> dict[str, Any]:
    containers = {
        "image": "imageMessage",
        "audio": "audioMessage",
        "video": "videoMessage",
        "document": "documentMessage",
        "sticker": "stickerMessage",
    }
    container_name = containers.get(message_type)
    if not container_name:
        return {}
    body = message.get(container_name)
    if not isinstance(body, dict):
        return {}
    mime_type = str(body.get("mimetype") or "").split(";", 1)[0].strip().lower()
    fallback_extensions = {
        "image": ".jpg",
        "audio": ".ogg",
        "video": ".mp4",
        "document": ".bin",
        "sticker": ".webp",
    }
    extension = mimetypes.guess_extension(mime_type) or fallback_extensions[message_type]
    filename = str(body.get("fileName") or body.get("filename") or "").strip()
    if not filename:
        filename = f"{message_type}-recebido{extension}"
    encoded = str(raw_message.get("base64") or message.get("base64") or "").strip()
    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    maximum_encoded_size = ((settings.max_upload_bytes + 2) // 3) * 4 + 8
    if len(encoded) > maximum_encoded_size:
        encoded = ""
        media_error = "media_exceeds_limit"
    elif not encoded:
        media_error = "media_base64_missing"
    else:
        media_error = ""
    return {
        "media_base64": encoded,
        "media_mime": mime_type,
        "media_name": filename[:240],
        "media_error": media_error,
    }


def evolution_inbound_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    event = str(payload.get("event") or payload.get("type") or "").strip().lower().replace("_", ".")
    if event not in {"messages.upsert", "message.upsert"}:
        return []
    source = payload.get("data")
    if isinstance(source, dict):
        candidates = source.get("messages") if isinstance(source.get("messages"), list) else [source]
    elif isinstance(source, list):
        candidates = source
    else:
        candidates = []
    items: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        key = candidate.get("key") if isinstance(candidate.get("key"), dict) else {}
        if key.get("fromMe"):
            continue
        remote_jid = str(key.get("remoteJid") or candidate.get("remoteJid") or "")
        alternate_jid = str(key.get("remoteJidAlt") or candidate.get("remoteJidAlt") or "")
        if remote_jid.endswith("@lid") and alternate_jid:
            remote_jid = alternate_jid
        if not remote_jid or remote_jid.endswith(("@g.us", "@broadcast")):
            continue
        if remote_jid.endswith("@lid"):
            continue
        raw_phone = remote_jid.split("@", 1)[0].split(":", 1)[0]
        if not raw_phone:
            continue
        raw_message = candidate.get("message") if isinstance(candidate.get("message"), dict) else candidate
        message = _unwrap_message(raw_message)
        text, message_type = _content_text(message)
        provider_message_id = str(key.get("id") or candidate.get("id") or "")
        media = _media_details(raw_message, message, message_type)
        items.append(
            {
                "phone": raw_phone,
                "name": str(candidate.get("pushName") or candidate.get("notifyName") or "")[:120],
                "text": text,
                "message_type": message_type,
                "provider_message_id": provider_message_id,
                **media,
                "raw": {
                    "source": "evolution",
                    "event": event,
                    "id": provider_message_id,
                    "remote_jid": remote_jid,
                    "message_type": message_type,
                    "timestamp": str(candidate.get("messageTimestamp") or ""),
                    **({"media_error": media["media_error"]} if media.get("media_error") else {}),
                },
            }
        )
    return items
