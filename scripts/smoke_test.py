from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path


os.environ.update(
    {
        "APP_ENV": "test",
        "APP_BASE_URL": "http://testserver",
        "ALLOWED_HOSTS": "testserver,localhost,127.0.0.1",
        "COOKIE_SECURE": "0",
        "DATABASE_URL": "",
        "LEONILDA_DB_PATH": "tmp/smoke.sqlite",
        "PRIVATE_UPLOAD_DIR": "tmp/uploads/whatsapp",
        "SUPABASE_URL": "",
        "SUPABASE_SECRET_KEY": "",
        "DOCS_ENABLED": "0",
        "MAX_UPLOAD_BYTES": "26214400",
        "REQUIRE_VIRUS_SCAN": "0",
        "METRICS_SALT": "smoke-metrics-salt",
        "WHATSAPP_DRY_RUN": "1",
        "WHATSAPP_QR_BRIDGE_TOKEN": "smoke-qr-bridge-token",
        "WHATSAPP_APP_SECRET": "smoke-app-secret",
        "WHATSAPP_VERIFY_TOKEN": "smoke-verify-token",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "smoke-password",
        "ADMIN_PASSWORD_HASH": "",
        "ADMIN_SESSION_SECRET": "smoke-session-secret",
    }
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app import whatsapp_evolution  # noqa: E402
from app.main import app, dispatch_pending_webhooks, is_whatsapp_opt_out_request, templates as jinja_templates  # noqa: E402
from app.settings import Settings, env_bool, settings  # noqa: E402
from app.whatsapp import (  # noqa: E402
    auto_reply_for_inbound,
    auto_triage_should_handoff,
    trusted_meta_media_url,
)
from app.whatsapp_evolution import evolution_inbound_messages, evolution_session_action  # noqa: E402


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def hidden_value(html: str, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}" value="([^"]+)"', html)
    if not match:
        raise AssertionError(f"Campo oculto ausente: {name}")
    return match.group(1)


def meta_value(html: str, name: str) -> str:
    match = re.search(rf'<meta name="{re.escape(name)}" content="([^"]+)"', html)
    if not match:
        raise AssertionError(f"Meta ausente: {name}")
    return match.group(1)


class FakeEvolutionResponse:
    def __init__(self, data: dict[str, object], status_code: int = 200) -> None:
        self.status_code = status_code
        self._data = data
        self.content = json.dumps(data).encode("utf-8")
        self.text = self.content.decode("utf-8")

    def json(self) -> dict[str, object]:
        return self._data


class FakeEvolutionHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.connection_state_requests = 0

    def request(self, method: str, url: str, **_kwargs: object) -> FakeEvolutionResponse:
        self.calls.append((method, url))
        if "/instance/connectionState/" in url:
            self.connection_state_requests += 1
            if self.connection_state_requests == 1:
                return FakeEvolutionResponse({"error": True, "message": "Instance not found"})
            return FakeEvolutionResponse({"instance": {"state": "close"}})
        if "/instance/create" in url:
            return FakeEvolutionResponse({"instance": {"instanceName": "bob-advogados"}})
        if "/webhook/set/" in url:
            return FakeEvolutionResponse({"webhook": {"enabled": True}})
        if "/instance/connect/" in url:
            return FakeEvolutionResponse({"base64": "A" * 300})
        raise AssertionError(f"Rota Evolution inesperada: {method} {url}")


class MarkupAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: Counter[str] = Counter()
        self.ids: list[str] = []
        self.images: list[dict[str, str]] = []
        self.forms: list[dict[str, str]] = []
        self.blank_links: list[dict[str, str]] = []
        self.buttons: list[dict[str, str]] = []
        self.controls: list[dict[str, str]] = []
        self.label_targets: set[str] = set()
        self.html_language = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        self.tags[tag] += 1
        if attributes.get("id"):
            self.ids.append(attributes["id"])
        if tag == "html":
            self.html_language = attributes.get("lang", "")
        if tag == "img":
            self.images.append(attributes)
        elif tag == "form":
            self.forms.append(attributes)
        elif tag == "a" and attributes.get("target") == "_blank":
            self.blank_links.append(attributes)
        elif tag == "button":
            self.buttons.append(attributes)
        elif tag in {"input", "select", "textarea"} and attributes.get("type") != "hidden":
            self.controls.append(attributes)
        if tag == "label" and attributes.get("for"):
            self.label_targets.add(attributes["for"])


def audit_public_markup(path: str, html: str) -> None:
    audit = MarkupAudit()
    audit.feed(html)
    expect(audit.tags["main"] == 1, f"{path}: quantidade de main")
    expect(audit.tags["h1"] == 1, f"{path}: quantidade de h1")
    expect(audit.html_language == "pt-BR", f"{path}: idioma do documento")
    expect(len(audit.ids) == len(set(audit.ids)), f"{path}: IDs duplicados")
    for image in audit.images:
        expect("alt" in image, f"{path}: imagem sem alt")
        expect(bool(image.get("width") and image.get("height")), f"{path}: imagem sem dimensões")
    for form in audit.forms:
        expected_method = "get" if form.get("role") == "search" else "post"
        expect(form.get("method", "").lower() == expected_method, f"{path}: método de formulário inválido")
        expect(form.get("action", "").startswith("/"), f"{path}: formulário sem ação local")
    for link in audit.blank_links:
        expect("noopener" in link.get("rel", "").split(), f"{path}: nova aba sem noopener")
    for button in audit.buttons:
        expect(bool(button.get("type")), f"{path}: botão sem tipo explícito")
    for control in audit.controls:
        expect(bool(control.get("id")), f"{path}: campo sem ID")
        expect(control["id"] in audit.label_targets, f"{path}: campo sem label associado")


def main() -> None:
    smoke_db = Path("tmp/smoke.sqlite")
    upload_dir = Path("tmp/uploads/whatsapp")
    smoke_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(smoke_db) + suffix).unlink(missing_ok=True)
    shutil.rmtree(upload_dir, ignore_errors=True)

    os.environ["SMOKE_INVALID_BOOL"] = "tru"
    try:
        env_bool("SMOKE_INVALID_BOOL", False)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Booleano inválido aceito silenciosamente")
    finally:
        os.environ.pop("SMOKE_INVALID_BOOL", None)
    private_network_keys = {
        key: os.environ.get(key)
        for key in (
            "WHATSAPP_PROVIDER",
            "WHATSAPP_DRY_RUN",
            "EVOLUTION_API_URL",
            "EVOLUTION_API_INTERNAL_HOSTPORT",
            "EVOLUTION_API_KEY",
            "EVOLUTION_WEBHOOK_TOKEN",
        )
    }
    try:
        os.environ.update(
            {
                "WHATSAPP_PROVIDER": "evolution",
                "WHATSAPP_DRY_RUN": "0",
                "EVOLUTION_API_URL": "https://evolution-public.example",
                "EVOLUTION_API_INTERNAL_HOSTPORT": "evolution-private:10000",
                "EVOLUTION_API_KEY": "smoke-evolution-key",
                "EVOLUTION_WEBHOOK_TOKEN": "smoke-evolution-webhook-token",
            }
        )
        private_settings = Settings()
        expect(
            private_settings.evolution_api_url == "http://evolution-private:10000",
            "Evolution não priorizou a rede privada do Render",
        )
        expect(
            private_settings.evolution_api_uses_private_network,
            "Evolution não identificou a rede privada do Render",
        )
    finally:
        for key, value in private_network_keys.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    expect(
        trusted_meta_media_url("https://lookaside.fbsbx.com/whatsapp_business/attachments?id=1"),
        "Host oficial de mídia rejeitado",
    )
    expect(
        not trusted_meta_media_url("https://evil.example/media?id=1"),
        "Host externo de mídia aceito",
    )
    evolution_messages = evolution_inbound_messages(
        {
            "event": "messages.upsert",
            "data": {
                "key": {"id": "evolution-smoke-1", "remoteJid": "5511999999999@s.whatsapp.net"},
                "pushName": "Contato teste",
                "message": {"extendedTextMessage": {"text": "Olá pelo Evolution"}},
            },
        }
    )
    expect(len(evolution_messages) == 1, "Evento Evolution válido não foi interpretado")
    expect(evolution_messages[0]["phone"] == "5511999999999", "Telefone Evolution inválido")
    expect(evolution_messages[0]["text"] == "Olá pelo Evolution", "Texto Evolution inválido")
    tiny_png = base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"evolution-media-smoke"
    ).decode("ascii")
    evolution_media_messages = evolution_inbound_messages(
        {
            "event": "MESSAGES_UPSERT",
            "data": {
                "key": {"id": "evolution-smoke-media", "remoteJid": "5511977776666@s.whatsapp.net"},
                "message": {
                    "imageMessage": {"caption": "Comprovante", "mimetype": "image/png"},
                    "base64": tiny_png,
                },
            },
        }
    )
    expect(len(evolution_media_messages) == 1, "Mídia Evolution não foi interpretada")
    expect(evolution_media_messages[0]["message_type"] == "image", "Tipo de mídia Evolution inválido")
    expect(evolution_media_messages[0]["media_mime"] == "image/png", "MIME Evolution inválido")
    expect(evolution_media_messages[0]["media_base64"] == tiny_png, "Base64 Evolution ausente")
    evolution_lid_messages = evolution_inbound_messages(
        {
            "event": "messages.upsert",
            "data": {
                "key": {
                    "id": "evolution-smoke-lid",
                    "remoteJid": "123456789@lid",
                    "remoteJidAlt": "5511988887777@s.whatsapp.net",
                },
                "pushName": "Contato LID",
                "message": {"conversation": "Mensagem com número alternativo"},
            },
        }
    )
    expect(len(evolution_lid_messages) == 1, "Evento Evolution com LID não foi interpretado")
    expect(evolution_lid_messages[0]["phone"] == "5511988887777", "LID não foi convertido para telefone")
    expect(
        not evolution_inbound_messages(
            {
                "event": "messages.upsert",
                "data": {
                    "key": {"id": "evolution-smoke-lid-only", "remoteJid": "123456789@lid"},
                    "message": {"conversation": "LID sem telefone"},
                },
            }
        ),
        "LID sem número alternativo entrou no inbox",
    )
    expect(
        not evolution_inbound_messages(
            {
                "event": "messages.upsert",
                "data": {
                    "key": {"id": "evolution-smoke-2", "remoteJid": "5511999999999@s.whatsapp.net", "fromMe": True},
                    "message": {"conversation": "Mensagem própria"},
                },
            }
        ),
        "Mensagem própria da Evolution entrou no inbox",
    )
    expect(is_whatsapp_opt_out_request(" PARAR "), "Gatilho de opt-out não reconhecido")
    expect(is_whatsapp_opt_out_request("remover"), "Variação de opt-out não reconhecida")
    expect(not is_whatsapp_opt_out_request("quero parar de trabalhar"), "Texto comum bloqueado como opt-out")
    evolution_values = {
        key: getattr(settings, key)
        for key in (
            "whatsapp_provider",
            "whatsapp_dry_run",
            "evolution_api_url",
            "evolution_api_key",
            "evolution_webhook_token",
        )
    }
    original_evolution_http = whatsapp_evolution.HTTP
    fake_evolution_http = FakeEvolutionHttp()
    try:
        settings.whatsapp_provider = "evolution"
        settings.whatsapp_dry_run = False
        settings.evolution_api_url = "https://evolution.test"
        settings.evolution_api_key = "smoke-evolution-key"
        settings.evolution_webhook_token = "smoke-evolution-webhook-token"
        whatsapp_evolution.HTTP = fake_evolution_http
        evolution_session = evolution_session_action("connect")
        expect(evolution_session["session"]["status"] == "qr", "QR da Evolution não foi disponibilizado")
        expect(
            str(evolution_session["session"]["qr_data_url"]).startswith("data:image/png;base64,"),
            "QR da Evolution sem imagem base64",
        )
        paths = "\n".join(url for _method, url in fake_evolution_http.calls)
        expect("/instance/create" in paths, "Instância Evolution inexistente não foi criada")
        expect("/webhook/set/" in paths, "Webhook Evolution não foi registrado")
        expect("/instance/connect/" in paths, "Conexão Evolution não solicitou QR")
    finally:
        whatsapp_evolution.HTTP = original_evolution_http
        for key, value in evolution_values.items():
            setattr(settings, key, value)

    try:
        for template_path in Path("templates").rglob("*.html"):
            jinja_templates.get_template(template_path.relative_to("templates").as_posix())
        with TestClient(app) as client:
            for path in ["/", "/bpc-loas-negado", "/sobre", "/atuacao", "/instituto", "/blog", "/contato", "/politica-de-privacidade"]:
                response = client.get(path)
                expect(response.status_code == 200, f"{path}: {response.status_code}")
                expect(response.headers.get("x-content-type-options") == "nosniff", f"headers: {path}")
                expect(response.headers.get("cross-origin-opener-policy") == "same-origin", f"COOP: {path}")
                expect('rel="canonical"' in response.text, f"canonical: {path}")
                audit_public_markup(path, response.text)
                if path == "/sobre":
                    expect("Leonilda Bob" in response.text and "85.766" in response.text, "Perfil ausente")
                    expect("Continuidade" not in response.text, "Seção Continuidade voltou para /sobre")
                    expect("Uma história profissional que também atravessa gerações" not in response.text, "Seção de continuidade voltou para /sobre")
                    expect("<h2>Ladislau Bob</h2>" not in response.text, "Perfil complementar recebeu destaque próprio")

            expect(client.get("/robots.txt").status_code == 200, "robots.txt")
            expect(client.get("/sitemap.xml").status_code == 200, "sitemap.xml")
            missing = client.get("/nao-existe")
            expect(missing.status_code == 404, "404")
            audit_public_markup("/nao-existe", missing.text)
            expect(client.get("/readyz").status_code == 200, "readiness")
            suspicious_request_id = client.get("/healthz", headers={"X-Request-ID": "valor invalido!"})
            expect(suspicious_request_id.headers.get("x-request-id") != "valor invalido!", "Request ID não filtrado")
            css_response = client.get("/static/styles.css", headers={"Accept-Encoding": "gzip"})
            expect(css_response.headers.get("content-encoding") == "gzip", "CSS sem compressão")
            avif_response = client.get("/static/assets/hero-law-office.avif", headers={"Accept-Encoding": "gzip"})
            expect(avif_response.headers.get("content-type") == "image/avif", "MIME AVIF")
            expect("content-encoding" not in avif_response.headers, "AVIF recomprimido")
            metrics_without_consent = client.post("/api/track", json={"path": "/"})
            expect(metrics_without_consent.status_code == 200, "Métrica sem consentimento")
            expect(metrics_without_consent.json().get("recorded") is False, "Métrica sem consentimento não grava")
            oversized_lead = client.post(
                "/api/leads",
                content=b"x" * (129 * 1024),
                headers={"Content-Type": "application/json"},
            )
            expect(oversized_lead.status_code == 413, "Corpo excessivo aceito")
            client.cookies.set("bob_analytics_consent", "accepted")
            metrics_with_gpc = client.post("/api/track", json={"path": "/"}, headers={"Sec-GPC": "1"})
            expect(metrics_with_gpc.status_code == 200, "GPC bloqueia métricas sem erro")
            expect(metrics_with_gpc.json().get("recorded") is False, "Métrica respeita sinal global de privacidade")
            expect(client.post("/api/track", json={"path": "/"}).status_code == 200, "Métrica autorizada")
            expect(client.post("/api/track", json={"path": "/bpc-loas-negado"}).status_code == 200, "Métrica BPC")
            expect(client.post("/api/track", json={"path": "/conversion/whatsapp/bpc"}).status_code == 200, "Clique WhatsApp BPC")
            expect(client.post("/api/track", json={"path": "/conversion/lead/bpc"}).status_code == 200, "Conversão BPC")
            original_record_page_view = db.record_page_view
            try:
                def fail_page_view(**_kwargs):
                    raise RuntimeError("falha simulada")

                db.record_page_view = fail_page_view
                logging.disable(logging.CRITICAL)
                internal_error = client.post("/api/track", json={"path": "/erro"})
                expect(internal_error.status_code == 500, "Erro interno não tratado")
                expect(internal_error.json().get("detail") == "Erro interno. Tente novamente mais tarde.", "Erro interno exposto")
                expect(internal_error.headers.get("x-content-type-options") == "nosniff", "Erro sem headers")
            finally:
                logging.disable(logging.NOTSET)
                db.record_page_view = original_record_page_view

            webhook_payload = {"object": "whatsapp_business_account", "entry": []}
            webhook_body = json.dumps(webhook_payload, separators=(",", ":")).encode("utf-8")
            webhook_signature = "sha256=" + hmac.new(
                b"smoke-app-secret", webhook_body, hashlib.sha256
            ).hexdigest()
            invalid_webhook = client.post(
                "/api/webhooks/whatsapp",
                content=webhook_body,
                headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=invalid"},
            )
            expect(invalid_webhook.status_code == 403, "Webhook aceitou assinatura inválida")
            valid_webhook = client.post(
                "/api/webhooks/whatsapp",
                content=webhook_body,
                headers={"Content-Type": "application/json", "X-Hub-Signature-256": webhook_signature},
            )
            expect(valid_webhook.status_code == 200, valid_webhook.text)
            duplicate_webhook = client.post(
                "/api/webhooks/whatsapp",
                content=webhook_body,
                headers={"Content-Type": "application/json", "X-Hub-Signature-256": webhook_signature},
            )
            expect(duplicate_webhook.json().get("duplicate") is True, "Webhook duplicado não detectado")
            dispatch_pending_webhooks()
            with db.connect() as connection:
                webhook_row = connection.execute(
                    "SELECT status, payload FROM whatsapp_webhook_events LIMIT 1"
                ).fetchone()
            expect(webhook_row["status"] == "processed", "Webhook não processado")
            expect(webhook_row["payload"] == "{}", "Payload processado não minimizado")
            verify_webhook = client.get(
                "/api/webhooks/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "smoke-verify-token",
                    "hub.challenge": "12345",
                },
            )
            expect(verify_webhook.status_code == 200 and verify_webhook.text == "12345", "Verificação do webhook")

            admin_redirect = client.get("/admin", follow_redirects=False)
            expect(admin_redirect.status_code == 303, "Admin não redirecionou")
            unauthenticated_upload = client.post(
                "/api/admin/conversations/1/messages",
                files={"attachment": ("teste.txt", b"conteudo", "text/plain")},
            )
            expect(unauthenticated_upload.status_code == 401, "Upload sem autenticação")

            login_page = client.get("/admin/login")
            login_csrf = hidden_value(login_page.text, "login_csrf")
            invalid_login_csrf = client.post(
                "/admin/login",
                data={
                    "username": "admin",
                    "password": "smoke-password",
                    "next_path": "/admin",
                    "login_csrf": "invalido",
                },
                follow_redirects=False,
            )
            expect(invalid_login_csrf.status_code == 403, "Login aceitou CSRF inválido")
            invalid_login = client.post(
                "/admin/login",
                data={
                    "username": "admin",
                    "password": "senha-errada",
                    "next_path": "/admin",
                    "login_csrf": login_csrf,
                },
                follow_redirects=False,
            )
            expect(invalid_login.status_code == 401, "Login aceitou senha inválida")
            original_base_url = settings.app_base_url
            try:
                settings.app_base_url = "http://127.0.0.1:8000"
                localhost_alias = client.post(
                    "/admin/login",
                    data={
                        "username": "admin",
                        "password": "senha-errada",
                        "next_path": "/admin",
                        "login_csrf": login_csrf,
                    },
                    headers={
                        "Origin": "http://localhost:8000",
                        "Referer": "http://localhost:8000/admin/login",
                    },
                    follow_redirects=False,
                )
                expect(localhost_alias.status_code == 401, "Alias local rejeitado indevidamente")
            finally:
                settings.app_base_url = original_base_url
            login = client.post(
                "/admin/login",
                data={
                    "username": "  ADMIN  ",
                    "password": "smoke-password",
                    "next_path": "/admin",
                    "login_csrf": login_csrf,
                },
                follow_redirects=False,
            )
            expect(login.status_code == 303, login.text)
            session_cookie = login.headers.get("set-cookie", "").lower()
            expect("httponly" in session_cookie and "samesite=lax" in session_cookie, "Cookie de sessão inseguro")

            admin_response = client.get("/admin")
            expect(admin_response.status_code == 200, admin_response.text)
            csrf = meta_value(admin_response.text, "csrf-token")
            admin_headers = {"X-CSRF-Token": csrf}
            qr_status = client.get("/api/admin/whatsapp/qr/session")
            expect(qr_status.status_code == 200, qr_status.text)
            expect(qr_status.json().get("enabled") is False, "QR habilitado sem provedor QR")
            qr_inbound_unauthorized = client.post(
                "/api/webhooks/whatsapp/qr",
                json={"phone": "5511994926810", "text": "Teste QR"},
            )
            expect(qr_inbound_unauthorized.status_code == 403, "Webhook QR sem token aceito")
            qr_inbound_disabled = client.post(
                "/api/webhooks/whatsapp/qr",
                headers={"X-QR-Bridge-Token": "smoke-qr-bridge-token"},
                json={"phone": "5511994926810", "text": "Teste QR"},
            )
            expect(qr_inbound_disabled.status_code == 409, "Webhook QR aceito sem provedor QR")

            evolution_runtime_values = {
                "whatsapp_provider": settings.whatsapp_provider,
                "whatsapp_dry_run": settings.whatsapp_dry_run,
                "evolution_webhook_token": settings.evolution_webhook_token,
                "whatsapp_auto_reply": settings.whatsapp_auto_reply,
            }
            try:
                settings.whatsapp_provider = "evolution"
                settings.whatsapp_dry_run = False
                settings.evolution_webhook_token = "smoke-evolution-webhook-token"
                settings.whatsapp_auto_reply = False
                evolution_webhook_payload = {
                    "event": "MESSAGES_UPSERT",
                    "data": {
                        "key": {
                            "id": "evolution-smoke-webhook-media",
                            "remoteJid": "5511977776666@s.whatsapp.net",
                        },
                        "pushName": "Contato com mídia",
                        "message": {
                            "imageMessage": {"caption": "Comprovante", "mimetype": "image/png"},
                            "base64": tiny_png,
                        },
                    },
                }
                unauthorized_evolution = client.post(
                    "/api/webhooks/whatsapp/evolution",
                    json=evolution_webhook_payload,
                )
                expect(unauthorized_evolution.status_code == 403, "Webhook Evolution aceitou token inválido")
                evolution_webhook = client.post(
                    "/api/webhooks/whatsapp/evolution",
                    headers={"X-Evolution-Webhook-Token": "smoke-evolution-webhook-token"},
                    json=evolution_webhook_payload,
                )
                expect(evolution_webhook.status_code == 200, evolution_webhook.text)
                expect(evolution_webhook.json().get("recorded") == 1, "Webhook Evolution não gravou mídia")
                duplicate_evolution = client.post(
                    "/api/webhooks/whatsapp/evolution",
                    headers={"X-Evolution-Webhook-Token": "smoke-evolution-webhook-token"},
                    json=evolution_webhook_payload,
                )
                expect(duplicate_evolution.json().get("duplicate") == 1, "Webhook Evolution duplicou mídia")
                with db.connect() as connection:
                    evolution_media_row = connection.execute(
                        """
                        SELECT media_url, media_mime, media_name, media_size, raw_payload
                        FROM whatsapp_messages
                        WHERE provider_message_id = ?
                        """,
                        ("evolution-smoke-webhook-media",),
                    ).fetchone()
                expect(evolution_media_row is not None, "Mídia Evolution não chegou ao inbox")
                expect(bool(evolution_media_row["media_url"]), "Mídia Evolution não recebeu URL privada")
                expect(evolution_media_row["media_mime"] == "image/png", "MIME da mídia não foi persistido")
                expect("base64" not in evolution_media_row["raw_payload"], "Base64 foi persistido no banco")
                with db.connect() as connection:
                    media_conversation = connection.execute(
                        "SELECT id FROM whatsapp_conversations WHERE phone = ?",
                        ("5511977776666",),
                    ).fetchone()
                    if media_conversation:
                        connection.execute(
                            "DELETE FROM whatsapp_messages WHERE conversation_id = ?",
                            (media_conversation["id"],),
                        )
                        connection.execute(
                            "DELETE FROM whatsapp_conversations WHERE id = ?",
                            (media_conversation["id"],),
                        )
            finally:
                for key, value in evolution_runtime_values.items():
                    setattr(settings, key, value)

            empty_blog_admin = client.get("/admin/artigos")
            expect(empty_blog_admin.status_code == 200, empty_blog_admin.text)
            expect("Escrever meu primeiro artigo" in empty_blog_admin.text, "Painel de artigos vazio")
            new_article_page = client.get("/admin/artigos/novo")
            expect(new_article_page.status_code == 200, new_article_page.text)
            expect("Guardar para terminar depois" in new_article_page.text, "Editor pouco claro")
            article_title = "Direitos que merecem atenção"
            article_excerpt = "Uma orientação clara sobre cuidados importantes nas relações de trabalho."
            article_body = (
                "A experiência ensina que informação e atenção ajudam a evitar decisões apressadas. "
                "Cada situação precisa ser compreendida com cuidado.\n\n"
                "Antes de assinar documentos, leia com calma, guarde uma cópia e procure orientação quando tiver dúvidas. "
                "<script>alert('x')</script>"
            )
            draft = client.post(
                "/admin/artigos/salvar",
                data={
                    "csrf_token": csrf,
                    "post_id": "",
                    "title": article_title,
                    "excerpt": article_excerpt,
                    "body": article_body,
                    "category": "Direito Trabalhista",
                    "action": "draft",
                },
                follow_redirects=False,
            )
            expect(
                draft.status_code == 303 and "salvo=rascunho" in draft.headers.get("location", ""),
                "Rascunho não salvo",
            )
            saved_post = db.list_blog_posts_admin()[0]
            post_id = int(saved_post["id"])
            post_slug = saved_post["slug"]
            expect(client.get(f"/blog/{post_slug}").status_code == 404, "Rascunho publicado indevidamente")
            expect(client.get(f"/admin/artigos/{post_id}/previa").status_code == 200, "Prévia indisponível")
            invalid_article_csrf = client.post(
                "/admin/artigos/salvar",
                data={
                    "csrf_token": "invalido",
                    "post_id": str(post_id),
                    "title": article_title,
                    "excerpt": article_excerpt,
                    "body": article_body,
                    "category": "Direito Trabalhista",
                    "action": "publish",
                },
                follow_redirects=False,
            )
            expect(invalid_article_csrf.status_code == 403, "Editor aceitou CSRF inválido")
            published = client.post(
                "/admin/artigos/salvar",
                data={
                    "csrf_token": csrf,
                    "post_id": str(post_id),
                    "title": article_title,
                    "excerpt": article_excerpt,
                    "body": article_body,
                    "category": "Direito Trabalhista",
                    "action": "publish",
                },
                follow_redirects=False,
            )
            expect(
                published.status_code == 303 and "salvo=publicado" in published.headers.get("location", ""),
                "Artigo não publicado",
            )
            public_article = client.get(f"/blog/{post_slug}")
            expect(public_article.status_code == 200, public_article.text)
            audit_public_markup(f"/blog/{post_slug}", public_article.text)
            expect(article_title in public_article.text, "Título público ausente")
            expect(
                "<script>alert" not in public_article.text and "&lt;script&gt;" in public_article.text,
                "Conteúdo do artigo sem escape",
            )
            blog_search = client.get("/blog", params={"busca": "atenção"})
            expect(blog_search.status_code == 200 and article_title in blog_search.text, "Busca do blog")
            expect(f"/blog/{post_slug}" in client.get("/sitemap.xml").text, "Artigo ausente no sitemap")

            lead_json = {
                "kind": "trabalhista",
                "area": "trabalhista",
                "name": "Lead Teste",
                "phone": "(11) 98765-4321",
                "email": "teste@example.com",
                "message": "Teste de triagem trabalhista.",
                "consent": True,
                "source_path": "/contato",
            }
            lead_response = client.post("/api/leads", json=lead_json, headers={"Idempotency-Key": "smoke-lead-1"})
            expect(lead_response.status_code == 200, lead_response.text)
            payload = lead_response.json()
            expect(payload["ok"] is True and payload["whatsapp"] == "dry_run", str(payload))
            expect("lead_id" not in payload, "Identificador interno exposto")

            duplicate = client.post("/api/leads", json=lead_json, headers={"Idempotency-Key": "smoke-lead-1"})
            expect(duplicate.json().get("duplicate") is True, duplicate.text)

            bpc_lead_json = {
                "kind": "bpc",
                "area": "previdenciario",
                "name": "Lead BPC",
                "phone": "(11) 91234-5678",
                "email": "bpc@example.com",
                "message": "Quero receber o checklist de documentos para BPC/LOAS.",
                "consent": True,
                "source_path": "/bpc-loas-negado",
                "landing_path": "/bpc-loas-negado",
            }
            bpc_lead_response = client.post(
                "/api/leads",
                json=bpc_lead_json,
                headers={"Idempotency-Key": "smoke-lead-bpc"},
            )
            expect(bpc_lead_response.status_code == 200, bpc_lead_response.text)

            no_script_success = client.post(
                "/contato/enviar",
                data={
                    "kind": "trabalhista",
                    "area": "trabalhista",
                    "name": "Lead Teste",
                    "phone": "(11) 98765-4321",
                    "email": "teste@example.com",
                    "message": "Teste de triagem trabalhista.",
                    "consent": "on",
                    "request_key": "smoke-lead-1",
                },
            )
            expect(no_script_success.status_code == 200, no_script_success.text)
            expect("Obrigado pelas informações" in no_script_success.text, "Confirmação sem JavaScript")
            no_script_error = client.post(
                "/contato/enviar",
                data={
                    "kind": "geral",
                    "area": "outro",
                    "name": "A",
                    "phone": "123",
                    "message": "curta",
                    "request_key": "smoke-invalid",
                },
            )
            expect(no_script_error.status_code == 422, "Erro do formulário sem JavaScript")
            expect("Não foi possível enviar" in no_script_error.text, "Página de erro do formulário")

            metrics_response = client.get("/api/admin/metrics")
            expect(metrics_response.status_code == 200, metrics_response.text)
            metrics_payload = metrics_response.json()
            expect(metrics_payload["totals"]["leads_total"] == 2, "Lead duplicado")
            expect(metrics_payload["totals"]["bpc_total"] == 1, "BPC não separado")
            expect(metrics_payload["totals"]["conversations_total"] == 2, "Métrica de conversas")
            expect(metrics_payload["funnel"]["bpc_visits"] == 1, "Visitas BPC ausentes")
            expect(metrics_payload["funnel"]["whatsapp_clicks"] == 1, "Clique WhatsApp ausente")
            expect(metrics_payload["funnel"]["lead_conversions"] == 1, "Conversão de lead ausente")

            conversations = client.get("/api/admin/conversations").json()["conversations"]
            expect(len(conversations) == 2, "Conversa ausente")
            conversation_id = next(item["id"] for item in conversations if item["phone"] == "5511987654321")

            missing_csrf = client.post(
                f"/api/admin/conversations/{conversation_id}/messages",
                data={"text": "Mensagem sem CSRF"},
            )
            expect(missing_csrf.status_code == 403, "Mensagem sem CSRF aceita")
            foreign_origin = client.post(
                f"/api/admin/conversations/{conversation_id}/messages",
                data={"text": "Mensagem de outra origem"},
                headers={**admin_headers, "Origin": "https://example.invalid"},
            )
            expect(foreign_origin.status_code == 403, "Origem externa aceita")

            old_date = "2000-01-01T00:00:00+00:00"
            with db.connect() as connection:
                connection.execute(
                    "UPDATE leads SET created_at = ?, status = 'closed' WHERE id = 1", (old_date,)
                )
                connection.execute(
                    "UPDATE whatsapp_conversations SET updated_at = ?, status = 'closed' WHERE id = ?",
                    (old_date, conversation_id),
                )
            expect("5511987654321" in db.list_expired_contact_phones(), "Retenção não encontrou caso expirado")
            with db.connect() as connection:
                connection.execute(
                    "UPDATE whatsapp_conversations SET updated_at = ?, status = 'open' WHERE id = ?",
                    (db.utc_now(), conversation_id),
                )
            expect("5511987654321" not in db.list_expired_contact_phones(), "Retenção apagaria conversa ativa")
            with db.connect() as connection:
                connection.execute(
                    "UPDATE leads SET created_at = ?, status = 'new' WHERE id = 1", (db.utc_now(),)
                )

            send_response = client.post(
                f"/api/admin/conversations/{conversation_id}/messages",
                data={"text": "Mensagem de teste"},
                headers=admin_headers,
            )
            expect(send_response.status_code == 200, send_response.text)

            invalid_office = client.post(
                f"/api/admin/conversations/{conversation_id}/messages",
                files={
                    "attachment": (
                        "invalido.docx",
                        b"PK\x03\x04arquivo-que-nao-e-office",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
                headers=admin_headers,
            )
            expect(invalid_office.status_code == 400, "Documento Office inválido aceito")

            file_response = client.post(
                f"/api/admin/conversations/{conversation_id}/messages",
                data={"text": "Arquivo de teste"},
                files={"attachment": ("teste.txt", b"conteudo", "text/plain")},
                headers=admin_headers,
            )
            expect(file_response.status_code == 200, file_response.text)

            messages = client.get(f"/api/admin/conversations/{conversation_id}/messages").json()["messages"]
            media_url = next(message["media_url"] for message in messages if message.get("media_url"))
            media_response = client.get(media_url)
            expect(media_response.status_code == 200 and media_response.content == b"conteudo", "Mídia privada")
            expect(client.get("/static/uploads/whatsapp/teste.txt").status_code == 404, "Upload público")

            with db.connect() as connection:
                connection.execute(
                    """
                    UPDATE whatsapp_outbox
                    SET status = 'pending', next_attempt_at = ?
                    WHERE id = (
                        SELECT id FROM whatsapp_outbox
                        WHERE recipient = ?
                        ORDER BY id DESC LIMIT 1
                    )
                    """,
                    (db.utc_now(), "5511987654321"),
                )
            db.set_whatsapp_opt_out("5511987654321", source="smoke", reason="PARAR")
            expect(db.is_whatsapp_opted_out("5511987654321"), "Preferência de opt-out não persistida")
            with db.connect() as connection:
                suppressed = connection.execute(
                    "SELECT status, next_attempt_at FROM whatsapp_outbox WHERE recipient = ? ORDER BY id DESC LIMIT 1",
                    ("5511987654321",),
                ).fetchone()
            expect(suppressed["status"] == "suppressed", "Fila pendente não foi suprimida")
            expect(suppressed["next_attempt_at"] is None, "Fila suprimida manteve retentativa")
            blocked_send = client.post(
                f"/api/admin/conversations/{conversation_id}/messages",
                data={"text": "Mensagem que não deve sair"},
                headers=admin_headers,
            )
            expect(blocked_send.status_code == 409, "Contato bloqueado recebeu novo envio")

            controls = client.post(
                f"/api/admin/conversations/{conversation_id}/controls",
                json={"bot_enabled": False, "status": "closed"},
                headers=admin_headers,
            )
            expect(controls.status_code == 200, controls.text)

            lead_status = client.post(
                "/api/admin/leads/1/status",
                json={"status": "qualified"},
                headers=admin_headers,
            )
            expect(lead_status.status_code == 200, lead_status.text)

            privacy_delete = client.post(
                "/api/admin/privacy/delete",
                json={"phone": "(11) 98765-4321"},
                headers=admin_headers,
            )
            expect(privacy_delete.status_code == 200, privacy_delete.text)
            expect(privacy_delete.json()["deleted"]["leads"] == 1, "Exclusão do contato")
            expect(client.get(media_url).status_code == 404, "Arquivo não removido na exclusão")
            bpc_privacy_delete = client.post(
                "/api/admin/privacy/delete",
                json={"phone": "(11) 91234-5678"},
                headers=admin_headers,
            )
            expect(bpc_privacy_delete.status_code == 200, bpc_privacy_delete.text)
            expect(not client.get("/api/admin/conversations").json()["conversations"], "Conversa não removida")

            triage_phone = "5511966665555"
            triage_id = db.get_or_create_conversation(triage_phone, kind="trabalhista")
            db.record_whatsapp_message(
                triage_id,
                direction="out",
                text="Conte com suas palavras o que aconteceu.",
                status="sent",
            )
            db.record_whatsapp_message(
                triage_id,
                direction="in",
                text="Fui demitido e não recebi as verbas rescisórias.",
                status="received",
            )
            second_question = auto_reply_for_inbound(
                "Fui demitido e não recebi as verbas rescisórias.",
                conversation_id=triage_id,
                kind="trabalhista",
            )
            expect("datas mais importantes" in second_question, "Triagem repetiu o relato inicial")
            for answer in (
                "A demissão ocorreu em 10 de junho e a empresa não respondeu.",
                "Tenho contrato, holerites e termo de rescisão.",
                "Quero entender os valores e prefiro retorno à tarde.",
            ):
                db.record_whatsapp_message(
                    triage_id,
                    direction="in",
                    text=answer,
                    status="received",
                )
            expect(auto_triage_should_handoff(triage_id), "Triagem não concluiu a passagem humana")
            with db.connect() as connection:
                connection.execute(
                    "DELETE FROM whatsapp_messages WHERE conversation_id = ?", (triage_id,)
                )
                connection.execute(
                    "DELETE FROM whatsapp_conversations WHERE id = ?", (triage_id,)
                )

            unpublished = client.post(
                f"/admin/artigos/{post_id}/retirar",
                data={"csrf_token": csrf},
                follow_redirects=False,
            )
            expect(unpublished.status_code == 303, "Artigo não retirado")
            expect(client.get(f"/blog/{post_slug}").status_code == 404, "Artigo retirado continua público")

            logout = client.post("/admin/logout", data={"csrf_token": csrf})
            expect(logout.status_code == 200, logout.text)
            expect(client.get("/api/admin/metrics").status_code == 401, "Sessão não revogada")
    finally:
        smoke_db.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(smoke_db) + suffix).unlink(missing_ok=True)
        shutil.rmtree(upload_dir, ignore_errors=True)

    print("smoke ok")


if __name__ == "__main__":
    main()
