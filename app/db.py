from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .settings import settings


_POSTGRES_POOL: Any = None
_POOL_LOCK = threading.Lock()


POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id BIGSERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('trabalhista', 'instituto', 'bpc', 'geral')),
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
    last_error TEXT,
    next_attempt_at TEXT
);

CREATE TABLE IF NOT EXISTS whatsapp_contact_preferences (
    phone TEXT PRIMARY KEY,
    opted_out_at TEXT NOT NULL,
    source TEXT NOT NULL,
    reason TEXT,
    updated_at TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS whatsapp_webhook_events (
    id BIGSERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    processed_at TEXT,
    payload_hash TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT
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

CREATE TABLE IF NOT EXISTS blog_posts (
    id BIGSERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT,
    title TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    excerpt TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT NOT NULL,
    author_name TEXT NOT NULL DEFAULT 'Leonilda Bob',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published'))
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at);
CREATE INDEX IF NOT EXISTS idx_leads_kind ON leads(kind);
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_request_key ON leads(request_key) WHERE request_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_outbox_status ON whatsapp_outbox(status);
CREATE INDEX IF NOT EXISTS idx_whatsapp_preferences_opted_out ON whatsapp_contact_preferences(opted_out_at DESC);
CREATE INDEX IF NOT EXISTS idx_page_views_path ON page_views(path);
CREATE INDEX IF NOT EXISTS idx_page_views_created_at ON page_views(created_at);
CREATE INDEX IF NOT EXISTS idx_page_views_visitor ON page_views(visitor_id, session_id);
CREATE INDEX IF NOT EXISTS idx_page_views_origin ON page_views(utm_source, utm_campaign);
CREATE INDEX IF NOT EXISTS idx_whatsapp_conversations_phone ON whatsapp_conversations(phone);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_case_key ON whatsapp_conversations(case_key);
CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_conversation ON whatsapp_messages(conversation_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_provider_id ON whatsapp_messages(provider_message_id)
WHERE provider_message_id IS NOT NULL AND provider_message_id != '';
CREATE INDEX IF NOT EXISTS idx_webhook_events_pending ON whatsapp_webhook_events(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_expiry ON admin_sessions(expires_at, revoked_at);
CREATE INDEX IF NOT EXISTS idx_blog_posts_public ON blog_posts(status, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_blog_posts_category ON blog_posts(category);

CREATE TABLE IF NOT EXISTS whatsapp_qr_auth_store (
    file_key TEXT PRIMARY KEY,
    file_data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
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
            and table not in {
                "admin_sessions",
                "schema_migrations",
                "whatsapp_contact_preferences",
                "whatsapp_qr_auth_store",
            }
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


def healthcheck() -> bool:
    try:
        with connect() as conn:
            row = conn.execute("SELECT 1 AS ok").fetchone()
            return bool(row and row["ok"] == 1)
    except Exception:
        return False


def hash_ip(ip: str | None) -> str:
    raw = f"{settings.metrics_salt}:{ip or 'unknown'}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


@contextmanager
def connect() -> Iterator[ConnectionAdapter]:
    if settings.database_url:
        pool = postgres_pool()
        with pool.connection(timeout=15) as raw:
            conn = ConnectionAdapter(raw, postgres=True)
            try:
                yield conn
                raw.commit()
            except Exception:
                raw.rollback()
                raise
        return
    else:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw = sqlite3.connect(settings.db_path, timeout=15)
        settings.db_path.chmod(0o600)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("PRAGMA busy_timeout = 15000")
        raw.execute("PRAGMA journal_mode = WAL")
        raw.execute("PRAGMA synchronous = NORMAL")
        conn = ConnectionAdapter(raw, postgres=False)
    try:
        yield conn
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def postgres_pool() -> Any:
    global _POSTGRES_POOL
    if _POSTGRES_POOL is not None:
        return _POSTGRES_POOL
    with _POOL_LOCK:
        if _POSTGRES_POOL is None:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            _POSTGRES_POOL = ConnectionPool(
                conninfo=settings.database_url,
                min_size=settings.db_pool_min_size,
                max_size=settings.db_pool_max_size,
                timeout=15,
                max_idle=300,
                max_lifetime=1_800,
                reconnect_timeout=30,
                kwargs={
                    "row_factory": dict_row,
                    "connect_timeout": 8,
                    "prepare_threshold": None,
                },
                open=True,
            )
    return _POSTGRES_POOL


def close_pool() -> None:
    global _POSTGRES_POOL
    with _POOL_LOCK:
        pool = _POSTGRES_POOL
        _POSTGRES_POOL = None
    if pool is not None:
        pool.close()


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
                next_attempt_at TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads(id)
            );

            CREATE TABLE IF NOT EXISTS whatsapp_contact_preferences (
                phone TEXT PRIMARY KEY,
                opted_out_at TEXT NOT NULL,
                source TEXT NOT NULL,
                reason TEXT,
                updated_at TEXT NOT NULL
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

            CREATE TABLE IF NOT EXISTS whatsapp_webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                processed_at TEXT,
                payload_hash TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_error TEXT
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

            CREATE TABLE IF NOT EXISTS blog_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                published_at TEXT,
                title TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                excerpt TEXT NOT NULL,
                body TEXT NOT NULL,
                category TEXT NOT NULL,
                author_name TEXT NOT NULL DEFAULT 'Leonilda Bob',
                status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published'))
            );

            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at);
            CREATE INDEX IF NOT EXISTS idx_leads_kind ON leads(kind);
            CREATE INDEX IF NOT EXISTS idx_outbox_status ON whatsapp_outbox(status);
            CREATE INDEX IF NOT EXISTS idx_whatsapp_preferences_opted_out ON whatsapp_contact_preferences(opted_out_at DESC);
            CREATE INDEX IF NOT EXISTS idx_page_views_path ON page_views(path);
            CREATE INDEX IF NOT EXISTS idx_whatsapp_conversations_phone ON whatsapp_conversations(phone);
            CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_conversation ON whatsapp_messages(conversation_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_webhook_events_pending ON whatsapp_webhook_events(status, next_attempt_at);
            CREATE INDEX IF NOT EXISTS idx_page_views_created_at ON page_views(created_at);
            CREATE INDEX IF NOT EXISTS idx_page_views_visitor ON page_views(visitor_id, session_id);
            CREATE INDEX IF NOT EXISTS idx_admin_sessions_expiry ON admin_sessions(expires_at, revoked_at);
            CREATE INDEX IF NOT EXISTS idx_blog_posts_public ON blog_posts(status, published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_blog_posts_category ON blog_posts(category);
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
        ensure_columns(
            conn,
            "whatsapp_outbox",
            {"provider_message_id": "TEXT", "next_attempt_at": "TEXT"},
        )
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_webhook_events_pending "
            "ON whatsapp_webhook_events(status, next_attempt_at)"
        )
        if conn.postgres:
            upgrade_lead_kind_constraint(conn)
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


def upgrade_lead_kind_constraint(conn: ConnectionAdapter) -> None:
    if not conn.postgres:
        return
    conn.execute("ALTER TABLE leads DROP CONSTRAINT IF EXISTS leads_kind_check")
    conn.execute(
        """
        ALTER TABLE leads
        ADD CONSTRAINT leads_kind_check
        CHECK (kind IN ('trabalhista', 'instituto', 'bpc', 'geral'))
        """
    )


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
        DROP TRIGGER IF EXISTS validate_lead_kind_insert;

        CREATE TRIGGER IF NOT EXISTS validate_lead_kind_insert
        BEFORE INSERT ON leads
        WHEN NEW.kind NOT IN ('trabalhista', 'instituto', 'bpc', 'geral')
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


def create_lead_bundle(data: dict[str, Any], first_message: str) -> tuple[int, int, int, bool]:
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
                return int(existing["id"]), int(outbox["id"]), int(conversation["id"]), False

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
        return lead_id, int(outbox_cursor.lastrowid), int(conversation_cursor.lastrowid), True


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


def set_whatsapp_opt_out(phone: str, *, source: str, reason: str = "PARAR") -> int:
    """Persist a contact's request not to receive WhatsApp messages and stop pending sends."""
    normalized_phone = str(phone or "").strip()
    if not normalized_phone:
        raise ValueError("Telefone obrigatório para interromper mensagens.")
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_contact_preferences(phone, opted_out_at, source, reason, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                opted_out_at = excluded.opted_out_at,
                source = excluded.source,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (normalized_phone, now, source[:80] or "inbound", reason[:160] or "PARAR", now),
        )
        conn.execute(
            """
            UPDATE whatsapp_conversations
            SET status = 'closed', bot_enabled = 0, updated_at = ?
            WHERE phone = ?
            """,
            (now, normalized_phone),
        )
        cancelled = conn.execute(
            """
            UPDATE whatsapp_outbox
            SET status = 'suppressed', last_error = ?, next_attempt_at = NULL
            WHERE recipient = ? AND status = 'pending'
            """,
            ("Contato solicitou não receber mensagens pelo WhatsApp.", normalized_phone),
        )
        return max(0, cancelled.rowcount)


def is_whatsapp_opted_out(phone: str) -> bool:
    normalized_phone = str(phone or "").strip()
    if not normalized_phone:
        return False
    with connect() as conn:
        row = conn.execute(
            "SELECT phone FROM whatsapp_contact_preferences WHERE phone = ?",
            (normalized_phone,),
        ).fetchone()
        return bool(row)


def whatsapp_message_exists(provider_message_id: str) -> bool:
    if not provider_message_id:
        return False
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM whatsapp_messages WHERE provider_message_id = ?",
            (provider_message_id,),
        ).fetchone()
        return bool(row)


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
            operator = "ILIKE" if conn.postgres else "LIKE"
            sql += f" WHERE name {operator} ? OR phone {operator} ? OR kind {operator} ?"
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
                attempts = attempts + CASE WHEN ? = 'dry_run' THEN 0 ELSE 1 END,
                provider_message_id = COALESCE(NULLIF(?, ''), provider_message_id),
                last_error = NULL,
                next_attempt_at = NULL
            WHERE id = ?
            """,
            (status, status, utc_now(), status, provider_message_id or "", outbox_id),
        )


def mark_outbox_failed(outbox_id: int, error: str) -> None:
    with connect() as conn:
        row = conn.execute("SELECT attempts FROM whatsapp_outbox WHERE id = ?", (outbox_id,)).fetchone()
        attempts = int(row["attempts"] or 0) + 1 if row else 1
        delay_minutes = min(60, 2 ** min(attempts, 5))
        next_attempt = (datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)).isoformat(
            timespec="seconds"
        )
        conn.execute(
            """
            UPDATE whatsapp_outbox
            SET status = CASE WHEN ? >= 5 THEN 'failed' ELSE 'pending' END,
                attempts = attempts + 1,
                last_error = ?,
                next_attempt_at = ?
            WHERE id = ?
            """,
            (attempts, error[:500], next_attempt, outbox_id),
        )


def mark_outbox_suppressed(outbox_id: int, reason: str = "Contato solicitou não receber mensagens pelo WhatsApp.") -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE whatsapp_outbox
            SET status = 'suppressed', last_error = ?, next_attempt_at = NULL
            WHERE id = ?
            """,
            (reason[:500], outbox_id),
        )


def list_pending_outbox(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT o.id, o.lead_id, o.recipient, o.message, o.attempts,
                   l.name, l.kind, l.area
            FROM whatsapp_outbox o
            JOIN leads l ON l.id = o.lead_id
            WHERE o.status = 'pending'
              AND o.attempts < 5
              AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= ?)
            ORDER BY o.id ASC
            LIMIT ?
            """,
            (utc_now(), max(1, min(limit, 100))),
        ).fetchall()
        return [dict(row) for row in rows]


def enqueue_whatsapp_webhook(raw_body: bytes) -> tuple[int, bool]:
    payload_hash = hashlib.sha256(raw_body).hexdigest()
    payload = raw_body.decode("utf-8")
    with connect() as conn:
        if conn.postgres:
            cursor = conn.execute(
                """
                INSERT INTO whatsapp_webhook_events(created_at, payload_hash, payload, status)
                VALUES (?, ?, ?, 'pending')
                ON CONFLICT (payload_hash) DO NOTHING
                """,
                (utc_now(), payload_hash, payload),
            )
        else:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO whatsapp_webhook_events(created_at, payload_hash, payload, status)
                VALUES (?, ?, ?, 'pending')
                """,
                (utc_now(), payload_hash, payload),
            )
        created = bool(cursor.rowcount)
        row = conn.execute(
            "SELECT id FROM whatsapp_webhook_events WHERE payload_hash = ?",
            (payload_hash,),
        ).fetchone()
        if not row:
            raise RuntimeError("Não foi possível registrar o evento do WhatsApp.")
        return int(row["id"]), created


def list_pending_whatsapp_webhooks(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, payload, attempts
            FROM whatsapp_webhook_events
            WHERE status = 'pending'
              AND attempts < 8
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY id ASC
            LIMIT ?
            """,
            (utc_now(), max(1, min(limit, 100))),
        ).fetchall()
        return [dict(row) for row in rows]


def mark_whatsapp_webhook_processed(event_id: int) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE whatsapp_webhook_events
            SET status = 'processed', processed_at = ?, payload = '{}',
                last_error = NULL, next_attempt_at = NULL
            WHERE id = ?
            """,
            (utc_now(), event_id),
        )


def mark_whatsapp_webhook_failed(event_id: int, error: str) -> None:
    with connect() as conn:
        row = conn.execute(
            "SELECT attempts FROM whatsapp_webhook_events WHERE id = ?", (event_id,)
        ).fetchone()
        attempts = int(row["attempts"] or 0) + 1 if row else 1
        delay_minutes = min(60, 2 ** min(attempts, 5))
        next_attempt = (datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)).isoformat(
            timespec="seconds"
        )
        conn.execute(
            """
            UPDATE whatsapp_webhook_events
            SET status = CASE WHEN ? >= 8 THEN 'failed' ELSE 'pending' END,
                attempts = attempts + 1, last_error = ?, next_attempt_at = ?
            WHERE id = ?
            """,
            (attempts, error[:500], next_attempt, event_id),
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
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - last_time < timedelta(seconds=cooldown_seconds):
                    return False
            except (TypeError, ValueError):
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
                SUM(CASE WHEN kind = 'trabalhista' OR area = 'trabalhista' THEN 1 ELSE 0 END) AS trabalhista_total,
                SUM(CASE WHEN kind = 'instituto' OR area = 'instituto' THEN 1 ELSE 0 END) AS instituto_total,
                SUM(CASE WHEN kind = 'bpc' OR area = 'previdenciario' OR source_path = '/bpc-loas-negado' OR landing_path = '/bpc-loas-negado' THEN 1 ELSE 0 END) AS bpc_total,
                (SELECT COUNT(*) FROM whatsapp_conversations) AS conversations_total
            FROM leads
            """
        ).fetchone()
        outbox = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status IN ('sent', 'delivered', 'read') THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN status = 'dry_run' THEN 1 ELSE 0 END) AS dry_run,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM whatsapp_outbox
            """
        ).fetchone()
        webhooks = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM whatsapp_webhook_events
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
        analytics = conn.execute(
            """
            SELECT
                COUNT(*) AS page_views,
                COUNT(DISTINCT NULLIF(visitor_id, '')) AS unique_visitors,
                COUNT(DISTINCT NULLIF(session_id, '')) AS sessions
            FROM page_views
            """
        ).fetchone()
        funnel = conn.execute(
            """
            SELECT
                SUM(CASE WHEN path = '/' THEN 1 ELSE 0 END) AS institutional_visits,
                SUM(CASE WHEN path = '/bpc-loas-negado' THEN 1 ELSE 0 END) AS bpc_visits,
                SUM(CASE WHEN path = '/conversion/lead' OR substr(path, 1, 17) = '/conversion/lead/' THEN 1 ELSE 0 END) AS lead_conversions,
                SUM(CASE WHEN path = '/conversion/whatsapp' OR substr(path, 1, 21) = '/conversion/whatsapp/' THEN 1 ELSE 0 END) AS whatsapp_clicks
            FROM page_views
            """
        ).fetchone()
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
            "webhooks": dict(webhooks or {}),
            "recent_leads": [dict(row) for row in recent_leads],
            "recent_outbox": [dict(row) for row in recent_outbox],
            "page_views": [dict(row) for row in page_views],
            "analytics": dict(analytics or {}),
            "funnel": dict(funnel or {}),
            "origins": [dict(row) for row in origins],
            "conversations": [dict(row) for row in conversations],
            "messages": [dict(row) for row in messages],
        }


