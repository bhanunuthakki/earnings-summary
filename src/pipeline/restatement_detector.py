"""Restatement detection for financial_facts (and forward-compatible for kpi_facts).

Why this exists
---------------
A 10-Q lands a Q1 revenue value. Months later the company issues its 10-K,
which restates that same Q1 with an adjusted number (immaterial line-item
reclassification, segment recasting, or a true accounting restatement).
Today both rows coexist in `financial_facts` with no link between them, so
downstream readers can't reconstruct what was knowable at the time of the
original Q1 release vs. what is known now.

This module wires the chain explicitly: when a NEW write would land on an
existing logical key (ticker, period_end, fiscal_period_type, line_item)
AND the source document is a later filing than the incumbent's, the new
row is written with `supersedes_id` pointing at the incumbent. The
incumbent is left untouched — both rows survive, and the loaders (via the
tier+id ordering in `src/timeseries/loaders.py`) will pick up the newer
value by default while still allowing time-travel.

Scope
-----
Both `financial_facts` and `kpi_facts` are covered. The
`uq_financial_facts_provenance` index has always been keyed on
source_doc_id so two different documents = two rows; the parallel
`uq_kpi_facts_provenance` for kpi_facts was rebuilt by migration 0059
after PR #152 wired the financial_facts side (migration 0030 had
previously narrowed it to a logical-key-only unique to fix a renderer
double-counting bug — now handled at the loader layer via tier-aware
dedup, so the constraint can safely widen again).

`insert_with_restatement_detection` writes to financial_facts;
`insert_kpi_with_restatement_detection` is its kpi_facts twin. Both
share `find_incumbent` (via the `table` parameter) and `is_later_filing`
so the chain-link decision is uniform.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from decimal import Decimal

log = logging.getLogger(__name__)


def _fiscal_year_of_document(
    conn: sqlite3.Connection, source_doc_id: int
) -> tuple[int | None, datetime | None]:
    """Return (fiscal_year, fetched_at) for a document, both best-effort.

    fiscal_year is inferred from documents.period_end (the end date of the
    reporting period the document covers) — a FY 10-K has period_end =
    last day of the fiscal year, a Q1/Q2/Q3 10-Q has the quarter-end.
    Used by `is_later_filing` to decide which of two competing documents
    is "newer" without depending on the FK to a fiscal-year table.

    Returns (None, None) if the document row is missing or unreadable.
    """
    try:
        row = conn.execute(
            "SELECT period_end, fetched_at FROM documents WHERE id = ?",
            (source_doc_id,),
        ).fetchone()
    except sqlite3.Error:
        return (None, None)
    if row is None:
        return (None, None)
    period_end_raw = row["period_end"] if hasattr(row, "keys") else row[0]
    fetched_at_raw = row["fetched_at"] if hasattr(row, "keys") else row[1]
    period_end = _parse_dt(period_end_raw)
    fetched_at = _parse_dt(fetched_at_raw)
    fiscal_year: int | None = None
    if period_end is not None:
        fiscal_year = period_end.year
    return (fiscal_year, fetched_at)


def _parse_dt(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True iff PRAGMA reports `column` on `table`. Used to fall back to the
    pre-0054 INSERT shape on synthetic test fixtures that don't carry the
    extracted_by + supersedes_id columns."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    for r in rows:
        name = r["name"] if hasattr(r, "keys") else r[1]
        if str(name) == column:
            return True
    return False


def is_later_filing(
    conn: sqlite3.Connection,
    *,
    new_source_doc_id: int,
    incumbent_source_doc_id: int,
) -> bool:
    """True iff `new_source_doc_id` is a strictly later filing than
    `incumbent_source_doc_id`.

    "Later" is defined by the source document's period_end year — a 10-K
    issued for FY2023 (period_end 2023-12-31) is later than a 10-Q from
    Q1 2023 (period_end 2023-03-31). When period_end is identical (two
    filings restating the same fiscal year, e.g. amended 10-K), we fall
    back to fetched_at.
    """
    new_year, new_fetched = _fiscal_year_of_document(conn, new_source_doc_id)
    inc_year, inc_fetched = _fiscal_year_of_document(conn, incumbent_source_doc_id)
    if new_year is None or inc_year is None:
        return False
    if new_year > inc_year:
        return True
    if new_year < inc_year:
        return False
    # Same fiscal year — compare fetched_at (best-effort).
    if new_fetched is None or inc_fetched is None:
        return False
    return new_fetched > inc_fetched


def find_incumbent(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: datetime,
    fiscal_period_type: str,
    line_item: str | int,
    table: str = "financial_facts",
) -> int | None:
    """Return id of the highest-id existing row for the logical key, or None.

    `line_item` is the per-table key-column value: a `line_item` string for
    financial_facts, a `kpi_definition_id` int for kpi_facts.

    The "highest id" choice means: when restatements have already been
    chained (A <- B <- C), `find_incumbent` returns C — the head of the
    chain — so the new row D supersedes C, preserving the linked list.
    """
    if table == "financial_facts":
        key_col = "line_item"
    elif table == "kpi_facts":
        key_col = "kpi_definition_id"
    else:
        raise ValueError(f"Unsupported table for restatement detection: {table!r}")
    try:
        row = conn.execute(
            f"""
            SELECT id FROM {table}
            WHERE ticker = ?
              AND period_end = ?
              AND fiscal_period_type = ?
              AND {key_col} = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (ticker.upper(), period_end, fiscal_period_type, line_item),
        ).fetchone()
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "restatement_find_incumbent_failed",
                "ticker": ticker,
                "line_item": line_item,
                "error": str(exc),
            }
        )
        return None
    if row is None:
        return None
    return int(row["id"] if hasattr(row, "keys") else row[0])


