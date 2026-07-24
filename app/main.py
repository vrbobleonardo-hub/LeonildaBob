from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import parse_qs, urlparse, urlunsplit

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationError, field_validator
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import authenticate, auth_is_configured, build_login_response, build_logout_response, current_admin, require_admin
from . import db, storage
from .security import (
    LOGIN_CSRF_COOKIE,
    new_login_csrf_token,
    rate_limit,
    request_ip,
    session_csrf_token,
    verify_login_csrf,
    verify_same_origin,
    verify_session_csrf,
    verify_whatsapp_signature,
)
from .settings import settings
from .uploads import safe_display_filename, save_validated_bytes, save_validated_upload
from .whatsapp import (
    auto_reply_for_inbound,
    fetch_official_media,
    first_contact_message,
    media_kind_from_mime,
    normalize_phone,
    send_whatsapp_media,
    send_whatsapp_text,
)


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("bob_advogados")

BLOG_CATEGORIES = (
    "Direito Trabalhista",
    "Orientação Jurídica",
    "Carreira e OAB",
    "Instituto Leonilda Bob",
    "Reflexões",
)
BLOG_PAGE_SIZE = 9
MONTH_NAMES = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)


class SelectiveGZipMiddleware:
    COMPRESSED_SUFFIXES = (
        ".avif",
        ".webp",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".mp3",
        ".mp4",
        ".ogg",
        ".pdf",
        ".zip",
    )

    def __init__(self, app: Any, minimum_size: int = 700) -> None:
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size, compresslevel=6)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = str(scope.get("path") or "").lower()
        if scope.get("type") == "http" and (
            path.endswith(self.COMPRESSED_SUFFIXES)
            or path.startswith("/admin")
            or path.startswith("/api/admin")
        ):
            await self.app(scope, receive, send)
            return
        await self.gzip(scope, receive, send)


class RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        route_limits = {
            "/admin/login": 64 * 1024,
            "/admin/logout": 64 * 1024,
            "/api/leads": 128 * 1024,
            "/api/track": 64 * 1024,
            "/api/webhooks/whatsapp": 2 * 1024 * 1024,
            "/contato/enviar": 128 * 1024,
        }
        request_limit = min(self.max_bytes, route_limits.get(path, self.max_bytes))
        if path.startswith("/admin/artigos"):
            request_limit = min(request_limit, 512 * 1024)
        if path.startswith("/api/admin/") and not re.fullmatch(
            r"/api/admin/conversations/\d+/messages", path
        ):
            request_limit = min(request_limit, 64 * 1024)
        for name, value in scope.get("headers") or []:
            if name.lower() == b"content-length":
                try:
                    if int(value) > request_limit:
                        response = JSONResponse({"detail": "Requisição muito grande."}, status_code=413)
                        await response(scope, receive, send)
                        return
                except ValueError:
                    response = JSONResponse({"detail": "Cabeçalho de tamanho inválido."}, status_code=400)
                    await response(scope, receive, send)
                    return
        received = 0
        response_started = False

        async def limited_receive() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > request_limit:
                    raise RequestBodyTooLarge
            return message

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if response_started:
                raise
            response = JSONResponse({"detail": "Requisição muito grande."}, status_code=413)
            await response(scope, receive, send)


@asynccontextmanager
async def lifespan(application: FastAPI):
    db.init_db()
    try:
        application.state.storage_ready = storage.ensure_bucket()
    except RuntimeError:
        application.state.storage_ready = False
    cleanup_retention()
    application.state.outbox_task = asyncio.create_task(outbox_worker())
    application.state.webhook_task = asyncio.create_task(webhook_worker())
    application.state.retention_task = asyncio.create_task(retention_worker())
    try:
        yield
    finally:
        tasks = (
            application.state.outbox_task,
            application.state.webhook_task,
            application.state.retention_task,
        )
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        db.close_pool()


app = FastAPI(
    title=settings.app_name,
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
    lifespan=lifespan,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
app.add_middleware(SelectiveGZipMiddleware, minimum_size=700)
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_bytes=settings.max_upload_bytes + 2 * 1024 * 1024,
)
templates = Jinja2Templates(directory=settings.template_dir)
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")

UPLOAD_DIR = settings.private_upload_dir


