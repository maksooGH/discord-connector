"""Read helpers over the capture database.

This is the layer you import when writing a throwaway analysis script. Nothing
here talks to Discord — it only reads the SQLite file the capture bot writes.
That is deliberate: analysis should be instant and repeatable, and should never
be rate-limited or change server state.

Everything is keyed on Discord snowflake IDs, never on names. Channel and
display names are renamed constantly in practice (a creator's chat channel, their
payment ticket, their username and their display name are frequently four
different strings), so any lookup built on names will silently drift.

    from core import query as q

    con = q.connect()
    for g in q.guilds(con):
        print(g["name"], q.message_count(con, g["id"]))
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

# Absolute by default. A CWD-relative literal would make sqlite3 silently CREATE an
# empty DB when a script runs from anywhere but the repo root, and the resulting
# "no such table: guilds" looks like a schema bug rather than a wrong-path bug.
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = os.environ.get("DISCORD_DB_PATH", str(_REPO_ROOT / "data" / "discord.db"))

# Channel types that can hold messages and be owned by a person. Categories, voice
# and stage rows live in the same table and must be excluded from anything that
# asks "who owns this" or "who can see this" — they are structure, not conversation.
MESSAGEABLE = ("text", "news")


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------

def connect(db_path: str | Path | None = None, *, require: bool = True) -> sqlite3.Connection:
    """Open the capture DB with dict-like rows.

    Rows come back as sqlite3.Row so you can use both r["name"] and r[0].

    `require` raises if the file does not exist, instead of letting sqlite3 create
    an empty one and failing later with a misleading "no such table".
    """
    path = Path(db_path or DEFAULT_DB)
    if require and not path.exists():
        raise FileNotFoundError(
            f"no capture DB at {path}. Set DISCORD_DB_PATH, or run bin/run_bot.py "
            f"to create one.")
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def rows(con: sqlite3.Connection, sql: str, *params) -> list[sqlite3.Row]:
    """Run a query and return all rows. Thin wrapper, saves a line everywhere."""
    return con.execute(sql, params).fetchall()


def one(con: sqlite3.Connection, sql: str, *params) -> Any:
    """Run a query and return the first column of the first row, or None."""
    r = con.execute(sql, params).fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------------------
# guilds / channels
# ---------------------------------------------------------------------------

def guilds(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every guild the bot has ever captured, newest activity first."""
    return rows(con, """
        SELECT g.*,
               (SELECT COUNT(*) FROM messages m WHERE m.guild_id = g.id) AS message_count,
               (SELECT MAX(m.created_at) FROM messages m WHERE m.guild_id = g.id) AS last_message
        FROM guilds g ORDER BY last_message DESC""")


def guild_id_by_name(con: sqlite3.Connection, needle: str) -> int | None:
    """Resolve a guild id from a partial, case-insensitive name."""
    return one(con, "SELECT id FROM guilds WHERE lower(name) LIKE ?", f"%{needle.lower()}%")


def channels(con: sqlite3.Connection, guild_id: int, *,
             category_like: str | None = None,
             include_deleted: bool = False,
             types: Iterable[str] | None = MESSAGEABLE) -> list[sqlite3.Row]:
    """Channels in a guild, optionally filtered by category and type.

    `types` defaults to text+news. Pass None for every row including categories,
    voice and stage — but be deliberate about it: those cannot hold messages, so
    any ownership or "who posts here" logic will mis-handle them.

    Deleted channels are excluded by default but retained in the DB — their
    history survives even after the channel is gone from Discord, which is often
    the only remaining copy.
    """
    sql = "SELECT * FROM channels WHERE guild_id = ?"
    args: list[Any] = [guild_id]
    if not include_deleted:
        sql += " AND (deleted = 0 OR deleted IS NULL)"
    if category_like:
        sql += " AND category_name LIKE ?"
        args.append(f"%{category_like}%")
    if types:
        types = list(types)
        sql += f" AND type IN ({','.join('?' * len(types))})"
        args.extend(types)
    return rows(con, sql + " ORDER BY position", *args)


def categories(con: sqlite3.Connection, guild_id: int) -> dict[str, int]:
    """{category name: channel count} for a guild."""
    return {r["category_name"] or "(none)": r["n"] for r in rows(con, """
        SELECT COALESCE(category_name,'(none)') AS category_name, COUNT(*) AS n
        FROM channels WHERE guild_id = ? AND (deleted = 0 OR deleted IS NULL)
        GROUP BY 1 ORDER BY n DESC""", guild_id)}


