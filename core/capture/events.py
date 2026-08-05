"""Gateway event handlers — logs everything to SQLite."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import discord

from .db import (
    get_db, upsert_channel, upsert_role, upsert_member, upsert_message,
    insert_event, insert_media,
)

log = logging.getLogger(__name__)


def _msg_to_dict(msg: discord.Message) -> dict[str, Any]:
    """Convert a discord.Message to DB-ready kwargs."""
    attachments = [
        {"id": a.id, "url": a.url, "filename": a.filename,
         "content_type": a.content_type, "size": a.size}
        for a in msg.attachments
    ]
    embeds = [e.to_dict() for e in msg.embeds]
    return dict(
        id=msg.id,
        guild_id=msg.guild.id if msg.guild else 0,
        channel_id=msg.channel.id,
        author_id=msg.author.id,
        content=msg.content,
        attachments=json.dumps(attachments),
        embeds=json.dumps(embeds),
        reference_id=msg.reference.message_id if msg.reference else None,
        is_pinned=int(msg.pinned),
        created_at=msg.created_at.isoformat(),
        edited_at=msg.edited_at.isoformat() if msg.edited_at else None,
    )


def _channel_to_dict(ch: discord.abc.GuildChannel) -> dict[str, Any]:
    return dict(
        id=ch.id,
        guild_id=ch.guild.id,
        name=ch.name,
        type=str(ch.type),
        category_id=ch.category_id,
        category_name=ch.category.name if ch.category else None,
        topic=getattr(ch, "topic", None),
        position=ch.position,
        created_at=ch.created_at.isoformat(),
    )


def _role_to_dict(role: discord.Role) -> dict[str, Any]:
    return dict(
        id=role.id,
        guild_id=role.guild.id,
        name=role.name,
        color=role.color.value,
        position=role.position,
        permissions=role.permissions.value,
        member_count=len(role.members),
    )


def _member_to_dict(member: discord.Member) -> dict[str, Any]:
    return dict(
        id=member.id,
        guild_id=member.guild.id,
        username=member.name,
        display_name=member.display_name,
        discriminator=member.discriminator,
        avatar_url=str(member.display_avatar.url) if member.display_avatar else None,
        bot=int(member.bot),
        joined_at=member.joined_at.isoformat() if member.joined_at else None,
        role_ids=json.dumps([r.id for r in member.roles]),
    )


def setup_events(bot: discord.Client) -> None:
    """Register all event handlers on the bot."""

    # -----------------------------------------------------------------------
    # Messages
    # -----------------------------------------------------------------------

    @bot.event
    async def on_message(message: discord.Message):
        if not message.guild:
            return
        db = await get_db()
        try:
            await upsert_message(db, **_msg_to_dict(message))
            # Track media
            for a in message.attachments:
                await insert_media(db, message_id=message.id, url=a.url,
                                   filename=a.filename, content_type=a.content_type,
                                   size=a.size)
            await db.commit()
        finally:
            await db.close()

    @bot.event
    async def on_message_edit(before: discord.Message, after: discord.Message):
        if not after.guild:
            return
        db = await get_db()
        try:
            await upsert_message(db, **_msg_to_dict(after))
            await insert_event(db, after.guild.id, "message_edit", {
                "message_id": after.id, "channel_id": after.channel.id,
                "before_content": before.content, "after_content": after.content,
            })
            await db.commit()
        finally:
            await db.close()

    @bot.event
    async def on_message_delete(message: discord.Message):
        if not message.guild:
            return
        db = await get_db()
        try:
            await db.execute(
                "UPDATE messages SET deleted = 1, updated_at = datetime('now') WHERE id = ?",
                (message.id,),
            )
            await insert_event(db, message.guild.id, "message_delete", {
                "message_id": message.id, "channel_id": message.channel.id,
                "author_id": message.author.id, "content": message.content,
            })
            await db.commit()
        finally:
            await db.close()

    @bot.event
    async def on_bulk_message_delete(messages: list[discord.Message]):
        if not messages or not messages[0].guild:
            return
        db = await get_db()
        try:
            ids = [m.id for m in messages]
            placeholders = ",".join("?" * len(ids))
            await db.execute(
                f"UPDATE messages SET deleted = 1, updated_at = datetime('now') WHERE id IN ({placeholders})",
                ids,
            )
            await insert_event(db, messages[0].guild.id, "bulk_message_delete", {
                "message_ids": ids, "channel_id": messages[0].channel.id,
            })
            await db.commit()
        finally:
            await db.close()

    # -----------------------------------------------------------------------
    # Channels
    # -----------------------------------------------------------------------

    @bot.event
    async def on_guild_channel_create(channel: discord.abc.GuildChannel):
        db = await get_db()
        try:
            await upsert_channel(db, **_channel_to_dict(channel))
            await insert_event(db, channel.guild.id, "channel_create", {
                "channel_id": channel.id, "name": channel.name, "type": str(channel.type),
            })
            await db.commit()
        finally:
            await db.close()

    @bot.event
    async def on_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        db = await get_db()
        try:
            await upsert_channel(db, **_channel_to_dict(after))
            changes = {}
            if before.name != after.name:
                changes["name"] = {"before": before.name, "after": after.name}
            if getattr(before, "topic", None) != getattr(after, "topic", None):
                changes["topic"] = {"before": getattr(before, "topic", None), "after": getattr(after, "topic", None)}
            if before.position != after.position:
                changes["position"] = {"before": before.position, "after": after.position}
            await insert_event(db, after.guild.id, "channel_update", {
                "channel_id": after.id, "changes": changes,
            })
            await db.commit()
        finally:
            await db.close()

    @bot.event
    async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
        db = await get_db()
        try:
            await db.execute(
                "UPDATE channels SET deleted = 1, updated_at = datetime('now') WHERE id = ?",
                (channel.id,),
            )
            await insert_event(db, channel.guild.id, "channel_delete", {
                "channel_id": channel.id, "name": channel.name,
            })
            await db.commit()
        finally:
            await db.close()

    # -----------------------------------------------------------------------
    # Roles
    # -----------------------------------------------------------------------

    @bot.event
    async def on_guild_role_create(role: discord.Role):
        db = await get_db()
        try:
            await upsert_role(db, **_role_to_dict(role))
            await insert_event(db, role.guild.id, "role_create", {
                "role_id": role.id, "name": role.name,
            })
            await db.commit()
        finally:
            await db.close()

    @bot.event
    async def on_guild_role_update(before: discord.Role, after: discord.Role):
        db = await get_db()
        try:
            await upsert_role(db, **_role_to_dict(after))
            changes = {}
            if before.name != after.name:
                changes["name"] = {"before": before.name, "after": after.name}
            if before.permissions != after.permissions:
                changes["permissions"] = {"before": before.permissions.value, "after": after.permissions.value}
            if before.color != after.color:
                changes["color"] = {"before": before.color.value, "after": after.color.value}
            await insert_event(db, after.guild.id, "role_update", {
                "role_id": after.id, "changes": changes,
            })
            await db.commit()
        finally:
            await db.close()

    @bot.event
    async def on_guild_role_delete(role: discord.Role):
        db = await get_db()
        try:
            await db.execute(
                "UPDATE roles SET deleted = 1, updated_at = datetime('now') WHERE id = ?",
                (role.id,),
            )
            await insert_event(db, role.guild.id, "role_delete", {
                "role_id": role.id, "name": role.name,
            })
            await db.commit()
        finally:
            await db.close()

    # -----------------------------------------------------------------------
    # Members
    # -----------------------------------------------------------------------

    @bot.event
    async def on_member_join(member: discord.Member):
        db = await get_db()
        try:
            await upsert_member(db, **_member_to_dict(member))
            await insert_event(db, member.guild.id, "member_join", {
                "member_id": member.id, "username": member.name,
            })
            await db.commit()
        finally:
            await db.close()

    @bot.event
    async def on_member_remove(member: discord.Member):
        db = await get_db()
        try:
            await db.execute(
                "UPDATE memberships SET left_at = datetime('now'), updated_at = datetime('now') "
                "WHERE user_id = ? AND guild_id = ?",
                (member.id, member.guild.id),
            )
            await insert_event(db, member.guild.id, "member_remove", {
                "member_id": member.id, "username": member.name,
            })
            await db.commit()
        finally:
            await db.close()

    @bot.event
    async def on_member_update(before: discord.Member, after: discord.Member):
        db = await get_db()
        try:
            await upsert_member(db, **_member_to_dict(after))
            changes = {}
            if before.display_name != after.display_name:
                changes["display_name"] = {"before": before.display_name, "after": after.display_name}
            if set(r.id for r in before.roles) != set(r.id for r in after.roles):
                changes["roles"] = {
                    "added": [r.name for r in after.roles if r not in before.roles],
                    "removed": [r.name for r in before.roles if r not in after.roles],
                }
            if changes:
                await insert_event(db, after.guild.id, "member_update", {
                    "member_id": after.id, "changes": changes,
                })
            await db.commit()
        finally:
            await db.close()

    @bot.event
    async def on_member_ban(guild: discord.Guild, user: discord.User):
        db = await get_db()
        try:
            await insert_event(db, guild.id, "member_ban", {
                "user_id": user.id, "username": user.name,
            })
            await db.commit()
        finally:
            await db.close()

    @bot.event
    async def on_member_unban(guild: discord.Guild, user: discord.User):
        db = await get_db()
        try:
            await insert_event(db, guild.id, "member_unban", {
                "user_id": user.id, "username": user.name,
            })
            await db.commit()
        finally:
            await db.close()

    # -----------------------------------------------------------------------
    # Reactions
    # -----------------------------------------------------------------------

    @bot.event
    async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
        if not reaction.message.guild:
            return
        db = await get_db()
        try:
            await insert_event(db, reaction.message.guild.id, "reaction_add", {
                "message_id": reaction.message.id, "channel_id": reaction.message.channel.id,
                "user_id": user.id, "emoji": str(reaction.emoji),
            })
            await db.commit()
        finally:
            await db.close()

    @bot.event
    async def on_reaction_remove(reaction: discord.Reaction, user: discord.User):
        if not reaction.message.guild:
            return
        db = await get_db()
        try:
            await insert_event(db, reaction.message.guild.id, "reaction_remove", {
                "message_id": reaction.message.id, "channel_id": reaction.message.channel.id,
                "user_id": user.id, "emoji": str(reaction.emoji),
            })
            await db.commit()
        finally:
            await db.close()

    # -----------------------------------------------------------------------
    # Voice
    # -----------------------------------------------------------------------

    @bot.event
    async def on_voice_state_update(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        db = await get_db()
        try:
            await insert_event(db, member.guild.id, "voice_state", {
                "member_id": member.id, "username": member.name,
                "before_channel": before.channel.id if before.channel else None,
                "after_channel": after.channel.id if after.channel else None,
                "self_mute": after.self_mute, "self_deaf": after.self_deaf,
            })
            await db.commit()
        finally:
            await db.close()

    # -----------------------------------------------------------------------
    # Threads
    # -----------------------------------------------------------------------

    @bot.event
    async def on_thread_create(thread: discord.Thread):
        db = await get_db()
        try:
            await upsert_channel(db, id=thread.id, guild_id=thread.guild.id,
                                 name=thread.name, type=str(thread.type),
                                 category_id=thread.parent_id,
                                 category_name=thread.parent.name if thread.parent else None,
                                 topic=None, position=0,
                                 created_at=thread.created_at.isoformat() if thread.created_at else None)
            await insert_event(db, thread.guild.id, "thread_create", {
                "thread_id": thread.id, "name": thread.name, "parent_id": thread.parent_id,
            })
            await db.commit()
        finally:
            await db.close()

    @bot.event
    async def on_thread_update(before: discord.Thread, after: discord.Thread):
        db = await get_db()
        try:
            await insert_event(db, after.guild.id, "thread_update", {
                "thread_id": after.id, "name": after.name, "archived": after.archived,
            })
            await db.commit()
        finally:
            await db.close()

    @bot.event
    async def on_thread_delete(thread: discord.Thread):
        db = await get_db()
        try:
            await db.execute(
                "UPDATE channels SET deleted = 1, updated_at = datetime('now') WHERE id = ?",
                (thread.id,),
            )
            await insert_event(db, thread.guild.id, "thread_delete", {
                "thread_id": thread.id, "name": thread.name,
            })
            await db.commit()
        finally:
            await db.close()

    # -----------------------------------------------------------------------
    # Guild-level
    # -----------------------------------------------------------------------

    @bot.event
    async def on_guild_update(before: discord.Guild, after: discord.Guild):
        db = await get_db()
        try:
            await upsert_guild(db, id=after.id, name=after.name,
                               icon_url=str(after.icon.url) if after.icon else None,
                               owner_id=after.owner_id,
                               member_count=after.member_count,
                               created_at=after.created_at.isoformat())
            changes = {}
            if before.name != after.name:
                changes["name"] = {"before": before.name, "after": after.name}
            if before.icon != after.icon:
                changes["icon"] = "changed"
            await insert_event(db, after.id, "guild_update", {"changes": changes})
            await db.commit()
        finally:
            await db.close()

    @bot.event
    async def on_guild_emojis_update(guild: discord.Guild, before, after):
        db = await get_db()
        try:
            await insert_event(db, guild.id, "emojis_update", {
                "before_count": len(before), "after_count": len(after),
            })
            await db.commit()
        finally:
            await db.close()

    log.info("All event handlers registered.")