def get_outbox_message(outbox_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, lead_id, recipient, message, status, attempts FROM whatsapp_outbox WHERE id = ?",
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
    auth_event_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=settings.auth_event_retention_days)
    ).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("DELETE FROM page_views WHERE created_at < ?", (analytics_cutoff,))
        conn.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (session_cutoff,))
        conn.execute("DELETE FROM admin_auth_events WHERE created_at < ?", (auth_event_cutoff,))
        webhook_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(
            timespec="seconds"
        )
        conn.execute(
            """
            DELETE FROM whatsapp_webhook_events
            WHERE status IN ('processed', 'failed')
              AND COALESCE(processed_at, created_at) < ?
            """,
            (webhook_cutoff,),
        )


def contact_media_urls(phone: str) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT m.media_url
            FROM whatsapp_messages m
            JOIN whatsapp_conversations c ON c.id = m.conversation_id
            WHERE c.phone = ? AND m.media_url IS NOT NULL AND m.media_url != ''
            """,
            (phone,),
        ).fetchall()
        return [str(row["media_url"]) for row in rows]


def list_expired_contact_phones() -> list[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.lead_retention_days)).isoformat(
        timespec="seconds"
    )
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT contacts.phone
            FROM (
                SELECT phone FROM leads
                UNION
                SELECT phone FROM whatsapp_conversations
            ) contacts
            WHERE NOT EXISTS (
                SELECT 1 FROM leads l
                WHERE l.phone = contacts.phone
                  AND (l.created_at >= ? OR l.status NOT IN ('closed', 'archived'))
            )
              AND NOT EXISTS (
                SELECT 1 FROM whatsapp_conversations c
                WHERE c.phone = contacts.phone
                  AND (c.updated_at >= ? OR c.status NOT IN ('closed', 'archived'))
            )
            """,
            (cutoff, cutoff),
        ).fetchall()
        return [str(row["phone"]) for row in rows]


