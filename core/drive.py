"""Google Drive access, by service account and by delegated user OAuth.

Two identities, because neither one alone can do the whole job.

SERVICE ACCOUNT — read, rename, move, organise, link
    Same identity as core.sheets. It can do everything to a file EXCEPT bring
    a new one into existence, because a service account owns no storage:

        403: Service Accounts do not have storage quota.
             Leverage shared drives, or use OAuth delegation.

    Folders are a special case — they occupy zero bytes, so a service account
    CAN create them. That is enough to build a folder tree and file existing
    documents into it.

    A human shares a folder with the service-account address as Editor; from
    then on every file inside is fully manageable.

USER OAUTH — upload
    Uploads land in the USER's Drive on the USER's quota, owned by the user.
    Request `drive.file` and nothing wider: it grants per-file access to files
    THIS APP CREATED, so a bug here can never reach the rest of someone's
    Drive. It is also a non-sensitive scope, so the consent screen can be
    published without Google's verification review.

    The corollary is the thing people trip on: `drive.file` cannot see files
    the user uploaded by hand. Reach those with the service account instead.

    Publish the consent screen. While it is in "Testing", Google expires
    refresh tokens after SEVEN DAYS and the flow has to be repeated weekly.

Set GOOGLE_SERVICE_ACCOUNT to the JSON key path, and for uploads
GOOGLE_OAUTH_CLIENT to the downloaded Desktop-app client JSON. The resulting
user token is cached beside it and is as sensitive as a password — chmod 600,
never commit it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

FOLDER_MIME = "application/vnd.google-apps.folder"

_SCOPES_SA = ["https://www.googleapis.com/auth/drive"]
_SCOPES_USER = ["https://www.googleapis.com/auth/drive.file"]

_TOKEN_ENV = "GOOGLE_OAUTH_TOKEN"
_CLIENT_ENV = "GOOGLE_OAUTH_CLIENT"


# ---------------------------------------------------------------------------
# clients
# ---------------------------------------------------------------------------

def service(readonly: bool = False):
    """Drive client authenticated as the SERVICE ACCOUNT.

    Can read/rename/move/delete anything shared with it, and create folders.
    Cannot upload file content — see the module docstring.
    """
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT")
    if not path:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT is not set — see docs/SETUP.md")
    scopes = ["https://www.googleapis.com/auth/drive.readonly"] if readonly else _SCOPES_SA
    return build("drive", "v3", credentials=Credentials.from_service_account_file(
        path, scopes=scopes), cache_discovery=False)


def service_account_email() -> str:
    """The address a human must share each folder with."""
    import json
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT")
    if not path:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT is not set")
    with open(path) as fh:
        return json.load(fh)["client_email"]


def _token_path() -> Path:
    if os.environ.get(_TOKEN_ENV):
        return Path(os.environ[_TOKEN_ENV])
    client = os.environ.get(_CLIENT_ENV)
    if not client:
        raise RuntimeError(
            f"{_CLIENT_ENV} is not set. Create a Desktop-app OAuth client in the "
            "Google Cloud console, download the JSON, and point this at it.")
    return Path(client).with_name(".google-oauth-token.json")


def user_service(*, interactive: bool = True):
    """Drive client authenticated as the USER, scoped to `drive.file`.

    First call opens a browser for consent and caches the refresh token; later
    calls are silent. Pass interactive=False in unattended contexts — it will
    raise rather than block forever waiting on a browser that nobody will see.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_file = _token_path()
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), _SCOPES_USER)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_file.write_text(creds.to_json())
        token_file.chmod(0o600)
    if not creds or not creds.valid:
        if not interactive:
            raise RuntimeError(
                f"no usable Google user token at {token_file}. Run the consent "
                "flow once from an interactive session.")
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:                       # pragma: no cover
            raise RuntimeError(
                "google-auth-oauthlib is not installed — pip3 install "
                "google-auth-oauthlib") from exc
        client = os.environ.get(_CLIENT_ENV)
        if not client:
            raise RuntimeError(f"{_CLIENT_ENV} is not set")
        flow = InstalledAppFlow.from_client_secrets_file(client, _SCOPES_USER)
        # port=0 picks a free loopback port; Desktop clients accept any.
        creds = flow.run_local_server(port=0, prompt="consent",
                                      authorization_prompt_message=
                                      "Approve in the browser window that just opened…")
        token_file.write_text(creds.to_json())
        token_file.chmod(0o600)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def authorised_as(svc) -> dict:
    """Who a client is acting as, and how much storage it has.

    A `limit` of "0" is the tell-tale of a service account: it can manage files
    but never create one.
    """
    return svc.about().get(fields="user,storageQuota").execute()


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def folder_id_from_url(url: str) -> str:
    """Pull a folder id out of a normal browser URL."""
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", url) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", url)
    if not m:
        raise ValueError(f"no folder id in {url!r}")
    return m.group(1)


_FIELDS = "id,name,mimeType,size,modifiedTime,webViewLink,parents,owners(emailAddress)"


