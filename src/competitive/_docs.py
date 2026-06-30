"""Synthetic ``documents`` rows backing competitive-tracking KPI facts.

Every ``kpi_facts`` row needs a non-NULL ``source_doc_id`` FK into ``documents``.
The competitive feeds aren't a single fetched file (a manual-entry seed, a
per-quarter transcript count), so we mint a small synthetic ``documents`` row
keyed idempotently by a stable ``source_key`` string — mirroring
``ir_pipeline.ingest._ensure_spreadsheet_document`` so provenance still chains
every fact back to a row a human can audit.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import sqlite3

from models.documents import SourceType, tier_for_source_type


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True iff ``column`` exists on ``table`` (PRAGMA), tolerating a minimal
    test schema that predates ``documents.source_quality_tier``."""
    return any(
        str(r["name"] if hasattr(r, "keys") else r[1]) == column
        for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    )


def ensure_synthetic_document(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    source_type: SourceType,
    doc_type: str,
    source_key: str,
    period_end: _dt.datetime | None = None,
) -> int:
    """Insert (idempotently, sha256-keyed on ``source_key``) a synthetic
    ``documents`` row and return its id.

    ``source_key`` is any stable string uniquely naming this logical source
    (e.g. ``"competitive_category_share:RBRK:2025"`` or a transcript file path);
    its sha256 is the dedup key, so re-running a loader reuses the same row.
    """
    sha = hashlib.sha256(source_key.encode("utf-8")).hexdigest()
    existing = conn.execute("SELECT id FROM documents WHERE sha256 = ?", (sha,)).fetchone()
    if existing is not None:
        return int(existing["id"])
    common = (
        ticker.upper(),
        source_type.value,
        doc_type,
        period_end,
        f"synthetic://{source_key}",
        sha,
        _dt.datetime.now(_dt.UTC),
        "ok",
        len(source_key),
    )
    if _has_column(conn, "documents", "source_quality_tier"):
        tier = tier_for_source_type(source_type).value
        cur = conn.execute(
            "INSERT INTO documents (ticker, source_type, doc_type, period_end, "
            "file_path, sha256, fetched_at, fetch_status, raw_bytes_size, "
            "source_quality_tier) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*common, tier),
        )
    else:
        cur = conn.execute(
            "INSERT INTO documents (ticker, source_type, doc_type, period_end, "
            "file_path, sha256, fetched_at, fetch_status, raw_bytes_size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            common,
        )
    conn.commit()
    return int(cur.lastrowid) if cur.lastrowid is not None else 0
