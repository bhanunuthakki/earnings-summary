"""Upload static backup artifacts to an app-owned Google Drive folder.

This is the headless transport counterpart to the local backup writers.  It
uses the existing least-privilege ``drive.file`` OAuth token, so it can run
without Google Drive for desktop and cannot browse arbitrary Drive content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterable
from contextlib import nullcontext
from pathlib import Path
from typing import Any, cast

from _lib import PROJECT_ROOT

from integrations.gsheets import (
    build_drive_client,
    load_credentials,
    media_file_upload,
)
from log_redact import redact
from runtime.backup_retention import (
    DATABASE_BACKUP_SETS,
    mark_uploaded,
    prune_uploaded,
    safe_file,
    unfinished_after,
    validated_receipt,
    verified_files,
)
from runtime.job_runtime import JobLock, inherited_lock_is_valid
from runtime.secrets import load_project_env

FOLDER_MIME = "application/vnd.google-apps.folder"
BACKUP_OWNER = "earnings-summary-headless-backup"
DEFAULT_ROOT_FOLDER = "Windows headless backups"

UPLOAD_CHUNK_RETRIES = 5


def _quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _children(drive: Any, parent_id: str) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        response = (
            drive.files()
            .list(
                q=f"'{_quoted(parent_id)}' in parents and trashed = false",
                spaces="drive",
                fields="nextPageToken,files(id,name,mimeType,size,md5Checksum,appProperties)",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )
        children.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return children


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


def _md5(path: Path) -> str:
    # Drive exposes md5Checksum for ordinary binary files.  SHA-256 remains the
    # app-owned identity marker, while MD5 is used only as an independent
    # transport-integrity receipt for the bytes Drive actually stored.
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_file(drive: Any, folder_id: str, path: Path, *, backup_set: str) -> str:
    if not safe_file(path):
        raise FileNotFoundError(path)
    local_receipt = (
        validated_receipt(path, family=backup_set) if backup_set in DATABASE_BACKUP_SETS else None
    )
    if backup_set in DATABASE_BACKUP_SETS and local_receipt is None:
        raise RuntimeError("database backup lacks a valid local verification receipt")
    checksum = _sha256(path)
    transport_checksum = _md5(path)
    size = path.stat().st_size
    visible = [item for item in _children(drive, folder_id) if item.get("name") == path.name]
    if len(visible) > 1:
        raise RuntimeError(f"multiple app-visible Drive files named {path.name!r}")
    properties = {
        "backup_owner": BACKUP_OWNER,
        "backup_set": backup_set,
        "sha256": checksum,
    }
    if local_receipt is not None:
        properties["upload_status"] = "complete"
        properties["verified_snapshot"] = "database"
        properties["source_identity"] = hashlib.sha256(local_receipt.source.encode()).hexdigest()
    if visible:
        remote = visible[0]
        raw_properties = remote.get("appProperties")
        remote_properties: dict[str, Any] = {}
        if isinstance(raw_properties, dict):
            remote_properties = {
                str(key): value for key, value in cast(dict[object, object], raw_properties).items()
            }
        if (
            remote_properties.get("sha256") == checksum
            and int(remote.get("size", -1)) == size
            and remote.get("md5Checksum") == transport_checksum
            and all(remote_properties.get(key) == value for key, value in properties.items())
        ):
            return "unchanged"

    media = media_file_upload(path, resumable=True, chunksize=8 * 1024 * 1024)
    transfer_properties = dict(properties)
    if local_receipt is not None:
        transfer_properties["upload_status"] = "pending"
    if visible:
        request = drive.files().update(
            fileId=str(visible[0]["id"]),
            body={"appProperties": transfer_properties},
            media_body=media,
            fields="id,name,size,md5Checksum,appProperties",
        )
        outcome = "updated"
    else:
        request = drive.files().create(
            body={"name": path.name, "parents": [folder_id], "appProperties": transfer_properties},
            media_body=media,
            fields="id,name,size,md5Checksum,appProperties",
        )
        outcome = "created"

    response = None
    while response is None:
        # googleapiclient applies randomized exponential backoff for resumable
        # chunk failures when num_retries is non-zero.  This matters most for
        # the multi-gigabyte weekly scratch archive: one transient 5xx or
        # connection reset must not discard hours of completed transfer.
        _, response = request.next_chunk(num_retries=UPLOAD_CHUNK_RETRIES)
    if int(response.get("size", -1)) != size:
        raise RuntimeError(f"Drive size verification failed for {path.name}")
    if response.get("md5Checksum") != transport_checksum:
        raise RuntimeError(f"Drive content checksum verification failed for {path.name}")
    if (response.get("appProperties") or {}).get("sha256") != checksum:
        raise RuntimeError(f"Drive checksum receipt missing for {path.name}")
    if local_receipt is not None:
        completion = (
            drive.files()
            .update(
                fileId=str(response["id"]),
                body={"appProperties": properties},
                fields="appProperties",
            )
            .execute()
        )
        if (completion.get("appProperties") or {}).get("upload_status") != "complete":
            raise RuntimeError("Drive upload completion receipt missing")
    return outcome


def prune_remote(
    drive: Any,
    folder_id: str,
    *,
    backup_set: str,
    retain: int,
    survivor: Path | None = None,
) -> list[str]:
    if retain < 1:
        raise ValueError("retain must be at least 1")
    owned = [
        item
        for item in _children(drive, folder_id)
        if (item.get("appProperties") or {}).get("backup_owner") == BACKUP_OWNER
        and (item.get("appProperties") or {}).get("backup_set") == backup_set
    ]
    if backup_set in DATABASE_BACKUP_SETS:
        if survivor is None:
            raise RuntimeError("database retirement requires a verified uploaded survivor")
        receipt = validated_receipt(survivor, family=backup_set)
        if receipt is None or not receipt.uploaded_at_utc:
            raise RuntimeError("database survivor has no completed upload receipt")
        source_identity = hashlib.sha256(receipt.source.encode()).hexdigest()
        matching = [item for item in owned if item.get("name") == survivor.name]
        if len(matching) != 1 or (
            (matching[0].get("appProperties") or {}).get("sha256") != receipt.ciphertext_sha256
            or int(matching[0].get("size", -1)) != receipt.size_bytes
            or matching[0].get("md5Checksum") != _md5(survivor)
            or (matching[0].get("appProperties") or {}).get("verified_snapshot") != "database"
            or (matching[0].get("appProperties") or {}).get("upload_status") != "complete"
        ):
            raise RuntimeError("remote survivor does not match the verified local bytes")
        owned = [
            item
            for item in owned
            if (
                (item.get("appProperties") or {}).get("verified_snapshot") == "database"
                and (item.get("appProperties") or {}).get("upload_status") == "complete"
                and (item.get("appProperties") or {}).get("source_identity") == source_identity
                and str(item.get("name", "")) <= survivor.name
            )
        ]
    stale = sorted(owned, key=lambda item: str(item.get("name", "")))[:-retain]
    stale = [
        item for item in stale if (item.get("appProperties") or {}).get("recovery_pin") != "true"
    ]
    removed: list[str] = []
    for item in stale:
        if survivor is not None:
            local = survivor.parent / str(item.get("name", ""))
            local_receipt = validated_receipt(local, family=backup_set)
            if local_receipt is not None and local_receipt.recovery_pin:
                continue
        drive.files().delete(fileId=str(item["id"])).execute()
        removed.append(str(item.get("name", "")))
    return removed


def _files(source_dir: Path, patterns: list[str]) -> list[Path]:
    selected = {path for pattern in patterns for path in source_dir.glob(pattern)}
    return sorted(path for path in selected if safe_file(path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--pattern", action="append", required=True)
    parser.add_argument("--folder", required=True, help="Drive subfolder below the app-owned root")
    parser.add_argument("--backup-set", required=True)
    parser.add_argument("--retain", type=int, default=1)
    parser.add_argument("--allow-empty", action="store_true")
    parser.add_argument("--latest-only", action="store_true")
    parser.add_argument(
        "--defer-retention",
        action="store_true",
        help="Upload only; preserve prior copies until all families succeed.",
    )
    parser.add_argument(
        "--finalize-only",
        action="store_true",
        help="Retire verified older copies after every required family uploaded.",
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    try:
        if args.retain < 1 or (args.defer_retention and args.finalize_only):
            raise ValueError("invalid retention mode or count")
        selected = _files(args.source_dir, args.pattern)
        database_family = args.backup_set in DATABASE_BACKUP_SETS
        if database_family:
            load_project_env(PROJECT_ROOT)
            configured = os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
            if not configured:
                raise RuntimeError("EARNINGS_SUMMARY_DB_PATH is required for database uploads")
            source = Path(configured).expanduser().resolve()
            if args.backup_set == "portfolio-gc-archive":
                source = source.parent / "archive/portfolio_gc_archive.db"
            valid = {
                path
                for path, _receipt in verified_files(
                    args.source_dir, family=args.backup_set, source=source
                )
            }
            physical = selected
            selected = [path for path in physical if path in valid]
            if physical and not selected:
                raise RuntimeError("no locally verified snapshot matches the configured source")
        if not selected:
            if args.allow_empty:
                print(json.dumps({"status": "empty", "backup_set": args.backup_set}))
                return 0
            raise RuntimeError("no backup artifacts matched the requested patterns")
        if args.latest_only:
            selected = selected[-1:]
        drive = build_drive_client(load_credentials(PROJECT_ROOT))
        folder_id = ensure_folder_path(drive, [DEFAULT_ROOT_FOLDER, args.folder])
        outcomes: dict[str, str] = {}
        if args.finalize_only and not database_family:
            raise ValueError("finalize-only is restricted to database backup families")
        for path in selected:
            if args.finalize_only:
                receipt = validated_receipt(path, family=args.backup_set)
                if receipt is None or not receipt.uploaded_at_utc:
                    raise RuntimeError("retirement requires a completed verified upload")
            else:
                outcomes[path.name] = upload_file(
                    drive, folder_id, path, backup_set=args.backup_set
                )
                if database_family:
                    mark_uploaded(path, family=args.backup_set)
        removed: list[str] = []
        local_removed: list[str] = []
        pending = False
        if database_family:
            survivor_receipt = validated_receipt(selected[-1], family=args.backup_set)
            if survivor_receipt is None:
                raise RuntimeError("backup survivor changed before retirement")
            pending = unfinished_after(
                args.source_dir,
                family=args.backup_set,
                survivor=selected[-1],
                source=survivor_receipt.source,
            )
        if not args.defer_retention and not pending:
            removed = prune_remote(
                drive,
                folder_id,
                backup_set=args.backup_set,
                retain=args.retain,
                survivor=selected[-1] if database_family else None,
            )
            if database_family:
                local_removed = prune_uploaded(
                    args.source_dir, family=args.backup_set, retain=args.retain
                )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "backup_set": args.backup_set,
                    "uploaded": outcomes,
                    "removed_count": len(removed),
                    "local_removed_count": len(local_removed),
                    "retained_newer_unfinished": pending,
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(f"ERROR: headless Drive backup upload failed: {redact(exc)}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lock = (
        nullcontext()
        if args.backup_set not in DATABASE_BACKUP_SETS
        or inherited_lock_is_valid(PROJECT_ROOT, "backup-drive-upload")
        else JobLock(PROJECT_ROOT, "backup-drive-upload-direct", ["backup-drive-upload"])
    )
    try:
        with lock:
            return _run(args)
    except Exception as exc:
        print(f"ERROR: headless Drive backup lock failed: {redact(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
