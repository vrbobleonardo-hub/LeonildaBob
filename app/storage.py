from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .settings import settings


def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "DELETE"}),
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


HTTP = _session()


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
    bucket_id = quote(settings.supabase_storage_bucket, safe="")
    existing = HTTP.get(
        f"{settings.supabase_url}/storage/v1/bucket/{bucket_id}",
        headers=_headers(),
        timeout=15,
    )
    if existing.status_code == 200:
        try:
            bucket = existing.json()
        except ValueError as exc:
            raise RuntimeError("O armazenamento retornou uma configuração inválida.") from exc
        if not isinstance(bucket, dict) or bucket.get("public") is True:
            raise RuntimeError("O bucket do atendimento deve permanecer privado.")
        return True
    if existing.status_code != 404:
        raise RuntimeError(
            f"Não foi possível verificar os arquivos do atendimento ({existing.status_code})."
        )
    endpoint = f"{settings.supabase_url}/storage/v1/bucket"
    response = HTTP.post(
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
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"Não foi possível preparar os arquivos do atendimento ({response.status_code}).")
    return True


def object_name(filename: str) -> str:
    extension = Path(filename or "arquivo").suffix.lower()[:12]
    return f"whatsapp/{uuid.uuid4().hex}{extension}"


def upload_file(path: str | Path, filename: str, mime_type: str | None = None) -> tuple[str, str]:
    if not configured():
        raise RuntimeError("Armazenamento do atendimento não configurado.")
    file_path = Path(path)
    object_path = object_name(filename)
    mime = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    endpoint = (
        f"{settings.supabase_url}/storage/v1/object/"
        f"{quote(settings.supabase_storage_bucket, safe='')}/{quote(object_path, safe='/')}"
    )
    headers = _headers(content_type=mime)
    headers["x-upsert"] = "false"
    with file_path.open("rb") as source:
        response = HTTP.post(endpoint, headers=headers, data=source, timeout=60)
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"Não foi possível guardar o arquivo do atendimento ({response.status_code}).")
    return object_path, admin_media_url(object_path, filename)


def download(path: str) -> tuple[bytes, str]:
    if not configured():
        raise RuntimeError("Armazenamento do atendimento não configurado.")
    endpoint = (
        f"{settings.supabase_url}/storage/v1/object/"
        f"{quote(settings.supabase_storage_bucket, safe='')}/{quote(path, safe='/')}"
    )
    with HTTP.get(endpoint, headers=_headers(), timeout=45, stream=True) as response:
        if response.status_code != 200:
            raise FileNotFoundError(path)
        try:
            content_length = int(response.headers.get("content-length") or 0)
        except ValueError:
            content_length = 0
        if content_length > settings.max_upload_bytes:
            raise RuntimeError("Arquivo armazenado excede o limite permitido.")
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(1024 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > settings.max_upload_bytes:
                raise RuntimeError("Arquivo armazenado excede o limite permitido.")
            chunks.append(chunk)
        return b"".join(chunks), response.headers.get("content-type", "application/octet-stream")


def delete(path: str) -> None:
    if not configured():
        return
    endpoint = (
        f"{settings.supabase_url}/storage/v1/object/"
        f"{quote(settings.supabase_storage_bucket, safe='')}/{quote(path, safe='/')}"
    )
    response = HTTP.delete(endpoint, headers=_headers(), timeout=30)
    if response.status_code not in {200, 204, 404}:
        raise RuntimeError(f"Não foi possível remover o arquivo do atendimento ({response.status_code}).")


def admin_media_url(path: str, filename: str = "arquivo") -> str:
    return "/api/admin/media?" + urlencode({"path": path, "name": filename})
