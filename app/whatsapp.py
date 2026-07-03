from __future__ import annotations

import mimetypes
from pathlib import Path
import re
from typing import Any, Literal

import requests

from . import db
from .settings import settings


LeadKind = Literal["trabalhista", "instituto", "geral"]


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D+", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits[1:]
    if len(digits) in {10, 11}:
        digits = f"55{digits}"
    if len(digits) < 12 or len(digits) > 13:
        raise ValueError("Telefone inválido. Informe DDD + número.")
    return digits


def first_contact_message(kind: LeadKind, name: str, message: str | None = None) -> str:
    first_name = name.strip().split(" ")[0]
    if kind == "instituto":
        return (
            f"Olá, {first_name}. Aqui é Lucas, do atendimento do Instituto Leonilda Bob. "
            "Recebemos seu interesse pelo site. A iniciativa está em constituição e é voltada a apoiar bacharéis em Direito "
            "na preparação para o Exame da OAB.\n\n"
            "Para entendermos melhor, pode me responder:\n"
            "1) Você já concluiu o curso de Direito?\n"
            "2) Já prestou o Exame da OAB? Quantas vezes?\n"
            "3) Em qual cidade você está?\n\n"
            "Assim conseguimos orientar o próximo contato com mais precisão."
        )
    if kind == "trabalhista":
        return (
            f"Olá, {first_name}. Aqui é Lucas, atendimento do Bob Advogados. "
            "Recebemos seu contato pelo site sobre uma questão trabalhista.\n\n"
            "Para o primeiro atendimento, pode me responder:\n"
            "1) Você ainda trabalha na empresa ou já saiu?\n"
            "2) Seu registro era CLT, PJ ou sem registro?\n"
            "3) Qual é o principal ponto: rescisão, horas extras, assédio, acidente, verbas em atraso ou outro?\n\n"
            "Essas informações ajudam a direcionar o atendimento. Esta conversa inicial não substitui análise jurídica formal."
        )
    return (
        f"Olá, {first_name}. Aqui é Lucas, atendimento do Bob Advogados. "
        "Recebemos seu contato pelo site. Pode me resumir o assunto e informar a melhor janela de horário para retorno?"
    )


def send_via_qr_bridge(to: str, text: str) -> None:
    if settings.whatsapp_dry_run:
        return
    if not settings.whatsapp_qr_bridge_url:
        raise RuntimeError("WHATSAPP_QR_BRIDGE_URL não configurado.")
    response = requests.post(
        f"{settings.whatsapp_qr_bridge_url}/send",
        json={"to": to, "text": text},
        timeout=8,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"QR bridge retornou {response.status_code}: {response.text[:240]}")


def send_via_official_api(to: str, text: str) -> dict[str, Any]:
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        raise RuntimeError("WhatsApp oficial não configurado.")
    endpoint = (
        f"https://graph.facebook.com/{settings.meta_graph_version}/"
        f"{settings.whatsapp_phone_number_id}/messages"
    )
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_phone(to),
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=12,
    )
    data = response.json() if response.text else {}
    if response.status_code >= 400:
        message = data.get("error", {}).get("message") if isinstance(data, dict) else response.text
        raise RuntimeError(str(message or response.text)[:500])
    return data if isinstance(data, dict) else {}


def _official_endpoint(path: str) -> str:
    return f"https://graph.facebook.com/{settings.meta_graph_version}/{path.lstrip('/')}"


def media_kind_from_mime(mime_type: str) -> str:
    mime = (mime_type or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    return "document"


def upload_official_media(file_path: Path, mime_type: str | None = None) -> dict[str, Any]:
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        raise RuntimeError("WhatsApp oficial não configurado.")
    mime = mime_type or mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    endpoint = _official_endpoint(f"{settings.whatsapp_phone_number_id}/media")
    with file_path.open("rb") as file_handle:
        response = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            data={"messaging_product": "whatsapp"},
            files={"file": (file_path.name, file_handle, mime)},
            timeout=30,
        )
    data = response.json() if response.text else {}
    if response.status_code >= 400:
        message = data.get("error", {}).get("message") if isinstance(data, dict) else response.text
        raise RuntimeError(str(message or response.text)[:500])
    return data if isinstance(data, dict) else {}


def send_via_official_media(
    to: str,
    *,
    media_id: str,
    media_type: str,
    filename: str = "",
    caption: str = "",
) -> dict[str, Any]:
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        raise RuntimeError("WhatsApp oficial não configurado.")
    endpoint = _official_endpoint(f"{settings.whatsapp_phone_number_id}/messages")
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_phone(to),
        "type": media_type,
        media_type: {"id": media_id},
    }
    if media_type in {"image", "video", "document"} and caption:
        payload[media_type]["caption"] = caption[:1024]
    if media_type == "document" and filename:
        payload[media_type]["filename"] = filename[:240]
    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    data = response.json() if response.text else {}
    if response.status_code >= 400:
        message = data.get("error", {}).get("message") if isinstance(data, dict) else response.text
        raise RuntimeError(str(message or response.text)[:500])
    return data if isinstance(data, dict) else {}


