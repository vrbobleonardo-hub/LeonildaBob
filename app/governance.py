from __future__ import annotations

import base64
import hashlib
import re
import unicodedata
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from .settings import settings


CONSENT_VERSION = "triagem-saude-v1"
CONSENT_PURPOSE = (
    "Coletar o mínimo necessário sobre saúde e limitações funcionais para organizar "
    "a triagem jurídica e o encaminhamento ao atendimento humano."
)


def _normalized(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    return "".join(character for character in text if not unicodedata.combining(character)).casefold()


def _fernet() -> Fernet:
    configured = settings.data_encryption_key.strip()
    if configured:
        try:
            key = configured.encode("ascii")
            Fernet(key)
            return Fernet(key)
        except (ValueError, UnicodeEncodeError):
            material = configured
    else:
        material = settings.admin_session_secret or settings.metrics_salt
    derived = hashlib.sha256(f"bob-sensitive-data-v1:{material}".encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_sensitive(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_sensitive(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeEncodeError):
        return "[conteúdo protegido indisponível]"


@dataclass(frozen=True)
class SafetyResult:
    allowed: bool
    reasons: tuple[str, ...]


_SAFETY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "promessa de resultado",
        re.compile(
            r"\b(garantimos|garantido|garantida|aprovacao certa|resultado certo|resultado garantido|"
            r"voce vai ganhar|voce recebera|tem direito garantido|inss liberou|causa ganha)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "diagnóstico jurídico conclusivo",
        re.compile(
            r"\b(voce tem direito(?: ao| a)?|seu caso esta ganho|a acao esta ganha|"
            r"essa doenca garante|esse diagnostico garante)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "prazo garantido",
        re.compile(
            r"\b(garantimos|certamente|com certeza)\b.{0,50}\b(em|dentro de|no prazo de)\s+\d+\s+"
            r"(horas?|dias?|semanas?|meses?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "pedido de credencial sigilosa",
        re.compile(
            r"\b(envie|mande|informe|passe|compartilhe)\b.{0,70}\b(senha|token|codigo de autenticacao|"
            r"codigo bancario|codigo recebido por sms|senha do meu inss)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "linguagem ofensiva",
        re.compile(r"\b(idiota|burro|imbecil|inutil|vagabundo)\b", re.IGNORECASE),
    ),
)


def inspect_outbound(text: str) -> SafetyResult:
    normalized = _normalized(text)
    reasons = [label for label, pattern in _SAFETY_RULES if pattern.search(normalized)]
    if re.search(r"r\$\s*\d", normalized) and re.search(
        r"\b(bpc|loas|beneficio|aposentadoria|pensao|auxilio|receber|retroativo)\b", normalized
    ):
        reasons.append("valor fixo de benefício")
    return SafetyResult(allowed=not reasons, reasons=tuple(dict.fromkeys(reasons)))


def contains_legal_claim(text: str) -> bool:
    normalized = _normalized(text)
    return bool(
        re.search(
            r"\b(lei|direito|inss|bpc|loas|beneficio|aposentadoria|pensao|auxilio|"
            r"recurso|revisao|judicial|administrativ[oa]|obrigatori[oa]|prazo legal|"
            r"indenizacao|verba rescisoria|fgts|usucapiao|curatela)\b",
            normalized,
        )
    )