def minimized_referrer(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("/") and not raw.startswith("//"):
        return raw.split("?", 1)[0].split("#", 1)[0][:500]
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", "", ""))[:500]


@app.middleware("http")
async def security_headers(request: Request, call_next):
    supplied_request_id = request.headers.get("x-request-id", "")
    request_id = (
        supplied_request_id
        if re.fullmatch(r"[A-Za-z0-9._-]{1,80}", supplied_request_id)
        else secrets.token_hex(12)
    )
    request.state.request_id = request_id
    request.state.csp_nonce = secrets.token_urlsafe(18)
    early_response: Response | None = None
    if request.url.path.startswith("/static/uploads/"):
        early_response = JSONResponse(
            {"detail": "Arquivo não disponível publicamente."}, status_code=404
        )
    if early_response is None:
        try:
            content_length = int(request.headers.get("content-length") or 0)
        except ValueError:
            early_response = JSONResponse(
                {"detail": "Cabeçalho de tamanho inválido."}, status_code=400
            )
        else:
            body_limit = settings.max_upload_bytes + 2 * 1024 * 1024
            if content_length < 0:
                early_response = JSONResponse(
                    {"detail": "Cabeçalho de tamanho inválido."}, status_code=400
                )
            elif content_length > body_limit:
                early_response = JSONResponse(
                    {"detail": "Requisição muito grande."}, status_code=413
                )
    preparse_limits = {
        ("POST", "/admin/login"): (30, 900),
        ("POST", "/api/leads"): (40, 60),
        ("POST", "/api/track"): (240, 60),
        ("POST", "/contato/enviar"): (40, 60),
    }
    limit_config = preparse_limits.get((request.method, request.url.path))
    if early_response is None and limit_config:
        try:
            rate_limit(
                request,
                f"preparse:{request.url.path}",
                limit=limit_config[0],
                window_seconds=limit_config[1],
            )
        except HTTPException as exc:
            early_response = JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )
    started = time.perf_counter()
    if early_response is not None:
        response = early_response
    else:
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.exception(
                json.dumps(
                    {
                        "event": "http.error",
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path,
                    },
                    ensure_ascii=False,
                )
            )
            response = await internal_error(request, exc)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), browsing-topics=()"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
        f"form-action 'self'; script-src 'self' 'nonce-{request.state.csp_nonce}'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; media-src 'self'; "
        "frame-src https://www.google.com https://maps.google.com"
    )
    if settings.is_production:
        response.headers["Content-Security-Policy"] += "; upgrade-insecure-requests"
    if settings.cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if response.status_code >= 400 or request.url.path.startswith("/admin"):
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    if response.status_code >= 500:
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/uploads/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/admin") or request.url.path.startswith("/api/admin"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    elif request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
    elif response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    if hasattr(request.state, "rate_limit_remaining"):
        response.headers["X-RateLimit-Remaining"] = str(request.state.rate_limit_remaining)
    logger.info(
        json.dumps(
            {
                "event": "http.request",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
            ensure_ascii=False,
        )
    )
    return response


class LeadPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["trabalhista", "instituto", "bpc", "geral"] = "geral"
    area: Optional[
        Literal[
            "trabalhista",
            "consumidor",
            "imobiliario",
            "tributario",
            "publico",
            "previdenciario",
            "familia",
            "idoso",
            "instituto",
            "outro",
        ]
    ] = None
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=8, max_length=32)
    email: Optional[EmailStr] = None
    message: str = Field(min_length=10, max_length=1200)
    consent: bool = False
    website: str = Field(default="", max_length=0)
    form_started_at: Optional[int] = None
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

    @field_validator("name", "message", mode="before")
    @classmethod
    def strip_required_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("area", mode="after")
    @classmethod
    def default_area(cls, value: str | None, info: Any) -> str | None:
        if value:
            return value
        kind = info.data.get("kind")
        if kind == "instituto":
            return "instituto"
        if kind == "trabalhista":
            return "trabalhista"
        if kind == "bpc":
            return "previdenciario"
        return None

    @field_validator("source_path", "landing_path", mode="before")
    @classmethod
    def local_attribution_path(cls, value: Any) -> str | None:
        path = str(value or "").strip()
        if not path:
            return None
        if not path.startswith("/") or path.startswith("//") or "\\" in path or any(
            character in path for character in ("\r", "\n", "\x00")
        ):
            raise ValueError("Caminho de origem inválido.")
        return path.split("?", 1)[0].split("#", 1)[0]

    @field_validator("referrer", mode="before")
    @classmethod
    def minimize_referrer_value(cls, value: Any) -> str | None:
        return minimized_referrer(value)


class TrackPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

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

    @field_validator("path")
    @classmethod
    def local_path_only(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or value.startswith("//")
            or "\\" in value
            or any(character in value for character in ("\r", "\n", "\x00"))
        ):
            raise ValueError("Caminho inválido.")
        return value.split("?", 1)[0].split("#", 1)[0]

    @field_validator("referrer", mode="before")
    @classmethod
    def minimize_referrer_value(cls, value: Any) -> str | None:
        return minimized_referrer(value)

    @field_validator(
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", mode="before"
    )
    @classmethod
    def normalize_campaign_value(cls, value: Any) -> str | None:
        normalized = str(value or "").strip().lower()
        return normalized or None


class ConversationControlPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["open", "closed", "archived"]] = None
    bot_enabled: Optional[bool] = None


class LeadStatusPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["new", "contacted", "qualified", "closed", "archived"]


class PrivacyDeletePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(min_length=8, max_length=32)


def delete_private_media(media_urls: list[str]) -> bool:
    success = True
    for media_url in media_urls:
        try:
            parsed = urlparse(media_url)
            object_path = parse_qs(parsed.query).get("path", [""])[0]
            if object_path.startswith("local/"):
                local_name = safe_filename(object_path.removeprefix("local/"))
                (UPLOAD_DIR / local_name).unlink(missing_ok=True)
            elif object_path:
                storage.delete(object_path)
        except Exception:
            success = False
    return success


def cleanup_retention() -> None:
    db.cleanup_expired_data()
    for phone in db.list_expired_contact_phones():
        if delete_private_media(db.contact_media_urls(phone)):
            db.delete_contact_data(phone)


def dispatch_pending_outbox() -> None:
    if settings.whatsapp_dry_run:
        return
    for item in db.list_pending_outbox(limit=20):
        try:
            conversation_id = db.get_or_create_conversation(
                item["recipient"],
                name=item.get("name") or "",
                kind=item.get("kind") or "",
                source_lead_id=item.get("lead_id"),
            )
            result = send_whatsapp_text(
                item["recipient"],
                item["message"],
                conversation_id=conversation_id,
                first_contact=True,
            )
            db.mark_outbox_sent(
                item["id"],
                status=result.get("status", "sent"),
                provider_message_id=result.get("provider_message_id"),
            )
        except Exception as exc:
            db.mark_outbox_failed(item["id"], str(exc))


async def outbox_worker() -> None:
    while True:
        try:
            await asyncio.to_thread(dispatch_pending_outbox)
        except Exception:
            logger.exception(json.dumps({"event": "whatsapp.outbox_worker_failed"}))
        await asyncio.sleep(5)


