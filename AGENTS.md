# Instructions for agents

You are working in a repo that mirrors Discord servers into SQLite. Read
`ARCHITECTURE.md` first — it contains the failure modes that produce confidently
wrong answers.

## Where to put code

Write throwaway analysis in **`scratch/`**. It is gitignored. One question, one
script, never maintained. Import from `core/`; do not re-implement queries.

If you write the same helper twice in `scratch/`, promote it to `core/`.

## The rules that matter

**1. Never key on names.** A person's username, display name, chat channel and
payment channel are routinely four different strings, and channels get renamed
without warning. Observed: `ant__9`→`#ninetysevenxbt`, `igorb2457`→`#igor-frank`,
`mg045821`→`#gok`, `kagibuu`→`#dan`.

Use `core.attribution.build()` — it resolves ownership from who posts in a
channel. Check `.name_matched_only()` and treat those as provisional, and read
`.unowned()` — an unowned channel usually means a departed member or a naming
pattern the fallback misses.

**2. Never call Discord from an analysis script.** `core.query` reads the DB.
Live calls are for `core.verify` and the capture daemon only.

**3. `role_ids` is a JSON array string.** Use `query.role_ids()`. Splitting on
commas returns nothing and fails silently.

**4. Membership is per-guild; identity is global.** `users` has no display name —
it lives on `memberships` and differs per server. Active membership is
`left_at IS NULL`, not an `is_active` column.

**5. Check your denominator before reporting coverage.** "X of Y creators are
missing" is wrong if Y counts people who are exempt. Category and role determine
eligibility, not role alone.

## Before reporting a gap

Verify it against the live server. Several times a "missing" record turned out to
be a rename, a shared channel, an overflow category (`PAYMENTS 2`), or a
submission that landed after the last read. State when your data was captured.

## Useful entry points

```python
from core import query as q, attribution as attr, verify

con = q.connect()
gid = q.guild_id_by_name(con, "server name")

q.guilds(con)                       # all captured servers + counts
q.channels(con, gid, category_like="CHATS")
q.members(con, gid)                 # parsed roles as set[str]
q.members_with_role(con, gid, role_id)
q.messages(con, channel_id=cid, since="2026-08-01")
q.conversation(con, cid)            # rendered text, good for reading
q.search(con, "retainer", guild_id=gid)
q.dormant(con, gid, days=30)
q.cross_guild(con)                  # people in more than one server
q.amounts("owed $1,250.00")         # -> [1250.0]

idx = attr.build(con, gid, staff_role_ids={"123"})
idx.channels_for(user_id)
idx.owner_of(channel_id)
idx.unowned()

verify.audit(con).render()
```

## Style

Match the surrounding code. Type hints on signatures, docstrings that explain
*why* rather than restating the signature. Prefer small readable functions over
clever ones — these scripts are read once by a human under time pressure.
