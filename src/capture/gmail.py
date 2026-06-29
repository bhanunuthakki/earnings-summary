"""Gmail capture adapter (The Ledger secondary mouth) — share-to-email capture.

The owner forwards a thought (typed body or a voice-memo attachment) to a self
address; a Gmail filter labels it ``Capture/Inbox``. This adapter polls that
label, lands each message through the SAME LLM-free ingest pipeline as Telegram
(``channel='gmail'``), and relabels ``Capture/Inbox`` -> ``Capture/Done`` (the
visible "processed" signal + the dedup latch). It runs as its OWN process
(decoupled from the Telegram poller, so a Gmail/API hiccup can never wedge the
primary mouth).

Reuses the existing Google OAuth machinery shape (``src/integrations/gsheets.py``)
but with a SECOND ``gmail.modify`` token (Google tokens are scope-bound). The
message PARSER (``extract_text`` / ``list_audio_parts``) is pure and unit-tested;
the live API calls are lazy-imported so the module + tests need no google libs.
"""

from __future__ import annotations

import base64
import binascii
import importlib
from pathlib import Path
from typing import Any, cast

from capture.token_store import CaptureSetupError

GMAIL_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.modify",)
_CREDENTIALS_REL = Path("data") / "secrets" / "gmail_credentials.json"
_TOKEN_REL = Path("data") / "secrets" / "gmail_token.json"
CAPTURE_INBOX_LABEL = "Capture/Inbox"
CAPTURE_DONE_LABEL = "Capture/Done"


def credentials_path(repo_root: Path) -> Path:
    return repo_root / _CREDENTIALS_REL


def token_path(repo_root: Path) -> Path:
    return repo_root / _TOKEN_REL


def _imp(name: str) -> Any:
    return importlib.import_module(name)


# --------------------------------------------------------------------------
# Pure message parsing (unit-tested; no google libs)
# --------------------------------------------------------------------------


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad)
    except (binascii.Error, ValueError):
        return b""


def _walk_parts(payload: dict[str, object]) -> list[dict[str, object]]:
    """Flatten a (possibly nested) MIME part tree."""
    out: list[dict[str, object]] = [payload]
    parts = payload.get("parts")
    if isinstance(parts, list):
        for part in cast("list[object]", parts):
            if isinstance(part, dict):
                out.extend(_walk_parts(cast("dict[str, object]", part)))
    return out


def extract_text(message: dict[str, object]) -> str:
    """The plain-text body of a Gmail message (first text/plain part), decoded."""
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return ""
    for part in _walk_parts(cast("dict[str, object]", payload)):
        if part.get("mimeType") == "text/plain":
            body = part.get("body")
            if isinstance(body, dict):
                data = cast("dict[str, object]", body).get("data")
                if isinstance(data, str) and data:
                    return _b64url_decode(data).decode("utf-8", "replace").strip()
    return ""


def list_audio_parts(message: dict[str, object]) -> list[tuple[str, str]]:
    """(filename, attachment_id) for every audio/* attachment in the message."""
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return []
    found: list[tuple[str, str]] = []
    for part in _walk_parts(cast("dict[str, object]", payload)):
        mime = part.get("mimeType")
        if isinstance(mime, str) and mime.startswith("audio/"):
            body = part.get("body")
            attach_id = (
                cast("dict[str, object]", body).get("attachmentId")
                if isinstance(body, dict)
                else None
            )
            filename = part.get("filename")
            if isinstance(attach_id, str) and attach_id:
                found.append((filename if isinstance(filename, str) else "voice.oga", attach_id))
    return found


# --------------------------------------------------------------------------
# Live API (lazy-imported; not unit-tested)
# --------------------------------------------------------------------------


