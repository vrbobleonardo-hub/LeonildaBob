from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from .auth import SESSION_COOKIE
from .settings import settings


LOGIN_CSRF_COOKIE = "bob_login_csrf"


class RateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, *, limit: int, window_seconds: int) -> int:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                retry_after = max(1, int(window_seconds - (now - events[0])))
                raise HTTPException(
                    status_code=429,
                    detail="Muitas tentativas. Aguarde antes de tentar novamente.",
                    headers={"Retry-After": str(retry_after)},
                )
            events.append(now)
            if len(self._events) > 10_000:
                for item_key in list(self._events)[:1000]:
                    if item_key != key:
                        self._events.pop(item_key, None)
            return max(0, limit - len(events))


rate_limiter = RateLimiter()


def request_ip(request: Request) -> str:
    # Proxy headers must be normalized by the trusted ASGI server. Never trust a
    # client-supplied X-Forwarded-For value directly in application code.
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, scope: str, *, limit: int, window_seconds: int) -> None:
    remaining = rate_limiter.check(
        f"{scope}:{request_ip(request)}",
        limit=limit,
        window_seconds=window_seconds,
    )
    request.state.rate_limit_remaining = remaining


def new_login_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_login_csrf(request: Request, submitted: str) -> None:
    cookie = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    if not cookie or not submitted or not hmac.compare_digest(cookie, submitted):
        raise HTTPException(status_code=403, detail="Sessão de login expirada. Recarregue a página.")


def session_csrf_token(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token or not settings.admin_session_secret:
        return ""
    return hmac.new(
        settings.admin_session_secret.encode("utf-8"),
        f"csrf:{token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_session_csrf(request: Request, submitted: str | None = None) -> None:
    expected = session_csrf_token(request)
    received = submitted or request.headers.get("x-csrf-token", "")
    if not expected or not received or not hmac.compare_digest(expected, received):
        raise HTTPException(status_code=403, detail="Verificação de segurança inválida.")


def _default_port(parsed) -> int | None:
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    if parsed.scheme == "http":
        return 80
    return None


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_trusted_source(source, target) -> bool:
    if source.scheme != target.scheme:
        return False
    if _default_port(source) != _default_port(target):
        return False
    if source.hostname == target.hostname:
        return True
    if not settings.is_production and _is_loopback_host(source.hostname) and _is_loopback_host(target.hostname):
        return True
    return False


def verify_same_origin(request: Request) -> None:
    target = urlparse(settings.app_base_url)
    candidates = []
    origin = (request.headers.get("origin") or "").strip()
    if origin and origin.lower() != "null":
        candidates.append(urlparse(origin))
    referer = (request.headers.get("referer") or "").strip()
    if referer:
        candidates.append(urlparse(referer))
    if not candidates:
        return
    if any(_is_trusted_source(source, target) for source in candidates):
        return
    raise HTTPException(status_code=403, detail="Origem não autorizada.")


def verify_whatsapp_signature(raw_body: bytes, received_signature: str) -> None:
    if not settings.whatsapp_app_secret:
        if settings.is_production:
            raise HTTPException(status_code=503, detail="Assinatura do webhook não configurada.")
        return
    expected = "sha256=" + hmac.new(
        settings.whatsapp_app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    if not received_signature or not hmac.compare_digest(received_signature, expected):
        raise HTTPException(status_code=403, detail="Assinatura inválida.")
