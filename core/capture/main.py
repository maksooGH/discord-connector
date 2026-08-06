"""Discord bot entry point — connects to Gateway with all intents."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import discord
from dotenv import load_dotenv

# .env must load before anything reads os.environ at import time.
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from .db import init_db, get_db, set_bot_state, get_bot_state  # noqa: E402
from .events import setup_events  # noqa: E402
from .backfill import run_recovery, run_full_backfill, snapshot_guild  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(os.environ.get("DISCORD_LOG", Path(__file__).resolve().parent.parent.parent / "data" / "bot.log"))),
    ],
)
log = logging.getLogger("discord-bot")


def create_bot() -> discord.Client:
    intents = discord.Intents.all()
    bot = discord.Client(intents=intents)

    async def _has_messages_for_guild(guild_id: int) -> bool:
        """True if this guild already has at least one message in the DB."""
        db = await get_db()
        try:
            cur = await db.execute(
                "SELECT 1 FROM messages WHERE guild_id = ? LIMIT 1", (guild_id,)
            )
            row = await cur.fetchone()
            return row is not None
        finally:
            await db.close()

    @bot.event
    async def on_ready():
        log.info("Connected as %s (ID: %s)", bot.user, bot.user.id)
        log.info("Guilds: %s", [g.name for g in bot.guilds])

        db = await get_db()
        try:
            last_shutdown = await get_bot_state(db, "last_shutdown")
            await set_bot_state(db, "last_startup", datetime.now(timezone.utc).isoformat())
            await db.commit()
        finally:
            await db.close()

        # Always refresh server state snapshot (channels, roles, members) on
        # startup — it's cheap and ensures cross-guild memberships stay accurate
        # for users who are in multiple servers.
        # Then per-guild logic: a guild with no messages → full backfill,
        # otherwise → recovery (catch up missed events while bot was offline).
        for guild in bot.guilds:
            try:
                log.info("Refreshing snapshot for %s", guild.name)
                await snapshot_guild(guild)
            except Exception as e:
                log.exception("Snapshot failed for %s: %s", guild.name, e)

            has_messages = await _has_messages_for_guild(guild.id)
            if not has_messages:
                log.info("First time seeing %s — full backfill", guild.name)
                await run_full_backfill(guild)
            elif last_shutdown:
                log.info("Recovering missed data since %s for %s", last_shutdown, guild.name)
                await run_recovery(guild)
            else:
                log.info("No last_shutdown recorded but %s already has messages — skipping message backfill", guild.name)

        log.info("Bot ready and listening.")

    @bot.event
    async def on_guild_join(guild: discord.Guild):
        """Triggered when the bot is added to a new server while running."""
        log.info("Joined new guild: %s (ID: %s) — running full backfill", guild.name, guild.id)
        try:
            await run_full_backfill(guild)
            log.info("Backfill complete for %s", guild.name)
        except Exception as e:
            log.exception("Backfill failed for %s: %s", guild.name, e)

    setup_events(bot)
    return bot


async def shutdown_hook(bot: discord.Client) -> None:
    """Record shutdown time so next startup knows the gap."""
    db = await get_db()
    try:
        await set_bot_state(db, "last_shutdown", datetime.now(timezone.utc).isoformat())
        await db.commit()
    finally:
        await db.close()
    log.info("Shutdown recorded.")


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        log.error("DISCORD_BOT_TOKEN not set in .env")
        sys.exit(1)

    bot = create_bot()

    async def runner():
        await init_db()
        try:
            await bot.start(token)
        finally:
            await shutdown_hook(bot)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        log.info("Interrupted by user.")


if __name__ == "__main__":
    main()
