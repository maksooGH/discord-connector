"""Google Sheets read/write via a service account.

A service account is its own identity with NO Drive storage of its own. It
therefore CANNOT create spreadsheets — `spreadsheets().create()` returns 403
"The caller does not have permission" and `storageQuota.limit` reads 0.

The working pattern is always:
  1. a human creates the sheet
  2. shares it with the service-account email (Viewer to read, Editor to write)
  3. scripts address it by spreadsheet id

Set GOOGLE_SERVICE_ACCOUNT to the JSON key path. See docs/SETUP.md.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable

_SCOPES_RO = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
_SCOPES_RW = ["https://www.googleapis.com/auth/spreadsheets"]


def _service(readonly: bool = True):
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT")
    if not path:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT is not set — see docs/SETUP.md")
    creds = Credentials.from_service_account_file(path, scopes=_SCOPES_RO if readonly else _SCOPES_RW)
    return build("sheets", "v4", credentials=creds)


def service_account_email() -> str:
    """The address a human must share each sheet with."""
    import json
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT")
    if not path:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT is not set")
    with open(path) as fh:
        return json.load(fh)["client_email"]


def sheet_id_from_url(url: str) -> str:
    """Pull the spreadsheet id out of a normal browser URL."""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url)
    if not m:
        raise ValueError(f"no spreadsheet id in {url!r}")
    return m.group(1)


def tabs(spreadsheet_id: str) -> list[dict]:
    """[{title, gid, rows, cols}] — note titles can carry trailing spaces,
    which breaks A1 ranges unless you use the exact string."""
    meta = _service().spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    return [{"title": s["properties"]["title"],
             "gid": s["properties"]["sheetId"],
             "rows": s["properties"]["gridProperties"].get("rowCount"),
             "cols": s["properties"]["gridProperties"].get("columnCount")}
            for s in meta["sheets"]]


def read(spreadsheet_id: str, tab: str, a1: str = "A1:AZ1000") -> list[list[str]]:
    """Values from a tab. Quote the tab name exactly — trailing spaces are real."""
    rng = f"'{tab}'!{a1}"
    return _service().spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=rng).execute().get("values", [])


def write(spreadsheet_id: str, tab: str, values: Iterable[Iterable[Any]],
          a1: str = "A1", clear: bool = True) -> None:
    """Overwrite a tab. Requires the sheet to be shared with the service account
    as EDITOR."""
    svc = _service(readonly=False).spreadsheets()
    if clear:
        svc.values().clear(spreadsheetId=spreadsheet_id, range=f"'{tab}'").execute()
    svc.values().update(spreadsheetId=spreadsheet_id, range=f"'{tab}'!{a1}",
                        valueInputOption="USER_ENTERED",
                        body={"values": [list(r) for r in values]}).execute()


def hyperlink(url: str, label: str, *, separator: str = ",") -> str:
    """A HYPERLINK formula.

    The argument separator is LOCALE DEPENDENT: most sheets use ',' but some use
    ';'. Guess wrong and every cell renders #ERROR!. Check an existing formula in
    the target sheet before bulk-writing.
    """
    safe = (label or url).replace('"', '""')
    return f'=HYPERLINK("{url}"{separator}"{safe}")'


def discord_link(guild_id: int, channel_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}"
