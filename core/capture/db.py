"""SQLite database layer for Discord event storage."""

from __future__ import annotations

import json
import logging
from datetime import datetime
import os
from pathlib import Path
from typing import Any, Optional

import aiosqlite

log = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("DISCORD_DB_PATH",
                              Path(__file__).resolve().parent.parent.parent / "data" / "discord.db"))


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    await db.executescript(SCHEMA)
    await db.commit()
    await db.close()
    log.info("Database initialized at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------

async def upsert_guild(db: aiosqlite.Connection, **kw: Any) -> None:
    await db.execute("""
        INSERT INTO guilds (id, name, icon_url, owner_id, member_count, created_at, updated_at)
        VALUES (:id, :name, :icon_url, :owner_id, :member_count, :created_at, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, icon_url=excluded.icon_url, owner_id=excluded.owner_id,
            member_count=excluded.member_count, updated_at=datetime('now')
    """, kw)


async def upsert_channel(db: aiosqlite.Connection, **kw: Any) -> None:
    await db.execute("""
        INSERT INTO channels (id, guild_id, name, type, category_id, category_name, topic, position, created_at, updated_at)
        VALUES (:id, :guild_id, :name, :type, :category_id, :category_name, :topic, :position, :created_at, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, type=excluded.type, category_id=excluded.category_id,
            category_name=excluded.category_name, topic=excluded.topic, position=excluded.position,
            updated_at=datetime('now')
    """, kw)


async def replace_channel_overwrites(db: aiosqlite.Connection, channel_id: int,
                                     guild_id: int, entries: list[dict]) -> None:
    """Replace every overwrite for one channel.

    Delete-then-insert rather than upsert, because an overwrite being REMOVED is
    a real change and an upsert would leave the stale row behind granting phantom
    access. Current state only — the audit trail of who changed what lives in
    `events`.
    """
    await db.execute("DELETE FROM channel_overwrites WHERE channel_id = ?", (channel_id,))
    if entries:
        await db.executemany("""
            INSERT INTO channel_overwrites
                (channel_id, guild_id, target_id, target_type, allow, deny, updated_at)
            VALUES (:channel_id, :guild_id, :target_id, :target_type, :allow, :deny, datetime('now'))
        """, [{"channel_id": channel_id, "guild_id": guild_id, **e} for e in entries])


async def upsert_role(db: aiosqlite.Connection, **kw: Any) -> None:
    await db.execute("""
        INSERT INTO roles (id, guild_id, name, color, position, permissions, member_count, updated_at)
        VALUES (:id, :guild_id, :name, :color, :position, :permissions, :member_count, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, color=excluded.color, position=excluded.position,
            permissions=excluded.permissions, member_count=excluded.member_count,
            updated_at=datetime('now')
    """, kw)


async def upsert_member(db: aiosqlite.Connection, **kw: Any) -> None:
    """Upsert a user + their membership in a single guild.

    Splits the input into two writes:
      - users(id, username, discriminator, avatar_url, bot) — global identity
      - memberships(user_id, guild_id, display_name, joined_at, role_ids, left_at) — per-guild
    Same `id` can have multiple membership rows (one per guild).
    """
    # Map the legacy member id → user id
    user_id = kw["id"]

    await db.execute("""
        INSERT INTO users (id, username, discriminator, avatar_url, bot, updated_at)
        VALUES (:id, :username, :discriminator, :avatar_url, :bot, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            username=excluded.username,
            discriminator=excluded.discriminator,
            avatar_url=excluded.avatar_url,
            bot=excluded.bot,
            updated_at=datetime('now')
    """, {
        "id": user_id,
        "username": kw.get("username"),
        "discriminator": kw.get("discriminator"),
        "avatar_url": kw.get("avatar_url"),
        "bot": kw.get("bot", 0),
    })

    await db.execute("""
        INSERT INTO memberships (user_id, guild_id, display_name, joined_at, role_ids, updated_at)
        VALUES (:user_id, :guild_id, :display_name, :joined_at, :role_ids, datetime('now'))
        ON CONFLICT(user_id, guild_id) DO UPDATE SET
            display_name=excluded.display_name,
            role_ids=excluded.role_ids,
            updated_at=datetime('now')
    """, {
        "user_id": user_id,
        "guild_id": kw["guild_id"],
        "display_name": kw.get("display_name"),
        "joined_at": kw.get("joined_at"),
        "role_ids": kw.get("role_ids"),
    })


async def upsert_message(db: aiosqlite.Connection, **kw: Any) -> None:
    await db.execute("""
        INSERT INTO messages (id, guild_id, channel_id, author_id, content, attachments, embeds, reference_id, is_pinned, created_at, edited_at)
        VALUES (:id, :guild_id, :channel_id, :author_id, :content, :attachments, :embeds, :reference_id, :is_pinned, :created_at, :edited_at)
        ON CONFLICT(id) DO UPDATE SET
            content=excluded.content, embeds=excluded.embeds,
            is_pinned=excluded.is_pinned, edited_at=excluded.edited_at,
            updated_at=datetime('now')
    """, kw)


async def insert_event(db: aiosqlite.Connection, guild_id: int, event_type: str, data: dict) -> None:
    await db.execute("""
        INSERT INTO events (guild_id, event_type, data, created_at)
        VALUES (?, ?, ?, datetime('now'))
    """, (guild_id, event_type, json.dumps(data, default=str)))


async def insert_media(db: aiosqlite.Connection, **kw: Any) -> None:
    await db.execute("""
        INSERT OR IGNORE INTO media (message_id, url, filename, content_type, size, created_at)
        VALUES (:message_id, :url, :filename, :content_type, :size, datetime('now'))
    """, kw)


# ---------------------------------------------------------------------------
# Recovery helpers
# ---------------------------------------------------------------------------

async def get_last_message_id(db: aiosqlite.Connection, channel_id: int) -> Optional[int]:
    """Get the most recent message ID stored for a channel."""
    cursor = await db.execute(
        "SELECT MAX(id) FROM messages WHERE channel_id = ?", (channel_id,)
    )
    row = await cursor.fetchone()
    return row[0] if row and row[0] else None


async def get_last_audit_id(db: aiosqlite.Connection, guild_id: int) -> Optional[int]:
    """Get the most recent audit log entry ID we stored."""
    cursor = await db.execute(
        "SELECT MAX(CAST(json_extract(data, '$.audit_id') AS INTEGER)) FROM events WHERE guild_id = ? AND event_type LIKE 'audit_%'",
        (guild_id,)
    )
    row = await cursor.fetchone()
    return row[0] if row and row[0] else None


async def get_bot_state(db: aiosqlite.Connection, key: str) -> Optional[str]:
    cursor = await db.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row[0] if row else None


async def set_bot_state(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute("""
        INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')
    """, (key, value))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
    CREATE TABLE IF NOT EXISTS guilds (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        icon_url TEXT,
        owner_id INTEGER,
        member_count INTEGER,
        created_at TEXT,
        updated_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        category_id INTEGER,
        category_name TEXT,
        topic TEXT,
        position INTEGER,
        created_at TEXT,
        updated_at TEXT DEFAULT (datetime('now')),
        deleted INTEGER DEFAULT 0,
        FOREIGN KEY (guild_id) REFERENCES guilds(id)
    );

    CREATE TABLE IF NOT EXISTS roles (
        id INTEGER PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        color INTEGER,
        position INTEGER,
        permissions INTEGER,
        member_count INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now')),
        deleted INTEGER DEFAULT 0,
        FOREIGN KEY (guild_id) REFERENCES guilds(id)
    );

    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL,
        discriminator TEXT,
        avatar_url TEXT,
        bot INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS memberships (
        user_id INTEGER NOT NULL,
        guild_id INTEGER NOT NULL,
        display_name TEXT,
        joined_at TEXT,
        left_at TEXT,
        role_ids TEXT,
        updated_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, guild_id),
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (guild_id) REFERENCES guilds(id)
    );

    CREATE INDEX IF NOT EXISTS idx_memberships_guild ON memberships(guild_id);
    CREATE INDEX IF NOT EXISTS idx_memberships_left ON memberships(left_at);

    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        author_id INTEGER NOT NULL,
        content TEXT,
        attachments TEXT,
        embeds TEXT,
        reference_id INTEGER,
        is_pinned INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        edited_at TEXT,
        deleted INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (guild_id) REFERENCES guilds(id),
        FOREIGN KEY (channel_id) REFERENCES channels(id)
    );

    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        data TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (guild_id) REFERENCES guilds(id)
    );

    CREATE TABLE IF NOT EXISTS media (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER,
        url TEXT NOT NULL UNIQUE,
        filename TEXT,
        content_type TEXT,
        size INTEGER,
        local_path TEXT,
        downloaded INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );

    -- Channel permission overwrites.
    -- Sparse: only explicitly-set entries exist (median 7 per channel), never one
    -- row per member. target_type is load-bearing — role ids and user ids are
    -- both snowflakes and cannot be told apart by value.
    -- Note the @everyone role id EQUALS the guild id.
    CREATE TABLE IF NOT EXISTS channel_overwrites (
        channel_id INTEGER NOT NULL,
        guild_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        target_type TEXT NOT NULL CHECK (target_type IN ('role','member')),
        allow INTEGER NOT NULL DEFAULT 0,
        deny INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT,
        PRIMARY KEY (channel_id, target_id, target_type)
    );
    CREATE INDEX IF NOT EXISTS idx_overwrites_guild ON channel_overwrites(guild_id);
    CREATE INDEX IF NOT EXISTS idx_overwrites_target ON channel_overwrites(target_id);

    CREATE TABLE IF NOT EXISTS bot_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT DEFAULT (datetime('now'))
    );

    -- Indexes
    CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_messages_author ON messages(author_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_messages_deleted ON messages(deleted);
    CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, created_at);
    CREATE INDEX IF NOT EXISTS idx_events_guild ON events(guild_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_media_message ON media(message_id);
    CREATE INDEX IF NOT EXISTS idx_media_downloaded ON media(downloaded);
"""
