from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

import requests

from .settings import settings


def configured() -> bool:
    return bool(settings.supabase_url and settings.supabase_secret_key)


def _headers(*, content_type: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": settings.supabase_secret_key,
        "Authorization": f"Bearer {settings.supabase_secret_key}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def ensure_bucket() -> bool:
    if not configured():
        return False
    endpoint = f"{settings.supabase_url}/storage/v1/bucket"
    response = requests.post(
        endpoint,
        headers=_headers(content_type="application/json"),
        json={
            "id": settings.supabase_storage_bucket,
            "name": settings.supabase_storage_bucket,
            "public": False,
            "file_size_limit": settings.max_upload_bytes,
        },
        timeout=15,
    )
    if response.status_code not in {200, 201, 409}:
        raise RuntimeError(f"Não foi possível preparar os arquivos do atendimento ({response.status_code}).")
    return True


def object_name(filename: str) -> str:
    safe_name = Path(filename or "arquivo").name.replace("/", "-")
    return f"whatsapp/{uuid.uuid4().hex}-{safe_name}"


def upload_bytes(content: bytes, filename: str, mime_type: str | None = None) -> tuple[str, str]:
    if not configured():
        raise RuntimeError("Armazenamento do atendimento não configurado.")
    path = object_name(filename)
    mime = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    endpoint = (
        f"{settings.supabase_url}/storage/v1/object/"
        f"{quote(settings.supabase_storage_bucket, safe='')}/{quote(path, safe='/')}"
    )
    headers = _headers(content_type=mime)
    headers["x-upsert"] = "false"
    response = requests.post(endpoint, headers=headers, data=content, timeout=45)
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"Não foi possível guardar o arquivo do atendimento ({response.status_code}).")
    return path, admin_media_url(path, filename)


def upload_file(path: str | Path, filename: str, mime_type: str | None = None) -> tuple[str, str]:
    return upload_bytes(Path(path).read_bytes(), filename, mime_type)


def download(path: str) -> tuple[bytes, str]:
    if not configured():
        raise RuntimeError("Armazenamento do atendimento não configurado.")
    endpoint = (
        f"{settings.supabase_url}/storage/v1/object/"
        f"{quote(settings.supabase_storage_bucket, safe='')}/{quote(path, safe='/')}"
    )
    response = requests.get(endpoint, headers=_headers(), timeout=45)
    if response.status_code != 200:
        raise FileNotFoundError(path)
    return response.content, response.headers.get("content-type", "application/octet-stream")


def delete(path: str) -> None:
    if not configured():
        return
    endpoint = (
        f"{settings.supabase_url}/storage/v1/object/"
        f"{quote(settings.supabase_storage_bucket, safe='')}/{quote(path, safe='/')}"
    )
    response = requests.delete(endpoint, headers=_headers(), timeout=30)
    if response.status_code not in {200, 204, 404}:
        raise RuntimeError(f"Não foi possível remover o arquivo de teste ({response.status_code}).")


def admin_media_url(path: str, filename: str = "arquivo") -> str:
    return "/api/admin/media?" + urlencode({"path": path, "name": filename})
