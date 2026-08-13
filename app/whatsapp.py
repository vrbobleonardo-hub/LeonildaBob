from __future__ import annotations

import mimetypes
from pathlib import Path
import re
import unicodedata
from typing import Any, Literal
from urllib.parse import urlparse

import requests

from . import db
from .settings import BRAZILIAN_AREA_CODES, settings
from .whatsapp_evolution import send_via_evolution, send_via_evolution_media
from .whatsapp_qr import send_via_qr_bridge as send_via_qr_connector


LeadKind = Literal["trabalhista", "instituto", "bpc", "geral"]
HTTP = requests.Session()
TRUSTED_META_MEDIA_HOSTS = frozenset({"graph.facebook.com", "lookaside.fbsbx.com"})


def _response_json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json() if response.content else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _provider_error(response: requests.Response, data: dict[str, Any]) -> str:
    error = data.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])[:500]
    return (response.text or f"Erro HTTP {response.status_code}")[:500]


def _provider_message_id(data: dict[str, Any]) -> str:
    messages = data.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        message_id = str(messages[0].get("id") or "")
        if message_id:
            return message_id
    raise RuntimeError("A Meta não retornou o identificador da mensagem.")


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D+", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits[1:]
    if len(digits) in {10, 11}:
        digits = f"55{digits}"
    if not digits.startswith("55") or len(digits) not in {12, 13}:
        raise ValueError("Telefone inválido. Informe DDD + número.")
    national = digits[2:]
    area_code = int(national[:2])
    subscriber = national[2:]
    if area_code not in BRAZILIAN_AREA_CODES or len(set(subscriber)) == 1:
        raise ValueError("Telefone inválido. Confira o DDD e o número.")
    if len(national) == 10 and subscriber.startswith("9"):
        digits = f"55{national[:2]}9{subscriber}"
        national = digits[2:]
        subscriber = national[2:]
    if len(national) == 11 and not subscriber.startswith("9"):
        raise ValueError("Celular inválido. O número deve começar com 9.")
    if len(national) == 10 and subscriber[0] not in {"2", "3", "4", "5"}:
        raise ValueError("Telefone fixo inválido.")
    return digits


def first_contact_message(kind: LeadKind, name: str, message: str | None = None) -> str:
    first_name = name.strip().split()[0]
    summary = (message or "").strip().replace("\n", " ")[:320]
    summary_note = f"\n\nResumo enviado no site: {summary}" if summary else ""
    if kind == "instituto":
        return (
            f"Olá, {first_name}. Aqui é Lucas, do atendimento do Instituto Leonilda Bob. "
            "Recebemos seu interesse pelo site. A iniciativa está em constituição e é voltada a apoiar bacharéis em Direito "
            "na preparação para o Exame da OAB.\n\n"
            "Para entendermos melhor, pode me responder:\n"
            "1) Você já concluiu o curso de Direito?\n"
            "2) Já prestou o Exame da OAB? Quantas vezes?\n"
            "3) Em qual cidade você está?\n\n"
            f"Assim conseguimos orientar o próximo contato com mais precisão.{summary_note}"
        )
    if kind == "trabalhista":
        return (
            f"Olá, {first_name}. Aqui é Lucas, atendimento do Bob Advogados. "
            "Recebemos seu contato pelo site sobre uma questão trabalhista.\n\n"
            "Para o primeiro atendimento, pode me responder:\n"
            "1) Você ainda trabalha na empresa ou já saiu?\n"
            "2) Seu registro era CLT, PJ ou sem registro?\n"
            "3) Qual é o principal ponto: rescisão, horas extras, assédio, acidente, verbas em atraso ou outro?\n\n"
            f"Essas informações ajudam a direcionar o atendimento. Esta conversa inicial não substitui análise jurídica formal.{summary_note}"
        )
    if kind == "bpc":
        return (
            f"Olá, {first_name}. Aqui é Lucas, atendimento do Bob Advogados. "
            "Recebemos seu pedido de triagem sobre BPC/LOAS pelo site.\n\n"
            "Para uma análise inicial organizada, pode me enviar:\n"
            "1) A carta de indeferimento do INSS;\n"
            "2) A data em que o pedido foi feito;\n"
            "3) Um resumo da renda familiar e das principais despesas com saúde, remédios, fraldas ou tratamentos.\n\n"
            f"A triagem é informativa e não envolve promessa de resultado. Cada caso exige análise individual.{summary_note}"
        )
    return (
        f"Olá, {first_name}. Aqui é Lucas, atendimento do Bob Advogados. "
        "Recebemos seu contato pelo site. Para que a equipe compreenda sua situação desde o início, "
        "conte com suas palavras o que aconteceu, quando começou e qual resultado você espera. "
        "Se souber, informe também a área jurídica relacionada.\n\n"
        f"Não envie senhas, códigos bancários ou dados de cartão por aqui.{summary_note}"
    )


