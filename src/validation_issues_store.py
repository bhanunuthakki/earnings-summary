"""Write path for resolving ``validation_issues`` rows (S10).

The quarantine ledger is *written* during ingest (``pipeline.kpi_persistence.
record_validation_issue``) and *read* by the report Sources tab and the System
Validation panel. This module owns the one remaining verb the
"provenance is actionable" work needs: an analyst marking an open issue
resolved from the console, with an audit trail (``resolved_at`` /
``resolved_by`` / ``resolution_note`` — the last two added in alembic 0094).

Parallels ``user_state.notes.resolve_note``: a focused, idempotent UPDATE that
returns the row's new state (here, the resolution stamp) or ``None`` when the
id is unknown or already resolved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from user_state._db import open_conn

if TYPE_CHECKING:
    from pathlib import Path


def resolve_validation_issue(
    issue_id: int,
    *,
    resolved_by: str | None = None,
    resolution_note: str | None = None,
    db_path: Path | str | None = None,
) -> str | None:
    """Mark one OPEN ``validation_issues`` row resolved.

    Sets ``resolved_at`` (naive UTC, the repo's storage convention),
    ``resolved_by`` and ``resolution_note`` in a single UPDATE scoped to
    ``resolved_at IS NULL`` so a re-resolve is a no-op. Returns the
    ``resolved_at`` value (ISO-ish ``"YYYY-MM-DD HH:MM:SS"``) when a row was
    updated, or ``None`` when the id is unknown or the issue was already
    resolved.
    """
    # Naive-UTC TEXT stamp (the repo storage convention) — stored as a string,
    # not a datetime object, to match the column's TEXT affinity and skip the
    # py3.12 default-adapter deprecation.
    resolved_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0).isoformat(sep=" ")
    note = (resolution_note or "").strip() or None
    by = (resolved_by or "").strip() or None
    conn = open_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE validation_issues "
            "SET resolved_at = ?, resolved_by = ?, resolution_note = ? "
            "WHERE id = ? AND resolved_at IS NULL",
            (resolved_at, by, note, issue_id),
        )
        if cur.rowcount == 0:
            return None
        conn.commit()
    finally:
        conn.close()
    return resolved_at
