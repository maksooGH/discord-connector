"""Offline resolution of Discord channel permissions.

Answers "who can see this channel" and "which channels can this member see"
straight from the capture DB, with no Discord connection.

This reimplements Discord's documented resolution order. It must be exact or the
answers are subtly wrong, so `validate()` checks it against discord.py's live
`permissions_for()` over real channel/member pairs — a test that can actually fail.

Resolution order (https://discord.com/developers/docs/topics/permissions):

  1. start from the @everyone role's guild-level permissions
  2. OR in every role the member holds
  3. if ADMINISTRATOR is set -> all permissions, stop. Administrator bypasses
     every channel overwrite, which is why a bot without it goes blind on private
     channels no matter what server-level permissions it has
  4. apply the @everyone channel overwrite: deny first, then allow
  5. apply the member's role overwrites: accumulate ALL denies, then ALL allows
  6. apply the member-specific overwrite: deny then allow
  7. implicit permissions: no VIEW_CHANNEL -> no channel permissions at all;
     no SEND_MESSAGES -> lose embed/attach/tts/mention-everyone
  8. timeout mask, conclusive and last

Steps 7 and 8 are the ones a naive implementation misses, and a validation that
only checks view_channel cannot see either of them: step 7 only ever changes
OTHER bits, and step 8 preserves view_channel by definition. Validate across
several permissions or the test is decorative.

Threads have no overwrites of their own — they resolve against their parent.

Two facts that bite:
  - the @everyone role id EQUALS the guild id
  - role ids and user ids are both snowflakes; `target_type` is the only way to
    tell an overwrite apart
"""

from __future__ import annotations

import sqlite3
from typing import Iterable

from . import query as q

# permission bits we care about by name; add more as needed
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
READ_MESSAGE_HISTORY = 1 << 16
ADMINISTRATOR = 1 << 3
MANAGE_CHANNELS = 1 << 4
MANAGE_ROLES = 1 << 28

# When a member is timed out Discord masks their permissions down to these two,
# applied LAST and unconditionally (except for administrators / channel owners).
# This is why a view_channel-only validation can never detect a missing timeout
# step: view_channel is one of the two bits the mask preserves.
TIMEOUT_MASK = VIEW_CHANNEL | READ_MESSAGE_HISTORY

# Every permission that applies at channel scope. Taken from discord.py's
# Permissions.all_channel(); if you cannot view a channel, all of these are
# stripped regardless of what any overwrite granted.
ALL_CHANNEL = 8555290110197585

SEND_TTS = 1 << 12
MENTION_EVERYONE = 1 << 17
EMBED_LINKS = 1 << 14
ATTACH_FILES = 1 << 15
SEND_DEPENDENT = SEND_TTS | MENTION_EVERYONE | EMBED_LINKS | ATTACH_FILES

BITS = {
    "view_channel": VIEW_CHANNEL,
    "send_messages": SEND_MESSAGES,
    "read_message_history": READ_MESSAGE_HISTORY,
    "administrator": ADMINISTRATOR,
    "manage_channels": MANAGE_CHANNELS,
    "manage_roles": MANAGE_ROLES,
}


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

THREAD_TYPES = ("public_thread", "private_thread", "news_thread")


def overwrites(con: sqlite3.Connection, channel_id: int) -> list[sqlite3.Row]:
    """Raw overwrite rows for a channel, resolving threads to their parent.

    Threads have NO overwrites of their own in Discord's model — permission checks
    delegate entirely to the parent channel (discord.py: Thread.permissions_for
    calls GuildChannel.permissions_for(self.parent, obj)). Reading the thread's own
    id would find nothing and fall back to bare guild-level role permissions,
    reporting a private channel's thread as visible to everyone.
    """
    row = q.rows(con, "SELECT type, category_id FROM channels WHERE id = ?", channel_id)
    if row and row[0]["type"] in THREAD_TYPES and row[0]["category_id"]:
        channel_id = row[0]["category_id"]   # threads store the parent channel here
    return q.rows(con, """SELECT target_id, target_type, allow, deny
                          FROM channel_overwrites WHERE channel_id = ?""", channel_id)


def role_permissions(con: sqlite3.Connection, guild_id: int) -> dict[int, int]:
    """{role id: guild-level permission bitfield}."""
    return {r["id"]: int(r["permissions"] or 0)
            for r in q.rows(con, "SELECT id, permissions FROM roles WHERE guild_id = ?", guild_id)}


