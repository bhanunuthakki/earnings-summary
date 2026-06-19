"""Restore data/portfolio.db from a snapshot produced by cron/backup_db.py.

The tested counterpart to backup_db.py (sre-3, 2026-06-18 hardening refresh): a
backup you have never restored is not a backup. Decompresses a `.gz` snapshot,
runs `PRAGMA integrity_check` on the result, and only then moves it into place;
refuses to overwrite an existing DB without `--force`.

Recovery objectives (sre-3):
  - RPO (max data loss): <= 24h — backup_db runs daily at 02:45 and retains 14
    snapshots. Re-run backup_db.py on demand before risky work to tighten it.
  - RTO (time to restore): minutes — `python cron/restore_db.py --latest` is a
    single gunzip + integrity check + move.

Usage:
    python cron/restore_db.py --list
    python cron/restore_db.py --latest --to /tmp/check.db   # verify a snapshot
    python cron/restore_db.py --latest --force              # restore over live DB
    python cron/restore_db.py <snapshot.gz> --to <path>
"""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "portfolio.db"
DEFAULT_BACKUP_DIR = Path(
    os.environ.get("ES_DB_BACKUP_DIR", r"C:\Users\Bhanu\My Drive\earnings-summary-db-backups")
)


def list_snapshots(backup_dir: Path) -> list[Path]:
    """Snapshots in ``backup_dir``, oldest -> newest (the filename encodes the stamp)."""
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


def restore_snapshot(snapshot: Path, target: Path, *, force: bool) -> None:
    """Decompress ``snapshot`` -> verify integrity -> move into ``target``.

    Raises on a missing snapshot, an existing target without ``force``, or a
    failed integrity check (the corrupt restore is discarded, target untouched).
    """
    if not snapshot.exists():
        raise FileNotFoundError(f"snapshot not found: {snapshot}")
    if target.exists() and not force:
        raise FileExistsError(f"refusing to overwrite {target} without --force")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(suffix=".db", prefix="restore.")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with gzip.open(snapshot, "rb") as gz, open(tmp_path, "wb") as raw:
            shutil.copyfileobj(gz, raw)
        if not integrity_ok(tmp_path):
            raise RuntimeError(f"integrity check FAILED for {snapshot} — not restoring")
        shutil.move(str(tmp_path), str(target))
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("snapshot", nargs="?", help="snapshot .gz to restore")
    p.add_argument("--latest", action="store_true", help="restore the newest snapshot")
    p.add_argument("--list", action="store_true", help="list available snapshots and exit")
    p.add_argument("--to", type=Path, default=DEFAULT_DB, help="restore target (default: live DB)")
    p.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    p.add_argument("--force", action="store_true", help="overwrite an existing target")
    args = p.parse_args(argv)

    backup_dir: Path = args.backup_dir
    target: Path = args.to
    do_list: bool = args.list
    do_latest: bool = args.latest
    force: bool = args.force
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
        restore_snapshot(snapshot, target, force=force)
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: restore failed: {exc}", file=sys.stderr)
        return 1
    print(f"OK restored {snapshot.name} -> {target}  (integrity check passed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
