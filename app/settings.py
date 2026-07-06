from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_env_file(path: Path = PROJECT_ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} deve ser um número inteiro.") from exc
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f" e no máximo {maximum}" if maximum is not None else ""
        raise RuntimeError(f"{name} deve ser no mínimo {minimum}{suffix}.")
    return value


def env_path(name: str, default: str) -> Path:
    value = Path(os.getenv(name, default)).expanduser()
    return value if value.is_absolute() else PROJECT_ROOT / value


load_env_file()


class Settings:
    def __init__(self) -> None:
        self.app_name = os.getenv("APP_NAME", "Leonilda Bob | Bob Advogados").strip()
        self.app_env = os.getenv("APP_ENV", "development").strip().lower()
        self.app_base_url = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/")
        self.database_url = os.getenv("DATABASE_URL", "").strip()
        self.db_path = env_path("LEONILDA_DB_PATH", "data/leonilda.sqlite")
        self.private_upload_dir = env_path("PRIVATE_UPLOAD_DIR", "data/uploads/whatsapp")
        self.supabase_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
        self.supabase_publishable_key = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
        self.supabase_secret_key = os.getenv("SUPABASE_SECRET_KEY", "")
        self.supabase_storage_bucket = os.getenv(
            "SUPABASE_STORAGE_BUCKET", "whatsapp-media"
        ).strip()
        self.template_dir = PROJECT_ROOT / "templates"
        self.static_dir = PROJECT_ROOT / "static"

        self.allowed_hosts = [
            host.strip()
            for host in os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost,testserver").split(",")
            if host.strip()
        ]
        self.cookie_secure = env_bool(
            "COOKIE_SECURE",
            self.app_env == "production" or self.app_base_url.startswith("https://"),
        )
        self.docs_enabled = env_bool("DOCS_ENABLED", self.app_env != "production")

        self.admin_username = os.getenv("ADMIN_USERNAME", "admin").strip()
        self.admin_password = os.getenv("ADMIN_PASSWORD", "")
        self.admin_password_hash = os.getenv("ADMIN_PASSWORD_HASH", "")
        self.admin_session_secret = os.getenv("ADMIN_SESSION_SECRET", "")
        self.admin_session_ttl_seconds = env_int(
            "ADMIN_SESSION_TTL_SECONDS", 28_800, minimum=900, maximum=604_800
        )

        self.meta_graph_version = os.getenv("META_GRAPH_VERSION", "v19.0").strip()
        self.whatsapp_provider = os.getenv("WHATSAPP_PROVIDER", "official").strip().lower()
        self.whatsapp_access_token = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
        self.whatsapp_phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
        self.whatsapp_app_secret = os.getenv("WHATSAPP_APP_SECRET", "")
        self.whatsapp_verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
        self.whatsapp_first_contact_template = os.getenv("WHATSAPP_FIRST_CONTACT_TEMPLATE", "")
        self.whatsapp_template_language = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "pt_BR")
        self.whatsapp_auto_reply = env_bool("WHATSAPP_AUTO_REPLY", True)
        self.whatsapp_auto_reply_cooldown_seconds = env_int(
            "WHATSAPP_AUTO_REPLY_COOLDOWN_SECONDS", 21_600, minimum=60, maximum=604_800
        )
        self.whatsapp_qr_bridge_url = os.getenv(
            "WHATSAPP_QR_BRIDGE_URL", "http://127.0.0.1:3333"
        ).rstrip("/")
        self.whatsapp_dry_run = env_bool("WHATSAPP_DRY_RUN", True)

        self.metrics_salt = os.getenv("METRICS_SALT", "local-development-salt")
        self.analytics_retention_days = env_int(
            "ANALYTICS_RETENTION_DAYS", 180, minimum=1, maximum=3650
        )
        self.lead_retention_days = env_int("LEAD_RETENTION_DAYS", 730, minimum=30, maximum=3650)
        self.max_upload_bytes = env_int(
            "MAX_UPLOAD_BYTES", 25 * 1024 * 1024, minimum=1024, maximum=100 * 1024 * 1024
        )

        self.contact_phone_display = os.getenv("CONTACT_PHONE_DISPLAY", "(11) 99492-6810").strip()
        self.contact_phone_digits = os.getenv("CONTACT_PHONE_DIGITS", "5511994926810").strip()
        self.contact_email = os.getenv("CONTACT_EMAIL", "").strip()
        self.address = os.getenv(
            "CONTACT_ADDRESS", "Rua Mariana Najar, 700 — Vila Nova Socorro, Mogi das Cruzes/SP"
        ).strip()
        self.address_zip = os.getenv("CONTACT_ADDRESS_ZIP", "CEP 08790-610").strip()
        self.office_hours = os.getenv("OFFICE_HOURS", "Atendimento mediante agendamento").strip()

        self.validate()

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def validate(self) -> None:
        if self.app_env not in {"development", "test", "production"}:
            raise RuntimeError("APP_ENV deve ser development, test ou production.")
        if self.whatsapp_provider not in {"official", "qr"}:
            raise RuntimeError("WHATSAPP_PROVIDER deve ser official ou qr.")
        parsed = urlparse(self.app_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("APP_BASE_URL deve ser uma URL absoluta HTTP ou HTTPS.")
        if not self.allowed_hosts:
            raise RuntimeError("ALLOWED_HOSTS deve conter ao menos um host.")
        if self.is_production:
            errors: list[str] = []
            if parsed.scheme != "https":
                errors.append("APP_BASE_URL deve usar HTTPS")
            if not self.cookie_secure:
                errors.append("COOKIE_SECURE deve estar ativo")
            if len(self.admin_session_secret) < 32:
                errors.append("ADMIN_SESSION_SECRET deve ter ao menos 32 caracteres")
            if not self.admin_password_hash:
                errors.append("ADMIN_PASSWORD_HASH deve ser usado no lugar de ADMIN_PASSWORD")
            if len(self.metrics_salt) < 24 or self.metrics_salt in {
                "local-development-salt",
                "change-me-local",
            }:
                errors.append("METRICS_SALT deve ser forte e exclusivo")
            if not self.whatsapp_dry_run and self.whatsapp_provider == "official":
                if not self.whatsapp_access_token or not self.whatsapp_phone_number_id:
                    errors.append("credenciais do WhatsApp oficial estão incompletas")
                if not self.whatsapp_verify_token or not self.whatsapp_app_secret:
                    errors.append("verificação e assinatura do webhook estão incompletas")
                if not self.whatsapp_first_contact_template:
                    errors.append("template de primeiro contato está ausente")
            if errors:
                raise RuntimeError("Configuração de produção inválida: " + "; ".join(errors) + ".")


settings = Settings()