def has_overwrite_data(con: sqlite3.Connection, guild_id: int) -> bool:
    """False if this guild predates overwrite capture — resolution would be wrong."""
    return bool(q.one(con, "SELECT 1 FROM channel_overwrites WHERE guild_id = ? LIMIT 1", guild_id))


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

def permissions_for(con: sqlite3.Connection, guild_id: int, channel_id: int,
                    member_role_ids: Iterable[int | str], *,
                    member_id: int | None = None,
                    timed_out: bool = False,
                    _role_perms: dict[int, int] | None = None,
                    _ows: list[sqlite3.Row] | None = None) -> int:
    """Resolved permission bitfield for a member in a channel.

    member_role_ids should include the @everyone role (== guild_id). It is added
    automatically if missing, because Discord always applies it.
    """
    role_perms = _role_perms if _role_perms is not None else role_permissions(con, guild_id)
    rids = {int(r) for r in member_role_ids} | {guild_id}

    # 1 + 2: base = @everyone OR every role held
    base = role_perms.get(guild_id, 0)
    for rid in rids:
        base |= role_perms.get(rid, 0)

    # 3: administrator short-circuits everything
    if base & ADMINISTRATOR:
        return ~0

    ows = _ows if _ows is not None else overwrites(con, channel_id)
    by_target = {(o["target_id"], o["target_type"]): o for o in ows}

    # 4: @everyone channel overwrite
    ev = by_target.get((guild_id, "role"))
    if ev:
        base &= ~int(ev["deny"])
        base |= int(ev["allow"])

    # 5: role overwrites — all denies, then all allows
    allow_acc = deny_acc = 0
    for rid in rids:
        if rid == guild_id:
            continue
        o = by_target.get((rid, "role"))
        if o:
            allow_acc |= int(o["allow"])
            deny_acc |= int(o["deny"])
    base &= ~deny_acc
    base |= allow_acc

    # 6: member-specific overwrite wins
    if member_id is not None:
        mo = by_target.get((member_id, "member"))
        if mo:
            base &= ~int(mo["deny"])
            base |= int(mo["allow"])

    # 7: implicit permissions. If you cannot read a channel you have NO channel
    # permissions in it, and if you cannot send you lose the send-dependent ones.
    # Mirrors discord.py's GuildChannel._apply_implicit_permissions. Omitting this
    # reports send_messages=True on channels the member cannot even see — which is
    # invisible to any validation that only checks view_channel.
    if not base & VIEW_CHANNEL:
        base &= ~ALL_CHANNEL
    elif not base & SEND_MESSAGES:
        base &= ~SEND_DEPENDENT

    # 8: timeout mask, conclusive and last
    if timed_out:
        base &= TIMEOUT_MASK
    return base


def can(con: sqlite3.Connection, guild_id: int, channel_id: int,
        member: dict, permission: str = "view_channel") -> bool:
    """Does this member (a dict from query.members) have `permission` here?"""
    bit = BITS[permission]
    perms = permissions_for(con, guild_id, channel_id, member["roles"],
                            member_id=member["id"],
                            timed_out=is_timed_out(member))
    return bool(perms & bit)


def is_timed_out(member: dict) -> bool:
    """True if the member's timeout is still in the future."""
    until = member.get("timed_out_until")
    if not until:
        return False
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(until)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt > datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# the questions people actually ask
# ---------------------------------------------------------------------------

def who_can_see(con: sqlite3.Connection, guild_id: int, channel_id: int, *,
                permission: str = "view_channel",
                include_bots: bool = False) -> list[dict]:
    """Every member who can see (or send in) a channel."""
    rp = role_permissions(con, guild_id)
    ows = overwrites(con, channel_id)
    bit = BITS[permission]
    out = []
    for m in q.members(con, guild_id, include_bots=include_bots):
        p = permissions_for(con, guild_id, channel_id, m["roles"],
                            member_id=m["id"], _role_perms=rp, _ows=ows)
        if p & bit:
            out.append(m)
    return out


def roles_that_can_see(con: sqlite3.Connection, guild_id: int, channel_id: int, *,
                       permission: str = "view_channel") -> list[str]:
    """Role ids whose holders would gain access purely from that role."""
    rp = role_permissions(con, guild_id)
    ows = overwrites(con, channel_id)
    bit = BITS[permission]
    out = []
    for rid in rp:
        if rid == guild_id:
            continue
        p = permissions_for(con, guild_id, channel_id, [rid],
                            _role_perms=rp, _ows=ows)
        if p & bit:
            out.append(str(rid))
    return out


