"""Google Drive/Sheets boundary for the DCF round-trip.

The DCF workbook (`dcf/<TICKER>.xlsx`) is pushed to a Google Sheet so the user
can edit assumptions in the browser, then pulled back down as `.xlsx` for
`workbook_reader.read_valuation` → `refresh_dcf.refresh_one` to recompute. This
module is the thin, mockable seam over the Google API:

  * :func:`export_workbook`     — push a local .xlsx → a (new or existing) Sheet
  * :func:`download_sheet_xlsx` — pull a Sheet → a local .xlsx
  * :func:`authorize_interactive` — one-time OAuth consent (mints the token)

Everything is plain Drive API (files.create/update/export with xlsx↔Sheets
conversion) — no per-cell Sheets calls. Least-privilege scope ``drive.file``
(per-file access to files this app creates/opens) covers create, update, share,
and export of app-created Sheets.

Credentials are NOT in the repo. Two shapes are auto-detected from the JSON:

  * **User OAuth** (documented default) — a downloaded OAuth *client-secrets*
    file (``{"installed": ...}``); the actual token is minted by a one-time
    consent flow (:func:`authorize_interactive`) and cached at the token path.
    The created Sheet is owned by the authorizing user.
  * **Service account** — a ``{"type": "service_account", ...}`` key; works
    headless with no consent flow. The created Sheet is owned by the service
    account, so :func:`export_workbook` shares it to ``share_with``.

See ``directives/dcf_gsheets_setup.md`` for the credential setup. The Google client
libraries are an OPTIONAL dependency (``pip install -e .[gsheets]``) imported
lazily here, so the credential-free re-ingest path (local .xlsx → recompute)
never needs them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from runtime.secrets import secret_read_path, secret_write_path, write_private_text

# Per-file Drive access — the app can create files and touch only the files it
# created/opened. Sufficient for the export→edit→import round-trip; a Sheet the
# user created by hand (not via export) is invisible under this scope.
SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/drive.file",)

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SHEET_MIME = "application/vnd.google-apps.spreadsheet"

# Default on-disk locations are outside the repository. Overridable via env so
# a different machine / CI can point elsewhere.
_CREDENTIALS_ENV = "DCF_GSHEETS_CREDENTIALS"
_TOKEN_ENV = "DCF_GSHEETS_TOKEN"

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")


class GSheetsError(Exception):
    """Base for any Google-Sheets integration failure."""


class GSheetsAuthError(GSheetsError):
    """Credentials are missing, malformed, or not yet authorized.

    Carries an actionable setup message. Raised before any Google library import
    when the cause is a missing/unrecognized credential file, so the
    'creds-not-configured' path is testable without the optional dependency.
    """


class GSheetsApiError(GSheetsError):
    """A Google API call failed (network, permission, not-found, quota)."""


@dataclass(frozen=True)
class ExportResult:
    """Outcome of :func:`export_workbook`."""

    sheet_id: str
    url: str
    created: bool  # True if a new Sheet was created, False if an existing one was updated


# --------------------------------------------------------------------------- #
# Pure helpers (no Google dependency)
# --------------------------------------------------------------------------- #
def sheet_url(sheet_id: str) -> str:
    """The canonical browser-editable URL for a Sheet id."""
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"


def parse_sheet_id(url_or_id: str) -> str:
    """Accept either a full Sheet URL or a bare id; return the id.

    A bare id (no slashes) passes through unchanged; a docs.google.com URL has
    its ``/spreadsheets/d/<id>`` segment extracted.
    """
    s = url_or_id.strip()
    m = _SHEET_ID_RE.search(s)
    if m:
        return m.group(1)
    if "/" in s or " " in s:
        raise GSheetsError(f"could not parse a Sheet id from {url_or_id!r}")
    return s


def default_credentials_path(repo_root: Path) -> Path:
    """Resolved credentials path: explicit env or external secret storage."""
    import os

    env = os.environ.get(_CREDENTIALS_ENV)
    return Path(env) if env else secret_read_path("gsheets_credentials.json", repo_root=repo_root)


def default_token_path(repo_root: Path) -> Path:
    """Resolved OAuth token path: explicit env or external secret storage."""
    import os

    env = os.environ.get(_TOKEN_ENV)
    return Path(env) if env else secret_write_path("gsheets_token.json")


def existing_token_path(repo_root: Path) -> Path:
    """Read an existing token from external storage or the legacy migration path."""
    import os

    env = os.environ.get(_TOKEN_ENV)
    return Path(env) if env else secret_read_path("gsheets_token.json", repo_root=repo_root)


def credential_kind(credentials_path: Path) -> str:
    """Classify a credential JSON: ``service_account`` | ``oauth_client`` | ``oauth_token``.

    Pure JSON inspection (no Google import), so callers can branch on the auth
    style before touching the optional dependency. Raises :class:`GSheetsAuthError`
    if the file is unreadable or its shape is unrecognized.
    """
    try:
        raw: object = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GSheetsAuthError(f"cannot read credential JSON at {credentials_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise GSheetsAuthError(f"credential JSON at {credentials_path} is not an object")
    data = cast("dict[str, object]", raw)
    if data.get("type") == "service_account":
        return "service_account"
    if "installed" in data or "web" in data:
        return "oauth_client"
    if data.get("type") == "authorized_user" or "refresh_token" in data:
        return "oauth_token"
    raise GSheetsAuthError(
        f"unrecognized credential JSON at {credentials_path} — expected a service-account key, "
        "an OAuth client-secrets file, or a saved OAuth token"
    )


def _setup_hint(credentials_path: Path) -> str:
    return (
        f"No Google credentials at {credentials_path}. Set up either a user-OAuth client or a "
        f"service-account key (see directives/dcf_gsheets_setup.md), drop the JSON there (or point "
        f"${_CREDENTIALS_ENV} at it), and for OAuth run `python execution/dcf_sheets.py auth` once."
    )


# --------------------------------------------------------------------------- #
# Lazy Google-library access — dynamic imports so the optional dependency is
# invisible to the static type checker (CI's pyright job installs only the base
# requirements) and never imported on the credential-free re-ingest path.
# --------------------------------------------------------------------------- #
def _import(module: str) -> Any:
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
        raise GSheetsError(
            f"the Google API client is not installed ({module!r} missing). Install the optional "
            "extra:  pip install -e .[gsheets]  (and `pip install -r requirements.txt`)"
        ) from exc


def load_credentials(
    repo_root: Path,
    *,
    credentials_path: Path | None = None,
    token_path: Path | None = None,
    interactive: bool = False,
) -> Any:
    """Resolve and build a Google credential object for the Drive client.

    Auto-detects the credential style. For service accounts the key is used
    directly. For user OAuth a cached token is loaded/refreshed; if there is no
    valid token and ``interactive`` is False (the dashboard/cron path), a
    :class:`GSheetsAuthError` directs the user to run the one-time ``auth`` flow.

    The missing-file and unrecognized-shape branches raise before importing any
    Google library (testable without the optional dependency).
    """
    creds_path = credentials_path or default_credentials_path(repo_root)
    if not creds_path.exists():
        raise GSheetsAuthError(_setup_hint(creds_path))

    kind = credential_kind(creds_path)
    if kind == "service_account":
        sa = _import("google.oauth2.service_account")
        return sa.Credentials.from_service_account_file(str(creds_path), scopes=list(SCOPES))

    # OAuth: the actual credential lives in the token file; creds_path may be the
    # client-secrets (for the consent flow) or already a saved token.
    tok_path = token_path or existing_token_path(repo_root)
    if kind == "oauth_token":
        tok_path = creds_path
    write_path = token_path or default_token_path(repo_root)
    return _load_oauth_credentials(
        creds_path,
        tok_path,
        interactive=interactive,
        token_write_path=write_path,
    )


def _load_oauth_credentials(
    client_secrets: Path,
    token_path: Path,
    *,
    interactive: bool,
    token_write_path: Path | None = None,
) -> Any:
    write_path = token_write_path or token_path
    oauth_creds = _import("google.oauth2.credentials")
    creds: Any = None
    if token_path.exists():
        creds = oauth_creds.Credentials.from_authorized_user_file(str(token_path), list(SCOPES))

    if creds is not None and creds.valid:
        return creds

    if creds is not None and creds.expired and creds.refresh_token:
        request = _import("google.auth.transport.requests").Request()
        try:
            creds.refresh(request)
        except Exception as exc:  # any refresh failure -> re-auth guidance
            raise GSheetsAuthError(
                f"OAuth token refresh failed ({exc}); re-run `python execution/dcf_sheets.py auth`"
            ) from exc
        _save_token(creds, write_path)
        return creds

    if not interactive:
        raise GSheetsAuthError(
            f"Google OAuth is not authorized yet (no valid token at {token_path}). Run "
            "`python execution/dcf_sheets.py auth` once to authorize in your browser."
        )

    flow_mod = _import("google_auth_oauthlib.flow")
    flow = flow_mod.InstalledAppFlow.from_client_secrets_file(str(client_secrets), list(SCOPES))
    creds = flow.run_local_server(port=0)
    _save_token(creds, write_path)
    return creds


def _save_token(creds: Any, token_path: Path) -> None:
    write_private_text(token_path, creds.to_json())


def authorize_interactive(
    repo_root: Path,
    *,
    credentials_path: Path | None = None,
    token_path: Path | None = None,
) -> Path:
    """Run the one-time OAuth consent flow and cache the token. Returns its path.

    No-op-friendly for service-account creds: those need no interactive consent,
    so this raises a clear message pointing at the headless path instead.
    """
    creds_path = credentials_path or default_credentials_path(repo_root)
    if not creds_path.exists():
        raise GSheetsAuthError(_setup_hint(creds_path))
    if credential_kind(creds_path) == "service_account":
        raise GSheetsAuthError(
            "the configured credential is a service account — no interactive authorization is "
            "needed; export/import work headlessly."
        )
    tok_path = token_path or default_token_path(repo_root)
    load_credentials(repo_root, credentials_path=creds_path, token_path=tok_path, interactive=True)
    return tok_path


def _build_drive(creds: Any) -> Any:
    discovery = _import("googleapiclient.discovery")
    # cache_discovery=False silences the file-cache warning under recent oauth2client.
    return discovery.build("drive", "v3", credentials=creds, cache_discovery=False)


def build_drive_client(creds: Any) -> Any:
    """Build a Drive v3 client for other narrow Drive integrations."""
    return _build_drive(creds)


def media_file_upload(path: Path, *, resumable: bool, chunksize: int) -> Any:
    """Build a lazily imported Drive media upload without a hard dependency."""
    return _import("googleapiclient.http").MediaFileUpload(
        str(path), resumable=resumable, chunksize=chunksize
    )


def _media_upload(xlsx_path: Path) -> Any:
    http = _import("googleapiclient.http")
    return http.MediaFileUpload(str(xlsx_path), mimetype=_XLSX_MIME, resumable=False)


# --------------------------------------------------------------------------- #
# Export / import
# --------------------------------------------------------------------------- #
def export_workbook(
    xlsx_path: Path,
    *,
    title: str,
    sheet_id: str | None = None,
    share_with: str | None = None,
    creds: Any = None,
    repo_root: Path | None = None,
) -> ExportResult:
    """Push ``xlsx_path`` to a Google Sheet (Drive xlsx→Sheets conversion).

    With ``sheet_id`` the existing Sheet's content is replaced; without it a new
    Sheet titled ``title`` is created and (if ``share_with`` is given) shared as
    editor — needed for the service-account flow, where the Sheet would otherwise
    be owned solely by the service account.

    NOTE: this OVERWRITES the Sheet's contents with the local workbook. Edits made
    in the browser since the last export are lost; use :func:`download_sheet_xlsx`
    (the re-ingest path) to pull edits back first.

    ``creds`` may be passed in (tests inject a fake); otherwise they are loaded
    from ``repo_root``.
    """
    if not xlsx_path.exists():
        raise GSheetsError(f"workbook not found: {xlsx_path}")
    if creds is None:
        if repo_root is None:
            raise GSheetsError("export_workbook needs either creds= or repo_root=")
        creds = load_credentials(repo_root)

    drive = _build_drive(creds)
    media = _media_upload(xlsx_path)

    if sheet_id:
        try:
            drive.files().update(fileId=sheet_id, media_body=media).execute()
        except Exception as exc:  # wrap any Google/transport error in a domain error
            raise GSheetsApiError(
                f"failed to update Sheet {sheet_id}; the stored id may be stale "
                f"(re-export with --new to create a fresh Sheet): {exc}"
            ) from exc
        return ExportResult(sheet_id=sheet_id, url=sheet_url(sheet_id), created=False)

    try:
        body = {"name": title, "mimeType": _SHEET_MIME}
        created = drive.files().create(body=body, media_body=media, fields="id").execute()
        new_id = str(created["id"])
        if share_with:
            _share(drive, new_id, share_with)
    except Exception as exc:  # wrap any Google/transport error in a domain error
        raise GSheetsApiError(f"failed to create Sheet for {title!r}: {exc}") from exc
    return ExportResult(sheet_id=new_id, url=sheet_url(new_id), created=True)


def _share(drive: Any, file_id: str, email: str) -> None:
    drive.permissions().create(
        fileId=file_id,
        body={"type": "user", "role": "writer", "emailAddress": email},
        sendNotificationEmail=False,
        fields="id",
    ).execute()


def download_sheet_xlsx(
    sheet_id: str,
    dest_path: Path,
    *,
    creds: Any = None,
    repo_root: Path | None = None,
) -> Path:
    """Export the Google Sheet ``sheet_id`` to ``dest_path`` as .xlsx. Returns the path."""
    if creds is None:
        if repo_root is None:
            raise GSheetsError("download_sheet_xlsx needs either creds= or repo_root=")
        creds = load_credentials(repo_root)

    drive = _build_drive(creds)
    try:
        data: bytes = drive.files().export(fileId=sheet_id, mimeType=_XLSX_MIME).execute()
    except Exception as exc:  # wrap any Google/transport error in a domain error
        raise GSheetsApiError(f"failed to export Sheet {sheet_id} as xlsx: {exc}") from exc
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(data)
    return dest_path
