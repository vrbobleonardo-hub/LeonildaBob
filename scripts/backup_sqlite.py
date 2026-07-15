from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_local_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise SystemExit("O arquivo .env deve usar permissão 600.")
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def main() -> None:
    load_local_env()
    if os.getenv("DATABASE_URL", "").strip():
        raise SystemExit("Este script é apenas para SQLite. Use o backup gerenciado do PostgreSQL.")
    source = Path(os.getenv("LEONILDA_DB_PATH", ROOT / "data/leonilda.sqlite"))
    if not source.is_absolute():
        source = ROOT / source
    if not source.exists():
        raise SystemExit(f"Banco não encontrado: {source}")
    backup_dir = ROOT / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"leonilda-{timestamp}.sqlite"
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as destination_db:
        source_db.backup(destination_db)
        integrity = destination_db.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            destination.unlink(missing_ok=True)
            raise SystemExit("O backup não passou pela verificação de integridade.")
    destination.chmod(0o600)
    print(destination)


if __name__ == "__main__":
    main()
