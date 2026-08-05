# discord-connector

Capture a Discord server into SQLite, then query it offline.

A long-running bot mirrors everything it can see — messages, edits, deletes,
channels, roles, members, audit log — into a local SQLite file. Analysis then
runs against that file: instant, repeatable, no rate limits, no risk of mutating
a server.

Built for servers where each member has their own private channel (support
tickets, per-creator chats, client rooms) and you need to answer questions across
all of them.

## Quickstart

```bash
git clone <this repo> && cd discord-connector
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # add DISCORD_BOT_TOKEN

python bin/run_bot.py     # starts capture; backfills on first join
```

Then, in any script:

```python
from core import query as q

con = q.connect()
gid = q.guild_id_by_name(con, "my server")
print(q.message_count(con, gid))
for m in q.dormant(con, gid, days=30):
    print(m["username"], m["days_inactive"])
```

## Layout

```
core/
  capture/        the daemon — gateway events + backfill. Writes the DB.
    main.py       entry point, on_ready / on_guild_join
    events.py     ~20 live event handlers
    backfill.py   full history walk, audit log, recovery
    db.py         schema + upserts
  query.py        read helpers — start here when writing a script
  attribution.py  which member owns which channel (by authorship, not name)
  verify.py       gap detection + repair
  sheets.py       Google Sheets read/write via service account
bin/              thin CLI wrappers
scratch/          throwaway scripts (gitignored)
docs/             SETUP.md
ARCHITECTURE.md   read before writing code
AGENTS.md         instructions for LLM agents
```

## The two rules

**Never key on names.** Channel names, usernames and display names drift
constantly and independently. Use ids, and use `core.attribution` to map channels
to people.

**Never call Discord from an analysis script.** `core.query` reads the DB. That
is enough for almost everything and it is instant.

## Commands

```bash
python bin/run_bot.py                 # start capture (run under a supervisor)
python bin/backfill.py <guild_id>     # re-run a full backfill for one guild
python bin/verify.py                  # audit the DB for gaps
python bin/verify.py --repair         # fix what can be fixed offline
python bin/verify.py --live           # permission-gap + rejoin checks
```

## Limits

- **DMs are invisible.** A bot cannot read direct messages.
- **Only what it can see.** If the bot lacks channel access at backfill time it
  captures nothing there and still reports success — run `bin/verify.py --live`
  after granting permissions. See ARCHITECTURE.md.
- **Bot token only.** Using a personal account token violates Discord's Terms of
  Service and gets accounts terminated. Not supported here.