def dispatch_pending_webhooks() -> None:
    for item in db.list_pending_whatsapp_webhooks(limit=20):
        try:
            payload = json.loads(item["payload"])
            if not isinstance(payload, dict):
                raise ValueError("Evento do WhatsApp inválido.")
            process_whatsapp_payload(payload)
            db.mark_whatsapp_webhook_processed(int(item["id"]))
        except Exception as exc:
            db.mark_whatsapp_webhook_failed(int(item["id"]), str(exc))


async def webhook_worker() -> None:
    while True:
        try:
            await asyncio.to_thread(dispatch_pending_webhooks)
        except Exception:
            logger.exception(json.dumps({"event": "whatsapp.webhook_worker_failed"}))
        await asyncio.sleep(2)


async def retention_worker() -> None:
    while True:
        await asyncio.sleep(86_400)
        try:
            await asyncio.to_thread(cleanup_retention)
        except Exception:
            logger.exception(json.dumps({"event": "retention.cleanup_failed"}))


@app.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/readyz", include_in_schema=False)
def readyz() -> JSONResponse:
    database_ready = db.healthcheck()
    storage_ready = not storage.configured() or bool(getattr(app.state, "storage_ready", False))
    status_code = 200 if database_ready and storage_ready else 503
    return JSONResponse(
        {"status": "ready" if status_code == 200 else "degraded", "database": database_ready, "storage": storage_ready},
        status_code=status_code,
    )


@app.exception_handler(404)
async def not_found_page(request: Request, _exc: Exception) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Recurso não encontrado."}, status_code=404)
    context = template_context(
        request,
        "not-found",
        "Página não encontrada | Bob Advogados",
        "A página solicitada não foi encontrada.",
    )
    return templates.TemplateResponse(request, "404.html", context=context, status_code=404)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = []
    for item in exc.errors():
        location = [str(part) for part in item.get("loc", ()) if part not in {"body", "query", "path"}]
        errors.append(
            {
                "field": ".".join(location) or "formulário",
                "message": item.get("msg", "Valor inválido."),
            }
        )
    return JSONResponse(
        {"detail": "Revise os campos informados.", "errors": errors},
        status_code=422,
    )


@app.exception_handler(Exception)
async def internal_error(request: Request, _exc: Exception) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Erro interno. Tente novamente mais tarde."}, status_code=500)
    context = template_context(
        request,
        "internal-error",
        "Serviço temporariamente indisponível | Bob Advogados",
        "Não foi possível concluir esta solicitação agora.",
    )
    return templates.TemplateResponse(request, "500.html", context=context, status_code=500)


@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
def robots() -> PlainTextResponse:
    return PlainTextResponse(
        f"User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api/\nSitemap: {settings.app_base_url}/sitemap.xml\n"
    )


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap() -> Response:
    paths = ["/", "/bpc-loas-negado", "/sobre", "/atuacao", "/blog", "/contato", "/politica-de-privacidade"]
    published_posts, _total = db.list_published_blog_posts(limit=1000)
    paths.extend(f"/blog/{post['slug']}" for post in published_posts)
    urls = "".join(f"<url><loc>{settings.app_base_url}{path}</loc></url>" for path in paths)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(xml, media_type="application/xml")


def client_ip(request: Request) -> str | None:
    return request_ip(request)


def template_context(request: Request, page: str, title: str, description: str) -> dict:
    canonical_url = f"{settings.app_base_url}{request.url.path}"
    maps_query = f"{settings.address}, {settings.address_zip}"
    try:
        admin_user = current_admin(request)
    except Exception:
        admin_user = None
    return {
        "request": request,
        "page": page,
        "title": title,
        "description": description,
        "admin_user": admin_user,
        "csrf_token": session_csrf_token(request),
        "canonical_url": canonical_url,
        "social_image_url": f"{settings.app_base_url}/static/assets/social-card.jpg",
        "og_type": "website",
        "whatsapp_display": settings.contact_phone_display,
        "whatsapp_digits": settings.contact_phone_digits,
        "contact_email": settings.contact_email,
        "address": settings.address,
        "address_zip": settings.address_zip,
        "office_hours": settings.office_hours,
        "analytics_retention_days": settings.analytics_retention_days,
        "lead_retention_days": settings.lead_retention_days,
        "max_upload_bytes": settings.max_upload_bytes,
        "maps_query": maps_query,
        "selected_area": request.query_params.get("area", "")[:30],
        "form_started_at": int(time.time()),
        "form_request_key": secrets.token_urlsafe(24),
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
        "structured_data": {
            "@context": "https://schema.org",
            "@type": "LegalService",
            "name": "Bob Advogados",
            "url": settings.app_base_url,
            "image": f"{settings.app_base_url}/static/assets/bobadv-logo-full.png",
            "telephone": f"+{settings.contact_phone_digits}",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": settings.address,
                "postalCode": settings.address_zip.replace("CEP", "").strip(),
                "addressLocality": "Mogi das Cruzes",
                "addressRegion": "SP",
                "addressCountry": "BR",
            },
            "areaServed": "Brasil",
        },
    }


def article_display_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return ""
    return f"{parsed.day} de {MONTH_NAMES[parsed.month - 1]} de {parsed.year}"


