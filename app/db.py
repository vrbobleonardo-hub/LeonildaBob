from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .settings import settings


POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id BIGSERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('trabalhista', 'instituto', 'geral')),
    area TEXT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    email TEXT,
    message TEXT,
    consent INTEGER NOT NULL DEFAULT 0,
    source_path TEXT,
    landing_path TEXT,
    referrer TEXT,
    visitor_id TEXT,
    session_id TEXT,
    utm_source TEXT,
    utm_medium TEXT,
    utm_campaign TEXT,
    utm_content TEXT,
    utm_term TEXT,
    gclid TEXT,
    fbclid TEXT,
    ip_hash TEXT,
    user_agent TEXT,
    request_key TEXT,
    status TEXT NOT NULL DEFAULT 'new'
);

CREATE TABLE IF NOT EXISTS lead_events (
    id BIGSERIAL PRIMARY KEY,
    lead_id BIGINT REFERENCES leads(id),
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS whatsapp_outbox (
    id BIGSERIAL PRIMARY KEY,
    lead_id BIGINT NOT NULL REFERENCES leads(id),
    created_at TEXT NOT NULL,
    sent_at TEXT,
    recipient TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    provider_message_id TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS whatsapp_conversations (
    id BIGSERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    phone TEXT NOT NULL,
    case_key TEXT NOT NULL UNIQUE,
    name TEXT,
    kind TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'archived')),
    last_message_at TEXT,
    last_message_preview TEXT,
    bot_enabled INTEGER NOT NULL DEFAULT 1,
    last_auto_reply_at TEXT,
    source_lead_id BIGINT REFERENCES leads(id)
);

