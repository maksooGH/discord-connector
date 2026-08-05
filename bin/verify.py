#!/usr/bin/env python3
"""Audit the capture DB for gaps.

    python bin/verify.py             # report only
    python bin/verify.py --repair    # fix what can be fixed offline
    python bin/verify.py --live      # permission-gap + rejoin checks (needs Discord)
    python bin/verify.py --live --repair
"""
import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from core import query as q, verify

load_dotenv()


def main() -> None:
    apply = "--repair" in sys.argv
    con = q.connect()

    print("=== offline audit ===")
    print(verify.audit(con).render())

    if apply:
        print("\n=== repair ===")
        for k, v in verify.repair(con, apply=True).items():
            print(f"  {k}: {v}")

    if "--live" not in sys.argv:
        return

    import discord
    client = discord.Client(intents=discord.Intents.all())

    @client.event
    async def on_ready():
        print("\n=== live checks ===")
        rejoined = await verify.fix_rejoined_members(client, con, apply=apply)
        print(f"  falsely marked as departed: {len(rejoined)}"
              + (" (fixed)" if apply else " (run --repair to fix)"))
        for g, u in rejoined:
            print(f"     {g}: {u}")
        for guild in client.guilds:
            missing = await verify.find_missing_channels(client, con, guild.id)
            flag = "  <-- PERMISSION GAP" if missing else ""
            print(f"  {guild.name}: {len(missing)} readable channels with uncaptured history{flag}")
            for ch in missing[:10]:
                print(f"     #{ch.name}")
            if missing:
                print(f"     fix: python bin/backfill.py {guild.id}")
        await client.close()

    client.run(os.environ["DISCORD_BOT_TOKEN"], log_handler=None)


if __name__ == "__main__":
    main()
