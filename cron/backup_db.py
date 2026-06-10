"""Consistent, sync-safe backup of data/portfolio.db.

Once the scratch tree is removed from Google Drive's folder backup, the live DB is no
longer backed up by Drive. This produces a CONSISTENT snapshot (SQLite online
backup API — safe even while the pipeline writes) and gzips it into a synced
backup folder. The gzip is a static file written once, so Drive can sync it with
zero corruption risk (unlike the live `.db` + `-wal`/`-shm`).

Run directly (cron/run_backup_db.bat schedules it daily):
    python cron/backup_db.py

Backup dir is `ES_DB_BACKUP_DIR` (default: a Google Drive folder so the snapshot
is cloud-backed). Keeps the most recent ES_DB_BACKUP_RETAIN snapshots.
"""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DB = PROJECT_ROOT / "data" / "portfolio.db"
DEFAULT_DEST = r"C:\Users\Bhanu\My Drive\earnings-summary-db-backups"
RETAIN = int(os.environ.get("ES_DB_BACKUP_RETAIN", "14"))


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


def main() -> int:
    if not SRC_DB.exists():
        print(f"ERROR: source DB not found: {SRC_DB}", file=sys.stderr)
        return 1
    dest_dir = Path(os.environ.get("ES_DB_BACKUP_DIR", DEFAULT_DEST))
    dest_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # snapshot to LOCAL temp (fast); only the small .gz lands in the synced dir
    tmp_path = Path(tempfile.gettempdir()) / f"portfolio.db.{stamp}.snapshot.tmp"
    final_path = dest_dir / f"portfolio.db.{stamp}.gz"

    _consistent_snapshot(SRC_DB, tmp_path)
    with open(tmp_path, "rb") as raw, gzip.open(final_path, "wb") as gz:
        shutil.copyfileobj(raw, gz)
    tmp_path.unlink()

    snapshots = sorted(dest_dir.glob("portfolio.db.*.gz"))
    for stale in snapshots[:-RETAIN]:
        stale.unlink()

    size_mb = final_path.stat().st_size / 1e6
    kept = min(len(snapshots), RETAIN)
    print(f"OK backup -> {final_path}  ({size_mb:.1f} MB gz)  retained={kept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
