"""Restore data/portfolio.db from a snapshot produced by cron/backup_db.py.

The tested counterpart to backup_db.py (sre-3, 2026-06-18 hardening refresh): a
backup you have never restored is not a backup. Authenticates/decrypts a
`.gz.enc` snapshot, decompresses it, runs `PRAGMA integrity_check`, and only
then moves it into place;
refuses to overwrite an existing DB without `--force`.

Recovery objectives (sre-3):
  - RPO (max data loss): <= 24h — backup_db runs daily at 02:45 and retains 14
    snapshots. Re-run backup_db.py on demand before risky work to tighten it.
  - RTO (time to restore): minutes — `python cron/restore_db.py --latest` is a
    single gunzip + integrity check + move.

Usage:
    python cron/restore_db.py --list
    python cron/restore_db.py --latest --to /tmp/check.db   # verify a snapshot
    python cron/restore_db.py --latest --to data/recovered.db
    python cron/restore_db.py <snapshot.gz.enc> --to <path>

The encryption key is external to both the repo and cloud backup directory.
Before host loss, escrow a copy of ``backup_encryption.key`` in an independent
password manager or offline encrypted medium. Restore it to the configured
secrets directory (or set ``ES_DB_BACKUP_KEY_FILE``) before recovery.
"""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from runtime.backup_crypto import decrypt_file, load_key  # noqa: E402
from runtime.job_runtime import JobLock, inherited_lock_is_valid, portfolio_db_path  # noqa: E402
from runtime.secrets import load_project_env  # noqa: E402

DEFAULT_DB = (PROJECT_ROOT / "data" / "portfolio.db").resolve()

MIRROR_DRIVE_ROOT = Path(r"C:\Users\Bhanu\My Drive")


def _google_drive_root() -> Path:
    """Locate the Google Drive root in either sync mode.

    In Stream mode Drive mounts a virtual drive (usually G:), and the old
    mirror folder at C:\\Users\\Bhanu\\My Drive lingers on disk as a stale,
    UNSYNCED leftover until manually deleted. A mounted "<letter>:\\My Drive"
    can only be the Drive mount, so any non-C: hit wins over the mirror path —
    checking C: first would keep writing backups into the dead folder while
    reporting OK. backup_db.py duplicates this deliberately (its restore
    counterpart must never drift to a different answer — keep them identical).
    """
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = Path(f"{letter}:/My Drive")
        if candidate.is_dir():
            return candidate
    return MIRROR_DRIVE_ROOT


DEFAULT_BACKUP_DIR = _google_drive_root() / "earnings-summary-db-backups"


def configured_backup_dir() -> Path:
    return Path(os.environ.get("ES_DB_BACKUP_DIR", DEFAULT_BACKUP_DIR))


def list_snapshots(backup_dir: Path) -> list[Path]:
    """Snapshots in ``backup_dir``, oldest -> newest (the filename encodes the stamp)."""
    return sorted(backup_dir.glob("portfolio.db.*.gz.enc"))


def list_legacy_snapshots(backup_dir: Path) -> list[Path]:
    """Unauthenticated snapshots available only to the explicit migration path."""
    return sorted(backup_dir.glob("portfolio.db.*.gz"))


def integrity_ok(db_path: Path) -> bool:
    """True iff SQLite's integrity_check reports a healthy database. A file that
    is not a SQLite database at all (e.g. a truncated/garbage snapshot) returns
    False rather than raising."""
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return False
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()
    return bool(row) and row[0] == "ok"


def _assert_live_restore_safe(target: Path, *, live_db: Path) -> None:
    if target.resolve() != live_db.resolve():
        return
    raise RuntimeError(
        "direct live-DB replacement is disabled; restore to a sibling path, "
        "stop all services, verify it, then perform an offline cutover"
    )


