from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, Field

from .auth import authenticate, auth_is_configured, build_login_response, build_logout_response, current_admin, require_admin
from . import db
from .settings import settings
from .whatsapp import (
    auto_reply_for_inbound,
    fetch_official_media,
    first_contact_message,
    media_kind_from_mime,
    normalize_phone,
    send_whatsapp_media,
    send_whatsapp_text,
)


app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

UPLOAD_DIR = Path("static/uploads/whatsapp")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_UPLOAD_MIMES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "audio/aac",
    "audio/amr",
    "audio/mpeg",
    "audio/mp4",
    "audio/ogg",
    "video/mp4",
    "video/3gpp",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/plain",
}


class LeadPayload(BaseModel):
    kind: Literal["trabalhista", "instituto", "geral"] = "geral"
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=8, max_length=32)
    email: Optional[EmailStr] = None
    message: Optional[str] = Field(default=None, max_length=1200)
    consent: bool = False
    source_path: Optional[str] = Field(default=None, max_length=300)
    landing_path: Optional[str] = Field(default=None, max_length=300)
    referrer: Optional[str] = Field(default=None, max_length=500)
    visitor_id: Optional[str] = Field(default=None, max_length=80)
    session_id: Optional[str] = Field(default=None, max_length=80)
    utm_source: Optional[str] = Field(default=None, max_length=120)
    utm_medium: Optional[str] = Field(default=None, max_length=120)
    utm_campaign: Optional[str] = Field(default=None, max_length=180)
    utm_content: Optional[str] = Field(default=None, max_length=180)
    utm_term: Optional[str] = Field(default=None, max_length=180)
    gclid: Optional[str] = Field(default=None, max_length=240)
    fbclid: Optional[str] = Field(default=None, max_length=240)


class TrackPayload(BaseModel):
    path: str = Field(min_length=1, max_length=300)
    referrer: Optional[str] = Field(default=None, max_length=500)
    landing_path: Optional[str] = Field(default=None, max_length=300)
    visitor_id: Optional[str] = Field(default=None, max_length=80)
    session_id: Optional[str] = Field(default=None, max_length=80)
    utm_source: Optional[str] = Field(default=None, max_length=120)
    utm_medium: Optional[str] = Field(default=None, max_length=120)
    utm_campaign: Optional[str] = Field(default=None, max_length=180)
    utm_content: Optional[str] = Field(default=None, max_length=180)
    utm_term: Optional[str] = Field(default=None, max_length=180)
    gclid: Optional[str] = Field(default=None, max_length=240)
    fbclid: Optional[str] = Field(default=None, max_length=240)


@app.on_event("startup")
def startup() -> None:
    db.init_db()


def client_ip(request: Request) -> str | None:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else None


def template_context(request: Request, page: str, title: str, description: str) -> dict:
    return {
        "request": request,
        "page": page,
        "title": title,
        "description": description,
        "admin_user": current_admin(request),
        "whatsapp_display": "(11) 99492-6810",
        "address": "Rua Mariana Najar, 700 — Vila Nova Socorro, Mogi das Cruzes/SP",
        "address_zip": "CEP 08790-610",
        "maps_query": "Rua Mariana Najar, 700, Vila Nova Socorro, Mogi das Cruzes, SP, 08790-610",
    }


def safe_next_path(raw: str | None) -> str:
    value = raw or "/admin"
    if not value.startswith("/") or value.startswith("//"):
        return "/admin"
    return value


def safe_filename(filename: str) -> str:
    base = Path(filename or "arquivo").name
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip(".-")
    return base[:160] or "arquivo"


def save_upload_file(upload: UploadFile) -> tuple[Path, str, str, int]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(upload.filename or "arquivo")
    mime_type = upload.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if mime_type not in ALLOWED_UPLOAD_MIMES and not (mime_type.startswith("image/") or mime_type.startswith("audio/") or mime_type.startswith("video/")):
        raise HTTPException(status_code=400, detail="Tipo de arquivo não aceito.")
    destination = UPLOAD_DIR / f"{db.utc_now().replace(':', '').replace('+', '-')}-{filename}"
    size = 0
    with destination.open("wb") as output:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail="Arquivo muito grande. Use até 25 MB.")
            output.write(chunk)
    return destination, filename, mime_type, size