def channels_visible_to(con: sqlite3.Connection, guild_id: int, member: dict, *,
                        permission: str = "view_channel",
                        category_like: str | None = None) -> list[sqlite3.Row]:
    """Every channel this member can see."""
    rp = role_permissions(con, guild_id)
    bit = BITS[permission]
    out = []
    for ch in q.channels(con, guild_id, category_like=category_like):
        p = permissions_for(con, guild_id, ch["id"], member["roles"],
                            member_id=member["id"], _role_perms=rp)
        if p & bit:
            out.append(ch)
    return out


def private_channels(con: sqlite3.Connection, guild_id: int) -> list[sqlite3.Row]:
    """Channels where @everyone is denied View Channel."""
    types = list(q.MESSAGEABLE)
    return q.rows(con, f"""
        SELECT c.* FROM channels c
        JOIN channel_overwrites o
          ON o.channel_id = c.id AND o.target_id = ? AND o.target_type = 'role'
        WHERE c.guild_id = ? AND (c.deleted = 0 OR c.deleted IS NULL)
          AND c.type IN ({','.join('?' * len(types))})
          AND (o.deny & ?) != 0""", guild_id, guild_id, *types, VIEW_CHANNEL)


def role_coverage(con: sqlite3.Connection, guild_id: int) -> list[tuple[str, int, int]]:
    """[(role name, channels visible, total private channels)], best first.

    Answers "is there a single role that reaches every private channel?" — usually
    no, which is what forces Administrator on servers built from private channels.
    """
    priv = private_channels(con, guild_id)
    names = q.roles(con, guild_id)
    rp = role_permissions(con, guild_id)
    out = []
    for rid, name in names.items():
        if int(rid) == guild_id:
            continue
        n = sum(1 for ch in priv
                if permissions_for(con, guild_id, ch["id"], [rid], _role_perms=rp) & VIEW_CHANNEL)
        out.append((name, n, len(priv)))
    return sorted(out, key=lambda x: -x[1])


# ---------------------------------------------------------------------------
# validation against live Discord
# ---------------------------------------------------------------------------

async def validate(client, con: sqlite3.Connection, guild_id: int, *,
                   members_per_channel: int | None = None) -> dict:
    """Compare offline resolution against discord.py's live permissions_for().

    Covers EVERY channel. An earlier version capped on total pairs and therefore
    only ever reached the first handful of channels — which made it pass even when
    the resolver was deliberately broken. Mutation-tested: removing the
    member-overwrite step or the administrator short-circuit must make this fail.

    members_per_channel caps work on huge guilds; None checks every member.

    Coverage limits, stated so nobody over-reads a green result:
      - only text channels (guild.text_channels); category/voice/stage overwrite
        rows exist in the DB but are not exercised here
      - four permission bits, not all of them
      - threads are only covered if the guild has any
    """
    guild = client.get_guild(guild_id)
    if guild is None:
        return {"checked": 0, "mismatches": 0, "channels": 0, "examples": ["guild not visible"]}

    rp = role_permissions(con, guild_id)
    members = {m["id"]: m for m in q.members(con, guild_id, include_bots=True)}
    checked = mismatches = 0
    bad: list[str] = []

    for ch in guild.text_channels:
        ows = overwrites(con, ch.id)
        pool = guild.members if members_per_channel is None else guild.members[:members_per_channel]
        for m in pool:
            rec = members.get(m.id)
            if rec is None:
                continue
            lp = ch.permissions_for(m)
            offline = permissions_for(con, guild_id, ch.id, rec["roles"],
                                      member_id=m.id, timed_out=is_timed_out(rec),
                                      _role_perms=rp, _ows=ows)
            for pname in ("view_channel", "send_messages", "read_message_history",
                          "manage_channels"):
                live = getattr(lp, pname)
                mine = bool(offline & BITS[pname])
                checked += 1
                if live != mine:
                    mismatches += 1
                    if len(bad) < 12:
                        bad.append(f"#{ch.name} / {m.name} / {pname}: live={live} offline={mine}")
    return {"checked": checked, "mismatches": mismatches,
            "channels": len(guild.text_channels), "examples": bad}