CREATE TABLE IF NOT EXISTS whatsapp_messages (
    id BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES whatsapp_conversations(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    message_type TEXT NOT NULL DEFAULT 'text',
    text TEXT,
    provider_message_id TEXT,
    media_url TEXT,
    media_mime TEXT,
    media_name TEXT,
    media_size BIGINT,
    media_provider_id TEXT,
    status TEXT,
    raw_payload TEXT
);

CREATE TABLE IF NOT EXISTS page_views (
    id BIGSERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    path TEXT NOT NULL,
    referrer TEXT,
    landing_path TEXT,
    visitor_id TEXT,
    session_id TEXT,
    utm_source TEXT,
    utm_medium TEXT,
    utm_campaign TEXT,
    utm_content TEXT,
    utm_term TEXT,
    gclid TEXT,
    fbclid TEXT,
    ip_hash TEXT,
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at BIGINT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS admin_auth_events (
    id BIGSERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    username TEXT,
    success INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    ip_hash TEXT,
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at);
CREATE INDEX IF NOT EXISTS idx_leads_kind ON leads(kind);
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_request_key ON leads(request_key) WHERE request_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_outbox_status ON whatsapp_outbox(status);
CREATE INDEX IF NOT EXISTS idx_page_views_path ON page_views(path);
CREATE INDEX IF NOT EXISTS idx_page_views_created_at ON page_views(created_at);
CREATE INDEX IF NOT EXISTS idx_page_views_visitor ON page_views(visitor_id, session_id);
CREATE INDEX IF NOT EXISTS idx_page_views_origin ON page_views(utm_source, utm_campaign);
CREATE INDEX IF NOT EXISTS idx_whatsapp_conversations_phone ON whatsapp_conversations(phone);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_case_key ON whatsapp_conversations(case_key);
CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_conversation ON whatsapp_messages(conversation_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_provider_id ON whatsapp_messages(provider_message_id)
WHERE provider_message_id IS NOT NULL AND provider_message_id != '';
CREATE INDEX IF NOT EXISTS idx_admin_sessions_expiry ON admin_sessions(expires_at, revoked_at);
"""


class CursorAdapter:
    def __init__(self, cursor: Any, lastrowid: int | None = None):
        self._cursor = cursor
        self.lastrowid = lastrowid if lastrowid is not None else getattr(cursor, "lastrowid", None)

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    @property
    def rowcount(self) -> int:
        return int(getattr(self._cursor, "rowcount", 0))


class ConnectionAdapter:
    def __init__(self, raw: Any, *, postgres: bool):
        self.raw = raw
        self.postgres = postgres

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> CursorAdapter:
        if not self.postgres:
            return CursorAdapter(self.raw.execute(query, params))
        sql = query.replace("?", "%s")
        insert_match = re.match(r"\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.IGNORECASE)
        table = insert_match.group(1).lower() if insert_match else ""
        returns_id = bool(
            table
            and table not in {"admin_sessions", "schema_migrations"}
            and " RETURNING " not in sql.upper()
        )
        if returns_id:
            sql = f"{sql.rstrip().rstrip(';')} RETURNING id"
        cursor = self.raw.execute(sql, params)
        lastrowid = None
        if returns_id:
            row = cursor.fetchone()
            if row and "id" in row:
                lastrowid = int(row["id"])
        return CursorAdapter(cursor, lastrowid)

    def executescript(self, script: str) -> None:
        if self.postgres:
            self.raw.execute(POSTGRES_SCHEMA)
        else:
            self.raw.executescript(script)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_ip(ip: str | None) -> str:
    raw = f"{settings.metrics_salt}:{ip or 'unknown'}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


@contextmanager
def connect() -> Iterator[ConnectionAdapter]:
    if settings.database_url:
        import psycopg
        from psycopg.rows import dict_row

        raw = None
        for attempt in range(3):
            try:
                raw = psycopg.connect(
                    settings.database_url,
                    row_factory=dict_row,
                    connect_timeout=8,
                    prepare_threshold=None,
                )
                break
            except psycopg.OperationalError:
                if attempt == 2:
                    raise
                time.sleep(0.6 * (attempt + 1))
        if raw is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Não foi possível conectar ao banco de dados.")
        conn = ConnectionAdapter(raw, postgres=True)
    else:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(settings.db_path, timeout=15)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("PRAGMA busy_timeout = 15000")
        raw.execute("PRAGMA journal_mode = WAL")
        raw.execute("PRAGMA synchronous = NORMAL")
        conn = ConnectionAdapter(raw, postgres=False)
    try:
        yield conn
        raw.commit()
    finally:
        raw.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                area TEXT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT,
                message TEXT,
                consent INTEGER NOT NULL DEFAULT 0,
                source_path TEXT,
                landing_path TEXT,
                referrer TEXT,
                visitor_id TEXT,
                session_id TEXT,
                utm_source TEXT,
                utm_medium TEXT,
                utm_campaign TEXT,
                utm_content TEXT,
                utm_term TEXT,
                gclid TEXT,
                fbclid TEXT,
                ip_hash TEXT,
                user_agent TEXT,
                request_key TEXT,
                status TEXT NOT NULL DEFAULT 'new'
            );

            CREATE TABLE IF NOT EXISTS lead_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads(id)
            );

            CREATE TABLE IF NOT EXISTS whatsapp_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                recipient TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                provider_message_id TEXT,
                last_error TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads(id)
            );

            CREATE TABLE IF NOT EXISTS whatsapp_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                phone TEXT NOT NULL,
                case_key TEXT NOT NULL UNIQUE,
                name TEXT,
                kind TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                last_message_at TEXT,
                last_message_preview TEXT,
                bot_enabled INTEGER NOT NULL DEFAULT 1,
                last_auto_reply_at TEXT,
                source_lead_id INTEGER,
                FOREIGN KEY (source_lead_id) REFERENCES leads(id)
            );

            CREATE TABLE IF NOT EXISTS whatsapp_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                direction TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'text',
                text TEXT,
                provider_message_id TEXT,
                media_url TEXT,
                media_mime TEXT,
                media_name TEXT,
                media_size INTEGER,
                media_provider_id TEXT,
                status TEXT,
                raw_payload TEXT,
                FOREIGN KEY (conversation_id) REFERENCES whatsapp_conversations(id)
            );

            CREATE TABLE IF NOT EXISTS page_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                path TEXT NOT NULL,
                referrer TEXT,
                landing_path TEXT,
                visitor_id TEXT,
                session_id TEXT,
                utm_source TEXT,
                utm_medium TEXT,
                utm_campaign TEXT,
                utm_content TEXT,
                utm_term TEXT,
                gclid TEXT,
                fbclid TEXT,
                ip_hash TEXT,
                user_agent TEXT
            );

            CREATE TABLE IF NOT EXISTS admin_sessions (
                token_hash TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                revoked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS admin_auth_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                username TEXT,
                success INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                ip_hash TEXT,
                user_agent TEXT
            );

            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at);
            CREATE INDEX IF NOT EXISTS idx_leads_kind ON leads(kind);
            CREATE INDEX IF NOT EXISTS idx_outbox_status ON whatsapp_outbox(status);
            CREATE INDEX IF NOT EXISTS idx_page_views_path ON page_views(path);
            CREATE INDEX IF NOT EXISTS idx_whatsapp_conversations_phone ON whatsapp_conversations(phone);
            CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_conversation ON whatsapp_messages(conversation_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_page_views_created_at ON page_views(created_at);
            CREATE INDEX IF NOT EXISTS idx_page_views_visitor ON page_views(visitor_id, session_id);
            CREATE INDEX IF NOT EXISTS idx_admin_sessions_expiry ON admin_sessions(expires_at, revoked_at);
            """
        )
        if not conn.postgres:
            migrate_conversations_schema(conn)
        ensure_columns(
            conn,
            "leads",
            {
                "area": "TEXT",
                "landing_path": "TEXT",
                "referrer": "TEXT",
                "visitor_id": "TEXT",
                "session_id": "TEXT",
                "utm_source": "TEXT",
                "utm_medium": "TEXT",
                "utm_campaign": "TEXT",
                "utm_content": "TEXT",
                "utm_term": "TEXT",
                "gclid": "TEXT",
                "fbclid": "TEXT",
                "request_key": "TEXT",
            },
        )
        ensure_columns(
            conn,
            "page_views",
            {
                "landing_path": "TEXT",
                "visitor_id": "TEXT",
                "session_id": "TEXT",
                "utm_source": "TEXT",
                "utm_medium": "TEXT",
                "utm_campaign": "TEXT",
                "utm_content": "TEXT",
                "utm_term": "TEXT",
                "gclid": "TEXT",
                "fbclid": "TEXT",
            },
        )
        ensure_columns(conn, "whatsapp_outbox", {"provider_message_id": "TEXT"})
        ensure_columns(
            conn,
            "whatsapp_messages",
            {
                "media_url": "TEXT",
                "media_mime": "TEXT",
                "media_name": "TEXT",
                "media_size": "INTEGER",
                "media_provider_id": "TEXT",
            },
        )
        ensure_columns(
            conn,
            "whatsapp_conversations",
            {
                "case_key": "TEXT",
                "last_auto_reply_at": "TEXT",
            },
        )
        conn.execute("UPDATE whatsapp_conversations SET case_key = 'legacy-' || id WHERE case_key IS NULL OR case_key = ''")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_request_key ON leads(request_key) WHERE request_key IS NOT NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_case_key ON whatsapp_conversations(case_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_whatsapp_conversations_phone ON whatsapp_conversations(phone)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_whatsapp_conversations_updated ON whatsapp_conversations(updated_at DESC)"
        )
        deduplicate_provider_messages(conn)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_provider_id ON whatsapp_messages(provider_message_id) "
            "WHERE provider_message_id IS NOT NULL AND provider_message_id != ''"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_page_views_origin ON page_views(utm_source, utm_campaign)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_page_views_created_at ON page_views(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_page_views_visitor ON page_views(visitor_id, session_id)")
        if conn.postgres:
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?) "
                "ON CONFLICT (version) DO NOTHING",
                ("2026-07-03-production-hardening", utc_now()),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                ("2026-07-03-production-hardening", utc_now()),
            )
            install_integrity_triggers(conn)
    cleanup_expired_data()


def migrate_conversations_schema(conn: ConnectionAdapter) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(whatsapp_conversations)").fetchall()}
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'whatsapp_conversations'"
    ).fetchone()
    schema_sql = str(schema_row["sql"] or "") if schema_row else ""
    if "case_key" in columns and "phone TEXT NOT NULL UNIQUE" not in schema_sql:
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE whatsapp_conversations_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                phone TEXT NOT NULL,
                case_key TEXT NOT NULL UNIQUE,
                name TEXT,
                kind TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                last_message_at TEXT,
                last_message_preview TEXT,
                bot_enabled INTEGER NOT NULL DEFAULT 1,
                last_auto_reply_at TEXT,
                source_lead_id INTEGER,
                FOREIGN KEY (source_lead_id) REFERENCES leads(id)
            );
            INSERT INTO whatsapp_conversations_new (
                id, created_at, updated_at, phone, case_key, name, kind, status,
                last_message_at, last_message_preview, bot_enabled, last_auto_reply_at, source_lead_id
            )
            SELECT id, created_at, updated_at, phone, 'legacy-' || id, name, kind, status,
                   last_message_at, last_message_preview, bot_enabled, NULL, source_lead_id
            FROM whatsapp_conversations;

            CREATE TABLE whatsapp_messages_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                direction TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'text',
                text TEXT,
                provider_message_id TEXT,
                media_url TEXT,
                media_mime TEXT,
                media_name TEXT,
                media_size INTEGER,
                media_provider_id TEXT,
                status TEXT,
                raw_payload TEXT,
                FOREIGN KEY (conversation_id) REFERENCES whatsapp_conversations_new(id) ON DELETE CASCADE
            );
            INSERT INTO whatsapp_messages_new (
                id, conversation_id, created_at, direction, message_type, text,
                provider_message_id, media_url, media_mime, media_name, media_size,
                media_provider_id, status, raw_payload
            )
            SELECT id, conversation_id, created_at, direction, message_type, text,
                   provider_message_id, media_url, media_mime, media_name, media_size,
                   media_provider_id, status, raw_payload
            FROM whatsapp_messages;

            DROP TABLE whatsapp_messages;
            DROP TABLE whatsapp_conversations;
            ALTER TABLE whatsapp_conversations_new RENAME TO whatsapp_conversations;
            ALTER TABLE whatsapp_messages_new RENAME TO whatsapp_messages;
            COMMIT;
            """
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def deduplicate_provider_messages(conn: ConnectionAdapter) -> None:
    duplicates = conn.execute(
        """
        SELECT provider_message_id
        FROM whatsapp_messages
        WHERE provider_message_id IS NOT NULL AND provider_message_id != ''
        GROUP BY provider_message_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in duplicates:
        ids = conn.execute(
            "SELECT id FROM whatsapp_messages WHERE provider_message_id = ? ORDER BY id",
            (row["provider_message_id"],),
        ).fetchall()
        for duplicate in ids[1:]:
            conn.execute(
                "UPDATE whatsapp_messages SET provider_message_id = NULL, status = 'duplicate' WHERE id = ?",
                (duplicate["id"],),
            )