def media_payload_from_message(message: dict[str, Any]) -> dict[str, Any]:
    message_type = str(message.get("type") or "text")
    if message_type not in {"image", "audio", "video", "document", "sticker"}:
        return {}
    media = message.get(message_type)
    if not isinstance(media, dict):
        return {}
    media_id = str(media.get("id") or "")
    filename = safe_filename(str(media.get("filename") or f"{message_type}-{media_id}"))
    mime_type = str(media.get("mime_type") or "") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    media_url = None
    media_size = None
    if media_id and settings.whatsapp_access_token:
        try:
            content, fetched_mime, fetched_size = fetch_official_media(media_id)
            mime_type = fetched_mime or mime_type
            extension = mimetypes.guess_extension(mime_type) or Path(filename).suffix or ".bin"
            local_name = safe_filename(f"{media_id}{extension}")
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            local_path = UPLOAD_DIR / local_name
            local_path.write_bytes(content)
            media_url = f"/static/uploads/whatsapp/{local_name}"
            media_size = fetched_size or len(content)
        except Exception as exc:
            return {
                "media_mime": mime_type,
                "media_name": filename,
                "media_provider_id": media_id,
                "raw_payload": {"media_error": str(exc), "message": message},
            }
    return {
        "media_url": media_url,
        "media_mime": mime_type,
        "media_name": filename,
        "media_size": media_size,
        "media_provider_id": media_id,
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        template_context(
            request,
            "home",
            "Leonilda Bob | Bob Advogados",
            "Advocacia trabalhista e atuação institucional em Mogi das Cruzes, com atendimento presencial e online.",
        ),
    )


@app.get("/sobre", response_class=HTMLResponse)
def sobre(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "sobre.html",
        template_context(
            request,
            "sobre",
            "Trajetória | Leonilda Bob",
            "Conheça a trajetória acadêmica e profissional de Leonilda Bob, OAB/SP 85.766.",
        ),
    )


@app.get("/atuacao", response_class=HTMLResponse)
def atuacao(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "atuacao.html",
        template_context(
            request,
            "atuacao",
            "Atuação jurídica | Bob Advogados",
            "Atendimento em Direito Trabalhista, Consumidor, Imobiliário, Tributário e Público.",
        ),
    )


@app.get("/instituto", response_class=HTMLResponse)
def instituto(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "instituto.html",
        template_context(
            request,
            "instituto",
            "Instituto Leonilda Bob",
            "Iniciativa em constituição para apoiar bacharéis em Direito na preparação para o Exame da OAB.",
        ),
    )


@app.get("/contato", response_class=HTMLResponse)
def contato(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "contato.html",
        template_context(
            request,
            "contato",
            "Contato | Bob Advogados",
            "Fale com o atendimento do Bob Advogados por formulário, WhatsApp, online ou presencialmente.",
        ),
    )


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login(request: Request, next: str = "/admin", error: str = "") -> HTMLResponse:
    context = template_context(
        request,
        "admin-login",
        "Entrar | Admin Bob Advogados",
        "Acesso interno protegido do Bob Advogados.",
    )
    context.update(
        {
            "next_path": safe_next_path(next),
            "error": error,
            "auth_configured": auth_is_configured(),
        }
    )
    return templates.TemplateResponse("admin_login.html", context)


@app.post("/admin/login")
def admin_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_path: str = Form("/admin"),
):
    user = authenticate(username, password)
    if not user:
        context = template_context(
            request,
            "admin-login",
            "Entrar | Admin Bob Advogados",
            "Acesso interno protegido do Bob Advogados.",
        )
        context.update(
            {
                "next_path": safe_next_path(next_path),
                "error": "Usuário ou senha inválidos.",
                "auth_configured": auth_is_configured(),
            }
        )
        return templates.TemplateResponse(
            "admin_login.html",
            context,
            status_code=401,
        )
    return build_login_response(user.username, safe_next_path(next_path))


@app.post("/admin/logout")
def admin_logout(request: Request):
    require_admin(request)
    return build_logout_response()


