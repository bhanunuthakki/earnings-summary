"""Shared timezone-aware parsing and protected receipt-path validation."""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path

from provenance.immutable_artifact import path_aliases_any, require_no_reparse_points


def parse_timezone_aware_datetime(value: str) -> datetime:
    """Parse timezone-aware ISO-8601 datetimes for argparse."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed


def validate_protected_receipt_path(
    receipt: Path,
    *,
    database: Path,
    protected_receipts: tuple[Path, ...],
    conflict_message: str,
) -> Path:
    """Reject output aliases to the database, sidecars, or input receipts."""

    for candidate in (receipt, database, *protected_receipts):
        require_no_reparse_points(candidate)
    destination = Path(os.path.abspath(receipt))
    database_path = Path(os.path.abspath(database))
    protected = {
        database_path,
        *(
            Path(os.path.abspath(f"{database_path}{suffix}"))
            for suffix in ("-wal", "-shm", "-journal")
        ),
        *(Path(os.path.abspath(item)) for item in protected_receipts),
    }
    if path_aliases_any(destination, protected):
        raise ValueError(conflict_message)
    return destination