def send_via_official_template(to: str) -> dict[str, Any]:
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        raise RuntimeError("WhatsApp oficial não configurado.")
    if not settings.whatsapp_first_contact_template:
        raise RuntimeError("Modelo aprovado de primeiro contato não configurado.")
    endpoint = (
        f"https://graph.facebook.com/{settings.meta_graph_version}/"
        f"{settings.whatsapp_phone_number_id}/messages"
    )
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_phone(to),
        "type": "template",
        "template": {
            "name": settings.whatsapp_first_contact_template,
            "language": {"code": settings.whatsapp_template_language or "pt_BR"},
        },
    }
    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=12,
    )
    data = response.json() if response.text else {}
    if response.status_code >= 400:
        message = data.get("error", {}).get("message") if isinstance(data, dict) else response.text
        raise RuntimeError(str(message or response.text)[:500])
    return data if isinstance(data, dict) else {}


def send_whatsapp_text(
    to: str,
    text: str,
    *,
    conversation_id: int | None = None,
    first_contact: bool = False,
) -> dict[str, Any]:
    provider_message_id = None
    status = "dry_run"
    raw: dict[str, Any] = {}
    if not settings.whatsapp_dry_run:
        if settings.whatsapp_provider == "qr":
            send_via_qr_bridge(to, text)
            status = "sent"
        else:
            raw = send_via_official_template(to) if first_contact and settings.whatsapp_first_contact_template else send_via_official_api(to, text)
            messages = raw.get("messages") if isinstance(raw, dict) else None
            if isinstance(messages, list) and messages and isinstance(messages[0], dict):
                provider_message_id = messages[0].get("id")
            status = "sent"
    if conversation_id:
        db.record_whatsapp_message(
            conversation_id,
            direction="out",
            text=text,
            provider_message_id=provider_message_id,
            status=status,
            raw_payload=raw,
        )
    return {"status": status, "provider_message_id": provider_message_id, "raw": raw}


def send_whatsapp_media(
    to: str,
    file_path: str | Path,
    *,
    mime_type: str | None = None,
    filename: str = "",
    caption: str = "",
    conversation_id: int | None = None,
    public_url: str | None = None,
) -> dict[str, Any]:
    path = Path(file_path)
    mime = mime_type or mimetypes.guess_type(filename or path.name)[0] or "application/octet-stream"
    media_type = media_kind_from_mime(mime)
    status = "dry_run"
    provider_message_id = None
    provider_media_id = None
    raw: dict[str, Any] = {}
    if not settings.whatsapp_dry_run:
        if settings.whatsapp_provider == "qr":
            fallback = f"Arquivo enviado: {filename or path.name}"
            if caption:
                fallback = f"{fallback}\n{caption}"
            send_via_qr_bridge(to, fallback)
            status = "sent"
        else:
            upload = upload_official_media(path, mime)
            provider_media_id = str(upload.get("id") or "")
            if not provider_media_id:
                raise RuntimeError("A Meta não retornou o identificador do arquivo.")
            raw = send_via_official_media(
                to,
                media_id=provider_media_id,
                media_type=media_type,
                filename=filename or path.name,
                caption=caption,
            )
            messages = raw.get("messages") if isinstance(raw, dict) else None
            if isinstance(messages, list) and messages and isinstance(messages[0], dict):
                provider_message_id = messages[0].get("id")
            status = "sent"
    if conversation_id:
        db.record_whatsapp_message(
            conversation_id,
            direction="out",
            text=caption,
            message_type=media_type,
            provider_message_id=provider_message_id,
            media_url=public_url,
            media_mime=mime,
            media_name=filename or path.name,
            media_size=path.stat().st_size if path.exists() else None,
            media_provider_id=provider_media_id,
            status=status,
            raw_payload=raw,
        )
    return {
        "status": status,
        "provider_message_id": provider_message_id,
        "provider_media_id": provider_media_id,
        "raw": raw,
    }


def fetch_official_media(media_id: str) -> tuple[bytes, str | None, int | None]:
    if not settings.whatsapp_access_token:
        raise RuntimeError("Token do WhatsApp oficial não configurado.")
    info_response = requests.get(
        _official_endpoint(media_id),
        headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
        timeout=15,
    )
    info = info_response.json() if info_response.text else {}
    if info_response.status_code >= 400:
        message = info.get("error", {}).get("message") if isinstance(info, dict) else info_response.text
        raise RuntimeError(str(message or info_response.text)[:500])
    url = str(info.get("url") or "")
    if not url:
        raise RuntimeError("A Meta não retornou o endereço do arquivo.")
    media_response = requests.get(url, headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"}, timeout=30)
    if media_response.status_code >= 400:
        raise RuntimeError(media_response.text[:500])
    return media_response.content, str(info.get("mime_type") or "") or None, int(info.get("file_size") or 0) or None


def auto_reply_for_inbound(text: str) -> str:
    normalized = (text or "").strip().lower()
    if any(word in normalized for word in ["oab", "bacharel", "exame", "prova"]):
        return (
            "Recebi sua mensagem. Para o Instituto Leonilda Bob, me diga por favor: "
            "você já concluiu Direito, já prestou a OAB e em qual cidade está?"
        )
    if any(word in normalized for word in ["demissão", "demissao", "rescisão", "rescisao", "fgts", "salário", "salario", "empresa"]):
        return (
            "Recebi sua mensagem. Para entender melhor a questão trabalhista, me diga por favor: "
            "você ainda trabalha na empresa, seu registro era CLT ou outro formato, e qual é o principal problema?"
        )
    return (
        "Recebi sua mensagem. Pode me mandar um resumo do assunto e o melhor horário para retorno? "
        "Assim o atendimento organiza o próximo passo."
    )