@app.post("/api/leads")
def create_lead(payload: LeadPayload, request: Request) -> JSONResponse:
    if not payload.consent:
        raise HTTPException(status_code=400, detail="Confirme a autorização de contato para continuar.")
    phone = normalize_phone(payload.phone)
    lead_data = payload.model_dump()
    lead_data.update(
        {
            "phone": phone,
            "ip_hash": db.hash_ip(client_ip(request)),
            "user_agent": request.headers.get("user-agent", "")[:500],
        }
    )
    lead_id = db.insert_lead(lead_data)
    message = first_contact_message(payload.kind, payload.name, payload.message)
    outbox_id = db.enqueue_whatsapp(lead_id, phone, message)
    conversation_id = db.get_or_create_conversation(
        phone,
        name=payload.name,
        kind=payload.kind,
        source_lead_id=lead_id,
    )
    dispatch_status = "queued"
    try:
        send_result = send_whatsapp_text(phone, message, conversation_id=conversation_id, first_contact=True)
        dispatch_status = send_result["status"]
        db.mark_outbox_sent(outbox_id, status=dispatch_status, provider_message_id=send_result.get("provider_message_id"))
    except Exception as exc:  # pragma: no cover - integration/runtime path
        db.mark_outbox_failed(outbox_id, str(exc))
        dispatch_status = "queued"
    return JSONResponse(
        {
            "ok": True,
            "lead_id": lead_id,
            "whatsapp": dispatch_status,
            "message": "Recebemos seu contato. O atendimento continuará pelo WhatsApp.",
        }
    )


@app.post("/api/track")
def track(payload: TrackPayload, request: Request) -> JSONResponse:
    db.record_page_view(
        path=payload.path,
        referrer=payload.referrer,
        ip_hash_value=db.hash_ip(client_ip(request)),
        user_agent=request.headers.get("user-agent", ""),
        origin=payload.model_dump(),
    )
    return JSONResponse({"ok": True})


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request) -> HTMLResponse:
    user = current_admin(request)
    if not user:
        return RedirectResponse("/admin/login?next=/admin", status_code=303)
    snapshot = db.admin_snapshot()
    context = template_context(
        request,
        "admin",
        "Admin | Leonilda Bob",
        "Painel local de contatos, visitas e WhatsApp.",
    )
    context.update(
        {
            "snapshot": snapshot,
            "dry_run": settings.whatsapp_dry_run,
            "whatsapp_provider": settings.whatsapp_provider,
            "official_ready": bool(settings.whatsapp_access_token and settings.whatsapp_phone_number_id),
            "webhook_url": f"{settings.app_base_url.rstrip('/')}/api/webhooks/whatsapp",
            "verify_token_set": bool(settings.whatsapp_verify_token),
            "template_set": bool(settings.whatsapp_first_contact_template),
            "admin_user": user,
        }
    )
    return templates.TemplateResponse("admin.html", context)


@app.get("/api/admin/metrics")
def metrics(request: Request) -> JSONResponse:
    require_admin(request)
    return JSONResponse(db.admin_snapshot())


@app.get("/api/admin/conversations")
def admin_conversations(request: Request) -> JSONResponse:
    require_admin(request)
    return JSONResponse({"ok": True, "conversations": db.list_conversations()})


@app.get("/api/admin/conversations/{conversation_id}/messages")
def admin_conversation_messages(conversation_id: int, request: Request) -> JSONResponse:
    require_admin(request)
    conversation = db.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa não encontrada.")
    return JSONResponse({"ok": True, "conversation": conversation, "messages": db.list_messages(conversation_id)})


@app.post("/api/admin/conversations/{conversation_id}/messages")
def admin_send_message(
    conversation_id: int,
    request: Request,
    text: str = Form(""),
    attachment: Optional[UploadFile] = File(None),
) -> JSONResponse:
    require_admin(request)
    conversation = db.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa não encontrada.")
    clean_text = (text or "").strip()
    if not clean_text and not attachment:
        raise HTTPException(status_code=400, detail="Escreva uma mensagem ou selecione um arquivo.")
    try:
        if attachment and attachment.filename:
            path, filename, mime_type, _size = save_upload_file(attachment)
            public_url = f"/static/uploads/whatsapp/{path.name}"
            result = send_whatsapp_media(
                conversation["phone"],
                path,
                mime_type=mime_type,
                filename=filename,
                caption=clean_text,
                conversation_id=conversation_id,
                public_url=public_url,
            )
        else:
            result = send_whatsapp_text(conversation["phone"], clean_text, conversation_id=conversation_id)
        return JSONResponse({"ok": True, "status": result.get("status", "sent")})
    except Exception as exc:
        if attachment and attachment.filename:
            db.record_whatsapp_message(
                conversation_id,
                direction="out",
                text=clean_text,
                message_type=media_kind_from_mime(attachment.content_type or ""),
                status="failed",
                raw_payload={"error": str(exc)},
            )
        else:
            db.record_whatsapp_message(
                conversation_id,
                direction="out",
                text=clean_text,
                status="failed",
                raw_payload={"error": str(exc)},
            )
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


