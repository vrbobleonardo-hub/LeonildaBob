from __future__ import annotations

import io
import mimetypes
import re
import shutil
# ClamAV is invoked directly without a shell.
import subprocess  # nosec B404
import uuid
import zipfile
from pathlib import Path

from fastapi import HTTPException, UploadFile

from .settings import settings


ALLOWED_UPLOADS: dict[str, set[str]] = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
    "image/gif": {".gif"},
    "audio/aac": {".aac"},
    "audio/amr": {".amr"},
    "audio/mpeg": {".mp3"},
    "audio/mp4": {".m4a", ".mp4"},
    "audio/ogg": {".ogg", ".oga", ".opus"},
    "video/mp4": {".mp4", ".m4v"},
    "video/3gpp": {".3gp", ".3gpp"},
    "application/pdf": {".pdf"},
    "application/msword": {".doc"},
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {".docx"},
    "application/vnd.ms-excel": {".xls"},
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {".xlsx"},
    "text/plain": {".txt"},
}
OFFICE_ARCHIVE_MARKERS = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word/document.xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xl/workbook.xml",
}


def _ensure_private_directory() -> None:
    settings.private_upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)


def safe_display_filename(filename: str) -> str:
    base = Path(filename or "arquivo").name
    base = re.sub(r"[^A-Za-z0-9À-ÿ._ -]+", "-", base).strip(" .-")
    if not base:
        return "arquivo"
    suffix = Path(base).suffix[:12]
    if suffix:
        stem = base[: -len(Path(base).suffix)].rstrip(" .-") or "arquivo"
        return f"{stem[: 160 - len(suffix)]}{suffix}"
    return base[:160]


def _matches_signature(mime_type: str, header: bytes) -> bool:
    if mime_type == "image/jpeg":
        return header.startswith(b"\xff\xd8\xff")
    if mime_type == "image/png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/webp":
        return header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    if mime_type == "image/gif":
        return header.startswith((b"GIF87a", b"GIF89a"))
    if mime_type == "application/pdf":
        return header.startswith(b"%PDF-")
    if mime_type in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        return header.startswith(b"PK\x03\x04")
    if mime_type in {"application/msword", "application/vnd.ms-excel"}:
        return header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    if mime_type == "audio/amr":
        return header.startswith((b"#!AMR\n", b"#!AMR-WB\n"))
    if mime_type == "audio/aac":
        return len(header) >= 2 and header[0] == 0xFF and header[1] & 0xF6 == 0xF0
    if mime_type == "audio/mpeg":
        return header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0)
    if mime_type == "audio/ogg":
        return header.startswith(b"OggS")
    if mime_type in {"audio/mp4", "video/mp4", "video/3gpp"}:
        return len(header) >= 12 and header[4:8] == b"ftyp"
    if mime_type == "text/plain":
        if b"\x00" in header:
            return False
        try:
            header.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False
    return False


def _valid_office_archive(source: Path | io.BytesIO, mime_type: str) -> bool:
    marker = OFFICE_ARCHIVE_MARKERS.get(mime_type)
    if not marker:
        return True
    try:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > 2_000:
                return False
            if any(entry.flag_bits & 0x1 for entry in entries):
                return False
            if sum(entry.file_size for entry in entries) > 250 * 1024 * 1024:
                return False
            names = {entry.filename for entry in entries}
            return "[Content_Types].xml" in names and marker in names
    except (OSError, ValueError, zipfile.BadZipFile):
        return False


def _scan_for_malware(path: Path) -> None:
    scanner = shutil.which("clamscan")
    if not scanner:
        if settings.require_virus_scan:
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=503, detail="Verificação de arquivos indisponível.")
        return
    try:
        # The executable is resolved from the server-controlled PATH.
        result = subprocess.run(  # nosec B603
            [scanner, "--no-summary", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=503, detail="A verificação do arquivo excedeu o tempo permitido."
        ) from exc
    if result.returncode == 1:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="O arquivo não passou pela verificação de segurança.")
    if result.returncode != 0:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=503, detail="Não foi possível verificar a segurança do arquivo.")


def save_validated_upload(upload: UploadFile) -> tuple[Path, str, str, int]:
    _ensure_private_directory()
    original_name = safe_display_filename(upload.filename or "arquivo")
    extension = Path(original_name).suffix.lower()
    declared_mime = (
        upload.content_type or mimetypes.guess_type(original_name)[0] or ""
    ).split(";", 1)[0].strip().lower()
    allowed_extensions = ALLOWED_UPLOADS.get(declared_mime)
    if not allowed_extensions or extension not in allowed_extensions:
        raise HTTPException(status_code=400, detail="Tipo ou extensão de arquivo não aceito.")

    identifier = uuid.uuid4().hex
    temporary = settings.private_upload_dir / f".{identifier}.upload"
    destination = settings.private_upload_dir / f"{identifier}{extension}"
    size = 0
    header = b""
    try:
        with temporary.open("xb") as output:
            temporary.chmod(0o600)
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                if len(header) < 16_384:
                    header += chunk[: 16_384 - len(header)]
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    max_megabytes = settings.max_upload_bytes // (1024 * 1024)
                    raise HTTPException(
                        status_code=400,
                        detail=f"Arquivo muito grande. Use até {max_megabytes} MB.",
                    )
                output.write(chunk)
        if size == 0 or not _matches_signature(declared_mime, header):
            raise HTTPException(status_code=400, detail="O conteúdo do arquivo não corresponde ao tipo informado.")
        temporary.replace(destination)
        destination.chmod(0o600)
        if not _valid_office_archive(destination, declared_mime):
            raise HTTPException(status_code=400, detail="O documento do Office está corrompido ou é inválido.")
        _scan_for_malware(destination)
        return destination, original_name, declared_mime, size
    except Exception:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise


def save_validated_bytes(content: bytes, filename: str, mime_type: str) -> tuple[Path, str, str, int]:
    original_name = safe_display_filename(filename)
    extension = Path(original_name).suffix.lower()
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if mime not in ALLOWED_UPLOADS or extension not in ALLOWED_UPLOADS[mime]:
        raise HTTPException(status_code=400, detail="Tipo de arquivo recebido não aceito.")
    if not content or len(content) > settings.max_upload_bytes or not _matches_signature(mime, content[:16_384]):
        raise HTTPException(status_code=400, detail="Conteúdo de arquivo recebido inválido.")
    if not _valid_office_archive(io.BytesIO(content), mime):
        raise HTTPException(status_code=400, detail="O documento do Office recebido é inválido.")
    _ensure_private_directory()
    destination = settings.private_upload_dir / f"{uuid.uuid4().hex}{extension}"
    try:
        destination.write_bytes(content)
        destination.chmod(0o600)
        _scan_for_malware(destination)
        return destination, original_name, mime, len(content)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
