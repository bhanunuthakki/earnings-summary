"""SQLite registration boundary for transcript acquisition receipts.

The connection runtime owns SQLite connection policy, while the acquisition
pipeline owns the receipt validator. This small lower-level module bridges
those responsibilities without making the connection runtime import the
pipeline (which would create a dependency cycle).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

ReceiptValidator = Callable[[Path, tuple[object, ...]], int]
_receipt_validator: ReceiptValidator | None = None


def project_root_for_database(database_path: str | os.PathLike[str]) -> Path:
    """Derive the trusted repository root from a canonical database location."""

    path = Path(database_path).resolve()
    return path.parent.parent if path.parent.name.lower() == "data" else path.parent


def register_transcript_receipt_validator(validator: ReceiptValidator) -> None:
    """Install the single pipeline-owned validator used by SQLite connections."""

    global _receipt_validator
    if _receipt_validator is not None and _receipt_validator is not validator:
        raise RuntimeError("transcript receipt validator authority is already registered")
    _receipt_validator = validator


def register_transcript_receipt_sqlite_functions(
    conn: sqlite3.Connection,
    *,
    database_path: str | os.PathLike[str],
) -> None:
    """Register deterministic validation used by the receipt INSERT trigger.

    A connection opened before the acquisition boundary is imported receives a
    fail-closed function permanently. Normal acquisition entrypoints import and
    register their validator before opening their connection.
    """

    project_root = project_root_for_database(database_path)
    validator = _receipt_validator

    def validate(*values: object) -> int:
        if validator is None or len(values) != 18:
            return 0
        return validator(project_root, values)

    conn.create_function(
        "transcript_receipt_valid",
        18,
        validate,
        deterministic=True,
    )


__all__ = [
    "ReceiptValidator",
    "project_root_for_database",
    "register_transcript_receipt_sqlite_functions",
    "register_transcript_receipt_validator",
]
