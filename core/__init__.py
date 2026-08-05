"""discord-connector — capture a Discord server into SQLite, then query it.

Two layers:
  core.capture.*   writes the DB (gateway events + backfill). Runs as a daemon.
  core.query / attribution / verify / sheets   read it. Import these in scripts.

Typical use from a throwaway script:

    from core import query as q
    con = q.connect()
    gid = q.guild_id_by_name(con, "my server")
    print(q.message_count(con, gid))
"""
__version__ = "0.1.0"