# ---------------------------------------------------------------------------
# members / roles
# ---------------------------------------------------------------------------

def role_ids(raw: str | None) -> set[str]:
    """Parse the role_ids column.

    Stored as a JSON array of ints. Returned as a set of STRINGS so it compares
    cleanly against role ids you paste from Discord (which arrive as strings).
    """
    if not raw:
        return set()
    try:
        return {str(i) for i in json.loads(raw)}
    except (ValueError, TypeError):
        return set()


def members(con: sqlite3.Connection, guild_id: int, *,
            include_left: bool = False, include_bots: bool = False) -> list[dict]:
    """Members of a guild with their parsed roles.

    Each dict: id, username, display_name, joined_at, left_at, bot, roles(set[str]).
    """
    sql = """SELECT u.id, u.username, u.bot, m.display_name, m.joined_at, m.left_at,
                    m.role_ids, m.timed_out_until
             FROM memberships m JOIN users u ON u.id = m.user_id WHERE m.guild_id = ?"""
    if not include_left:
        sql += " AND m.left_at IS NULL"
    if not include_bots:
        sql += " AND u.bot = 0"
    out = []
    for r in rows(con, sql, guild_id):
        out.append({"id": r["id"], "username": r["username"], "bot": bool(r["bot"]),
                    "display_name": r["display_name"] or r["username"],
                    "joined_at": r["joined_at"], "left_at": r["left_at"],
                    "timed_out_until": (r["timed_out_until"]
                                        if "timed_out_until" in r.keys() else None),
                    "roles": role_ids(r["role_ids"])})
    return out


def roles(con: sqlite3.Connection, guild_id: int) -> dict[str, str]:
    """{role id (str): role name} for a guild."""
    return {str(r["id"]): r["name"] for r in rows(
        con, "SELECT id, name FROM roles WHERE guild_id = ?", guild_id)}


def members_with_role(con: sqlite3.Connection, guild_id: int, role_id: str | int) -> list[dict]:
    """Everyone currently holding a given role."""
    rid = str(role_id)
    return [m for m in members(con, guild_id) if rid in m["roles"]]


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

def message_count(con: sqlite3.Connection, guild_id: int | None = None) -> int:
    if guild_id is None:
        return one(con, "SELECT COUNT(*) FROM messages") or 0
    return one(con, "SELECT COUNT(*) FROM messages WHERE guild_id = ?", guild_id) or 0


def messages(con: sqlite3.Connection, *, guild_id: int | None = None,
             channel_id: int | None = None, author_id: int | None = None,
             since: str | None = None, until: str | None = None,
             contains: str | None = None, limit: int | None = None) -> list[sqlite3.Row]:
    """Messages with the common filters, joined to channel and author names.

    `since` / `until` are ISO strings compared lexically — '2026-08-01' works
    because created_at is stored ISO-8601 in UTC.
    `contains` is a case-insensitive substring match on content.
    """
    sql = ["""SELECT m.*, c.name AS channel_name, c.category_name, u.username, u.bot,
                     mem.display_name
              FROM messages m
              JOIN channels c ON c.id = m.channel_id
              JOIN users u ON u.id = m.author_id
              LEFT JOIN memberships mem ON mem.user_id = m.author_id AND mem.guild_id = m.guild_id
              WHERE 1=1"""]
    args: list[Any] = []
    for clause, val in (("m.guild_id = ?", guild_id), ("m.channel_id = ?", channel_id),
                        ("m.author_id = ?", author_id), ("m.created_at >= ?", since),
                        ("m.created_at < ?", until)):
        if val is not None:
            sql.append("AND " + clause)
            args.append(val)
    if contains:
        sql.append("AND lower(m.content) LIKE ?")
        args.append(f"%{contains.lower()}%")
    sql.append("ORDER BY m.created_at")
    if limit:
        sql.append(f"LIMIT {int(limit)}")
    return rows(con, " ".join(sql), *args)


def conversation(con: sqlite3.Connection, channel_id: int, *,
                 since: str | None = None, width: int = 160) -> str:
    """A channel rendered as readable text — for reading, or feeding to an LLM.

    One line per message: timestamp, author, collapsed-whitespace content.
    """
    out = []
    for m in messages(con, channel_id=channel_id, since=since):
        text = " ".join((m["content"] or "").split())[:width]
        out.append(f'{m["created_at"][:16]} {m["username"]:<20} {text}')
    return "\n".join(out)