@app.get("/api/admin/whatsapp/status")
def admin_whatsapp_status(request: Request) -> JSONResponse:
    require_admin(request)
    return JSONResponse(
        {
            "ok": True,
            "dry_run": settings.whatsapp_dry_run,
            "provider": settings.whatsapp_provider,
            "official_ready": bool(settings.whatsapp_access_token and settings.whatsapp_phone_number_id),
            "phone_number_id_set": bool(settings.whatsapp_phone_number_id),
            "access_token_set": bool(settings.whatsapp_access_token),
            "verify_token_set": bool(settings.whatsapp_verify_token),
            "template_set": bool(settings.whatsapp_first_contact_template),
            "webhook_url": f"{settings.app_base_url.rstrip('/')}/api/webhooks/whatsapp",
        }
    )


@app.post("/api/admin/outbox/{outbox_id}/retry")
def retry_outbox(outbox_id: int, request: Request) -> JSONResponse:
    require_admin(request)
    item = db.get_outbox_message(outbox_id)
    if not item:
        raise HTTPException(status_code=404, detail="Mensagem não encontrada.")
    try:
        conversation_id = db.get_or_create_conversation(item["recipient"])
        send_result = send_whatsapp_text(item["recipient"], item["message"], conversation_id=conversation_id)
        dispatch_status = send_result["status"]
        db.mark_outbox_sent(outbox_id, status=dispatch_status, provider_message_id=send_result.get("provider_message_id"))
        return JSONResponse({"ok": True, "status": dispatch_status})
    except Exception as exc:  # pragma: no cover - integration/runtime path
        db.mark_outbox_failed(outbox_id, str(exc))
        return JSONResponse({"ok": False, "status": "queued", "error": str(exc)}, status_code=502)


@app.get("/api/webhooks/whatsapp")
def verify_whatsapp_webhook(request: Request) -> PlainTextResponse:
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token and token == settings.whatsapp_verify_token:
        return PlainTextResponse(challenge or "")
    raise HTTPException(status_code=403, detail="Verificação inválida.")


def extract_message_text(message: dict[str, Any]) -> str:
    if isinstance(message.get("text"), dict):
        return str(message["text"].get("body") or "").strip()
    if isinstance(message.get("button"), dict):
        return str(message["button"].get("text") or "").strip()
    if isinstance(message.get("interactive"), dict):
        interactive = message["interactive"]
        for key in ("button_reply", "list_reply"):
            if isinstance(interactive.get(key), dict):
                return str(interactive[key].get("title") or interactive[key].get("id") or "").strip()
    return ""


@app.post("/api/webhooks/whatsapp")
async def receive_whatsapp_webhook(request: Request) -> JSONResponse:
    payload = await request.json()
    entries = payload.get("entry") if isinstance(payload, dict) else []
    handled = 0
    for entry in entries or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for status in value.get("statuses") or []:
                db.update_outbox_provider_status(str(status.get("id") or ""), str(status.get("status") or ""))
                handled += 1
            contacts = value.get("contacts") or []
            contact_name = ""
            if contacts and isinstance(contacts[0], dict):
                contact_name = str((contacts[0].get("profile") or {}).get("name") or "")
            for message in value.get("messages") or []:
                try:
                    phone = normalize_phone(str(message.get("from") or ""))
                except ValueError:
                    continue
                if not phone:
                    continue
                text = extract_message_text(message)
                message_type = str(message.get("type") or "text")
                conversation_id = db.get_or_create_conversation(phone, name=contact_name)
                media_payload = media_payload_from_message(message)
                raw_payload = message
                if media_payload.get("raw_payload"):
                    raw_payload = media_payload.pop("raw_payload")
                db.record_whatsapp_message(
                    conversation_id,
                    direction="in",
                    text=text,
                    message_type=message_type,
                    provider_message_id=str(message.get("id") or ""),
                    status="received",
                    raw_payload=raw_payload,
                    **media_payload,
                )
                handled += 1
                if settings.whatsapp_auto_reply and text:
                    reply = auto_reply_for_inbound(text)
                    try:
                        send_whatsapp_text(phone, reply, conversation_id=conversation_id)
                    except Exception as exc:  # pragma: no cover - integration/runtime path
                        db.record_whatsapp_message(
                            conversation_id,
                            direction="out",
                            text=reply,
                            status="failed",
                            raw_payload={"error": str(exc)},
                        )
    return JSONResponse({"ok": True, "handled": handled})