def load_credentials(repo_root: Path, *, interactive: bool = False) -> Any:
    """Load/refresh the gmail.modify OAuth credential (separate token from Drive)."""
    creds_path = credentials_path(repo_root)
    if not creds_path.exists():
        raise CaptureSetupError(
            f"Gmail client-secrets not found at {creds_path}. Drop your Google OAuth "
            "client-secrets there and run `python execution/capture_gmail_auth.py` once."
        )
    tok_path = token_path(repo_root)
    oauth_creds = _imp("google.oauth2.credentials")
    creds: Any = None
    if tok_path.exists():
        creds = oauth_creds.Credentials.from_authorized_user_file(str(tok_path), list(GMAIL_SCOPES))
    if creds is not None and creds.valid:
        return creds
    if creds is not None and creds.expired and creds.refresh_token:
        request = _imp("google.auth.transport.requests").Request()
        creds.refresh(request)
        _save_token(creds, tok_path)
        return creds
    if not interactive:
        raise CaptureSetupError(
            f"Gmail OAuth not authorized (no valid token at {tok_path}). Run "
            "`python execution/capture_gmail_auth.py` once."
        )
    flow_mod = _imp("google_auth_oauthlib.flow")
    flow = flow_mod.InstalledAppFlow.from_client_secrets_file(str(creds_path), list(GMAIL_SCOPES))
    creds = flow.run_local_server(port=0)
    _save_token(creds, tok_path)
    return creds


def _save_token(creds: Any, tok_path: Path) -> None:
    tok_path.parent.mkdir(parents=True, exist_ok=True)
    tok_path.write_text(creds.to_json(), encoding="utf-8")


def build_service(repo_root: Path, *, interactive: bool = False) -> Any:
    creds = load_credentials(repo_root, interactive=interactive)
    return _imp("googleapiclient.discovery").build(
        "gmail", "v1", credentials=creds, cache_discovery=False
    )


def _label_id(labels: list[object], name: str) -> str | None:
    for label in labels:
        if isinstance(label, dict):
            data = cast("dict[str, object]", label)
            if data.get("name") == name:
                lid = data.get("id")
                return lid if isinstance(lid, str) else None
    return None


def poll_gmail(
    repo_root: Path, db_path: Path | str | None, *, roster: object = None, limit: int = 25
) -> dict[str, int]:
    """Poll the ``Capture/Inbox`` label, ingest each message (text or audio)
    through the shared pipeline, and relabel to ``Capture/Done`` (dedup latch).
    Returns counts. Best-effort: a per-message failure is counted, not raised."""
    from capture import ingest
    from capture.matcher import RosterIndex, load_roster

    service = build_service(repo_root)
    users = service.users()
    labels = users.labels().list(userId="me").execute().get("labels", [])
    inbox_id = _label_id(labels, CAPTURE_INBOX_LABEL)
    done_id = _label_id(labels, CAPTURE_DONE_LABEL)
    counts = {"messages": 0, "landed": 0, "failed": 0}
    if inbox_id is None:
        return counts  # the owner hasn't created the Capture/Inbox label yet

    index = roster if isinstance(roster, RosterIndex) else load_roster(db_path)
    audio_dir = repo_root / "data" / "capture" / "audio"
    listing = (
        users.messages()
        .list(userId="me", labelIds=[inbox_id], maxResults=limit)
        .execute()
        .get("messages", [])
    )
    for ref in listing:
        mid = cast("dict[str, object]", ref).get("id") if isinstance(ref, dict) else None
        if not isinstance(mid, str):
            continue
        counts["messages"] += 1
        full = cast(
            "dict[str, object]", users.messages().get(userId="me", id=mid, format="full").execute()
        )
        landed = False
        audio = list_audio_parts(full)
        if audio:
            audio_dir.mkdir(parents=True, exist_ok=True)
            for filename, attach_id in audio:
                att = (
                    users.messages()
                    .attachments()
                    .get(userId="me", messageId=mid, id=attach_id)
                    .execute()
                )
                dest = audio_dir / f"gmail_{mid}_{filename}"
                dest.write_bytes(_b64url_decode(str(att.get("data") or "")))
                result = ingest.ingest_capture(
                    channel="gmail",
                    media_kind="voice",
                    audio_path=dest,
                    external_ref=f"gmail:{mid}:{attach_id}",
                    roster=index,
                    db_path=db_path,
                )
                if result.status == "landed":
                    dest.unlink(missing_ok=True)
                    landed = True
        else:
            text = extract_text(full)
            if text:
                result = ingest.ingest_capture(
                    channel="gmail",
                    media_kind="text",
                    text=text,
                    external_ref=f"gmail:{mid}",
                    roster=index,
                    db_path=db_path,
                )
                landed = result.status in ("landed", "duplicate")
        if done_id is not None:
            users.messages().modify(
                userId="me",
                id=mid,
                body={"addLabelIds": [done_id], "removeLabelIds": [inbox_id]},
            ).execute()
        counts["landed" if landed else "failed"] += 1
    return counts
