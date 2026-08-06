"""Pick the right Google identity automatically.

Two credentials exist because neither is sufficient alone (see core/drive.py):

    service account   reads and writes anything SHARED with its address, and
                      creates folders — but owns no storage, so it can never
                      bring a file into existence.
    user OAuth        creates and uploads, and fully manages whatever it
                      created — but `drive.file` makes everything else
                      INVISIBLE, not merely forbidden.

Their reach is disjoint in a way that is easy to get wrong by hand, because
the failure is a 404 rather than a 403 — a file that "does not exist" for one
identity is sitting right there for the other. So don't choose by hand:

    from core import google
    ss  = google.sheets_for(sheet_id)      # right client, whichever it is
    drv = google.drive_for(file_id)
    up  = google.uploader()                # always the OAuth one

`reach()` answers the diagnostic question directly — which identities can see
this thing at all — and is the first thing worth running when something 404s.
"""

from __future__ import annotations

from typing import Any, Literal

from . import drive as _drive

Identity = Literal["service_account", "oauth"]


def _sa_drive(readonly: bool = False):
    return _drive.service(readonly=readonly)


def _oauth_drive():
    return _drive.user_service(interactive=False)


def _sa_sheets(readonly: bool = False):
    from . import sheets as _sheets
    return _sheets._service(readonly=readonly).spreadsheets()


def _oauth_sheets():
    import os
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    token = os.environ.get("GOOGLE_OAUTH_TOKEN") or str(_drive._token_path())
    creds = Credentials.from_authorized_user_file(token, _drive._SCOPES_USER)
    return build("sheets", "v4", credentials=creds,
                 cache_discovery=False).spreadsheets()


def _can_see(svc, file_id: str) -> bool:
    from googleapiclient.errors import HttpError
    try:
        svc.files().get(fileId=file_id, fields="id", supportsAllDrives=True).execute()
        return True
    except HttpError:
        return False


def reach(file_id: str) -> dict[str, Any]:
    """Which identities can see this file, and why.

    Run this first when something returns 404 — with `drive.file` a 404 means
    "not created by this app", not "does not exist".
    """
    out: dict[str, Any] = {"file_id": file_id, "service_account": False,
                           "oauth": False, "name": None}
    try:
        sa = _sa_drive(readonly=True)
        if _can_see(sa, file_id):
            out["service_account"] = True
            out["name"] = sa.files().get(fileId=file_id, fields="name",
                                         supportsAllDrives=True).execute()["name"]
    except Exception:
        pass
    try:
        oa = _oauth_drive()
        if _can_see(oa, file_id):
            out["oauth"] = True
            out["name"] = out["name"] or oa.files().get(
                fileId=file_id, fields="name",
                supportsAllDrives=True).execute()["name"]
    except Exception:
        pass
    return out


def identity_for(file_id: str) -> Identity:
    """Which identity to use for a given file.

    The service account is preferred when both can see it: it never expires,
    needs no browser, and is the one a human has deliberately shared with.
    """
    r = reach(file_id)
    if r["service_account"]:
        return "service_account"
    if r["oauth"]:
        return "oauth"
    raise FileNotFoundError(
        f"{file_id} is not visible to either identity.\n"
        f"  - share it with {_drive.service_account_email()} (Editor), or\n"
        f"  - it must have been created by the OAuth app to be reachable that way")


def drive_for(file_id: str, *, readonly: bool = False):
    """A Drive client that can actually see `file_id`."""
    return (_sa_drive(readonly=readonly)
            if identity_for(file_id) == "service_account" else _oauth_drive())


def sheets_for(spreadsheet_id: str, *, readonly: bool = False):
    """A Sheets `.spreadsheets()` client that can actually see the sheet.

    The Sheets API accepts `drive.file`, so the OAuth identity drives sheets it
    created just as well as the service account drives shared ones.
    """
    return (_sa_sheets(readonly=readonly)
            if identity_for(spreadsheet_id) == "service_account"
            else _oauth_sheets())


def uploader():
    """Always the OAuth client — the service account cannot create files."""
    return _oauth_drive()


def whoami() -> dict[str, Any]:
    """Both identities, their addresses and their storage. Cheap health check."""
    out: dict[str, Any] = {}
    try:
        sa = _drive.authorised_as(_sa_drive(readonly=True))
        out["service_account"] = {
            "email": sa["user"]["emailAddress"],
            "storage_limit": int(sa["storageQuota"].get("limit", 0) or 0)}
    except Exception as exc:
        out["service_account"] = {"error": str(exc)[:120]}
    try:
        oa = _drive.authorised_as(_oauth_drive())
        q = oa["storageQuota"]
        out["oauth"] = {"email": oa["user"]["emailAddress"],
                        "storage_limit": int(q.get("limit", 0) or 0),
                        "storage_used": int(q.get("usage", 0) or 0)}
    except Exception as exc:
        out["oauth"] = {"error": str(exc)[:120]}
    return out
