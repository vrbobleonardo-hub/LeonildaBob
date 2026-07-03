from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .settings import settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_ip(ip: str | None) -> str:
    raw = f"{settings.metrics_salt}:{ip or 'unknown'}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL,
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
                phone TEXT NOT NULL UNIQUE,
                name TEXT,
                kind TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                last_message_at TEXT,
                last_message_preview TEXT,
                bot_enabled INTEGER NOT NULL DEFAULT 1,
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

            CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at);
            CREATE INDEX IF NOT EXISTS idx_leads_kind ON leads(kind);
            CREATE INDEX IF NOT EXISTS idx_outbox_status ON whatsapp_outbox(status);
            CREATE INDEX IF NOT EXISTS idx_page_views_path ON page_views(path);
            CREATE INDEX IF NOT EXISTS idx_whatsapp_conversations_phone ON whatsapp_conversations(phone);
            CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_conversation ON whatsapp_messages(conversation_id, created_at);
            """
        )
        ensure_columns(
            conn,
            "leads",
            {
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_page_views_origin ON page_views(utm_source, utm_campaign)")


def ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def insert_lead(data: dict[str, Any]) -> int:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO leads (
                created_at, kind, name, phone, email, message, consent,
                source_path, landing_path, referrer, visitor_id, session_id,
                utm_source, utm_medium, utm_campaign, utm_content, utm_term,
                gclid, fbclid, ip_hash, user_agent, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                data["kind"],
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
                "new",
            ),
        )
        lead_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO lead_events (lead_id, created_at, event_type, payload) VALUES (?, ?, ?, ?)",
            (lead_id, utc_now(), "lead.created", data.get("source_path") or ""),
        )
        return lead_id


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
        row = conn.execute("SELECT id FROM whatsapp_conversations WHERE phone = ?", (phone,)).fetchone()
        now = utc_now()
        if row:
            conn.execute(
                """
                UPDATE whatsapp_conversations
                SET updated_at = ?, name = COALESCE(NULLIF(?, ''), name),
                    kind = COALESCE(NULLIF(?, ''), kind),
                    source_lead_id = COALESCE(?, source_lead_id)
                WHERE id = ?
                """,
                (now, name, kind, source_lead_id, row["id"]),
            )
            return int(row["id"])
        cursor = conn.execute(
            """
            INSERT INTO whatsapp_conversations (
                created_at, updated_at, phone, name, kind, source_lead_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (now, now, phone, name or None, kind or None, source_lead_id),
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


def list_conversations(limit: int = 80) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, updated_at, phone, name, kind, status,
                   last_message_at, last_message_preview, bot_enabled, source_lead_id
            FROM whatsapp_conversations
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
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
            SELECT id, conversation_id, created_at, direction, message_type, text,
                   provider_message_id, media_url, media_mime, media_name, media_size,
                   media_provider_id, status
            FROM whatsapp_messages
            WHERE conversation_id = ?
            ORDER BY id ASC
            LIMIT ?
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


def mark_outbox_sent(outbox_id: int, status: str = "sent", provider_message_id: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE whatsapp_outbox
            SET status = ?,
                sent_at = ?,
                attempts = attempts + 1,
                provider_message_id = COALESCE(NULLIF(?, ''), provider_message_id),
                last_error = NULL
            WHERE id = ?
            """,
            (status, utc_now(), provider_message_id or "", outbox_id),
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


def admin_snapshot(limit: int = 25) -> dict[str, Any]:
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
            SELECT id, created_at, kind, name, phone, email, message, consent,
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
