"""Detect and repair gaps in the capture database.

The capture bot is reliable while it is running with the right permissions. The
failure modes it cannot see itself are:

1. PERMISSION GAPS. The bot joins a server and immediately backfills. If it was
   not yet granted access to private channels, that backfill silently captures
   almost nothing — 7 of 135 channels on one server, 36 of 230 on another. The
   log records "Forbidden" and the bot carries on looking healthy.

2. REJOIN. `left_at` is set when a member leaves and never cleared when they
   come back, so a returning member reads as departed indefinitely.

3. DUPLICATE AUDIT EVENTS. `insert_event` has no unique constraint, so every
   re-run of the audit backfill duplicates the whole log.

4. ORPHAN AUTHORS. Backfilled messages upsert the message but not always the
   author, leaving messages whose author_id has no users row.

`audit()` reports all of these. `repair()` fixes what can be fixed from the DB
alone; permission gaps need a live client, so `find_missing_channels()` takes one.

    from core import verify
    report = verify.audit(con)
    verify.repair(con, apply=True)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import query as q


@dataclass
class Finding:
    kind: str
    detail: str
    count: int
    fixable: bool


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, kind: str, detail: str, count: int, fixable: bool = False) -> None:
        if count:
            self.findings.append(Finding(kind, detail, count, fixable))

    def __bool__(self) -> bool:
        return bool(self.findings)

    def render(self) -> str:
        if not self.findings:
            return "No gaps found."
        w = max(len(f.kind) for f in self.findings)
        return "\n".join(
            f"  {f.kind.ljust(w)}  {f.count:>6}  {f.detail}" + ("  [fixable]" if f.fixable else "")
            for f in self.findings)


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

def audit(con: sqlite3.Connection) -> Report:
    """Check the DB for the known integrity problems. Read-only."""
    r = Report()

    r.add("duplicate-audit-events",
          "audit rows sharing an audit_id — no unique constraint on events",
          q.one(con, """SELECT COUNT(*) - COUNT(DISTINCT guild_id || ':' ||
                        COALESCE(json_extract(data,'$.audit_id'),rowid))
                        FROM events WHERE event_type LIKE 'audit_%'""") or 0,
          fixable=True)

    r.add("orphan-authors",
          "messages whose author has no users row",
          q.one(con, """SELECT COUNT(*) FROM messages m
                        LEFT JOIN users u ON u.id = m.author_id WHERE u.id IS NULL""") or 0)

    r.add("orphan-channels",
          "messages whose channel has no channels row",
          q.one(con, """SELECT COUNT(*) FROM messages m
                        LEFT JOIN channels c ON c.id = m.channel_id WHERE c.id IS NULL""") or 0)

    r.add("legacy-table",
          "rows in members_legacy, superseded by memberships",
          q.one(con, "SELECT COUNT(*) FROM members_legacy") or 0
          if _has_table(con, "members_legacy") else 0,
          fixable=True)

    missing_idx = [name for name, sql in (
        ("messages(guild_id)", "CREATE INDEX IF NOT EXISTS idx_messages_guild ON messages(guild_id)"),
        ("messages(created_at)", "CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at)"),
        ("channels(guild_id)", "CREATE INDEX IF NOT EXISTS idx_channels_guild ON channels(guild_id)"),
    ) if not _has_index(con, name.split("(")[0], sql)]
    r.add("missing-index", "indexes that make common queries fast: " + ", ".join(missing_idx),
          len(missing_idx), fixable=True)
    return r


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return bool(q.one(con, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", name))


def _has_index(con: sqlite3.Connection, table: str, create_sql: str) -> bool:
    want = create_sql.split("ON ")[-1].strip()
    for r in q.rows(con, "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=?", table):
        if r["sql"] and want.replace(" ", "") in r["sql"].replace(" ", ""):
            return True
    return False


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------

def repair(con: sqlite3.Connection, *, apply: bool = False) -> dict[str, int]:
    """Fix what can be fixed without talking to Discord.

    Pass apply=False (default) for a dry run. Nothing is deleted that cannot be
    reconstructed: duplicate audit rows keep the OLDEST copy, because the
    `changes` field embeds a repr of live objects captured at fetch time and the
    earliest fetch is closest to the event.
    """
    done: dict[str, int] = {}

    dupes = q.rows(con, """
        SELECT rowid AS rid, guild_id, json_extract(data,'$.audit_id') AS aid
        FROM events WHERE event_type LIKE 'audit_%'
          AND json_extract(data,'$.audit_id') IS NOT NULL
        ORDER BY guild_id, aid, rid""")
    seen: set[tuple] = set()
    kill: list[int] = []
    for row in dupes:
        key = (row["guild_id"], row["aid"])
        if key in seen:
            kill.append(row["rid"])   # keep the OLDEST row for each audit_id
        else:
            seen.add(key)
    done["duplicate_audit_events"] = len(kill)
    if apply and kill:
        con.executemany("DELETE FROM events WHERE rowid = ?", [(k,) for k in kill])

    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_messages_guild ON messages(guild_id)",
        "CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_channels_guild ON channels(guild_id)",
        # stops the duplicate-audit bug recurring
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_audit_unique "
        "ON events(guild_id, json_extract(data,'$.audit_id')) "
        "WHERE json_extract(data,'$.audit_id') IS NOT NULL",
    ):
        if apply:
            con.execute(sql)
    done["indexes_created"] = 4 if apply else 0

    if _has_table(con, "members_legacy"):
        done["legacy_rows_dropped"] = q.one(con, "SELECT COUNT(*) FROM members_legacy") or 0
        if apply:
            con.execute("DROP TABLE members_legacy")

    if apply:
        con.commit()
    return done


# ---------------------------------------------------------------------------
# live checks — need a connected discord client
# ---------------------------------------------------------------------------

async def find_missing_channels(client, con: sqlite3.Connection, guild_id: int) -> list:
    """Channels the bot can read that have content on Discord but nothing in the DB.

    This is the permission-gap check. Run it after granting access to a server the
    bot joined blind, or on a schedule — it is the only way to notice that a
    backfill silently captured a fraction of the server.
    """
    guild = client.get_guild(guild_id)
    if guild is None:
        return []
    captured = {r["channel_id"] for r in q.rows(
        con, "SELECT DISTINCT channel_id FROM messages WHERE guild_id = ?", guild_id)}
    missing = []
    for ch in guild.text_channels:
        perms = ch.permissions_for(guild.me)
        if not (perms.view_channel and perms.read_message_history):
            continue
        if ch.id in captured:
            continue
        try:
            async for _ in ch.history(limit=1):
                missing.append(ch)
                break
        except Exception:
            continue
    return missing


async def fix_rejoined_members(client, con: sqlite3.Connection, *, apply: bool = False) -> list[tuple]:
    """Clear left_at for members flagged as departed who are actually present.

    Root cause is in the capture layer: on_member_join does not clear left_at.
    Until that is patched this needs running periodically, or any membership
    query will under-report.
    """
    fixed = []
    for guild in client.guilds:
        present = {m.id for m in guild.members}
        for r in q.rows(con, """SELECT m.user_id, u.username FROM memberships m
                                JOIN users u ON u.id = m.user_id
                                WHERE m.guild_id = ? AND m.left_at IS NOT NULL""", guild.id):
            if r["user_id"] in present:
                fixed.append((guild.name, r["username"]))
                if apply:
                    con.execute("UPDATE memberships SET left_at = NULL WHERE guild_id = ? AND user_id = ?",
                                (guild.id, r["user_id"]))
    if apply:
        con.commit()
    return fixed