def install_integrity_triggers(conn: ConnectionAdapter) -> None:
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS validate_lead_kind_insert
        BEFORE INSERT ON leads
        WHEN NEW.kind NOT IN ('trabalhista', 'instituto', 'geral')
        BEGIN SELECT RAISE(ABORT, 'invalid lead kind'); END;

        CREATE TRIGGER IF NOT EXISTS validate_message_direction_insert
        BEFORE INSERT ON whatsapp_messages
        WHEN NEW.direction NOT IN ('in', 'out')
        BEGIN SELECT RAISE(ABORT, 'invalid message direction'); END;

        CREATE TRIGGER IF NOT EXISTS validate_conversation_status_update
        BEFORE UPDATE OF status ON whatsapp_conversations
        WHEN NEW.status NOT IN ('open', 'closed', 'archived')
        BEGIN SELECT RAISE(ABORT, 'invalid conversation status'); END;
        """
    )


def ensure_columns(conn: ConnectionAdapter, table: str, columns: dict[str, str]) -> None:
    if conn.postgres:
        for name, definition in columns.items():
            pg_definition = "BIGINT" if definition == "INTEGER" and name == "media_size" else definition
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {pg_definition}")
        return
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def insert_lead(data: dict[str, Any]) -> int:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO leads (
                created_at, kind, area, name, phone, email, message, consent,
                source_path, landing_path, referrer, visitor_id, session_id,
                utm_source, utm_medium, utm_campaign, utm_content, utm_term,
                gclid, fbclid, ip_hash, user_agent, request_key, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                data["kind"],
                data.get("area"),
                data["name"],
                data["phone"],
                data.get("email"),
                data.get("message"),
                1 if data.get("consent") else 0,
                data.get("source_path"),
                data.get("landing_path"),
                data.get("referrer"),
                data.get("visitor_id"),
                data.get("session_id"),
                data.get("utm_source"),
                data.get("utm_medium"),
                data.get("utm_campaign"),
                data.get("utm_content"),
                data.get("utm_term"),
                data.get("gclid"),
                data.get("fbclid"),
                data.get("ip_hash"),
                data.get("user_agent"),
                data.get("request_key"),
                "new",
            ),
        )
        lead_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO lead_events (lead_id, created_at, event_type, payload) VALUES (?, ?, ?, ?)",
            (lead_id, utc_now(), "lead.created", data.get("source_path") or ""),
        )
        return lead_id


def get_lead_by_request_key(request_key: str | None) -> dict[str, Any] | None:
    if not request_key:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT id, phone, status FROM leads WHERE request_key = ?",
            (request_key,),
        ).fetchone()
        return dict(row) if row else None


def create_lead_bundle(data: dict[str, Any], first_message: str) -> tuple[int, int, int]:
    """Create the lead, outbox row and isolated case conversation atomically."""
    with connect() as conn:
        existing = None
        if data.get("request_key"):
            existing = conn.execute(
                "SELECT id, phone FROM leads WHERE request_key = ?",
                (data["request_key"],),
            ).fetchone()
        if existing:
            outbox = conn.execute(
                "SELECT id FROM whatsapp_outbox WHERE lead_id = ? ORDER BY id DESC LIMIT 1",
                (existing["id"],),
            ).fetchone()
            conversation = conn.execute(
                "SELECT id FROM whatsapp_conversations WHERE source_lead_id = ? ORDER BY id DESC LIMIT 1",
                (existing["id"],),
            ).fetchone()
            if outbox and conversation:
                return int(existing["id"]), int(outbox["id"]), int(conversation["id"])

        lead_cursor = conn.execute(
            """
            INSERT INTO leads (
                created_at, kind, area, name, phone, email, message, consent,
                source_path, landing_path, referrer, visitor_id, session_id,
                utm_source, utm_medium, utm_campaign, utm_content, utm_term,
                gclid, fbclid, ip_hash, user_agent, request_key, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
            """,
            (
                utc_now(),
                data["kind"],
                data.get("area"),
                data["name"],
                data["phone"],
                data.get("email"),
                data.get("message"),
                1 if data.get("consent") else 0,
                data.get("source_path"),
                data.get("landing_path"),
                data.get("referrer"),
                data.get("visitor_id"),
                data.get("session_id"),
                data.get("utm_source"),
                data.get("utm_medium"),
                data.get("utm_campaign"),
                data.get("utm_content"),
                data.get("utm_term"),
                data.get("gclid"),
                data.get("fbclid"),
                data.get("ip_hash"),
                data.get("user_agent"),
                data.get("request_key"),
            ),
        )
        lead_id = int(lead_cursor.lastrowid)
        conn.execute(
            "INSERT INTO lead_events (lead_id, created_at, event_type, payload) VALUES (?, ?, ?, ?)",
            (lead_id, utc_now(), "lead.created", data.get("source_path") or ""),
        )
        outbox_cursor = conn.execute(
            """
            INSERT INTO whatsapp_outbox (lead_id, created_at, recipient, message, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (lead_id, utc_now(), data["phone"], first_message),
        )
        conversation_cursor = conn.execute(
            """
            INSERT INTO whatsapp_conversations (
                created_at, updated_at, phone, case_key, name, kind, source_lead_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                utc_now(),
                data["phone"],
                f"lead-{lead_id}",
                data["name"],
                data["kind"],
                lead_id,
            ),
        )
        return lead_id, int(outbox_cursor.lastrowid), int(conversation_cursor.lastrowid)


def enqueue_whatsapp(lead_id: int, recipient: str, message: str, status: str = "pending", error: str | None = None) -> int:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO whatsapp_outbox (
                lead_id, created_at, recipient, message, status, last_error
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (lead_id, utc_now(), recipient, message, status, error),
        )
        return int(cursor.lastrowid)


def get_or_create_conversation(phone: str, *, name: str = "", kind: str = "", source_lead_id: int | None = None) -> int:
    with connect() as conn:
        if source_lead_id:
            row = conn.execute(
                "SELECT id FROM whatsapp_conversations WHERE source_lead_id = ? ORDER BY id DESC LIMIT 1",
                (source_lead_id,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id FROM whatsapp_conversations
                WHERE phone = ? AND status = 'open'
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (phone,),
            ).fetchone()
        now = utc_now()
        if row:
            conn.execute(
                """
                UPDATE whatsapp_conversations
                SET updated_at = ?, name = COALESCE(NULLIF(?, ''), name),
                    kind = COALESCE(NULLIF(?, ''), kind),
                    source_lead_id = COALESCE(source_lead_id, ?)
                WHERE id = ?
                """,
                (now, name, kind, source_lead_id, row["id"]),
            )
            return int(row["id"])
        cursor = conn.execute(
            """
            INSERT INTO whatsapp_conversations (
                created_at, updated_at, phone, case_key, name, kind, source_lead_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                phone,
                f"lead-{source_lead_id}" if source_lead_id else f"inbound-{uuid.uuid4().hex}",
                name or None,
                kind or None,
                source_lead_id,
            ),
        )
        return int(cursor.lastrowid)


def record_whatsapp_message(
    conversation_id: int,
    *,
    direction: str,
    text: str = "",
    message_type: str = "text",
    provider_message_id: str | None = None,
    media_url: str | None = None,
    media_mime: str | None = None,
    media_name: str | None = None,
    media_size: int | None = None,
    media_provider_id: str | None = None,
    status: str | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> int:
    preview = (text or media_name or message_type or "").strip().replace("\n", " ")[:180]
    now = utc_now()
    with connect() as conn:
        if provider_message_id:
            existing = conn.execute(
                "SELECT id FROM whatsapp_messages WHERE provider_message_id = ?",
                (provider_message_id,),
            ).fetchone()
            if existing:
                return int(existing["id"])
        cursor = conn.execute(
            """
            INSERT INTO whatsapp_messages (
                conversation_id, created_at, direction, message_type, text,
                provider_message_id, media_url, media_mime, media_name, media_size,
                media_provider_id, status, raw_payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                now,
                direction,
                message_type,
                text,
                provider_message_id,
                media_url,
                media_mime,
                media_name,
                media_size,
                media_provider_id,
                status,
                json.dumps(raw_payload or {}, ensure_ascii=False),
            ),
        )
        conn.execute(
            """
            UPDATE whatsapp_conversations
            SET updated_at = ?, last_message_at = ?, last_message_preview = ?
            WHERE id = ?
            """,
            (now, now, preview, conversation_id),
        )
        return int(cursor.lastrowid)


def list_conversations(limit: int = 80, offset: int = 0, query: str = "") -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    with connect() as conn:
        sql = """
            SELECT id, created_at, updated_at, phone, name, kind, status,
                   last_message_at, last_message_preview, bot_enabled, source_lead_id
            FROM whatsapp_conversations
        """
        params: list[Any] = []
        if query.strip():
            sql += " WHERE name LIKE ? OR phone LIKE ? OR kind LIKE ?"
            needle = f"%{query.strip()[:80]}%"
            params.extend([needle, needle, needle])
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def get_conversation(conversation_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, created_at, updated_at, phone, name, kind, status,
                   last_message_at, last_message_preview, bot_enabled, source_lead_id
            FROM whatsapp_conversations
            WHERE id = ?
            """,
            (conversation_id,),
        ).fetchone()
        return dict(row) if row else None


def list_messages(conversation_id: int, limit: int = 160) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT id, conversation_id, created_at, direction, message_type, text,
                       provider_message_id, media_url, media_mime, media_name, media_size,
                       media_provider_id, status
                FROM whatsapp_messages
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT ?
            ) recent
            ORDER BY id ASC
            """,
            (conversation_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def update_outbox_provider_status(provider_message_id: str, status: str) -> None:
    if not provider_message_id:
        return
    with connect() as conn:
        conn.execute(
            """
            UPDATE whatsapp_outbox
            SET status = ?
            WHERE provider_message_id = ?
            """,
            (status[:50], provider_message_id),
        )
        conn.execute(
            "UPDATE whatsapp_messages SET status = ? WHERE provider_message_id = ?",
            (status[:50], provider_message_id),
        )


def mark_outbox_sent(outbox_id: int, status: str = "sent", provider_message_id: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE whatsapp_outbox
            SET status = ?,
                sent_at = CASE WHEN ? = 'dry_run' THEN sent_at ELSE ? END,
                attempts = attempts + 1,
                provider_message_id = COALESCE(NULLIF(?, ''), provider_message_id),
                last_error = NULL
            WHERE id = ?
            """,
            (status, status, utc_now(), provider_message_id or "", outbox_id),
        )