def send_via_qr_bridge(to: str, text: str) -> dict[str, Any]:
    return send_via_qr_connector(to, text)


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
    response = HTTP.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=12,
    )
    data = _response_json(response)
    if response.status_code >= 400:
        raise RuntimeError(_provider_error(response, data))
    return data if isinstance(data, dict) else {}


def _official_endpoint(path: str) -> str:
    return f"https://graph.facebook.com/{settings.meta_graph_version}/{path.lstrip('/')}"


def trusted_meta_media_url(value: str) -> bool:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname in TRUSTED_META_MEDIA_HOSTS
        and port in {None, 443}
        and not parsed.username
        and not parsed.password
    )


def media_kind_from_mime(mime_type: str) -> str:
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
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
        response = HTTP.post(
            endpoint,
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            data={"messaging_product": "whatsapp"},
            files={"file": (file_path.name, file_handle, mime)},
            timeout=30,
        )
    data = _response_json(response)
    if response.status_code >= 400:
        raise RuntimeError(_provider_error(response, data))
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
    response = HTTP.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    data = _response_json(response)
    if response.status_code >= 400:
        raise RuntimeError(_provider_error(response, data))
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
    response = HTTP.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=12,
    )
    data = _response_json(response)
    if response.status_code >= 400:
        raise RuntimeError(_provider_error(response, data))
    return data if isinstance(data, dict) else {}


