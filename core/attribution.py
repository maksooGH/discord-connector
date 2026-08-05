"""Work out which member a channel belongs to.

Servers that give each member their own channel (support tickets, per-creator
chats, per-client rooms) almost never keep the channel name in sync with the
member. Observed in the wild on one server alone:

    member ant__9          -> channel #ninetysevenxbt
    member igorb2457       -> channel #igor-frank      (word order reversed)
    member mg045821        -> channel #gok
    member kagibuu         -> channel #dan
    member sam_012345678   -> channel #v_a_n_t_h_e_r   (later renamed to match)

So ownership is derived from WHO POSTS IN THE CHANNEL, not from its name. Name
matching is kept only as a fallback for channels nobody has posted in yet
(freshly opened tickets), and it is always reported as a weaker signal.

    from core import attribution as attr
    idx = attr.build(con, guild_id, staff_role_ids={"123", "456"})
    idx.channels_for(user_id)        # -> [channel rows]
    idx.owner_of(channel_id)         # -> user id or None
    idx.unowned()                    # -> channels nobody can be matched to
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable

from . import query as q


def normalise(name: str) -> str:
    """Reduce a channel or user name to a comparable key.

    Strips ticket-number prefixes ('82-laura' -> 'laura'), status suffixes
    ('-inactive', '-aff', '-closed'), a 'fib-' style brand prefix, and all
    punctuation/emoji. Deliberately lossy — it is only ever a fallback.
    """
    s = (name or "").lower()
    s = re.sub(r"^(fib|fiber|prob)-", "", s)
    s = re.sub(r"^\d+-", "", s)
    s = re.sub(r"-(aff|irl|inactive|innactive|onabreak|closed|done|august|payment-pending).*$", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


@dataclass
class Index:
    """Resolved channel ownership for one guild."""
    guild_id: int
    owned: dict[int, list[sqlite3.Row]] = field(default_factory=dict)   # user_id -> channels
    owner: dict[int, int] = field(default_factory=dict)                 # channel_id -> user_id
    weak: set[int] = field(default_factory=set)        # channel ids matched by NAME only
    orphans: list[sqlite3.Row] = field(default_factory=list)            # no owner at all

    def channels_for(self, user_id: int, *, category_like: str | None = None) -> list[sqlite3.Row]:
        """Channels belonging to a member, optionally filtered by category."""
        out = self.owned.get(user_id, [])
        if category_like:
            out = [c for c in out if category_like.lower() in (c["category_name"] or "").lower()]
        return out

    def owner_of(self, channel_id: int) -> int | None:
        return self.owner.get(channel_id)

    def unowned(self) -> list[sqlite3.Row]:
        """Channels no member could be matched to.

        Worth reading after every run — an unowned channel usually means either a
        member who left, or a naming pattern the fallback does not cover.
        """
        return self.orphans

    def name_matched_only(self) -> list[int]:
        """Channels resolved by name rather than authorship — treat as provisional."""
        return sorted(self.weak)


def build(con: sqlite3.Connection, guild_id: int, *,
          staff_role_ids: Iterable[str] = (),
          staff_user_ids: Iterable[int] = (),
          category_like: str | None = None,
          include_shared: bool = True) -> Index:
    """Resolve channel ownership for a guild.

    staff_role_ids / staff_user_ids
        Staff post in everyone's channels, so they must be excluded or they will
        own every channel. Pass whichever you have.
    category_like
        Restrict to categories whose name contains this (e.g. "CHATS", "PAYMENTS").
    include_shared
        Some channels legitimately have two occupants (a creator plus their
        partner, or two people on one deal). When True, secondary posters are
        also credited with the channel — but only if they own no channel of
        their own, so an incidental visitor never inherits someone else's.
    """
    staff_roles = {str(r) for r in staff_role_ids}
    staff = set(staff_user_ids)
    if staff_roles:
        for m in q.members(con, guild_id, include_left=True, include_bots=True):
            if m["roles"] & staff_roles or m["bot"]:
                staff.add(m["id"])

    idx = Index(guild_id=guild_id)
    secondary: dict[int, list[sqlite3.Row]] = {}
    by_norm: dict[str, list[sqlite3.Row]] = {}

    for ch in q.channels(con, guild_id, category_like=category_like):
        by_norm.setdefault(normalise(ch["name"]), []).append(ch)
        posters = q.rows(con, """
            SELECT m.author_id, COUNT(*) AS n FROM messages m
            JOIN users u ON u.id = m.author_id
            WHERE m.channel_id = ? AND u.bot = 0
            GROUP BY m.author_id ORDER BY n DESC""", ch["id"])
        non_staff = [r["author_id"] for r in posters if r["author_id"] not in staff]
        if non_staff:
            idx.owned.setdefault(non_staff[0], []).append(ch)
            idx.owner[ch["id"]] = non_staff[0]
            if include_shared:
                for uid in non_staff[1:]:
                    secondary.setdefault(uid, []).append(ch)
        else:
            idx.orphans.append(ch)

    # secondary posters only inherit if they own nothing themselves
    for uid, chans in secondary.items():
        if uid not in idx.owned:
            idx.owned[uid] = chans
            for ch in chans:
                idx.owner.setdefault(ch["id"], uid)

    # name fallback for members with no channel yet
    for m in q.members(con, guild_id):
        if m["id"] in idx.owned:
            continue
        for alias in {normalise(m["username"]), normalise(m["display_name"])} - {""}:
            for ch in by_norm.get(alias, []):
                if ch["id"] in idx.owner:
                    continue
                idx.owned.setdefault(m["id"], []).append(ch)
                idx.owner[ch["id"]] = m["id"]
                idx.weak.add(ch["id"])
                if ch in idx.orphans:
                    idx.orphans.remove(ch)
            if m["id"] in idx.owned:
                break
    return idx