def delete_contact_data(phone: str) -> dict[str, Any]:
    """Delete one contact's cases, messages and lead history for a verified privacy request."""
    with connect() as conn:
        conn.execute("DELETE FROM whatsapp_contact_preferences WHERE phone = ?", (phone,))
        lead_rows = conn.execute("SELECT id FROM leads WHERE phone = ?", (phone,)).fetchall()
        lead_ids = [int(row["id"]) for row in lead_rows]
        conversation_rows = conn.execute(
            "SELECT id FROM whatsapp_conversations WHERE phone = ?", (phone,)
        ).fetchall()
        conversation_ids = [int(row["id"]) for row in conversation_rows]
        message_count = 0
        for conversation_id in conversation_ids:
            message_count += int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM whatsapp_messages WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()["count"]
            )
            conn.execute(
                "DELETE FROM whatsapp_messages WHERE conversation_id = ?",
                (conversation_id,),
            )
            conn.execute(
                "DELETE FROM whatsapp_conversations WHERE id = ?",
                (conversation_id,),
            )
        for lead_id in lead_ids:
            conn.execute("DELETE FROM lead_events WHERE lead_id = ?", (lead_id,))
            conn.execute("DELETE FROM whatsapp_outbox WHERE lead_id = ?", (lead_id,))
            conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        return {
            "leads": len(lead_ids),
            "conversations": len(conversation_ids),
            "messages": int(message_count),
        }


