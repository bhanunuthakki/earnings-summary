"""Fingerprint-backed validation-issue lifecycle persistence."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

from schema_compat import require_current_for_write


def issue_fingerprint(
    *, source_doc_id: int | None, ticker: str | None, rule: str, raw_value: str | None, expected: str | None
) -> str:
    """Stable identity for the underlying data defect, independent of an attempt."""
    payload = "\x1f".join(
        (str(source_doc_id or ""), (ticker or "").upper(), rule, raw_value or "", expected or "")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_issue(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    source_doc_id: int | None,
    ticker: str | None,
    severity: str,
    rule: str,
    raw_value: str | None,
    expected: str | None,
) -> int:
    """Create or re-open one issue and advance its observed lifecycle.

    On pre-migration schemas the function retains the original insert-only
    behavior, which keeps historical/fixture DBs readable during rollout.
    """
    require_current_for_write(conn)
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(validation_issues)")}
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0).isoformat(sep=" ")
    normalized_ticker = ticker.upper() if ticker else None
    if "fingerprint" not in columns:
        cur = conn.execute(
            "INSERT INTO validation_issues "
            "(run_id, source_doc_id, ticker, severity, rule, raw_value, expected, raised_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (run_id, source_doc_id, normalized_ticker, severity, rule, raw_value, expected, now),
        )
        return int(cur.lastrowid or 0)

    fingerprint = issue_fingerprint(
        source_doc_id=source_doc_id, ticker=normalized_ticker, rule=rule, raw_value=raw_value, expected=expected
    )
    existing = conn.execute(
        "SELECT id FROM validation_issues WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE validation_issues SET run_id=?, severity=?, last_seen_at=?, "
            "occurrence_count=COALESCE(occurrence_count, 1)+1, resolved_at=NULL "
            "WHERE id=?",
            (run_id, severity, now, int(existing[0])),
        )
        return int(existing[0])
    cur = conn.execute(
        "INSERT INTO validation_issues "
        "(run_id, source_doc_id, ticker, severity, rule, raw_value, expected, raised_at, resolved_at, "
        "fingerprint, first_seen_at, last_seen_at, occurrence_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 1)",
        (run_id, source_doc_id, normalized_ticker, severity, rule, raw_value, expected, now,
         fingerprint, now, now),
    )
    return int(cur.lastrowid or 0)