def prepare_blog_post(post: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(post)
    prepared["display_date"] = article_display_date(post.get("published_at") or post.get("updated_at"))
    prepared["reading_minutes"] = max(1, (len(str(post.get("body") or "").split()) + 199) // 200)
    prepared["body_blocks"] = [
        block.strip()
        for block in re.split(r"\n\s*\n", str(post.get("body") or ""))
        if block.strip()
    ]
    return prepared


def safe_next_path(raw: str | None) -> str:
    value = raw or "/admin"
    if not value.startswith("/") or value.startswith("//") or "\\" in value or "\r" in value or "\n" in value:
        return "/admin"
    return value


def safe_filename(filename: str) -> str:
    return safe_display_filename(filename)


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
            local_path, local_name, mime_type, validated_size = save_validated_bytes(
                content, local_name, mime_type
            )
            if storage.configured():
                _object_path, media_url = storage.upload_file(local_path, local_name, mime_type)
                local_path.unlink(missing_ok=True)
            else:
                media_url = storage.admin_media_url(f"local/{local_path.name}", filename)
            media_size = fetched_size or validated_size
        except Exception as exc:
            return {
                "media_mime": mime_type,
                "media_name": filename,
                "media_provider_id": media_id,
                "raw_payload": {
                    "media_error": str(exc),
                    "message_id": str(message.get("id") or ""),
                    "message_type": message_type,
                },
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
    latest_posts, _total = db.list_published_blog_posts(limit=3)
    context = template_context(
        request,
        "home",
        "Leonilda Bob | Bob Advogados",
        "Advocacia trabalhista e atuação institucional em Mogi das Cruzes, com atendimento presencial e online.",
    )
    context["latest_posts"] = [prepare_blog_post(post) for post in latest_posts]
    return templates.TemplateResponse(
        request,
        "index.html",
        context=context,
    )


@app.get("/bpc-loas-negado", response_class=HTMLResponse)
def landing_bpc(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "landing_bpc.html",
        context=template_context(
            request,
            "landing-bpc",
            "BPC/LOAS negado? Triagem gratuita | Bob Advogados",
            "Análise técnica gratuita da carta de indeferimento do BPC/LOAS, com orientação informativa e atendimento digital seguro em todo o Brasil.",
        ),
    )


@app.get("/sobre", response_class=HTMLResponse)
def sobre(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "sobre.html",
        context=template_context(
            request,
            "sobre",
            "Leonilda Bob | Bob Advogados",
            "Conheça a trajetória profissional e acadêmica de Leonilda Bob, OAB/SP 85.766.",
        ),
    )


@app.get("/atuacao", response_class=HTMLResponse)
def atuacao(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "atuacao.html",
        context=template_context(
            request,
            "atuacao",
            "Atuação jurídica | Bob Advogados",
            "Atendimento em Direito Previdenciário, Consumidor, Família, Trabalhista, Imobiliário e Direito do Idoso.",
        ),
    )


@app.get("/instituto", response_class=HTMLResponse)
def instituto(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "instituto.html",
        context=template_context(
            request,
            "instituto",
            "Instituto Leonilda Bob",
            "Iniciativa em constituição para apoiar bacharéis em Direito na preparação para o Exame da OAB.",
        ),
    )


@app.get("/blog", response_class=HTMLResponse)
def blog(
    request: Request,
    busca: str = Query("", max_length=100),
    tema: str = Query("", max_length=80),
    pagina: int = Query(1, ge=1, le=10_000),
) -> HTMLResponse:
    offset = (pagina - 1) * BLOG_PAGE_SIZE
    posts, total = db.list_published_blog_posts(
        limit=BLOG_PAGE_SIZE,
        offset=offset,
        query=busca,
        category=tema,
    )
    total_pages = max(1, (total + BLOG_PAGE_SIZE - 1) // BLOG_PAGE_SIZE)
    if pagina > total_pages and total:
        raise HTTPException(status_code=404, detail="Página não encontrada.")
    context = template_context(
        request,
        "blog",
        "Artigos de Leonilda Bob | Bob Advogados",
        "Reflexões e orientações de Leonilda Bob sobre Direito, trabalho, formação e cidadania.",
    )
    context.update(
        {
            "posts": [prepare_blog_post(post) for post in posts],
            "categories": db.list_published_blog_categories(),
            "selected_category": tema,
            "search_query": busca,
            "current_page": pagina,
            "total_pages": total_pages,
            "total_posts": total,
        }
    )
    return templates.TemplateResponse(request, "blog.html", context=context)


@app.get("/blog/{slug}", response_class=HTMLResponse)
def blog_post(slug: str, request: Request) -> HTMLResponse:
    if not re.fullmatch(r"[a-z0-9-]{1,140}", slug):
        raise HTTPException(status_code=404, detail="Artigo não encontrado.")
    raw_post = db.get_published_blog_post(slug)
    if not raw_post:
        raise HTTPException(status_code=404, detail="Artigo não encontrado.")
    post = prepare_blog_post(raw_post)
    related, _total = db.list_published_blog_posts(limit=4, category=post["category"])
    context = template_context(
        request,
        "blog-post",
        f"{post['title']} | Leonilda Bob",
        post["excerpt"],
    )
    context.update(
        {
            "post": post,
            "related_posts": [
                prepare_blog_post(item) for item in related if item["id"] != post["id"]
            ][:3],
            "og_type": "article",
            "structured_data": {
                "@context": "https://schema.org",
                "@type": "BlogPosting",
                "headline": post["title"],
                "description": post["excerpt"],
                "datePublished": post["published_at"],
                "dateModified": post["updated_at"],
                "author": {"@type": "Person", "name": post["author_name"]},
                "publisher": {"@type": "Organization", "name": "Bob Advogados"},
                "mainEntityOfPage": context["canonical_url"],
            },
        }
    )
    return templates.TemplateResponse(request, "blog_post.html", context=context)


@app.get("/contato", response_class=HTMLResponse)
def contato(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "contato.html",
        context=template_context(
            request,
            "contato",
            "Contato | Bob Advogados",
            "Fale com o atendimento do Bob Advogados por formulário, WhatsApp, online ou presencialmente.",
        ),
    )


@app.get("/politica-de-privacidade", response_class=HTMLResponse)
def privacy(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "privacidade.html",
        context=template_context(
            request,
            "privacy",
            "Política de privacidade | Bob Advogados",
            "Saiba como o Bob Advogados trata os dados enviados pelo site.",
        ),
    )


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login(request: Request, next: str = "/admin", error: str = "") -> HTMLResponse:
    login_csrf = new_login_csrf_token()
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
            "login_csrf": login_csrf,
        }
    )
    response = templates.TemplateResponse(request, "admin_login.html", context=context)
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        login_csrf,
        max_age=600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/admin/login",
    )
    return response


@app.post("/admin/login")
def admin_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_path: str = Form("/admin"),
    login_csrf: str = Form(...),
):
    rate_limit(request, "admin-login", limit=5, window_seconds=900)
    verify_same_origin(request)
    verify_login_csrf(request, login_csrf)
    user = authenticate(username, password)
    db.record_auth_event(
        username=(username or "").strip(),
        success=bool(user),
        event_type="login",
        ip_hash_value=db.hash_ip(client_ip(request)),
        user_agent=request.headers.get("user-agent", ""),
    )
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
                "login_csrf": login_csrf,
            }
        )
        return templates.TemplateResponse(
            request,
            "admin_login.html",
            context=context,
            status_code=401,
        )
    response = build_login_response(user.username, safe_next_path(next_path))
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/admin/login")
    return response