def _blog_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    ascii_value = "".join(character for character in normalized if not unicodedata.combining(character))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")[:120]
    return slug or "artigo"


def _available_blog_slug(conn: ConnectionAdapter, title: str) -> str:
    base = _blog_slug(title)
    candidate = base
    suffix = 2
    while conn.execute("SELECT id FROM blog_posts WHERE slug = ?", (candidate,)).fetchone():
        ending = f"-{suffix}"
        candidate = f"{base[: 120 - len(ending)]}{ending}"
        suffix += 1
    return candidate


def save_blog_post(
    *,
    post_id: int | None,
    title: str,
    excerpt: str,
    body: str,
    category: str,
    status: str,
) -> dict[str, Any]:
    if status not in {"draft", "published"}:
        raise ValueError("Status de artigo inválido.")
    now = utc_now()
    with connect() as conn:
        if post_id is None:
            slug = _available_blog_slug(conn, title)
            published_at = now if status == "published" else None
            cursor = conn.execute(
                """
                INSERT INTO blog_posts (
                    created_at, updated_at, published_at, title, slug, excerpt,
                    body, category, author_name, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    published_at,
                    title,
                    slug,
                    excerpt,
                    body,
                    category,
                    "Leonilda Bob",
                    status,
                ),
            )
            post_id = int(cursor.lastrowid)
        else:
            existing = conn.execute(
                "SELECT id, published_at FROM blog_posts WHERE id = ?", (post_id,)
            ).fetchone()
            if not existing:
                raise LookupError("Artigo não encontrado.")
            published_at = existing["published_at"]
            if status == "published" and not published_at:
                published_at = now
            conn.execute(
                """
                UPDATE blog_posts
                SET updated_at = ?, published_at = ?, title = ?, excerpt = ?,
                    body = ?, category = ?, status = ?
                WHERE id = ?
                """,
                (now, published_at, title, excerpt, body, category, status, post_id),
            )
        row = conn.execute("SELECT * FROM blog_posts WHERE id = ?", (post_id,)).fetchone()
        return dict(row)


def get_blog_post_by_id(post_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM blog_posts WHERE id = ?", (post_id,)).fetchone()
        return dict(row) if row else None


def get_published_blog_post(slug: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM blog_posts WHERE slug = ? AND status = 'published'",
            (slug,),
        ).fetchone()
        return dict(row) if row else None


def list_blog_posts_admin() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM blog_posts
            ORDER BY CASE WHEN status = 'draft' THEN 0 ELSE 1 END, updated_at DESC, id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def blog_admin_totals() -> dict[str, int]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'published' THEN 1 ELSE 0 END) AS published,
                   SUM(CASE WHEN status = 'draft' THEN 1 ELSE 0 END) AS drafts
            FROM blog_posts
            """
        ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "published": int(row["published"] or 0),
            "drafts": int(row["drafts"] or 0),
        }


