#!/usr/bin/env python3
"""Re-run a full backfill for one guild.

Use after granting the bot access to channels it could not see when it joined —
the join-time backfill will have silently captured almost nothing.

    python bin/backfill.py 123456789
"""
import asyncio, os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import discord
from dotenv import load_dotenv
from core.capture.backfill import run_full_backfill

load_dotenv()


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: backfill.py <guild_id>")
    guild_id = int(sys.argv[1])
    client = discord.Client(intents=discord.Intents.all())

    @client.event
    async def on_ready():
        guild = client.get_guild(guild_id)
        if guild is None:
            print(f"guild {guild_id} not visible to this token")
        else:
            await run_full_backfill(guild)
            print(f"done: {guild.name}")
        await client.close()

    client.run(os.environ["DISCORD_BOT_TOKEN"], log_handler=None)


if __name__ == "__main__":
    main()