@app.post("/admin/logout")
def admin_logout(request: Request, csrf_token: str = Form(...)):
    user = require_admin(request)
    verify_same_origin(request)
    verify_session_csrf(request, csrf_token)
    db.record_auth_event(
        username=user.username,
        success=True,
        event_type="logout",
        ip_hash_value=db.hash_ip(client_ip(request)),
        user_agent=request.headers.get("user-agent", ""),
    )
    return build_logout_response(request.cookies.get("bob_admin_session"))


def process_lead(payload: LeadPayload, request: Request, request_key: str | None = None) -> dict[str, Any]:
    rate_limit(request, "lead", limit=6, window_seconds=3600)
    if not payload.consent:
        raise HTTPException(status_code=400, detail="Confirme a autorização de contato para continuar.")
    if payload.form_started_at and int(time.time()) - payload.form_started_at < 2:
        raise HTTPException(status_code=400, detail="Envio rápido demais. Revise os dados e tente novamente.")
    try:
        phone = normalize_phone(payload.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    lead_data = payload.model_dump()
    lead_data["area"] = payload.area or "outro"
    clean_request_key = (request_key or "").strip()[:120] or None
    lead_data.update(
        {
            "phone": phone,
            "ip_hash": db.hash_ip(client_ip(request)),
            "user_agent": request.headers.get("user-agent", "")[:500],
            "request_key": clean_request_key,
        }
    )
    message = first_contact_message(payload.kind, payload.name, payload.message)
    try:
        lead_id, outbox_id, conversation_id, created = db.create_lead_bundle(lead_data, message)
    except Exception:
        existing = db.get_lead_by_request_key(clean_request_key)
        if not existing:
            raise
        created = False
        outbox_id = 0
        conversation_id = 0
    if not created:
        return {
            "ok": True,
            "duplicate": True,
            "whatsapp": "already_received",
            "message": "Este contato já foi recebido. Não enviaremos uma mensagem duplicada.",
        }
    dispatch_status = "queued"
    if settings.whatsapp_dry_run:
        try:
            send_result = send_whatsapp_text(phone, message, conversation_id=conversation_id, first_contact=True)
            dispatch_status = send_result["status"]
            db.mark_outbox_sent(
                outbox_id,
                status=dispatch_status,
                provider_message_id=send_result.get("provider_message_id"),
            )
        except Exception as exc:  # pragma: no cover - integration/runtime path
            db.mark_outbox_failed(outbox_id, str(exc))
    return {
        "ok": True,
        "whatsapp": dispatch_status,
        "message": "Recebemos seu contato. O atendimento continuará pelo WhatsApp.",
    }


@app.post("/api/leads")
def create_lead(
    payload: LeadPayload,
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
) -> JSONResponse:
    return JSONResponse(process_lead(payload, request, idempotency_key))


@app.post("/contato/enviar", response_class=HTMLResponse, include_in_schema=False)
def create_lead_form(
    request: Request,
    kind: str = Form("geral"),
    area: str = Form("outro"),
    name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(""),
    message: str = Form(...),
    consent: Optional[str] = Form(None),
    website: str = Form(""),
    form_started_at: Optional[int] = Form(None),
    request_key: str = Form(""),
) -> HTMLResponse:
    try:
        payload = LeadPayload(
            kind=kind,
            area=area,
            name=name,
            phone=phone,
            email=email or None,
            message=message,
            consent=consent == "on",
            website=website,
            form_started_at=form_started_at,
            source_path=(urlparse(request.headers.get("referer", "")).path or "/contato")[:300],
        )
        result = process_lead(payload, request, request_key)
    except (ValidationError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else "Revise os campos obrigatórios."
        status_code = exc.status_code if isinstance(exc, HTTPException) else 422
        headers = exc.headers if isinstance(exc, HTTPException) else None
        context = template_context(
            request,
            "form-error",
            "Revise o contato | Bob Advogados",
            "Alguns dados do contato precisam ser corrigidos.",
        )
        context["error_message"] = str(detail)
        return templates.TemplateResponse(
            request,
            "form_error.html",
            context=context,
            status_code=status_code,
            headers=headers,
        )
    context = template_context(
        request,
        "obrigado",
        "Contato recebido | Bob Advogados",
        "Seu contato foi recebido pelo Bob Advogados.",
    )
    context["result_message"] = result["message"]
    return templates.TemplateResponse(request, "obrigado.html", context=context)


@app.post("/api/track")
def track(payload: TrackPayload, request: Request) -> JSONResponse:
    if (
        request.cookies.get("bob_analytics_consent") != "accepted"
        or request.headers.get("sec-gpc") == "1"
        or request.headers.get("dnt") == "1"
    ):
        return JSONResponse({"ok": True, "recorded": False})
    rate_limit(request, "track", limit=120, window_seconds=60)
    db.record_page_view(
        path=payload.path,
        referrer=payload.referrer,
        ip_hash_value=db.hash_ip(client_ip(request)),
        user_agent=request.headers.get("user-agent", ""),
        origin=payload.model_dump(),
    )
    return JSONResponse({"ok": True, "recorded": True})


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
    return templates.TemplateResponse(request, "admin.html", context=context)


def blog_editor_context(
    request: Request,
    user: Any,
    post: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    context = template_context(
        request,
        "admin-blog-editor",
        "Escrever artigo | Leonilda Bob",
        "Editor simples de artigos.",
    )
    context.update(
        {
            "admin_user": user,
            "post": post
            or {
                "id": None,
                "title": "",
                "excerpt": "",
                "body": "",
                "category": BLOG_CATEGORIES[0],
                "status": "draft",
            },
            "blog_categories": BLOG_CATEGORIES,
            "error": error,
        }
    )
    return context


@app.get("/admin/artigos", response_class=HTMLResponse)
def admin_blog(request: Request, salvo: str = "") -> Response:
    user = current_admin(request)
    if not user:
        return RedirectResponse("/admin/login?next=/admin/artigos", status_code=303)
    notices = {
        "rascunho": "O artigo foi guardado. Você pode continuar quando quiser.",
        "publicado": "O artigo foi publicado e já está disponível no site.",
        "atualizado": "As alterações foram salvas.",
        "retirado": "O artigo foi retirado do site e ficou guardado no painel.",
    }
    context = template_context(
        request,
        "admin-blog",
        "Meus artigos | Leonilda Bob",
        "Painel de artigos de Leonilda Bob.",
    )
    context.update(
        {
            "admin_user": user,
            "posts": db.list_blog_posts_admin(),
            "totals": db.blog_admin_totals(),
            "notice": notices.get(salvo, ""),
        }
    )
    return templates.TemplateResponse(request, "admin_blog.html", context=context)


@app.get("/admin/artigos/novo", response_class=HTMLResponse)
def admin_blog_new(request: Request) -> Response:
    user = current_admin(request)
    if not user:
        return RedirectResponse("/admin/login?next=/admin/artigos/novo", status_code=303)
    return templates.TemplateResponse(
        request,
        "admin_blog_editor.html",
        context=blog_editor_context(request, user),
    )


@app.get("/admin/artigos/{post_id}/editar", response_class=HTMLResponse)
def admin_blog_edit(post_id: int, request: Request) -> Response:
    user = current_admin(request)
    if not user:
        return RedirectResponse(
            f"/admin/login?next=/admin/artigos/{post_id}/editar", status_code=303
        )
    post = db.get_blog_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Artigo não encontrado.")
    return templates.TemplateResponse(
        request,
        "admin_blog_editor.html",
        context=blog_editor_context(request, user, post),
    )


@app.get("/admin/artigos/{post_id}/previa", response_class=HTMLResponse)
def admin_blog_preview(post_id: int, request: Request) -> Response:
    user = current_admin(request)
    if not user:
        return RedirectResponse(
            f"/admin/login?next=/admin/artigos/{post_id}/previa", status_code=303
        )
    raw_post = db.get_blog_post_by_id(post_id)
    if not raw_post:
        raise HTTPException(status_code=404, detail="Artigo não encontrado.")
    post = prepare_blog_post(raw_post)
    context = template_context(
        request,
        "blog-preview",
        f"Prévia: {post['title']}",
        post["excerpt"],
    )
    context.update(
        {
            "admin_user": user,
            "post": post,
            "related_posts": [],
            "preview_mode": True,
        }
    )
    return templates.TemplateResponse(request, "blog_post.html", context=context)


@app.post("/admin/artigos/salvar")
def admin_blog_save(
    request: Request,
    csrf_token: str = Form(...),
    post_id: str = Form(""),
    title: str = Form(...),
    excerpt: str = Form(...),
    body: str = Form(...),
    category: str = Form(...),
    action: str = Form(...),
) -> Response:
    user = require_admin(request)
    verify_same_origin(request)
    verify_session_csrf(request, csrf_token)
    rate_limit(request, "admin-blog-save", limit=30, window_seconds=3600)
    clean_title = " ".join(title.split())
    clean_excerpt = " ".join(excerpt.split())
    clean_body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    clean_category = category.strip()
    parsed_id: int | None = None
    if post_id:
        if not post_id.isdigit():
            raise HTTPException(status_code=400, detail="Identificação do artigo inválida.")
        parsed_id = int(post_id)
    submitted = {
        "id": parsed_id,
        "title": clean_title,
        "excerpt": clean_excerpt,
        "body": clean_body,
        "category": clean_category,
        "status": "published" if action == "publish" else "draft",
    }
    errors: list[str] = []
    if not 5 <= len(clean_title) <= 160:
        errors.append("o título deve ter entre 5 e 160 caracteres")
    if not 20 <= len(clean_excerpt) <= 360:
        errors.append("a pequena apresentação deve ter entre 20 e 360 caracteres")
    if not 80 <= len(clean_body) <= 50_000:
        errors.append("o texto deve ter entre 80 e 50.000 caracteres")
    if clean_category not in BLOG_CATEGORIES:
        errors.append("escolha um dos temas disponíveis")
    if action not in {"draft", "publish"}:
        errors.append("escolha guardar ou publicar")
    if errors:
        context = blog_editor_context(request, user, submitted, "; ".join(errors) + ".")
        return templates.TemplateResponse(
            request,
            "admin_blog_editor.html",
            context=context,
            status_code=422,
        )
    try:
        existing = db.get_blog_post_by_id(parsed_id) if parsed_id else None
        db.save_blog_post(
            post_id=parsed_id,
            title=clean_title,
            excerpt=clean_excerpt,
            body=clean_body,
            category=clean_category,
            status=submitted["status"],
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if action == "draft":
        notice = "rascunho"
    elif existing and existing["status"] == "published":
        notice = "atualizado"
    else:
        notice = "publicado"
    return RedirectResponse(f"/admin/artigos?salvo={notice}", status_code=303)


@app.post("/admin/artigos/{post_id}/retirar")
def admin_blog_unpublish(
    post_id: int,
    request: Request,
    csrf_token: str = Form(...),
) -> Response:
    require_admin(request)
    verify_same_origin(request)
    verify_session_csrf(request, csrf_token)
    if not db.unpublish_blog_post(post_id):
        raise HTTPException(status_code=404, detail="Artigo não encontrado.")
    return RedirectResponse("/admin/artigos?salvo=retirado", status_code=303)


@app.get("/api/admin/metrics")
def metrics(request: Request) -> JSONResponse:
    require_admin(request)
    return JSONResponse(db.admin_snapshot())


@app.get("/api/admin/conversations")
def admin_conversations(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: str = Query("", max_length=80),
) -> JSONResponse:
    require_admin(request)
    conversations = db.list_conversations(limit=limit, offset=offset, query=q)
    return JSONResponse(
        {
            "ok": True,
            "conversations": conversations,
            "next_offset": offset + len(conversations),
            "has_more": len(conversations) == limit,
        }
    )


@app.get("/api/admin/conversations/{conversation_id}/messages")
def admin_conversation_messages(conversation_id: int, request: Request) -> JSONResponse:
    require_admin(request)
    conversation = db.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa não encontrada.")
    return JSONResponse({"ok": True, "conversation": conversation, "messages": db.list_messages(conversation_id)})


@app.get("/api/admin/media")
def admin_media(path: str, request: Request, name: str = "arquivo") -> Response:
    require_admin(request)
    filename = safe_filename(name)
    try:
        if path.startswith("local/"):
            local_name = safe_filename(path.removeprefix("local/"))
            local_path = UPLOAD_DIR / local_name
            if not local_path.is_file():
                raise FileNotFoundError(local_name)
            content = local_path.read_bytes()
            mime_type = mimetypes.guess_type(filename or local_name)[0] or "application/octet-stream"
        else:
            content, mime_type = storage.download(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    disposition = "inline" if mime_type.startswith(("image/", "audio/", "video/")) else "attachment"
    return Response(
        content,
        media_type=mime_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Cache-Control": "private, max-age=300",
        },
    )


@app.post("/api/admin/conversations/{conversation_id}/messages")
def admin_send_message(
    conversation_id: int,
    request: Request,
    _admin: Any = Depends(require_admin),
    text: str = Form(""),
    attachment: Optional[UploadFile] = File(None),
) -> JSONResponse:
    verify_same_origin(request)
    verify_session_csrf(request)
    rate_limit(request, "admin-message", limit=60, window_seconds=60)
    conversation = db.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa não encontrada.")
    clean_text = (text or "").strip()
    if len(clean_text) > 4096:
        raise HTTPException(status_code=400, detail="Mensagem muito longa. Use até 4.096 caracteres.")
    if attachment and attachment.filename and len(clean_text) > 1024:
        raise HTTPException(status_code=400, detail="Legenda muito longa. Use até 1.024 caracteres com arquivos.")
    if not clean_text and not attachment:
        raise HTTPException(status_code=400, detail="Escreva uma mensagem ou selecione um arquivo.")
    saved_path: Optional[Path] = None
    remote_object_path: Optional[str] = None
    try:
        if attachment and attachment.filename:
            path, filename, mime_type, _size = save_validated_upload(attachment)
            saved_path = path
            if storage.configured():
                remote_object_path, public_url = storage.upload_file(path, filename, mime_type)
            else:
                public_url = storage.admin_media_url(f"local/{path.name}", filename)
            result = send_whatsapp_media(
                conversation["phone"],
                path,
                mime_type=mime_type,
                filename=filename,
                caption=clean_text,
                conversation_id=conversation_id,
                public_url=public_url,
            )
            if storage.configured():
                path.unlink(missing_ok=True)
        else:
            result = send_whatsapp_text(conversation["phone"], clean_text, conversation_id=conversation_id)
        return JSONResponse({"ok": True, "status": result.get("status", "sent")})
    except HTTPException:
        if saved_path:
            saved_path.unlink(missing_ok=True)
        if remote_object_path:
            try:
                storage.delete(remote_object_path)
            except Exception:
                logger.exception(
                    json.dumps(
                        {"event": "storage.orphan_cleanup_failed", "object_path": remote_object_path}
                    )
                )
        raise
    except Exception as exc:
        if saved_path:
            saved_path.unlink(missing_ok=True)
        if remote_object_path:
            try:
                storage.delete(remote_object_path)
            except Exception:
                logger.exception(
                    json.dumps(
                        {"event": "storage.orphan_cleanup_failed", "object_path": remote_object_path}
                    )
                )
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


@app.post("/api/admin/conversations/{conversation_id}/controls")
def admin_conversation_controls(
    conversation_id: int,
    payload: ConversationControlPayload,
    request: Request,
) -> JSONResponse:
    require_admin(request)
    verify_same_origin(request)
    verify_session_csrf(request)
    updated = db.update_conversation_controls(
        conversation_id,
        status=payload.status,
        bot_enabled=payload.bot_enabled,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Conversa não encontrada.")
    return JSONResponse({"ok": True, "conversation": updated})


@app.post("/api/admin/leads/{lead_id}/status")
def admin_lead_status(lead_id: int, payload: LeadStatusPayload, request: Request) -> JSONResponse:
    require_admin(request)
    verify_same_origin(request)
    verify_session_csrf(request)
    if not db.update_lead_status(lead_id, payload.status):
        raise HTTPException(status_code=404, detail="Contato não encontrado.")
    return JSONResponse({"ok": True, "status": payload.status})


@app.post("/api/admin/privacy/delete")
def admin_privacy_delete(payload: PrivacyDeletePayload, request: Request) -> JSONResponse:
    require_admin(request)
    verify_same_origin(request)
    verify_session_csrf(request)
    try:
        phone = normalize_phone(payload.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not delete_private_media(db.contact_media_urls(phone)):
        raise HTTPException(status_code=503, detail="Não foi possível excluir todos os arquivos. Tente novamente.")
    deleted = db.delete_contact_data(phone)
    return JSONResponse({"ok": True, "deleted": deleted})


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
    verify_same_origin(request)
    verify_session_csrf(request)
    item = db.get_outbox_message(outbox_id)
    if not item:
        raise HTTPException(status_code=404, detail="Mensagem não encontrada.")
    if item["status"] not in {"failed", "dry_run"}:
        raise HTTPException(
            status_code=409,
            detail="Este envio já foi concluído ou está aguardando a fila automática.",
        )
    try:
        conversation_id = db.get_or_create_conversation(
            item["recipient"], source_lead_id=item.get("lead_id")
        )
        send_result = send_whatsapp_text(
            item["recipient"], item["message"], conversation_id=conversation_id, first_contact=True
        )
        dispatch_status = send_result["status"]
        db.mark_outbox_sent(outbox_id, status=dispatch_status, provider_message_id=send_result.get("provider_message_id"))
        return JSONResponse({"ok": True, "status": dispatch_status})
    except Exception as exc:  # pragma: no cover - integration/runtime path
        db.mark_outbox_failed(outbox_id, str(exc))
        return JSONResponse({"ok": False, "status": "failed", "error": str(exc)}, status_code=502)


@app.get("/api/webhooks/whatsapp")
def verify_whatsapp_webhook(request: Request) -> PlainTextResponse:
    rate_limit(request, "webhook-verify", limit=30, window_seconds=300)
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if (
        mode == "subscribe"
        and token
        and settings.whatsapp_verify_token
        and secrets.compare_digest(token, settings.whatsapp_verify_token)
    ):
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


def process_whatsapp_payload(payload: dict[str, Any]) -> None:
    entries = payload.get("entry") or []
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes") or []
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata") or {}
            received_number_id = str(metadata.get("phone_number_id") or "") if isinstance(metadata, dict) else ""
            if (
                settings.whatsapp_phone_number_id
                and received_number_id != settings.whatsapp_phone_number_id
            ):
                continue
            statuses = value.get("statuses") or []
            if not isinstance(statuses, list):
                statuses = []
            for status in statuses:
                if not isinstance(status, dict):
                    continue
                provider_status = str(status.get("status") or "")
                if provider_status in {"sent", "delivered", "read", "failed", "deleted"}:
                    db.update_outbox_provider_status(
                        str(status.get("id") or ""), provider_status
                    )
            contacts = value.get("contacts") or []
            contact_name = ""
            if isinstance(contacts, list) and contacts and isinstance(contacts[0], dict):
                profile = contacts[0].get("profile") or {}
                if isinstance(profile, dict):
                    contact_name = str(profile.get("name") or "")[:120]
            messages = value.get("messages") or []
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict):
                    continue
                provider_message_id = str(message.get("id") or "")
                if provider_message_id and db.whatsapp_message_exists(provider_message_id):
                    continue
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
                raw_payload = {
                    "id": provider_message_id,
                    "type": message_type,
                    "timestamp": str(message.get("timestamp") or ""),
                    "context": message.get("context") if isinstance(message.get("context"), dict) else None,
                }
                if media_payload.get("raw_payload"):
                    raw_payload = media_payload.pop("raw_payload")
                db.record_whatsapp_message(
                    conversation_id,
                    direction="in",
                    text=text,
                    message_type=message_type,
                    provider_message_id=provider_message_id,
                    status="received",
                    raw_payload=raw_payload,
                    **media_payload,
                )
                if (
                    settings.whatsapp_auto_reply
                    and text
                    and db.can_auto_reply(
                        conversation_id, settings.whatsapp_auto_reply_cooldown_seconds
                    )
                ):
                    reply = auto_reply_for_inbound(text)
                    try:
                        send_whatsapp_text(phone, reply, conversation_id=conversation_id)
                        db.mark_auto_reply(conversation_id)
                    except Exception as exc:  # pragma: no cover - integration/runtime path
                        db.record_whatsapp_message(
                            conversation_id,
                            direction="out",
                            text=reply,
                            status="failed",
                            raw_payload={"error": str(exc)},
                        )


@app.post("/api/webhooks/whatsapp")
async def receive_whatsapp_webhook(request: Request) -> JSONResponse:
    rate_limit(request, "webhook-receive", limit=300, window_seconds=60)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Use conteúdo JSON.")
    raw_body = await request.body()
    verify_whatsapp_signature(raw_body, request.headers.get("x-hub-signature-256", ""))
    try:
        payload_text = raw_body.decode("utf-8")
        payload = json.loads(payload_text or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Conteúdo inválido.") from exc
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        raise HTTPException(status_code=400, detail="Evento de webhook inválido.")
    _event_id, created = db.enqueue_whatsapp_webhook(raw_body)
    return JSONResponse({"ok": True, "accepted": True, "duplicate": not created})