def search(con: sqlite3.Connection, needle: str, *, guild_id: int | None = None,
           since: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
    """Substring search across message content.

    Truncates at `limit`. Use search_count() first if the total matters — a
    silently truncated result set has produced wrong conclusions before ("only
    200 messages mention payment" when the real answer was 829).
    """
    return messages(con, guild_id=guild_id, since=since, contains=needle, limit=limit)


def search_count(con: sqlite3.Connection, needle: str, *, guild_id: int | None = None,
                 since: str | None = None) -> int:
    """Total matches for a search, ignoring any limit."""
    sql = "SELECT COUNT(*) FROM messages WHERE lower(content) LIKE ?"
    args: list[Any] = [f"%{needle.lower()}%"]
    if guild_id is not None:
        sql += " AND guild_id = ?"
        args.append(guild_id)
    if since:
        sql += " AND created_at >= ?"
        args.append(since)
    return one(con, sql, *args) or 0


def message_volume(con: sqlite3.Connection, guild_id: int, *,
                   by: str = "month") -> list[tuple[str, int]]:
    """Message counts grouped by 'day' or 'month'."""
    fmt = "%Y-%m-%d" if by == "day" else "%Y-%m"
    return [(r[0], r[1]) for r in rows(con, f"""
        SELECT strftime('{fmt}', created_at) AS period, COUNT(*) FROM messages
        WHERE guild_id = ? GROUP BY period ORDER BY period""", guild_id)]


# ---------------------------------------------------------------------------
# activity helpers
# ---------------------------------------------------------------------------

def last_active(con: sqlite3.Connection, user_id: int,
                guild_id: int | None = None) -> str | None:
    """When this user last posted anywhere (or in one guild)."""
    if guild_id:
        return one(con, "SELECT MAX(created_at) FROM messages WHERE author_id = ? AND guild_id = ?",
                   user_id, guild_id)
    return one(con, "SELECT MAX(created_at) FROM messages WHERE author_id = ?", user_id)


def days_since(iso: str | None, now: datetime | None = None) -> int | None:
    """Whole days between an ISO timestamp and now. None if no timestamp."""
    if not iso:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = datetime.fromisoformat(iso[:19] + "+00:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).days


def dormant(con: sqlite3.Connection, guild_id: int, *, days: int = 30) -> list[dict]:
    """Members with no message in the guild for N+ days. Never-posted included."""
    out = []
    for m in members(con, guild_id):
        last = last_active(con, m["id"], guild_id)
        d = days_since(last)
        if d is None or d >= days:
            out.append({**m, "last_active": last, "days_inactive": d})
    return sorted(out, key=lambda x: (x["days_inactive"] is not None, -(x["days_inactive"] or 0)))


def cross_guild(con: sqlite3.Connection) -> dict[int, list[str]]:
    """{user_id: [guild names]} for anyone present in more than one guild.

    Matched on user id, which is the only reliable cross-server key.
    """
    out: dict[int, list[str]] = {}
    for r in rows(con, """SELECT m.user_id, g.name FROM memberships m
                          JOIN guilds g ON g.id = m.guild_id
                          JOIN users u ON u.id = m.user_id
                          WHERE m.left_at IS NULL AND u.bot = 0"""):
        out.setdefault(r["user_id"], []).append(r["name"])
    return {k: v for k, v in out.items() if len(v) > 1}


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------

MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")


def amounts(text: str | None) -> list[float]:
    """Every dollar figure in a string, as floats. '$1,250.00' -> 1250.0"""
    return [float(m.replace(",", "")) for m in MONEY.findall(text or "")]


def events(con: sqlite3.Connection, guild_id: int, *, kind: str | None = None,
           since: str | None = None) -> list[sqlite3.Row]:
    """Audit-log and gateway events. `kind` matches event_type with LIKE."""
    sql = "SELECT * FROM events WHERE guild_id = ?"
    args: list[Any] = [guild_id]
    if kind:
        sql += " AND event_type LIKE ?"
        args.append(f"%{kind}%")
    if since:
        sql += " AND created_at >= ?"
        args.append(since)
    return rows(con, sql + " ORDER BY created_at DESC", *args)