def insert_with_restatement_detection(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: datetime,
    fiscal_period_type: str,
    line_item: str,
    value: Decimal,
    currency: str | None,
    unit: str,
    source_doc_id: int,
    confidence: float = 1.0,
    extracted_by: str | None = None,
    locator: str | None = None,
) -> tuple[int | None, int | None]:
    """Insert one financial_facts row, setting `supersedes_id` when this is
    a restatement of an existing row from an earlier filing.

    Returns `(new_row_id, superseded_row_id)`:
      - `new_row_id` is the id of the inserted row, or None if the insert
        was a true no-op (UNIQUE conflict on the same source_doc_id).
      - `superseded_row_id` is the id of the predecessor in the chain,
        or None if this is the first row for the logical key OR the new
        document is NOT a later filing than the incumbent (in which case
        the row is inserted standalone, no chain link).

    `locator` is the pre-serialized sub-document locator JSON (alembic 0075;
    serialize via models.facts.FactLocator.to_json). Like extracted_by /
    supersedes_id, it is silently dropped when the schema predates its
    column — acceptable for synthetic test fixtures.

    Callers should use this in place of a raw INSERT OR IGNORE when the
    extractor wants the restatement chain populated. Existing call sites
    (compute/_common.py::insert_financial_facts) keep their plain INSERT
    OR IGNORE — adopting this helper is opt-in to avoid behavior changes
    in the load path until extractor-by-extractor wiring lands.
    """
    incumbent_id = find_incumbent(
        conn,
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=fiscal_period_type,
        line_item=line_item,
        table="financial_facts",
    )
    supersedes_id: int | None = None
    if incumbent_id is not None:
        # Look up the incumbent's source_doc_id to decide if this is a
        # restatement vs. a redundant re-write of the same document.
        incumbent_row = conn.execute(
            "SELECT source_doc_id FROM financial_facts WHERE id = ?",
            (incumbent_id,),
        ).fetchone()
        if incumbent_row is not None:
            incumbent_doc_id = int(
                incumbent_row["source_doc_id"] if hasattr(incumbent_row, "keys") else incumbent_row[0]
            )
            if incumbent_doc_id != source_doc_id and is_later_filing(
                conn,
                new_source_doc_id=source_doc_id,
                incumbent_source_doc_id=incumbent_doc_id,
            ):
                supersedes_id = incumbent_id

    has_audit_cols = _table_has_column(
        conn, "financial_facts", "supersedes_id"
    ) and _table_has_column(conn, "financial_facts", "extracted_by")
    write_locator = locator is not None and _table_has_column(conn, "financial_facts", "locator")
    try:
        if has_audit_cols and write_locator:
            cur = conn.execute(
                "INSERT OR IGNORE INTO financial_facts "
                "(ticker, period_end, fiscal_period_type, line_item, value, "
                " currency, unit, source_doc_id, confidence, extracted_by, "
                " supersedes_id, locator) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ticker.upper(),
                    period_end,
                    fiscal_period_type,
                    line_item,
                    str(value),
                    currency,
                    unit,
                    source_doc_id,
                    confidence,
                    extracted_by,
                    supersedes_id,
                    locator,
                ),
            )
        elif has_audit_cols:
            cur = conn.execute(
                "INSERT OR IGNORE INTO financial_facts "
                "(ticker, period_end, fiscal_period_type, line_item, value, "
                " currency, unit, source_doc_id, confidence, extracted_by, supersedes_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ticker.upper(),
                    period_end,
                    fiscal_period_type,
                    line_item,
                    str(value),
                    currency,
                    unit,
                    source_doc_id,
                    confidence,
                    extracted_by,
                    supersedes_id,
                ),
            )
        else:
            # Pre-0054 schema (synthetic test fixtures): drop the audit columns.
            # supersedes_id is None anyway in this branch since the column is
            # missing; extracted_by is silently lost — acceptable for tests.
            cur = conn.execute(
                "INSERT OR IGNORE INTO financial_facts "
                "(ticker, period_end, fiscal_period_type, line_item, value, "
                " currency, unit, source_doc_id, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ticker.upper(),
                    period_end,
                    fiscal_period_type,
                    line_item,
                    str(value),
                    currency,
                    unit,
                    source_doc_id,
                    confidence,
                ),
            )
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "restatement_insert_failed",
                "ticker": ticker,
                "line_item": line_item,
                "error": str(exc),
            }
        )
        return (None, supersedes_id)

    if cur.rowcount == 0:
        # UNIQUE conflict on (ticker, period_end, fiscal_period_type,
        # line_item, source_doc_id) — same document re-ingested, no row
        # written. supersedes_id is irrelevant in that case.
        return (None, None)
    return (
        int(cur.lastrowid) if cur.lastrowid is not None else None,
        supersedes_id,
    )


