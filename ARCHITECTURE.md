# Architecture

Read this before writing code against the database.

## The shape

```
Discord gateway ──► core/capture ──► SQLite ──► core/query ──► your script
   (live events)     (daemon)        (file)     (library)      (scratch/)
```

Two layers with a hard boundary between them:

**Capture** (`core/capture/`) is a long-running daemon. It holds a gateway
connection and writes everything it sees. It never reads for analysis.

**Query** (`core/query.py`, `attribution.py`, `verify.py`) reads the SQLite file.
It never talks to Discord. Analysis is therefore instant, repeatable, offline,
and cannot be rate-limited or accidentally mutate a server.

Keep that boundary. The temptation when writing a script is to fetch something
live "just this once"; that turns a 200 ms query into a rate-limited crawl and
makes the result unreproducible.

## How data arrives

Three paths, all converging on the same upserts:

1. **Live events** — ~20 handlers in `capture/events.py`: messages, edits,
   deletes, bulk deletes, channel create/update/delete, role create/update/delete,
   member join/leave/update, bans, threads.
2. **Join backfill** — `on_guild_join` runs a full history walk of every readable
   channel plus the audit log.
3. **Startup recovery** — `on_ready` snapshots every guild (channels, roles, all
   members) then, per guild: no messages yet → full backfill; otherwise → recover
   the gap since `last_shutdown`.

Messages upsert on Discord's own message id, so re-running a backfill is
idempotent — it repairs holes without duplicating rows.

## Schema

| table | holds | notes |
|---|---|---|
| `guilds` | servers | |
| `channels` | channels + categories | `deleted=1` retains history after removal |
| `messages` | content, attachments, embeds | `deleted=1` retains tombstones |
| `users` | global user identity | one row per person across all guilds |
| `memberships` | per-guild: display name, joined_at, left_at, role_ids | `role_ids` is a **JSON array string** |
| `roles` | role definitions | |
| `events` | audit log + gateway events | |
| `media` | attachment metadata | |
| `bot_state` | last_startup, per-guild backfill markers | |

Two schema facts that catch people out:

- **`role_ids` is JSON, not CSV.** Use `query.role_ids(raw)`; splitting on commas
  silently returns nothing.
- **Membership is per-guild, identity is global.** `users` has no display name —
  that lives on `memberships`, and differs per server.

## Identity: use ids, never names

The single most important rule. In practice a person has four different
identifiers and none of them agree:

| | example |
|---|---|
| username | `ant__9` |
| display name | `Anthony` |
| chat channel | `#ninetysevenxbt` |
| payment channel | `#anthonyxbt` |

Real observed renames: `igorb2457` → `#igor-frank` (word order reversed),
`mg045821` → `#gok`, `kagibuu` → `#dan`, `sam_012345678` → `#v_a_n_t_h_e_r`
(later renamed back). Any lookup built on names will drift silently and produce
confidently wrong answers.

Use `core.attribution` — it derives ownership from **who posts in a channel** and
falls back to name matching only for channels nobody has posted in yet, flagging
those as provisional.

## Known failure modes

These are the things the capture layer cannot detect about itself. Run
`core.verify.audit()` on a schedule.

**Permission gaps.** The bot backfills the moment it joins. If private channels
have not been granted yet, that backfill captures almost nothing and still logs
"complete". Observed: 7/135 channels, 36/230, 11/58 on three different servers.
Fix: grant access, then re-run the backfill. `verify.find_missing_channels()`
detects it.

**Rejoin.** `left_at` is set on leave and never cleared on rejoin, so a returning
member reads as departed forever. `verify.fix_rejoined_members()` repairs it.

**Duplicate audit events.** `insert_event` had no unique constraint, so every
audit backfill duplicated the whole log. `verify.repair()` dedupes and adds the
constraint.

**DMs are invisible.** A bot cannot read direct messages. Any decision made in a
DM leaves no trace here. If something matters, it has to happen in a channel.

## Permissions

Functionally the bot needs three: **View Channels**, **Read Message History**,
**View Audit Log** — `permissions=66688`. It is read-only; there is no `send()`
anywhere in `core/capture/`.

But Discord resolves channel overwrites *after* role permissions, and a private
channel that denies `@everyone` blocks any role not explicitly allowed on it.
Server-level grants do **not** override a channel-level deny. **Administrator is
the only permission that bypasses overwrites.**

So on a server built from private per-member channels, the practical choice is:

- **Administrator** — sees everything immediately, zero maintenance.
- **66688 + per-category grants** — least privilege, but the bot's role must be
  added to every private category, and any hand-made channel outside the ticket
  tool's template will be missed.

Measured on one production server: no single staff role covered more than 76% of
private channels. Choose accordingly.

## Intents

Required, enabled in the Developer Portal:

- **Server Members** — membership, roles, joins/leaves
- **Message Content** — without it every `content` is empty

**Presence is not needed.** `capture/main.py` requests `Intents.all()`, which
includes it; narrow that to `default() + members + message_content` if you want
to switch it off. Do the code change *first* — disabling an intent the code still
requests stops the bot booting.
