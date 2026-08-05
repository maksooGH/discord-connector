# scratch/

Throwaway scripts live here. Gitignored — nothing in this folder is shared.

This is where an agent should write one-off analysis: a question gets asked, a
script gets written here, it imports from `core/`, it prints or writes a CSV,
and it is never maintained again.

    # scratch/who_went_quiet.py
    from core import query as q

    con = q.connect()
    gid = q.guild_id_by_name(con, "my server")
    for m in q.dormant(con, gid, days=30):
        print(m["username"], m["days_inactive"])

Rules of thumb:
- Import from `core`, never re-implement queries here.
- If you write the same helper twice in scratch, promote it to `core/`.
- Never call Discord from here. `core.query` reads the DB; that is enough for
  almost everything and it is instant.
