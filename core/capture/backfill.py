"""History backfill and recovery — handles downtime gaps."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import discord

from .db import (
    get_db, upsert_guild, upsert_channel, upsert_role, upsert_member,
    upsert_message, insert_media, insert_event, get_last_message_id,
    set_bot_state, get_bot_state,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Server state snapshot — used by both full backfill and recovery
# ---------------------------------------------------------------------------

async def snapshot_guild(guild: discord.Guild) -> None:
    """Snapshot current server state: guild info, channels, roles, members."""
    db = await get_db()
    try:
        # Guild metadata
        await upsert_guild(db,
            id=guild.id, name=guild.name,
            icon_url=str(guild.icon.url) if guild.icon else None,
            owner_id=guild.owner_id, member_count=guild.member_count,
            created_at=guild.created_at.isoformat(),
        )

        # All channels
        for ch in guild.channels:
            await upsert_channel(db,
                id=ch.id, guild_id=guild.id, name=ch.name, type=str(ch.type),
                category_id=ch.category_id,
                category_name=ch.category.name if ch.category else None,
                topic=getattr(ch, "topic", None), position=ch.position,
                created_at=ch.created_at.isoformat(),
            )

        # All roles
        for role in guild.roles:
            await upsert_role(db,
                id=role.id, guild_id=guild.id, name=role.name,
                color=role.color.value, position=role.position,
                permissions=role.permissions.value, member_count=len(role.members),
            )

        # All members (requires GUILD_MEMBERS intent)
        member_count = 0
        async for member in guild.fetch_members(limit=None):
            await upsert_member(db,
                id=member.id, guild_id=guild.id, username=member.name,
                display_name=member.display_name, discriminator=member.discriminator,
                avatar_url=str(member.display_avatar.url) if member.display_avatar else None,
                bot=int(member.bot),
                joined_at=member.joined_at.isoformat() if member.joined_at else None,
                role_ids=json.dumps([r.id for r in member.roles]),
            )
            member_count += 1
        log.info("  Snapshot: %d channels, %d roles, %d members",
                 len(guild.channels), len(guild.roles), member_count)

        await db.commit()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Message backfill for a single channel
# ---------------------------------------------------------------------------

async def backfill_channel(
    channel: discord.TextChannel,
    after: Optional[discord.Object] = None,
) -> int:
    """Fetch all messages in a channel, optionally after a given message ID.

    Returns the number of messages stored.
    """
    count = 0
    db = await get_db()
    try:
        async for message in channel.history(limit=None, after=after, oldest_first=True):
            attachments = [
                {"id": a.id, "url": a.url, "filename": a.filename,
                 "content_type": a.content_type, "size": a.size}
                for a in message.attachments
            ]
            embeds = [e.to_dict() for e in message.embeds]

            await upsert_message(db,
                id=message.id, guild_id=message.guild.id,
                channel_id=message.channel.id, author_id=message.author.id,
                content=message.content, attachments=json.dumps(attachments),
                embeds=json.dumps(embeds),
                reference_id=message.reference.message_id if message.reference else None,
                is_pinned=int(message.pinned),
                created_at=message.created_at.isoformat(),
                edited_at=message.edited_at.isoformat() if message.edited_at else None,
            )

            for a in message.attachments:
                await insert_media(db, message_id=message.id, url=a.url,
                                   filename=a.filename, content_type=a.content_type,
                                   size=a.size)
            count += 1

            # Commit in batches of 500 to avoid huge transactions
            if count % 500 == 0:
                await db.commit()
                log.info("    ... %d messages so far in #%s", count, channel.name)

        await db.commit()
    finally:
        await db.close()

    return count


# ---------------------------------------------------------------------------
# Audit log backfill — recovers non-message events during downtime
# ---------------------------------------------------------------------------

async def backfill_audit_log(guild: discord.Guild, after_time: Optional[datetime] = None) -> int:
    """Pull audit log entries, which capture channel edits, role changes,
    bans, kicks, etc. — even if the bot was offline when they happened.

    Discord keeps audit logs for ~90 days.
    """
    count = 0
    db = await get_db()
    try:
        async for entry in guild.audit_logs(limit=None):
            # If we have a cutoff time, stop when we reach entries before it
            if after_time and entry.created_at < after_time:
                break

            await insert_event(db, guild.id, f"audit_{entry.action.name}", {
                "audit_id": entry.id,
                "user_id": entry.user.id if entry.user else None,
                "user_name": str(entry.user) if entry.user else None,
                "target_id": entry.target.id if entry.target else None,
                "target_type": type(entry.target).__name__ if entry.target else None,
                "reason": entry.reason,
                "changes": str(entry.changes) if entry.changes else None,
                "created_at": entry.created_at.isoformat(),
            })
            count += 1

            if count % 200 == 0:
                await db.commit()

        await db.commit()
    finally:
        await db.close()

    return count


# ---------------------------------------------------------------------------
# Full backfill — first run ever
# ---------------------------------------------------------------------------

async def run_full_backfill(guild: discord.Guild) -> None:
    """Complete backfill: snapshot state, fetch ALL messages, fetch audit log."""
    log.info("=== FULL BACKFILL: %s ===", guild.name)

    # 1. Snapshot current state
    log.info("Step 1/3: Snapshotting server state...")
    await snapshot_guild(guild)

    # 2. Backfill all text channels
    log.info("Step 2/3: Backfilling message history...")
    total_messages = 0
    text_channels = [
        ch for ch in guild.channels
        if isinstance(ch, discord.TextChannel)
    ]
    for i, channel in enumerate(text_channels, 1):
        try:
            count = await backfill_channel(channel)
            total_messages += count
            log.info("  [%d/%d] #%s: %d messages",
                     i, len(text_channels), channel.name, count)
        except discord.Forbidden:
            log.warning("  [%d/%d] #%s: no access (Forbidden)",
                        i, len(text_channels), channel.name)
        except Exception as e:
            log.error("  [%d/%d] #%s: error: %s",
                      i, len(text_channels), channel.name, e)

    log.info("  Total: %d messages across %d channels", total_messages, len(text_channels))

    # 3. Audit log
    log.info("Step 3/3: Fetching audit log...")
    audit_count = await backfill_audit_log(guild)
    log.info("  %d audit log entries", audit_count)

    # Mark completion
    db = await get_db()
    try:
        await set_bot_state(db, f"backfill_complete_{guild.id}",
                            datetime.now(timezone.utc).isoformat())
        await db.commit()
    finally:
        await db.close()

    log.info("=== FULL BACKFILL COMPLETE: %s ===", guild.name)


# ---------------------------------------------------------------------------
# Recovery — fills the gap since last shutdown
# ---------------------------------------------------------------------------

async def run_recovery(guild: discord.Guild) -> None:
    """Recover data missed during downtime.

    Strategy:
    1. Re-snapshot server state (catches any channel/role/member changes)
    2. For each text channel, fetch messages after the last known message
    3. Fetch audit log entries since last shutdown
    """
    log.info("=== RECOVERY: %s ===", guild.name)

    db = await get_db()
    try:
        last_shutdown = await get_bot_state(db, "last_shutdown")
    finally:
        await db.close()

    shutdown_time = (
        datetime.fromisoformat(last_shutdown).replace(tzinfo=timezone.utc)
        if last_shutdown else None
    )

    # 1. Re-snapshot state
    log.info("Step 1/3: Re-snapshotting server state...")
    await snapshot_guild(guild)

    # 2. Catch up messages per channel
    log.info("Step 2/3: Catching up messages since downtime...")
    total_new = 0
    text_channels = [
        ch for ch in guild.channels
        if isinstance(ch, discord.TextChannel)
    ]

    db = await get_db()
    try:
        for i, channel in enumerate(text_channels, 1):
            try:
                last_id = await get_last_message_id(db, channel.id)
                if last_id:
                    after = discord.Object(id=last_id)
                else:
                    # Never seen this channel — full backfill for it
                    after = None

                count = await backfill_channel(channel, after=after)
                if count > 0:
                    total_new += count
                    log.info("  [%d/%d] #%s: %d new messages",
                             i, len(text_channels), channel.name, count)
            except discord.Forbidden:
                pass
            except Exception as e:
                log.error("  [%d/%d] #%s: error: %s",
                          i, len(text_channels), channel.name, e)
    finally:
        await db.close()

    log.info("  Recovered %d messages", total_new)

    # 3. Audit log since shutdown
    log.info("Step 3/3: Fetching audit log since downtime...")
    audit_count = await backfill_audit_log(guild, after_time=shutdown_time)
    log.info("  %d audit log entries", audit_count)

    log.info("=== RECOVERY COMPLETE: %s ===", guild.name)