def list_published_blog_posts(
    *,
    limit: int = 12,
    offset: int = 0,
    query: str = "",
    category: str = "",
) -> tuple[list[dict[str, Any]], int]:
    clean_query = query.strip()
    clean_category = category.strip()
    if clean_query and clean_category:
        term = f"%{clean_query}%"
        params: tuple[Any, ...] = (term, term, term, clean_category)
        count_sql = """
            SELECT COUNT(*) AS count FROM blog_posts
            WHERE status = 'published'
              AND (LOWER(title) LIKE LOWER(?) OR LOWER(excerpt) LIKE LOWER(?) OR LOWER(body) LIKE LOWER(?))
              AND category = ?
        """
        select_sql = """
            SELECT * FROM blog_posts
            WHERE status = 'published'
              AND (LOWER(title) LIKE LOWER(?) OR LOWER(excerpt) LIKE LOWER(?) OR LOWER(body) LIKE LOWER(?))
              AND category = ?
            ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?
        """
    elif clean_query:
        term = f"%{clean_query}%"
        params = (term, term, term)
        count_sql = """
            SELECT COUNT(*) AS count FROM blog_posts
            WHERE status = 'published'
              AND (LOWER(title) LIKE LOWER(?) OR LOWER(excerpt) LIKE LOWER(?) OR LOWER(body) LIKE LOWER(?))
        """
        select_sql = """
            SELECT * FROM blog_posts
            WHERE status = 'published'
              AND (LOWER(title) LIKE LOWER(?) OR LOWER(excerpt) LIKE LOWER(?) OR LOWER(body) LIKE LOWER(?))
            ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?
        """
    elif clean_category:
        params = (clean_category,)
        count_sql = """
            SELECT COUNT(*) AS count FROM blog_posts
            WHERE status = 'published' AND category = ?
        """
        select_sql = """
            SELECT * FROM blog_posts
            WHERE status = 'published' AND category = ?
            ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?
        """
    else:
        params = ()
        count_sql = "SELECT COUNT(*) AS count FROM blog_posts WHERE status = 'published'"
        select_sql = """
            SELECT * FROM blog_posts
            WHERE status = 'published'
            ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?
        """
    with connect() as conn:
        count_row = conn.execute(count_sql, params).fetchone()
        rows = conn.execute(
            select_sql,
            (*params, limit, offset),
        ).fetchall()
        return [dict(row) for row in rows], int(count_row["count"] or 0)