def ls(svc, folder_id: str, *, recursive: bool = False,
       include_trashed: bool = False) -> list[dict]:
    """Everything directly inside a folder (or the whole subtree)."""
    out: list[dict] = []
    q = f"'{folder_id}' in parents"
    if not include_trashed:
        q += " and trashed = false"
    page = None
    while True:
        resp = svc.files().list(
            q=q, pageSize=1000, pageToken=page,
            fields=f"nextPageToken,files({_FIELDS})",
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        out.extend(resp.get("files", []))
        page = resp.get("nextPageToken")
        if not page:
            break
    if recursive:
        for f in list(out):
            if f["mimeType"] == FOLDER_MIME:
                out.extend(ls(svc, f["id"], recursive=True,
                              include_trashed=include_trashed))
    return out


def get(svc, file_id: str) -> dict:
    return svc.files().get(fileId=file_id, fields=_FIELDS,
                           supportsAllDrives=True).execute()


def download(svc, file_id: str, dest: str | Path) -> Path:
    """Download binary content. Google-native docs need export(), not this."""
    from googleapiclient.http import MediaIoBaseDownload
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    with dest.open("wb") as fh:
        dl = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _status, done = dl.next_chunk()
    return dest


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def ensure_folder(svc, name: str, parent_id: str | None = None) -> str:
    """Folder id, creating it if absent. Idempotent.

    Works with the service account: folders are zero bytes, so they do not need
    storage quota.
    """
    q = (f"mimeType = '{FOLDER_MIME}' and trashed = false "
         f"and name = '{name.replace(chr(39), chr(92) + chr(39))}'")
    if parent_id:
        q += f" and '{parent_id}' in parents"
    hits = svc.files().list(q=q, fields="files(id)", pageSize=10,
                            supportsAllDrives=True,
                            includeItemsFromAllDrives=True).execute().get("files", [])
    if hits:
        return hits[0]["id"]
    body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    return svc.files().create(body=body, fields="id",
                              supportsAllDrives=True).execute()["id"]


def rename(svc, file_id: str, new_name: str) -> dict:
    return svc.files().update(fileId=file_id, body={"name": new_name},
                              fields=_FIELDS, supportsAllDrives=True).execute()


def move(svc, file_id: str, new_parent_id: str) -> dict:
    cur = svc.files().get(fileId=file_id, fields="parents",
                          supportsAllDrives=True).execute()
    return svc.files().update(
        fileId=file_id, addParents=new_parent_id,
        removeParents=",".join(cur.get("parents", [])),
        fields=_FIELDS, supportsAllDrives=True).execute()


def upload(svc, path: str | Path, *, parent_id: str | None = None,
           name: str | None = None, mime: str | None = None) -> dict:
    """Upload a local file. MUST be a user_service() client.

    A service-account client raises 403 "Service Accounts do not have storage
    quota" here — the failure is immediate and unambiguous, so it is left to
    surface rather than being wrapped.
    """
    import mimetypes
    from googleapiclient.http import MediaFileUpload
    path = Path(path)
    body: dict[str, Any] = {"name": name or path.name}
    if parent_id:
        body["parents"] = [parent_id]
    mime = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    media = MediaFileUpload(str(path), mimetype=mime, resumable=path.stat().st_size > 5_000_000)
    return svc.files().create(body=body, media_body=media, fields=_FIELDS,
                              supportsAllDrives=True).execute()


def share(svc, file_id: str, email: str, role: str = "reader") -> dict:
    """Grant a person access. role: reader | commenter | writer."""
    return svc.permissions().create(
        fileId=file_id, sendNotificationEmail=False,
        body={"type": "user", "role": role, "emailAddress": email},
        supportsAllDrives=True).execute()


def link(file_id: str) -> str:
    """Stable viewer URL. webViewLink from the API is equivalent."""
    return f"https://drive.google.com/file/d/{file_id}/view"


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def canonical_name(handle: str, kind: str, date: str, ext: str = "pdf") -> str:
    """`<discord handle>__<KIND>__<YYYY-MM-DD>.<ext>`

        adriaaanm__W9__2026-07-21.pdf
        marti_rasmijn1__W8BEN__2026-07-24.pdf

    Handle first because that is the key the payments sheet and the Discord
    capture DB both join on. Real names drift — people marry, use a company on
    the invoice, or sign with a middle initial — handles do not.
    """
    h = _SAFE.sub("-", (handle or "").strip().lower()).strip("-.")
    k = _SAFE.sub("", (kind or "").strip().upper())
    return f"{h}__{k}__{date}.{ext.lstrip('.')}"


def parse_name(name: str) -> dict | None:
    """Inverse of canonical_name; None if it does not follow the convention."""
    m = re.match(r"^(?P<handle>.+?)__(?P<kind>[A-Z0-9]+)__"
                 r"(?P<date>\d{4}-\d{2}-\d{2})\.(?P<ext>[A-Za-z0-9]+)$", name or "")
    return m.groupdict() if m else None