def restore_snapshot(
    snapshot: Path,
    target: Path,
    *,
    force: bool,
    allow_legacy: bool = False,
) -> None:
    """Decompress ``snapshot`` -> verify integrity -> move into ``target``.

    Raises on a missing snapshot, an existing target without ``force``, or a
    failed integrity check (the corrupt restore is discarded, target untouched).
    """
    if not snapshot.exists():
        raise FileNotFoundError(f"snapshot not found: {snapshot}")
    encrypted = snapshot.name.endswith(".gz.enc")
    if not encrypted and not allow_legacy:
        raise RuntimeError("plaintext snapshots require the explicit legacy migration path")
    if target.exists() and not force:
        raise FileExistsError(f"refusing to overwrite {target} without --force")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(suffix=".db", prefix=".restore.", dir=target.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    encrypted_gz: Path | None = None
    try:
        gzip_source = snapshot
        if encrypted:
            gz_fd, gz_name = tempfile.mkstemp(suffix=".gz", prefix="restore.")
            os.close(gz_fd)
            encrypted_gz = Path(gz_name)
            decrypt_file(snapshot, encrypted_gz, key=load_key())
            gzip_source = encrypted_gz
        with gzip.open(gzip_source, "rb") as gz, open(tmp_path, "wb") as raw:
            shutil.copyfileobj(gz, raw)
        if not integrity_ok(tmp_path):
            raise RuntimeError(f"integrity check FAILED for {snapshot} — not restoring")
        with tmp_path.open("r+b") as verified:
            os.fsync(verified.fileno())
        if force:
            os.replace(tmp_path, target)
        elif os.name == "nt":
            os.rename(tmp_path, target)
        else:
            os.link(tmp_path, target)
            tmp_path.unlink()
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
        if encrypted_gz is not None:
            encrypted_gz.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    load_project_env(PROJECT_ROOT)
    live_db = portfolio_db_path(PROJECT_ROOT)
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("snapshot", nargs="?", help="snapshot .gz to restore")
    p.add_argument("--latest", action="store_true", help="restore the newest snapshot")
    p.add_argument("--list", action="store_true", help="list available snapshots and exit")
    p.add_argument("--to", type=Path, default=live_db, help="restore target (default: live DB)")
    p.add_argument("--backup-dir", type=Path, default=configured_backup_dir())
    p.add_argument("--force", action="store_true", help="overwrite an existing target")
    p.add_argument(
        "--allow-legacy",
        action="store_true",
        help="allow an explicitly named plaintext .gz only for controlled migration",
    )
    args = p.parse_args(argv)

    backup_dir: Path = args.backup_dir
    target: Path = args.to
    do_list: bool = args.list
    do_latest: bool = args.latest
    force: bool = args.force
    allow_legacy: bool = args.allow_legacy
    snapshot_arg: str | None = args.snapshot

    snaps = list_snapshots(backup_dir)
    if do_list:
        if not snaps:
            print(f"(no snapshots in {backup_dir})")
        for s in snaps:
            print(f"{s.name}  ({s.stat().st_size / 1e6:.1f} MB)")
        return 0

    if do_latest:
        if not snaps:
            print(f"ERROR: no snapshots in {backup_dir}", file=sys.stderr)
            return 1
        snapshot = snaps[-1]
    elif snapshot_arg:
        snapshot = Path(snapshot_arg)
    else:
        print("ERROR: pass a snapshot path, --latest, or --list", file=sys.stderr)
        return 1

    try:
        _assert_live_restore_safe(target, live_db=live_db)
        lock = (
            nullcontext()
            if inherited_lock_is_valid(PROJECT_ROOT, "portfolio-db")
            or target.resolve() != live_db.resolve()
            else JobLock(PROJECT_ROOT, "restore_db", ["portfolio-db"])
        )
        with lock:
            restore_snapshot(snapshot, target, force=force, allow_legacy=allow_legacy)
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: restore failed: {exc}", file=sys.stderr)
        return 1
    print(f"OK restored {snapshot.name} -> {target}  (integrity check passed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
