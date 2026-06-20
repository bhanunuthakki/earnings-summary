"""Consistent SQLite snapshot via the online backup API.

A raw file copy of a live SQLite database can be torn when the daily refresh
pipeline is mid-write (the `-wal` sidecar holds uncommitted pages). The online
backup API produces a transactionally consistent copy that is safe to take even
under concurrent writers — it reflects the last committed state and is a fully
valid, standalone `.db` file.

Used by `cron/backup_es_monthly.ps1` to capture `data/portfolio.db` before
folding it into the monthly project archive. Standalone CLI so the PowerShell
orchestrator can invoke it through the repo venv without importing the heavier
`backup_db.py` (which also gzips + writes to a Drive folder).

    python cron/snapshot_db.py <src.db> <dst.db>
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def snapshot(src: Path, dst: Path) -> None:
    """Write a consistent copy of ``src`` to ``dst`` via SQLite's backup API.

    Safe under concurrent writers. Raises ``FileNotFoundError`` if ``src`` does
    not exist (so the caller fails loud rather than shipping an empty backup).
    """
    if not src.exists():
        raise FileNotFoundError(f"source DB not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(src))
    dest = sqlite3.connect(str(dst))
    try:
        with dest:
            source.backup(dest)
    finally:
        dest.close()
        source.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: snapshot_db.py <src.db> <dst.db>", file=sys.stderr)
        return 2
    snapshot(Path(argv[0]), Path(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