def list_published_blog_categories() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT category, COUNT(*) AS count
            FROM blog_posts
            WHERE status = 'published'
            GROUP BY category
            ORDER BY category
            """
        ).fetchall()
        return [dict(row) for row in rows]


def unpublish_blog_post(post_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE blog_posts SET status = 'draft', updated_at = ? WHERE id = ?",
            (utc_now(), post_id),
        )
        return cursor.rowcount > 0


def save_qr_auth_files(auth_dir: str) -> int:
    """Save all files in auth_dir to the database."""
    import os
    saved = 0
    now = utc_now()
    for filename in os.listdir(auth_dir):
        filepath = os.path.join(auth_dir, filename)
        if not os.path.isfile(filepath):
            continue
        try:
            with open(filepath, "r") as f:
                data = f.read()
        except Exception:
            continue
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO whatsapp_qr_auth_store (file_key, file_data, updated_at)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (file_key)
                   DO UPDATE SET file_data = EXCLUDED.file_data, updated_at = EXCLUDED.updated_at""",
                (filename, data, now),
            )
        saved += 1
    return saved


def restore_qr_auth_files(auth_dir: str) -> int:
    """Restore auth files from the database to auth_dir."""
    import os
    os.makedirs(auth_dir, exist_ok=True)
    restored = 0
    with get_conn() as conn:
        cursor = conn.execute("SELECT file_key, file_data FROM whatsapp_qr_auth_store")
        rows = cursor.fetchall()
    for row in rows:
        key = row["file_key"] if isinstance(row, dict) else row[0]
        data = row["file_data"] if isinstance(row, dict) else row[1]
        filepath = os.path.join(auth_dir, key)
        try:
            with open(filepath, "w") as f:
                f.write(data)
            restored += 1
        except Exception:
            continue
    return restored