def insert_kpi_with_restatement_detection(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: datetime,
    fiscal_period_type: str,
    kpi_definition_id: int,
    value: Decimal,
    unit: str,
    source_doc_id: int,
    confidence: float = 1.0,
    extracted_by: str | None = None,
    locator: str | None = None,
    source_excerpt: str | None = None,
) -> tuple[int | None, int | None]:
    """kpi_facts twin of `insert_with_restatement_detection`.

    Returns `(new_row_id, superseded_row_id)`:
      - `new_row_id` is the id of the inserted row, or None when the insert
        was a true no-op (UNIQUE conflict on the same source_doc_id under
        post-0059 `uq_kpi_facts_provenance`, or on the logical key under
        legacy `uq_kpi_facts_logical`).
      - `superseded_row_id` is the id of the predecessor in the chain when
        the new document is strictly later than the incumbent's; None
        otherwise (first row for the logical key, or older-than-incumbent
        replay).

    `locator` is the pre-serialized sub-document locator JSON (alembic 0075;
    serialize via models.facts.FactLocator.to_json) and `source_excerpt` the
    verbatim quote supporting the value (column added in 0033); both are
    dropped like the audit columns when the schema predates them.

    Schema tolerance: when `kpi_facts` lacks the audit columns
    (`supersedes_id`, `extracted_by`, `confidence` — all added in 0054),
    falls back to the pre-0054 INSERT shape. supersedes_id is silently
    dropped in that branch since the column is missing; extracted_by and
    confidence are also dropped. This keeps synthetic test fixtures
    (e.g. `tests/test_compute_say_do.py`) working without forcing them
    to carry the full prod schema.
    """
    incumbent_id = find_incumbent(
        conn,
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=fiscal_period_type,
        line_item=kpi_definition_id,
        table="kpi_facts",
    )
    supersedes_id: int | None = None
    if incumbent_id is not None:
        incumbent_row = conn.execute(
            "SELECT source_doc_id FROM kpi_facts WHERE id = ?",
            (incumbent_id,),
        ).fetchone()
        if incumbent_row is not None:
            incumbent_doc_id = int(
                incumbent_row["source_doc_id"]
                if hasattr(incumbent_row, "keys")
                else incumbent_row[0]
            )
            if incumbent_doc_id != source_doc_id and is_later_filing(
                conn,
                new_source_doc_id=source_doc_id,
                incumbent_source_doc_id=incumbent_doc_id,
            ):
                supersedes_id = incumbent_id

    has_audit_cols = (
        _table_has_column(conn, "kpi_facts", "supersedes_id")
        and _table_has_column(conn, "kpi_facts", "extracted_by")
        and _table_has_column(conn, "kpi_facts", "confidence")
    )
    # Optional tail columns: written only when provided AND the schema has
    # them (locator: 0075; source_excerpt: 0033) — same drop-on-legacy
    # tolerance as the audit columns.
    tail_cols: list[str] = []
    tail_vals: list[str] = []
    if locator is not None and _table_has_column(conn, "kpi_facts", "locator"):
        tail_cols.append("locator")
        tail_vals.append(locator)
    if source_excerpt is not None and _table_has_column(conn, "kpi_facts", "source_excerpt"):
        tail_cols.append("source_excerpt")
        tail_vals.append(source_excerpt)
    try:
        if has_audit_cols:
            tail_names = "".join(f", {c}" for c in tail_cols)
            tail_marks = ", ?" * len(tail_cols)
            cur = conn.execute(
                "INSERT OR IGNORE INTO kpi_facts "
                "(ticker, period_end, fiscal_period_type, kpi_definition_id, "
                " value, unit, source_doc_id, confidence, extracted_by, supersedes_id"
                f"{tail_names}) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?{tail_marks})",
                (
                    ticker.upper(),
                    period_end,
                    fiscal_period_type,
                    kpi_definition_id,
                    str(value),
                    unit,
                    source_doc_id,
                    confidence,
                    extracted_by,
                    supersedes_id,
                    *tail_vals,
                ),
            )
        else:
            cur = conn.execute(
                "INSERT OR IGNORE INTO kpi_facts "
                "(ticker, period_end, fiscal_period_type, kpi_definition_id, "
                " value, unit, source_doc_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ticker.upper(),
                    period_end,
                    fiscal_period_type,
                    kpi_definition_id,
                    str(value),
                    unit,
                    source_doc_id,
                ),
            )
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "restatement_kpi_insert_failed",
                "ticker": ticker,
                "kpi_definition_id": kpi_definition_id,
                "error": str(exc),
            }
        )
        return (None, supersedes_id)

    if cur.rowcount == 0:
        # Conflict on UNIQUE (same source_doc_id under provenance; same
        # logical key under legacy logical-only). No row written.
        return (None, None)
    return (
        int(cur.lastrowid) if cur.lastrowid is not None else None,
        supersedes_id,
    )


def latest_in_chain(
    conn: sqlite3.Connection, row_id: int, *, table: str = "financial_facts"
) -> int:
    """Walk forward through `supersedes_id` links from `row_id`. Return the
    id of the most recent restated row in the chain.

    If `row_id` has no successor (nobody supersedes it), returns row_id.
    Robust to cycles (returns the most recent visited id and stops).
    """
    current = row_id
    seen: set[int] = set()
    while True:
        if current in seen:
            break
        seen.add(current)
        try:
            row = conn.execute(
                f"SELECT id FROM {table} WHERE supersedes_id = ?",
                (current,),
            ).fetchone()
        except sqlite3.Error:
            break
        if row is None:
            return current
        current = int(row["id"] if hasattr(row, "keys") else row[0])
    return current
