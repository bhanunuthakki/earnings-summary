"""Consistent, sync-safe backup of data/portfolio.db.

Once the scratch tree is removed from Google Drive's folder backup, the live DB is no
longer backed up by Drive. This produces a CONSISTENT snapshot (SQLite online
backup API — safe even while the pipeline writes) and gzips it into a synced
backup folder. The gzip is encrypted with AES-256-GCM before it reaches the
synced directory, so Drive receives only a static authenticated ciphertext.

Run through the shared lock wrapper (cron/run_backup_db.bat schedules it daily):
    cron\run_python.bat backup_db portfolio-db cron\backup_db.py

Backup dir is `ES_DB_BACKUP_DIR` (default: a Google Drive folder so the snapshot
is cloud-backed). Keeps the most recent ES_DB_BACKUP_RETAIN snapshots.

Content-skip (workstream C3): after the integrity gate the snapshot's sha256 is
compared with the last successfully uploaded backup recorded in a LOCAL receipt
beside the DB (`<db-name>.backup_receipt.json`); when the bytes are unchanged AND
that uploaded snapshot is still present, gzip/encrypt/upload are skipped and the
accounting row records StageStatus.SKIPPED with a `skipped_unchanged` marker
(same change-skip philosophy as the db_gc archive sidecar below). RPO is
therefore 24h of *changes* — see cron/restore_db.py.

Restore is the tested counterpart `cron/restore_db.py` (integrity-checked
gunzip + move), which also documents the RPO/RTO targets (sre-3).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from models.runs import StageStatus  # noqa: E402
from pipeline.run_accounting import (  # noqa: E402
    PipelineRunSuppressedError,
    end_run,
    start_run,
    suppression_payload,
)
from runtime.backup_crypto import encrypt_file, load_or_create_key  # noqa: E402
from runtime.job_runtime import JobLock, inherited_lock_is_valid, portfolio_db_path  # noqa: E402
from runtime.secrets import load_project_env  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

DEFAULT_SRC_DB = (PROJECT_ROOT / "data" / "portfolio.db").resolve()
SRC_DB = DEFAULT_SRC_DB
MIRROR_DRIVE_ROOT = Path(os.environ.get("ES_MIRROR_DRIVE_ROOT") or Path.home() / "My Drive")


def _google_drive_root() -> Path:
    """Locate the Google Drive root in either sync mode.

    In Stream mode Drive mounts a virtual drive (usually G:), and the old
    mirror folder under the user profile lingers on disk as a stale,
    UNSYNCED leftover until manually deleted. A mounted "<letter>:\\My Drive"
    can only be the Drive mount, so any non-C: hit wins over the mirror path —
    checking C: first would keep writing backups into the dead folder while
    reporting OK. restore_db.py duplicates this deliberately (backup and
    restore must never resolve to different answers — keep them identical).
    """
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = Path(f"{letter}:/My Drive")
        if candidate.is_dir():
            return candidate
    return MIRROR_DRIVE_ROOT


DEFAULT_DEST = _google_drive_root() / "earnings-summary-db-backups"
DEFAULT_RETAIN = 14
# The db_gc archive sidecar (execution/db_gc.py) holds the ONLY copy of pruned
# rows and underpins the "reversible" contract, yet it lived unbacked-up until
# 2026-08-03. It is append-only, so a snapshot only needs to be newer than the
# last prune, not daily — keep fewer, and skip re-encrypting an unchanged file.
ARCHIVE_PREFIX = "portfolio_gc_archive.db"
DEFAULT_ARCHIVE_RETAIN = 6


@dataclass(frozen=True)
class UploadReceipt:
    """The last successfully uploaded backup, as recorded in the local receipt."""

    snapshot_name: str
    snapshot_sha256: str
    uploaded_at_utc: str


def _upload_receipt_path() -> Path:
    """Local receipt beside the DB (NOT in the synced backup dir, which must
    receive only authenticated ciphertext). Losing it merely re-uploads."""
    return SRC_DB.parent / f"{SRC_DB.name}.backup_receipt.json"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_upload_receipt() -> UploadReceipt | None:
    """Return the last-upload receipt, or None when absent or unusable.

    Absent, unreadable, malformed, or incomplete receipts return None, which
    fails toward performing a real upload — the skip must never engage on
    ambiguous evidence.
    """
    try:
        payload: object = json.loads(_upload_receipt_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    record = cast(Mapping[object, object], payload)
    snapshot_sha256 = record.get("snapshot_sha256")
    snapshot_name = record.get("snapshot_name")
    uploaded_at_utc = record.get("uploaded_at_utc")
    if not isinstance(snapshot_sha256, str) or not snapshot_sha256.strip():
        return None
    if not isinstance(snapshot_name, str) or not snapshot_name.strip():
        return None
    if not isinstance(uploaded_at_utc, str) or not uploaded_at_utc.strip():
        return None
    return UploadReceipt(
        snapshot_sha256=snapshot_sha256.strip(),
        snapshot_name=snapshot_name.strip(),
        uploaded_at_utc=uploaded_at_utc.strip(),
    )


def _write_upload_receipt(snapshot_name: str, snapshot_sha256: str) -> None:
    """Record the last successfully uploaded backup (atomic replace).

    Never fatal: a missing/unwritable receipt cannot lose data — it only means
    the next unchanged day uploads again instead of skipping.
    """
    receipt_path = _upload_receipt_path()
    payload = {
        "snapshot_name": snapshot_name,
        "snapshot_sha256": snapshot_sha256,
        "uploaded_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    try:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        staged = receipt_path.with_name(f"{receipt_path.name}.staged")
        staged.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(staged, receipt_path)
    except OSError as exc:
        print(
            f"WARN: backup upload receipt not recorded (next unchanged run will "
            f"re-upload): {redact(exc)}",
            file=sys.stderr,
        )


def _skipped_unchanged_payload(receipt: UploadReceipt) -> dict[str, str]:
    """Stable CLI/scheduler response for the intentional unchanged no-op.

    Mirrors pipeline.run_accounting.suppression_payload — the accounting row
    carries StageStatus.SKIPPED plus the `skipped_unchanged` marker, and this
    JSON line is the machine-readable stdout record.
    """
    return {
        "status": "skipped_unchanged",
        "last_uploaded_snapshot": receipt.snapshot_name,
        "snapshot_sha256": receipt.snapshot_sha256,
        "uploaded_at_utc": receipt.uploaded_at_utc,
    }


def _consistent_snapshot(src_db: Path, tmp_path: Path) -> None:
    """SQLite online backup -> tmp_path (consistent even under concurrent writes)."""
    src = sqlite3.connect(str(src_db))
    dst = sqlite3.connect(str(tmp_path))
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()


def _integrity_ok(db_path: Path) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"
    finally:
        conn.close()


def _source_schema_revision(conn: sqlite3.Connection) -> str:
    """Read the single current Alembic revision used in backup identity."""
    try:
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("source Alembic revision is unreadable") from exc
    if len(rows) != 1:
        raise RuntimeError("source Alembic revision is unreadable: expected exactly one revision")
    revision: object = rows[0][0]
    if not isinstance(revision, str) or not revision.strip():
        raise RuntimeError("source Alembic revision is unreadable: value is empty")
    return revision.strip()


def _backup_invocation_inputs(
    dest_dir: Path,
    retain: int,
    schema_revision: str,
    run_date: str,
) -> dict[str, str | int]:
    """Build the complete logical identity for one schema/day backup."""
    return {
        "backup_dir": str(dest_dir.resolve()),
        "retain": retain,
        "run_date": run_date,
        "source_db": str(SRC_DB.resolve()),
        "source_schema_revision": schema_revision,
    }


def _start_accounting(dest_dir: Path, retain: int) -> tuple[sqlite3.Connection, str]:
    """Claim the daily backup invocation before snapshot/encryption begins."""
    conn = connect_sqlite(
        SRC_DB,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    try:
        schema_revision = _source_schema_revision(conn)
        run_date = datetime.now().date().isoformat()
        run_id = start_run(
            conn,
            directive="backup_db",
            ticker_scope=[],
            invocation_inputs=_backup_invocation_inputs(
                dest_dir,
                retain,
                schema_revision,
                run_date,
            ),
            deduplicate_completed=True,
        )
    except Exception:
        conn.close()
        raise
    return conn, run_id


def _finish_accounting(
    accounting: tuple[sqlite3.Connection, str] | None,
    *,
    success: bool,
    error_msg: str | None = None,
    skipped_unchanged: bool = False,
) -> None:
    if accounting is None:
        return
    conn, run_id = accounting
    try:
        try:
            if skipped_unchanged:
                status = StageStatus.SKIPPED
            elif success:
                status = StageStatus.OK
            else:
                status = StageStatus.FAILED
            end_run(conn, run_id, status, error_msg)
        except Exception as exc:
            print(
                f"WARN: backup accounting completion unavailable: {redact(exc)}",
                file=sys.stderr,
            )
    finally:
        conn.close()


def _run_backup() -> int:
    if not SRC_DB.exists():
        print(f"ERROR: source DB not found: {SRC_DB}", file=sys.stderr)
        return 1
    dest_dir = Path(os.environ.get("ES_DB_BACKUP_DIR", DEFAULT_DEST))
    retain = int(os.environ.get("ES_DB_BACKUP_RETAIN", str(DEFAULT_RETAIN)))
    if retain < 1:
        raise ValueError("ES_DB_BACKUP_RETAIN must be at least 1")
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        accounting = _start_accounting(dest_dir, retain)
    except PipelineRunSuppressedError as exc:
        print(json.dumps(suppression_payload(exc)))
        return 0
    except Exception as exc:
        # The online snapshot is deliberately reader-safe and may overlap a
        # writer. Accounting is observability, not a backup prerequisite.
        print(
            f"WARN: backup accounting unavailable; snapshot proceeding: {redact(exc)}",
            file=sys.stderr,
        )
        accounting = None

    # Refuse up front rather than half-write onto a full volume. Staging holds the raw
    # snapshot AND its gzip before either is released, so the true cost is ~2x the DB
    # (plus the compressed artifact landing in dest_dir). A backup that dies mid-write
    # on a full disk is indistinguishable from one that never ran.
    db_bytes = SRC_DB.stat().st_size
    need_staging = 2 * db_bytes + 256 * 1024 * 1024
    free_staging = shutil.disk_usage(tempfile.gettempdir()).free
    if free_staging < need_staging:
        msg = (
            f"insufficient free space for backup staging: need "
            f"{need_staging / 1e9:.2f} GB, have {free_staging / 1e9:.2f} GB "
            f"on {tempfile.gettempdir()}"
        )
        _finish_accounting(accounting, success=False, error_msg=msg)
        print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    final_path = dest_dir / f"portfolio.db.{stamp}.gz.enc"

    try:
        with tempfile.TemporaryDirectory(prefix="portfolio_backup.") as staging:
            tmp_path = Path(staging) / "portfolio.db"
            tmp_gz = Path(staging) / "portfolio.db.gz"
            _consistent_snapshot(SRC_DB, tmp_path)
            if not _integrity_ok(tmp_path):
                raise RuntimeError("consistent snapshot failed SQLite integrity_check")
            snapshot_sha256 = _file_sha256(tmp_path)
            last_upload = _load_upload_receipt()
            if (
                last_upload is not None
                and last_upload.snapshot_sha256 == snapshot_sha256
                and (dest_dir / last_upload.snapshot_name).exists()
            ):
                # Content-skip (C3): the consistent snapshot is byte-identical
                # to the last successfully uploaded backup AND that upload is
                # still present, so gzip/encrypt/upload would only produce
                # fresh ciphertext for unchanged content. The run is recorded
                # as a healthy no-op: StageStatus.SKIPPED + the marker in the
                # accounting row, the stable JSON line on stdout, and the
                # receipt left untouched (no upload happened). A moved/wiped
                # backup dir re-uploads because the named snapshot is gone.
                print(
                    "OK backup skipped (skipped_unchanged) — snapshot matches "
                    f"last uploaded backup {last_upload.snapshot_name}"
                )
                print(json.dumps(_skipped_unchanged_payload(last_upload)))
                _backup_archive_sidecar(dest_dir)
                _finish_accounting(
                    accounting,
                    success=True,
                    skipped_unchanged=True,
                    error_msg="skipped_unchanged: snapshot sha256 matches last uploaded backup",
                )
                return 0
            with open(tmp_path, "rb") as raw, gzip.open(tmp_gz, "wb") as gz:
                shutil.copyfileobj(raw, gz)
            encrypt_file(tmp_gz, final_path, key=load_or_create_key())
        # Retention spans EVERY snapshot format, not just the one this build writes.
        # Globbing only "*.gz.enc" made the pruner blind to its own history: when the
        # running checkout wrote plaintext ".gz" (2026-07-27 onward) the 15 encrypted
        # snapshots became unprunable and sat at ~3.7 GB with nothing able to reclaim
        # them. A format switch must never orphan the previous format's files.
        snapshots = sorted(dest_dir.glob("portfolio.db.*.gz*"))
        for stale in snapshots[:-retain]:
            stale.unlink()

        size_mb = final_path.stat().st_size / 1e6
        kept = min(len(snapshots), retain)
        print(f"OK backup -> {final_path}  ({size_mb:.1f} MB encrypted)  retained={kept}")
    except Exception as exc:
        _finish_accounting(accounting, success=False, error_msg=str(exc))
        print(f"ERROR: backup failed: {exc}", file=sys.stderr)
        return 1
    _write_upload_receipt(final_path.name, snapshot_sha256)
    _backup_archive_sidecar(dest_dir)
    _finish_accounting(accounting, success=True)
    return 0


def _backup_archive_sidecar(dest_dir: Path) -> None:
    """Best-effort encrypted snapshot of the db_gc archive sidecar.

    Deliberately non-fatal: a missing archive (GC never ran) or a failed
    archive snapshot must NOT fail the portfolio.db backup that already
    succeeded — the archive is a recovery aid, not the primary asset. Skips
    re-encrypting an archive already captured at its current size+mtime
    (append-only, so those two fields moving is a reliable change signal).
    """
    archive = SRC_DB.parent / "archive" / ARCHIVE_PREFIX
    if not archive.exists():
        print("(no db_gc archive to back up — GC has not pruned yet)")
        return
    retain = int(os.environ.get("ES_ARCHIVE_BACKUP_RETAIN", str(DEFAULT_ARCHIVE_RETAIN)))
    existing = sorted(dest_dir.glob(f"{ARCHIVE_PREFIX}.*.gz.enc"))
    stat = archive.stat()
    tag = f"{stat.st_size}_{int(stat.st_mtime)}"
    if existing and existing[-1].name.endswith(f".{tag}.gz.enc"):
        print(f"(archive unchanged since last backup: {existing[-1].name})")
        return
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    final_path = dest_dir / f"{ARCHIVE_PREFIX}.{stamp}.{tag}.gz.enc"
    try:
        with tempfile.TemporaryDirectory(prefix="archive_backup.") as staging:
            tmp_path = Path(staging) / ARCHIVE_PREFIX
            tmp_gz = Path(staging) / f"{ARCHIVE_PREFIX}.gz"
            _consistent_snapshot(archive, tmp_path)
            if not _integrity_ok(tmp_path):
                raise RuntimeError("archive snapshot failed SQLite integrity_check")
            with open(tmp_path, "rb") as raw, gzip.open(tmp_gz, "wb") as gz:
                shutil.copyfileobj(raw, gz)
            encrypt_file(tmp_gz, final_path, key=load_or_create_key())
        for stale in sorted(dest_dir.glob(f"{ARCHIVE_PREFIX}.*.gz.enc"))[:-retain]:
            stale.unlink()
        size_mb = final_path.stat().st_size / 1e6
        print(f"OK archive backup -> {final_path.name}  ({size_mb:.1f} MB encrypted)")
    except Exception as exc:  # never fail the primary backup on the archive leg
        print(f"WARN: archive backup skipped: {exc}", file=sys.stderr)


def main() -> int:
    global SRC_DB
    if SRC_DB == DEFAULT_SRC_DB:
        load_project_env(PROJECT_ROOT)
        SRC_DB = portfolio_db_path(PROJECT_ROOT)
    # "db-backup", NOT "portfolio-db". This job READS the database through
    # SQLite's online-backup API, which is explicitly safe while other writers
    # work (and _integrity_ok below is what actually proves the snapshot good).
    # Claiming the database's exclusive write set bought no safety and cost
    # every scheduled run that overlapped any writer: JobLock is fail-fast with
    # zero wait, so on 2026-08-03 the 02:45 run gave up 12 ms in with
    # "write set busy: portfolio-db" while an hourly onboard job -- started at
    # 01:17 and still running at 03:20 -- held it. Four consecutive scheduled
    # backups were lost that way. The lock this job does need is against ANOTHER
    # backup: two concurrent runs would race on dest_dir and its retention prune.
    lock = (
        nullcontext()
        if inherited_lock_is_valid(PROJECT_ROOT, "db-backup")
        else JobLock(PROJECT_ROOT, "backup_db_direct", ["db-backup"])
    )
    try:
        with lock:
            return _run_backup()
    except Exception as exc:
        print(f"ERROR: backup lock failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
