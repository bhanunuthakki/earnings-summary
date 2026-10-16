"""Hash-bound lifecycle receipts for immutable database backup files.

Only uploaded, verified members of the same source/family can age out. Unknown
legacy files, failed/pending attempts, and explicit recovery pins remain intact.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

DATABASE_BACKUP_SETS = frozenset({"portfolio-db", "portfolio-gc-archive"})


class BackupReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    family: str = Field(min_length=1)
    source: str = Field(min_length=1)
    snapshot_name: str = Field(min_length=1)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ciphertext_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    verified_at_utc: str = Field(min_length=1)
    uploaded_at_utc: str | None = None
    recovery_pin: bool = False


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_file(path: Path) -> bool:
    """Reject links/junctions in every path component, and nonregular files."""
    try:
        for part in (path, *path.parents):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return False
        return stat.S_ISREG(path.stat().st_mode)
    except OSError:
        return False


def receipt_path(path: Path) -> Path:
    return path.with_name(path.name + ".receipt.json")


def write_receipt(path: Path, receipt: BackupReceipt) -> None:
    target = receipt_path(path)
    if not safe_file(path):
        raise ValueError("unsafe backup receipt target")
    fd, staged_name = tempfile.mkstemp(prefix=".backup-receipt.", dir=target.parent)
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(receipt.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def record_snapshot(path: Path, *, family: str, source: Path, snapshot_sha256: str) -> None:
    if not safe_file(path) or path.resolve() == source.resolve():
        raise ValueError("unsafe backup snapshot path")
    write_receipt(
        path,
        BackupReceipt(
            family=family,
            source=str(source.resolve()),
            snapshot_name=path.name,
            snapshot_sha256=snapshot_sha256,
            ciphertext_sha256=file_sha256(path),
            size_bytes=path.stat().st_size,
            verified_at_utc=datetime.now(UTC).isoformat(),
        ),
    )


def validated_receipt(path: Path, *, family: str) -> BackupReceipt | None:
    if not safe_file(path) or not safe_file(receipt_path(path)):
        return None
    try:
        receipt = BackupReceipt.model_validate_json(receipt_path(path).read_text(encoding="utf-8"))
        if (
            receipt.family != family
            or receipt.snapshot_name != path.name
            or Path(receipt.source).resolve() == path.resolve()
            or path.stat().st_size != receipt.size_bytes
            or file_sha256(path) != receipt.ciphertext_sha256
        ):
            return None
        return receipt
    except (OSError, UnicodeError, ValidationError):
        return None


def verified_files(
    directory: Path, *, family: str, source: Path | None = None
) -> list[tuple[Path, BackupReceipt]]:
    prefix = "portfolio.db" if family == "portfolio-db" else "portfolio_gc_archive.db"
    files: list[tuple[Path, BackupReceipt]] = []
    for path in sorted(directory.glob(f"{prefix}.*.gz.enc")):
        receipt = validated_receipt(path, family=family)
        if receipt is not None and (source is None or receipt.source == str(source.resolve())):
            files.append((path, receipt))
    return files


def mark_uploaded(path: Path, *, family: str) -> None:
    receipt = validated_receipt(path, family=family)
    if receipt is None:
        raise RuntimeError("backup lacks a valid local verification receipt")
    write_receipt(
        path,
        receipt.model_copy(update={"uploaded_at_utc": datetime.now(UTC).isoformat()}),
    )


def unfinished_after(directory: Path, *, family: str, survivor: Path, source: str) -> bool:
    """Newer unknown/unfinished bytes preserve the source's previous good set."""
    prefix = "portfolio.db" if family == "portfolio-db" else "portfolio_gc_archive.db"
    for path in directory.glob(f"{prefix}.*.gz.enc"):
        if path.name <= survivor.name:
            continue
        receipt = validated_receipt(path, family=family)
        if receipt is None or receipt.source == source:
            return True
    return False


def prune_uploaded(directory: Path, *, family: str, retain: int) -> list[str]:
    if retain < 1:
        raise ValueError("retain must be at least 1")
    files = verified_files(directory, family=family)
    uploaded = [(path, receipt) for path, receipt in files if receipt.uploaded_at_utc]
    if not uploaded:
        raise RuntimeError("retirement requires a verified uploaded survivor")
    by_source: dict[str, list[tuple[Path, BackupReceipt]]] = {}
    for path, receipt in uploaded:
        by_source.setdefault(receipt.source, []).append((path, receipt))
    removed: list[str] = []
    for members in by_source.values():
        survivor, newest = members[-1]
        # Unclassified newer attempts cannot grant retirement of last-good files.
        if unfinished_after(directory, family=family, survivor=survivor, source=newest.source):
            continue
        for path, receipt in members[:-retain]:
            if (
                receipt.recovery_pin
                or Path(str(path) + "-wal").exists()
                or Path(str(path) + "-shm").exists()
            ):
                continue
            if validated_receipt(path, family=family) != receipt:
                raise RuntimeError("backup changed during retirement")
            if validated_receipt(survivor, family=family) != newest:
                raise RuntimeError("backup survivor changed during retirement")
            path.unlink()
            # Preserve the small receipt as the historical retirement ledger.
            removed.append(path.name)

    return removed
