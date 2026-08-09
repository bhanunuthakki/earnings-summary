"""Read-only resolver for the encrypted database-backup destination.

The backup and restore CLIs retain their existing destination contract; this
module gives health probes the same environment-first lookup without changing
where either recovery command writes or reads.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

BACKUP_DIR_ENV = "ES_DB_BACKUP_DIR"


@lru_cache(maxsize=1)
def default_drive_root() -> Path:
    """Return the active Google Drive root without rescanning per request."""
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = Path(f"{letter}:/My Drive")
        if candidate.is_dir():
            return candidate
    return Path.home() / "My Drive"


def backup_dir() -> Path:
    """Resolve the configured encrypted-snapshot directory."""
    configured = os.environ.get(BACKUP_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    return default_drive_root() / "earnings-summary-db-backups"


__all__ = ["BACKUP_DIR_ENV", "backup_dir", "default_drive_root"]