def send_whatsapp_text(
    to: str,
    text: str,
    *,
    conversation_id: int | None = None,
    first_contact: bool = False,
) -> dict[str, Any]:
    try:
        recipient = normalize_phone(to)
    except ValueError:
        recipient = str(to or "").strip()
    if db.is_whatsapp_opted_out(recipient):
        raise RuntimeError("Este contato solicitou não receber mensagens pelo WhatsApp.")
    provider_message_id = None
    status = "dry_run"
    raw: dict[str, Any] = {}
    if not settings.whatsapp_dry_run:
        if settings.whatsapp_provider == "qr":
            raw = send_via_qr_bridge(to, text)
            if not raw.get("sent"):
                raise RuntimeError(str(raw.get("reason") or "Não foi possível enviar pelo QR."))
            provider_message_id = str(raw.get("provider_message_id") or "") or None
            status = "sent"
        elif settings.whatsapp_provider == "evolution":
            raw = send_via_evolution(to, text)
            if not raw.get("sent"):
                raise RuntimeError(str(raw.get("reason") or "A Evolution API recusou o envio."))
            provider_message_id = str(raw.get("provider_message_id") or "") or None
            status = "sent"
        else:
            raw = (
                send_via_official_template(to)
                if first_contact and settings.whatsapp_first_contact_template
                else send_via_official_api(to, text)
            )
            provider_message_id = _provider_message_id(raw)
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
    try:
        recipient = normalize_phone(to)
    except ValueError:
        recipient = str(to or "").strip()
    if db.is_whatsapp_opted_out(recipient):
        raise RuntimeError("Este contato solicitou não receber mensagens pelo WhatsApp.")
    path = Path(file_path)
    mime = mime_type or mimetypes.guess_type(filename or path.name)[0] or "application/octet-stream"
    media_type = media_kind_from_mime(mime)
    status = "dry_run"
    provider_message_id = None
    provider_media_id = None
    raw: dict[str, Any] = {}
    if not settings.whatsapp_dry_run:
        if settings.whatsapp_provider == "qr":
            raise RuntimeError("O provedor QR configurado não suporta envio seguro de arquivos.")
        if settings.whatsapp_provider == "evolution":
            raw = send_via_evolution_media(
                to,
                file_path=path,
                media_type=media_type,
                mime_type=mime,
                filename=filename or path.name,
                caption=caption,
            )
            if not raw.get("sent"):
                raise RuntimeError(str(raw.get("reason") or "A Evolution API recusou o arquivo."))
            provider_message_id = str(raw.get("provider_message_id") or "") or None
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
            provider_message_id = _provider_message_id(raw)
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
    if not media_id.isdigit():
        raise RuntimeError("Identificador de mídia do WhatsApp inválido.")
    info_response = HTTP.get(
        _official_endpoint(media_id),
        headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
        timeout=15,
    )
    info = _response_json(info_response)
    if info_response.status_code >= 400:
        raise RuntimeError(_provider_error(info_response, info))
    url = str(info.get("url") or "")
    if not trusted_meta_media_url(url):
        raise RuntimeError("A Meta retornou um endereço de arquivo não autorizado.")
    declared_size = int(info.get("file_size") or 0)
    if declared_size > settings.max_upload_bytes:
        raise RuntimeError("Arquivo recebido excede o limite permitido.")
    with HTTP.get(
        url,
        headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
        timeout=30,
        stream=True,
    ) as media_response:
        if media_response.status_code >= 400:
            raise RuntimeError(media_response.text[:500])
        content_parts: list[bytes] = []
        size = 0
        for chunk in media_response.iter_content(1024 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > settings.max_upload_bytes:
                raise RuntimeError("Arquivo recebido excede o limite permitido.")
            content_parts.append(chunk)
        return b"".join(content_parts), str(info.get("mime_type") or "") or None, size or None


def _normalized_words(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", (value or "").strip().lower())
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return set(re.findall(r"[a-z0-9]+", normalized))


def _triage_area(kind: str | None, transcript: str) -> str:
    words = _normalized_words(transcript)
    if kind == "instituto" or words.intersection({"oab", "bacharel", "exame", "prova"}):
        return "instituto"
    if kind == "bpc" or words.intersection(
        {"bpc", "loas", "inss", "aposentadoria", "pensao", "previdenciario", "beneficio"}
    ):
        return "previdenciario"
    if kind == "trabalhista" or words.intersection(
        {"demissao", "rescisao", "fgts", "salario", "empresa", "trabalho", "trabalhista"}
    ):
        return "trabalhista"
    if words.intersection({"divorcio", "guarda", "alimentos", "inventario", "heranca", "familia"}):
        return "familia"
    if words.intersection({"banco", "fraude", "cobranca", "serasa", "produto", "consumidor"}):
        return "consumidor"
    if words.intersection({"imovel", "aluguel", "locacao", "usucapiao", "construtora"}):
        return "imobiliario"
    if words.intersection({"idoso", "curatela", "abandono", "golpe"}):
        return "idoso"
    return "geral"


def _case_story_question(area: str) -> str:
    prompts = {
        "instituto": (
            "Para eu compreender seu momento, conte se já concluiu Direito, se já prestou a OAB "
            "e qual tem sido sua maior dificuldade na preparação."
        ),
        "previdenciario": (
            "Para eu compreender seu caso, conte com suas palavras qual benefício foi pedido, "
            "o que o INSS respondeu e quando isso aconteceu."
        ),
        "trabalhista": (
            "Para eu compreender seu caso, conte com suas palavras o que aconteceu no trabalho, "
            "se o vínculo ainda existe e quando o problema começou."
        ),
        "familia": (
            "Para eu compreender a situação com o cuidado necessário, conte o que aconteceu, "
            "quem está envolvido e se já existe processo ou acordo."
        ),
        "consumidor": (
            "Para eu compreender o problema, conte o que foi contratado ou cobrado, "
            "quando aconteceu e como a empresa respondeu."
        ),
        "imobiliario": (
            "Para eu compreender o caso, conte qual é sua relação com o imóvel, "
            "o que aconteceu e se existe contrato, notificação ou processo."
        ),
        "idoso": (
            "Para eu compreender e preservar a segurança da pessoa idosa, conte o que aconteceu, "
            "há quanto tempo e se existe algum risco imediato."
        ),
        "geral": (
            "Para eu compreender e direcionar corretamente, conte com suas palavras o que aconteceu, "
            "quando começou e qual é a situação hoje."
        ),
    }
    return prompts[area]


def _auto_triage_step(history: list[dict[str, Any]]) -> int:
    inbound = [
        item for item in history if item.get("direction") == "in" and item.get("text")
    ]
    inbound_count = len(inbound)
    # Leads submitted through the site already receive the first question in the
    # outbound welcome message. Their first answer should advance the triage.
    first_inbound_id = min((int(item["id"]) for item in inbound), default=0)
    has_prior_outbound = any(
        item.get("direction") == "out"
        and item.get("text")
        and int(item.get("id") or 0) < first_inbound_id
        for item in history
    )
    return max(1, inbound_count + (1 if has_prior_outbound else 0))


def auto_triage_should_handoff(conversation_id: int) -> bool:
    """Indica quando o relato já tem informação suficiente para revisão humana."""
    return _auto_triage_step(db.list_messages(conversation_id, limit=24)) >= 5


def auto_reply_for_inbound(
    text: str,
    *,
    conversation_id: int | None = None,
    kind: str | None = None,
) -> str:
    """Conduz uma triagem curta e responsiva, sem prometer resultado jurídico."""
    history = db.list_messages(conversation_id, limit=24) if conversation_id else []
    inbound = [item for item in history if item.get("direction") == "in" and item.get("text")]
    transcript = " ".join(str(item.get("text") or "") for item in inbound) or text
    area = _triage_area(kind, transcript)
    step = _auto_triage_step(history) if history else 1
    urgent_words = _normalized_words(text).intersection(
        {"hoje", "amanha", "audiencia", "prazo", "urgente", "internacao", "despejo", "violencia"}
    )

    if step == 1:
        return (
            "Olá. Sou o assistente de atendimento do Bob Advogados. Vou fazer algumas perguntas curtas "
            "para que a equipe receba seu relato de forma organizada.\n\n"
            f"{_case_story_question(area)}\n\n"
            "Não envie senhas, códigos bancários ou dados de cartão por aqui."
        )
    if urgent_words:
        return (
            "Entendi que pode haver urgência. Qual é o prazo ou risco imediato e em que data ele ocorre? "
            "Se houver perigo à integridade de alguém, procure também o serviço público de emergência adequado."
        )
    if step == 2:
        return (
            "Obrigado por explicar. Para situar a equipe: quais são as datas mais importantes, "
            "o que já foi tentado e qual foi a última resposta da outra parte ou do órgão envolvido?"
        )
    if step == 3:
        return (
            "Certo. Você possui documentos relacionados ao caso, como contrato, carta, decisão, "
            "comprovantes, conversas ou laudos? Diga apenas quais possui; a equipe orientará depois "
            "a forma segura de envio."
        )
    if step == 4:
        return (
            "Para concluir esta triagem: qual resultado você espera e qual período é melhor para a equipe "
            "retornar, manhã ou tarde? A análise de viabilidade é individual e os próximos passos serão "
            "explicados com transparência, sem promessa de resultado."
        )
    return (
        "Perfeito. Seu relato inicial ficou organizado e será encaminhado à equipe para revisão. "
        "O atendimento humano continuará por este número no período informado. Se surgir um prazo novo "
        "antes do retorno, avise nesta conversa."
    )