def mark_outbox_failed(outbox_id: int, error: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE whatsapp_outbox
            SET status = 'pending', attempts = attempts + 1, last_error = ?
            WHERE id = ?
            """,
            (error[:500], outbox_id),
        )


def update_lead_status(lead_id: int, status: str) -> bool:
    if status not in {"new", "contacted", "qualified", "closed", "archived"}:
        raise ValueError("Status de contato inválido.")
    with connect() as conn:
        cursor = conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
        if cursor.rowcount:
            conn.execute(
                "INSERT INTO lead_events (lead_id, created_at, event_type, payload) VALUES (?, ?, ?, ?)",
                (lead_id, utc_now(), "lead.status_changed", status),
            )
        return bool(cursor.rowcount)


def update_conversation_controls(
    conversation_id: int,
    *,
    status: str | None = None,
    bot_enabled: bool | None = None,
) -> dict[str, Any] | None:
    if status is not None and status not in {"open", "closed", "archived"}:
        raise ValueError("Status de conversa inválido.")
    with connect() as conn:
        current = conn.execute(
            "SELECT id, status, bot_enabled FROM whatsapp_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if not current:
            return None
        conn.execute(
            """
            UPDATE whatsapp_conversations
            SET status = ?, bot_enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status if status is not None else current["status"],
                int(bot_enabled) if bot_enabled is not None else current["bot_enabled"],
                utc_now(),
                conversation_id,
            ),
        )
        updated = conn.execute(
            "SELECT id, status, bot_enabled FROM whatsapp_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        return dict(updated) if updated else None


def can_auto_reply(conversation_id: int, cooldown_seconds: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT status, bot_enabled, last_auto_reply_at FROM whatsapp_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if not row or row["status"] != "open" or not row["bot_enabled"]:
            return False
        last = row["last_auto_reply_at"]
        if last:
            try:
                last_time = datetime.fromisoformat(last)
                if datetime.now(timezone.utc) - last_time < timedelta(seconds=cooldown_seconds):
                    return False
            except ValueError:
                pass
        return True


def mark_auto_reply(conversation_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE whatsapp_conversations SET last_auto_reply_at = ? WHERE id = ?",
            (utc_now(), conversation_id),
        )


def record_page_view(
    path: str,
    referrer: str | None,
    ip_hash_value: str,
    user_agent: str | None,
    origin: dict[str, Any] | None = None,
) -> None:
    origin = origin or {}
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO page_views (
                created_at, path, referrer, landing_path, visitor_id, session_id,
                utm_source, utm_medium, utm_campaign, utm_content, utm_term,
                gclid, fbclid, ip_hash, user_agent
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                path[:300],
                (referrer or "")[:500],
                str(origin.get("landing_path") or "")[:300],
                str(origin.get("visitor_id") or "")[:80],
                str(origin.get("session_id") or "")[:80],
                str(origin.get("utm_source") or "")[:120],
                str(origin.get("utm_medium") or "")[:120],
                str(origin.get("utm_campaign") or "")[:180],
                str(origin.get("utm_content") or "")[:180],
                str(origin.get("utm_term") or "")[:180],
                str(origin.get("gclid") or "")[:240],
                str(origin.get("fbclid") or "")[:240],
                ip_hash_value,
                (user_agent or "")[:500],
            ),
        )


def admin_snapshot(limit: int = 50) -> dict[str, Any]:
    with connect() as conn:
        totals = conn.execute(
            """
            SELECT
                COUNT(*) AS leads_total,
                SUM(CASE WHEN kind = 'trabalhista' THEN 1 ELSE 0 END) AS trabalhista_total,
                SUM(CASE WHEN kind = 'instituto' THEN 1 ELSE 0 END) AS instituto_total
            FROM leads
            """
        ).fetchone()
        outbox = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN status = 'dry_run' THEN 1 ELSE 0 END) AS dry_run
            FROM whatsapp_outbox
            """
        ).fetchone()
        recent_leads = conn.execute(
            """
            SELECT id, created_at, kind, area, name, phone, email, message, consent,
                   source_path, landing_path, referrer, utm_source, utm_medium,
                   utm_campaign, utm_content, utm_term, gclid, fbclid, status
            FROM leads
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        recent_outbox = conn.execute(
            """
            SELECT o.id, o.created_at, o.sent_at, o.recipient, o.status, o.attempts, o.last_error, l.name
            FROM whatsapp_outbox o
            JOIN leads l ON l.id = o.lead_id
            ORDER BY o.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        page_views = conn.execute(
            """
            SELECT path, COUNT(*) AS views
            FROM page_views
            GROUP BY path
            ORDER BY views DESC
            LIMIT 10
            """
        ).fetchall()
        origins = conn.execute(
            """
            SELECT
                COALESCE(NULLIF(utm_source, ''), 'direto') AS source,
                COALESCE(NULLIF(utm_campaign, ''), 'sem campanha') AS campaign,
                COUNT(*) AS visits
            FROM page_views
            GROUP BY source, campaign
            ORDER BY visits DESC
            LIMIT 12
            """
        ).fetchall()
        conversations = conn.execute(
            """
            SELECT id, updated_at, phone, name, kind, status, last_message_preview, bot_enabled
            FROM whatsapp_conversations
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        messages = conn.execute(
            """
            SELECT m.id, m.created_at, m.direction, m.message_type, m.text, m.status,
                   m.media_url, m.media_mime, m.media_name, m.media_size, c.phone, c.name
            FROM whatsapp_messages m
            JOIN whatsapp_conversations c ON c.id = m.conversation_id
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return {
            "totals": dict(totals or {}),
            "outbox": dict(outbox or {}),
            "recent_leads": [dict(row) for row in recent_leads],
            "recent_outbox": [dict(row) for row in recent_outbox],
            "page_views": [dict(row) for row in page_views],
            "origins": [dict(row) for row in origins],
            "conversations": [dict(row) for row in conversations],
            "messages": [dict(row) for row in messages],
        }


def get_outbox_message(outbox_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, recipient, message FROM whatsapp_outbox WHERE id = ?",
            (outbox_id,),
        ).fetchone()
        return dict(row) if row else None


def create_admin_session(token_hash: str, username: str, expires_at: int) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO admin_sessions(token_hash, username, created_at, expires_at, revoked_at)
            VALUES (?, ?, ?, ?, NULL)
            """,
            (token_hash, username, utc_now(), expires_at),
        )


def admin_session_is_active(token_hash: str, username: str, now_epoch: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT token_hash FROM admin_sessions
            WHERE token_hash = ? AND username = ? AND revoked_at IS NULL AND expires_at >= ?
            """,
            (token_hash, username, now_epoch),
        ).fetchone()
        return bool(row)


def revoke_admin_session(token_hash: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE admin_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (utc_now(), token_hash),
        )


def record_auth_event(
    *,
    username: str,
    success: bool,
    event_type: str,
    ip_hash_value: str,
    user_agent: str,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO admin_auth_events(
                created_at, username, success, event_type, ip_hash, user_agent
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                username[:120],
                int(success),
                event_type[:50],
                ip_hash_value,
                user_agent[:500],
            ),
        )


def cleanup_expired_data() -> None:
    analytics_cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.analytics_retention_days)).isoformat(
        timespec="seconds"
    )
    session_cutoff = int(datetime.now(timezone.utc).timestamp())
    with connect() as conn:
        conn.execute("DELETE FROM page_views WHERE created_at < ?", (analytics_cutoff,))
        conn.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (session_cutoff,))


def delete_contact_data(phone: str) -> dict[str, int]:
    """Delete one contact's cases, messages and lead history for a verified privacy request."""
    with connect() as conn:
        lead_rows = conn.execute("SELECT id FROM leads WHERE phone = ?", (phone,)).fetchall()
        lead_ids = [int(row["id"]) for row in lead_rows]
        conversation_rows = conn.execute(
            "SELECT id FROM whatsapp_conversations WHERE phone = ?", (phone,)
        ).fetchall()
        conversation_ids = [int(row["id"]) for row in conversation_rows]
        message_count = 0
        if conversation_ids:
            placeholders = ",".join("?" for _ in conversation_ids)
            message_count = conn.execute(
                f"SELECT COUNT(*) AS count FROM whatsapp_messages WHERE conversation_id IN ({placeholders})",
                conversation_ids,
            ).fetchone()["count"]
            conn.execute(
                f"DELETE FROM whatsapp_messages WHERE conversation_id IN ({placeholders})",
                conversation_ids,
            )
            conn.execute(
                f"DELETE FROM whatsapp_conversations WHERE id IN ({placeholders})",
                conversation_ids,
            )
        if lead_ids:
            placeholders = ",".join("?" for _ in lead_ids)
            conn.execute(f"DELETE FROM lead_events WHERE lead_id IN ({placeholders})", lead_ids)
            conn.execute(f"DELETE FROM whatsapp_outbox WHERE lead_id IN ({placeholders})", lead_ids)
            conn.execute(f"DELETE FROM leads WHERE id IN ({placeholders})", lead_ids)
        return {
            "leads": len(lead_ids),
            "conversations": len(conversation_ids),
            "messages": int(message_count),
        }
