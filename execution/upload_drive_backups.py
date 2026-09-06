"""Upload static backup artifacts to an app-owned Google Drive folder.

This is the headless transport counterpart to the local backup writers.  It
uses the existing least-privilege ``drive.file`` OAuth token, so it can run
without Google Drive for desktop and cannot browse arbitrary Drive content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from _lib import PROJECT_ROOT

from integrations.gsheets import (
    build_drive_client,
    load_credentials,
    media_file_upload,
)
from log_redact import redact

FOLDER_MIME = "application/vnd.google-apps.folder"
BACKUP_OWNER = "earnings-summary-headless-backup"
DEFAULT_ROOT_FOLDER = "Windows headless backups"


def _quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _children(drive: Any, parent_id: str) -> list[dict[str, Any]]:
    response = (
        drive.files()
        .list(
            q=f"'{_quoted(parent_id)}' in parents and trashed = false",
            spaces="drive",
            fields="files(id,name,mimeType,size,appProperties)",
            pageSize=1000,
        )
        .execute()
    )
    return list(response.get("files", []))


def _ensure_folder(drive: Any, parent_id: str, name: str) -> str:
    matches = [
        item
        for item in _children(drive, parent_id)
        if item.get("name") == name and item.get("mimeType") == FOLDER_MIME
    ]
    if len(matches) > 1:
        raise RuntimeError(f"multiple app-visible Drive folders named {name!r}")
    if matches:
        return str(matches[0]["id"])
    created = (
        drive.files()
        .create(
            body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
            fields="id",
        )
        .execute()
    )
    return str(created["id"])


def ensure_folder_path(drive: Any, parts: Iterable[str]) -> str:
    parent_id = "root"
    for raw_part in parts:
        part = raw_part.strip()
        if not part or part in {".", ".."} or "/" in part or "\\" in part:
            raise ValueError(f"invalid Drive folder segment: {raw_part!r}")
        parent_id = _ensure_folder(drive, parent_id, part)
    return parent_id


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_file(drive: Any, folder_id: str, path: Path, *, backup_set: str) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    checksum = _sha256(path)
    size = path.stat().st_size
    visible = [item for item in _children(drive, folder_id) if item.get("name") == path.name]
    if len(visible) > 1:
        raise RuntimeError(f"multiple app-visible Drive files named {path.name!r}")
    properties = {
        "backup_owner": BACKUP_OWNER,
        "backup_set": backup_set,
        "sha256": checksum,
    }
    if visible:
        remote = visible[0]
        raw_properties = remote.get("appProperties")
        remote_properties: dict[str, Any] = {}
        if isinstance(raw_properties, dict):
            remote_properties = {
                str(key): value for key, value in cast(dict[object, object], raw_properties).items()
            }
        if remote_properties.get("sha256") == checksum and int(remote.get("size", -1)) == size:
            return "unchanged"

    media = media_file_upload(path, resumable=True, chunksize=8 * 1024 * 1024)
    if visible:
        request = drive.files().update(
            fileId=str(visible[0]["id"]),
            body={"appProperties": properties},
            media_body=media,
            fields="id,name,size,appProperties",
        )
        outcome = "updated"
    else:
        request = drive.files().create(
            body={"name": path.name, "parents": [folder_id], "appProperties": properties},
            media_body=media,
            fields="id,name,size,appProperties",
        )
        outcome = "created"

    response = None
    while response is None:
        _, response = request.next_chunk()
    if int(response.get("size", -1)) != size:
        raise RuntimeError(f"Drive size verification failed for {path.name}")
    if (response.get("appProperties") or {}).get("sha256") != checksum:
        raise RuntimeError(f"Drive checksum receipt missing for {path.name}")
    return outcome


def prune_remote(drive: Any, folder_id: str, *, backup_set: str, retain: int) -> list[str]:
    if retain < 1:
        raise ValueError("retain must be at least 1")
    owned = [
        item
        for item in _children(drive, folder_id)
        if (item.get("appProperties") or {}).get("backup_owner") == BACKUP_OWNER
        and (item.get("appProperties") or {}).get("backup_set") == backup_set
    ]
    stale = sorted(owned, key=lambda item: str(item.get("name", "")))[:-retain]
    removed: list[str] = []
    for item in stale:
        drive.files().delete(fileId=str(item["id"])).execute()
        removed.append(str(item.get("name", "")))
    return removed


def _files(source_dir: Path, patterns: list[str]) -> list[Path]:
    selected = {path.resolve() for pattern in patterns for path in source_dir.glob(pattern)}
    return sorted(path for path in selected if path.is_file())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--pattern", action="append", required=True)
    parser.add_argument("--folder", required=True, help="Drive subfolder below the app-owned root")
    parser.add_argument("--backup-set", required=True)
    parser.add_argument("--retain", type=int, required=True)
    parser.add_argument("--allow-empty", action="store_true")
    parser.add_argument("--latest-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        selected = _files(args.source_dir, args.pattern)
        if not selected:
            if args.allow_empty:
                print(json.dumps({"status": "empty", "backup_set": args.backup_set}))
                return 0
            raise RuntimeError("no backup artifacts matched the requested patterns")
        if args.latest_only:
            selected = selected[-1:]
        drive = build_drive_client(load_credentials(PROJECT_ROOT))
        folder_id = ensure_folder_path(drive, [DEFAULT_ROOT_FOLDER, args.folder])
        outcomes = {
            path.name: upload_file(drive, folder_id, path, backup_set=args.backup_set)
            for path in selected
        }
        removed = prune_remote(drive, folder_id, backup_set=args.backup_set, retain=args.retain)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "backup_set": args.backup_set,
                    "uploaded": outcomes,
                    "removed_count": len(removed),
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(f"ERROR: headless Drive backup upload failed: {redact(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
