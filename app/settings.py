from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRAZILIAN_AREA_CODES = frozenset(
    {
        11, 12, 13, 14, 15, 16, 17, 18, 19,
        21, 22, 24, 27, 28,
        31, 32, 33, 34, 35, 37, 38,
        41, 42, 43, 44, 45, 46, 47, 48, 49,
        51, 53, 54, 55,
        61, 62, 63, 64, 65, 66, 67, 68, 69,
        71, 73, 74, 75, 77, 79,
        81, 82, 83, 84, 85, 86, 87, 88, 89,
        91, 92, 93, 94, 95, 96, 97, 98, 99,
    }
)


def load_env_file(path: Path = PROJECT_ROOT / ".env") -> None:
    if not path.exists():
        return
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise RuntimeError("O arquivo .env deve ser privado (permissão 600).")
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
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(
        f"{name} deve usar 1/0, true/false, yes/no ou on/off."
    )


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
        self.db_pool_min_size = env_int("DB_POOL_MIN_SIZE", 0, minimum=0, maximum=10)
        self.db_pool_max_size = env_int("DB_POOL_MAX_SIZE", 5, minimum=1, maximum=30)
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

        self.allowed_hosts = list({
            host.strip()
            for host in (os.getenv("ALLOWED_HOSTS", "") + ",127.0.0.1,localhost,testserver").split(",")
            if host.strip()
        })
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

        self.meta_graph_version = os.getenv("META_GRAPH_VERSION", "v19.0" if self.app_env != "production" else "").strip()
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
        self.whatsapp_qr_bridge_token = os.getenv("WHATSAPP_QR_BRIDGE_TOKEN", "").strip()
        self.whatsapp_qr_bridge_autostart = env_bool("WHATSAPP_QR_BRIDGE_AUTOSTART", True)
        self.whatsapp_dry_run = env_bool("WHATSAPP_DRY_RUN", True)

        self.metrics_salt = os.getenv("METRICS_SALT", "local-development-salt")
        self.analytics_retention_days = env_int(
            "ANALYTICS_RETENTION_DAYS", 180, minimum=1, maximum=3650
        )
        self.auth_event_retention_days = env_int(
            "AUTH_EVENT_RETENTION_DAYS", 365, minimum=30, maximum=3650
        )
        self.lead_retention_days = env_int("LEAD_RETENTION_DAYS", 730, minimum=30, maximum=3650)
        self.max_upload_bytes = env_int(
            "MAX_UPLOAD_BYTES", 25 * 1024 * 1024, minimum=1024, maximum=100 * 1024 * 1024
        )
        self.require_virus_scan = env_bool("REQUIRE_VIRUS_SCAN", False)

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
        if self.admin_password_hash:
            password_hash = re.fullmatch(
                r"pbkdf2_sha256\$(\d+)\$[0-9a-f]{32}\$[0-9a-f]{64}",
                self.admin_password_hash,
            )
            if not password_hash or not 100_000 <= int(password_hash.group(1)) <= 1_500_000:
                raise RuntimeError("ADMIN_PASSWORD_HASH possui formato inválido.")
        if self.whatsapp_provider not in {"official", "qr"}:
            raise RuntimeError("WHATSAPP_PROVIDER deve ser official ou qr.")
        qr_bridge = urlparse(self.whatsapp_qr_bridge_url)
        if self.whatsapp_provider == "qr" and (
            qr_bridge.scheme not in {"http", "https"} or not qr_bridge.netloc
        ):
            raise RuntimeError("WHATSAPP_QR_BRIDGE_URL deve ser uma URL HTTP absoluta.")
        if self.whatsapp_provider == "qr" and self.is_production and not self.whatsapp_qr_bridge_token:
            raise RuntimeError("WHATSAPP_QR_BRIDGE_TOKEN deve ser configurado em produção com QR.")
        if self.database_url and not self.database_url.startswith(("postgresql://", "postgres://")):
            raise RuntimeError("DATABASE_URL deve apontar para PostgreSQL.")
        if self.db_pool_min_size > self.db_pool_max_size:
            raise RuntimeError("DB_POOL_MIN_SIZE não pode exceder DB_POOL_MAX_SIZE.")
        if self.supabase_url and not self.supabase_url.startswith("https://"):
            raise RuntimeError("SUPABASE_URL deve usar HTTPS.")
        if bool(self.supabase_url) != bool(self.supabase_secret_key):
            raise RuntimeError("SUPABASE_URL e SUPABASE_SECRET_KEY devem ser configurados juntos.")
        if self.supabase_url and not self.supabase_storage_bucket:
            raise RuntimeError("SUPABASE_STORAGE_BUCKET deve ser configurado.")
        if not re.fullmatch(r"55\d{10,11}", self.contact_phone_digits):
            raise RuntimeError("CONTACT_PHONE_DIGITS deve conter 55, DDD e número.")
        if int(self.contact_phone_digits[2:4]) not in BRAZILIAN_AREA_CODES:
            raise RuntimeError("CONTACT_PHONE_DIGITS contém um DDD inválido.")
        if self.contact_email and not re.fullmatch(
            r"[^\s@]+@[^\s@]+\.[^\s@]+", self.contact_email
        ):
            raise RuntimeError("CONTACT_EMAIL deve conter um endereço válido.")
        parsed = urlparse(self.app_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("APP_BASE_URL deve ser uma URL absoluta HTTP ou HTTPS.")
        if parsed.username or parsed.password:
            raise RuntimeError("APP_BASE_URL não deve conter credenciais.")
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            raise RuntimeError("APP_BASE_URL não deve conter caminho, parâmetros ou fragmento.")
        if not self.allowed_hosts:
            raise RuntimeError("ALLOWED_HOSTS deve conter ao menos um host.")
        host_allowed = parsed.hostname in self.allowed_hosts or "*" in self.allowed_hosts or any(
            allowed.startswith("*.") and parsed.hostname and parsed.hostname.endswith(allowed[1:])
            for allowed in self.allowed_hosts
        )
        if not host_allowed:
            raise RuntimeError("ALLOWED_HOSTS deve incluir o host de APP_BASE_URL.")
        if self.is_production:
            errors: list[str] = []
            if parsed.scheme != "https":
                errors.append("APP_BASE_URL deve usar HTTPS")
            if not self.cookie_secure:
                errors.append("COOKIE_SECURE deve estar ativo")
            if "*" in self.allowed_hosts:
                errors.append("ALLOWED_HOSTS não pode liberar todos os hosts")
            if len(self.admin_session_secret) < 32:
                errors.append("ADMIN_SESSION_SECRET deve ter ao menos 32 caracteres")
            if not self.admin_password_hash:
                errors.append("ADMIN_PASSWORD_HASH deve ser usado no lugar de ADMIN_PASSWORD")
            if self.admin_password:
                errors.append("ADMIN_PASSWORD não deve ser mantida em texto puro")
            if not self.database_url:
                errors.append("DATABASE_URL deve usar banco persistente em produção")
            if len(self.metrics_salt) < 24 or self.metrics_salt in {
                "local-development-salt",
                "change-me-local",
            }:
                errors.append("METRICS_SALT deve ser forte e exclusivo")
            if (
                self.whatsapp_provider == "official"
                and (
                    not self.whatsapp_dry_run
                    or any(
                        (
                            self.whatsapp_access_token,
                            self.whatsapp_phone_number_id,
                            self.whatsapp_app_secret,
                            self.whatsapp_verify_token,
                        )
                    )
                )
                and not re.fullmatch(r"v\d+\.\d+", self.meta_graph_version)
            ):
                errors.append("META_GRAPH_VERSION deve ser definida com a versão vigente da Meta")
            if not self.whatsapp_dry_run and self.whatsapp_provider == "official":
                if not self.whatsapp_access_token or not self.whatsapp_phone_number_id:
                    errors.append("credenciais do WhatsApp oficial estão incompletas")
                elif not self.whatsapp_phone_number_id.isdigit():
                    errors.append("WHATSAPP_PHONE_NUMBER_ID deve conter apenas números")
                if not self.whatsapp_verify_token or not self.whatsapp_app_secret:
                    errors.append("verificação e assinatura do webhook estão incompletas")
                elif len(self.whatsapp_verify_token) < 16 or len(self.whatsapp_app_secret) < 16:
                    errors.append("tokens de verificação do WhatsApp são curtos demais")
                if not self.whatsapp_first_contact_template:
                    errors.append("template de primeiro contato está ausente")
                elif not re.fullmatch(r"[a-z0-9_]{1,512}", self.whatsapp_first_contact_template):
                    errors.append("WHATSAPP_FIRST_CONTACT_TEMPLATE possui formato inválido")
            if errors:
                raise RuntimeError("Configuração de produção inválida: " + "; ".join(errors) + ".")


settings = Settings()
