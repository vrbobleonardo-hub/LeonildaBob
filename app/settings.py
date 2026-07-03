from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()


class Settings:
    app_name: str = os.getenv("APP_NAME", "Leonilda Bob | Bob Advogados")
    app_base_url: str = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000")
    db_path: Path = Path(os.getenv("LEONILDA_DB_PATH", "data/leonilda.sqlite"))
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    admin_password_hash: str = os.getenv("ADMIN_PASSWORD_HASH", "")
    admin_session_secret: str = os.getenv("ADMIN_SESSION_SECRET", "")
    admin_session_ttl_seconds: int = int(os.getenv("ADMIN_SESSION_TTL_SECONDS", "28800"))
    meta_graph_version: str = os.getenv("META_GRAPH_VERSION", "v19.0")
    whatsapp_provider: str = os.getenv("WHATSAPP_PROVIDER", "official").strip().lower()
    whatsapp_access_token: str = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    whatsapp_phone_number_id: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    whatsapp_verify_token: str = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    whatsapp_first_contact_template: str = os.getenv("WHATSAPP_FIRST_CONTACT_TEMPLATE", "")
    whatsapp_template_language: str = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "pt_BR")
    whatsapp_auto_reply: bool = os.getenv("WHATSAPP_AUTO_REPLY", "1").strip().lower() in {"1", "true", "yes", "on"}
    whatsapp_qr_bridge_url: str = os.getenv("WHATSAPP_QR_BRIDGE_URL", "http://127.0.0.1:3333").rstrip("/")
    whatsapp_dry_run: bool = os.getenv("WHATSAPP_DRY_RUN", "1").strip().lower() in {"1", "true", "yes", "on"}
    metrics_salt: str = os.getenv("METRICS_SALT", "local-development-salt")


settings = Settings()
