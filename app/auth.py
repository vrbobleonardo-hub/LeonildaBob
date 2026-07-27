from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from . import db
from .settings import settings


SESSION_COOKIE = "bob_admin_session"


@dataclass(frozen=True)
class AdminUser:
    username: str


def normalize_username(value: str) -> str:
    """Normalize harmless typing differences without changing account identity."""
    normalized = unicodedata.normalize("NFKC", value or "")
    return " ".join(normalized.split()).casefold()


def auth_is_configured() -> bool:
    return bool(
        settings.admin_username
        and settings.admin_session_secret
        and (settings.admin_password or settings.admin_password_hash)
    )


def hash_password(password: str, *, iterations: int = 260_000) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_password(password: str) -> bool:
    password = password or ""
    stored_hash = settings.admin_password_hash
    if stored_hash:
        try:
            algorithm, iterations_raw, salt, expected = stored_hash.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            iterations = int(iterations_raw)
            if iterations < 100_000 or iterations > 1_500_000:
                return False
            digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations).hex()
            return hmac.compare_digest(digest, expected)
        except Exception:
            return False
    if settings.admin_password:
        return hmac.compare_digest(password, settings.admin_password)
    return False


def authenticate(username: str, password: str) -> AdminUser | None:
    if not auth_is_configured():
        return None
    if not hmac.compare_digest(
        normalize_username(username), normalize_username(settings.admin_username)
    ):
        return None
    if not verify_password(password):
        return None
    return AdminUser(username=settings.admin_username)


def _sign(payload: str) -> str:
    return hmac.new(settings.admin_session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _credential_fingerprint() -> str:
    material = settings.admin_password_hash or settings.admin_password
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def create_session_token(username: str) -> str:
    expires_at = int(time.time()) + max(settings.admin_session_ttl_seconds, 900)
    nonce = secrets.token_urlsafe(32)
    payload = f"{username}:{expires_at}:{nonce}:{_credential_fingerprint()}"
    signature = _sign(payload)
    token = base64.urlsafe_b64encode(f"{payload}:{signature}".encode("utf-8")).decode("ascii")
    db.create_admin_session(_token_hash(token), username, expires_at)
    return token


def verify_session_token(token: str | None) -> AdminUser | None:
    if not token or not auth_is_configured():
        return None
    try:
        decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        username, expires_raw, nonce, credential_fingerprint, signature = decoded.rsplit(":", 4)
        payload = f"{username}:{expires_raw}:{nonce}:{credential_fingerprint}"
        if not hmac.compare_digest(signature, _sign(payload)):
            return None
        if int(expires_raw) < int(time.time()):
            return None
        if not hmac.compare_digest(username, settings.admin_username):
            return None
        if not hmac.compare_digest(credential_fingerprint, _credential_fingerprint()):
            return None
        if not db.admin_session_is_active(_token_hash(token), username, int(time.time())):
            return None
        return AdminUser(username=username)
    except Exception:
        return None


def current_admin(request: Request) -> AdminUser | None:
    return verify_session_token(request.cookies.get(SESSION_COOKIE))


def require_admin(request: Request) -> AdminUser:
    user = current_admin(request)
    if user:
        return user
    accepts_html = "text/html" in request.headers.get("accept", "")
    if accepts_html:
        next_path = request.url.path
        if request.url.query:
            next_path = f"{next_path}?{request.url.query}"
        raise HTTPException(
            status_code=303,
            headers={"Location": f"/admin/login?next={quote(next_path, safe='/')}"},
        )
    raise HTTPException(status_code=401, detail="Acesso restrito.")


def build_login_response(username: str, next_path: str = "/admin") -> RedirectResponse:
    response = RedirectResponse(next_path or "/admin", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(username),
        max_age=max(settings.admin_session_ttl_seconds, 900),
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return response


def build_logout_response(token: str | None = None) -> RedirectResponse:
    if token:
        db.revoke_admin_session(_token_hash(token))
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
