# Setup

## 1. Discord application

1. https://discord.com/developers/applications → **New Application**
2. **Bot** tab → **Reset Token** → copy into `.env` as `DISCORD_BOT_TOKEN`
   (the token IS the login — never commit it, never paste it anywhere)
3. Same tab, **Privileged Gateway Intents**:
   - ✅ **Server Members Intent** — required
   - ✅ **Message Content Intent** — required, or every message body is empty
   - ⬜ Presence Intent — not needed (see note in ARCHITECTURE.md before disabling)

## 2. Invite it

Take the **Application ID** from *General Information* (same value as OAuth2
Client ID and the bot's user id).

Read-only, least privilege — `View Channels` + `Read Message History` +
`View Audit Log`:

```
https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=66688
```

Full visibility including private channels — **Administrator**:

```
https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=8
```

The number is a bitfield: `View Audit Log 128` + `View Channels 1024` +
`Read Message History 65536` = **66688**. Administrator is `8`.

**Which to use:** if the server has private per-member channels, `66688` alone
will read almost nothing — server-level permissions do not override a channel's
`@everyone` deny. Either grant Administrator, or add the bot's role to every
private category by hand. See ARCHITECTURE.md → Permissions.

**Grant access before or immediately after the invite.** The bot backfills the
instant it joins; if it is still blind at that moment the backfill captures
nothing and reports success. Then run:

```bash
python bin/verify.py --live      # detects the gap
python bin/backfill.py <guild_id>  # repairs it
```

## 3. Run it

```bash
python bin/run_bot.py
```

Keep it under a supervisor (systemd, launchd, pm2, tmux) — it is meant to run
permanently. On restart it snapshots every guild and recovers anything missed.

## 4. Google Sheets (optional)

Only needed if you use `core.sheets`.

1. Google Cloud Console → new project → enable **Google Sheets API**
2. **IAM & Admin → Service Accounts** → Create → skip role grants
3. **Keys → Add Key → JSON** → download
4. Save outside the repo, set `GOOGLE_SERVICE_ACCOUNT=/path/to/key.json`
5. Open the JSON, copy `client_email` (`…@….iam.gserviceaccount.com`)
6. **In each spreadsheet: Share → paste that email** — Viewer to read, Editor to write

```python
from core import sheets
print(sheets.service_account_email())   # the address to share with
```

**A service account has no Drive storage.** It cannot create spreadsheets —
`create()` returns 403 and `storageQuota.limit` is 0. A human creates the sheet
and shares it; the script only reads/writes.

Two more traps:
- Tab titles can contain **trailing spaces**, which break A1 ranges. Use
  `sheets.tabs()` to get the exact string.
- The `HYPERLINK` argument separator is **locale dependent** (`,` vs `;`). Wrong
  one renders `#ERROR!` in every cell. Check an existing formula first.

## 5. Google Drive (optional)

Only needed if you use `core.drive`. It needs **two** identities, because
neither can do the whole job.

### 5a. Service account — read, rename, move, organise, link

Reuses the key from §4. Enable the **Google Drive API** in the same project,
then share each folder with the service-account address as **Editor**.

It can do everything to a file except create one:

```
403: Service Accounts do not have storage quota.
     Leverage shared drives, or use OAuth delegation.
```

Folders are the exception — they are zero bytes, so `drive.ensure_folder()`
works fine and a whole folder tree can be built this way.

### 5b. User OAuth — upload

Uploads land in the user's Drive, on the user's quota, owned by the user.

1. **Google Auth Platform** (formerly "OAuth consent screen") → External
2. Add scope **`https://www.googleapis.com/auth/drive.file` and nothing wider**
3. **Publish the app.** In "Testing", Google expires refresh tokens after
   **7 days** and the consent flow has to be repeated weekly. `drive.file` is
   non-sensitive, so publishing needs no verification review.
4. **Credentials → Create OAuth client ID → Desktop app** → download JSON
5. `pip install google-auth-oauthlib`
6. `export GOOGLE_OAUTH_CLIENT=/path/to/client.json`

```python
from core import drive
svc = drive.user_service()          # opens a browser once, then caches
drive.upload(svc, "form.pdf", parent_id=folder_id)
```

The token caches next to the client JSON as `.google-oauth-token.json`, chmod
600. It is as sensitive as a password — never commit it.

**`drive.file` is per-file, not per-Drive.** It grants access only to files the
app itself created. That is what makes it safe, and it is also why it cannot
see files a human uploaded by hand — reach those with the service account
(§5a) instead. Revoke any time at myaccount.google.com/permissions.
