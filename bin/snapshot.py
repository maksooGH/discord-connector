#!/usr/bin/env python3
"""Refresh guild state (channels, roles, members, permission overwrites).

Cheap and idempotent — run it before any analysis that depends on current roles
or channel access. The capture daemon does this on startup, but it only restarts
occasionally, so live role changes can lag until an event arrives.

    python bin/snapshot.py            # all guilds
    python bin/snapshot.py <guild_id> # one
"""
import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import discord
from dotenv import load_dotenv
from core.capture.db import init_db
from core.capture.backfill import snapshot_guild

load_dotenv()


def main() -> None:
    only = int(sys.argv[1]) if len(sys.argv) > 1 else None
    client = discord.Client(intents=discord.Intents.all())

    @client.event
    async def on_ready():
        await init_db()
        for g in client.guilds:
            if only and g.id != only:
                continue
            await snapshot_guild(g)
            print(f"  {g.name}: {len(g.channels)} channels, {g.member_count} members")
        await client.close()

    client.run(os.environ["DISCORD_BOT_TOKEN"], log_handler=None)


if __name__ == "__main__":
    main()
