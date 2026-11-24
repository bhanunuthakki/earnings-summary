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
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from provenance.financial_fact_resolution import resolve_fact_row

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
        row = conn.execute(  # nosec B608 -- table/key are selected from closed literals above
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
        return raw.astimezone(UTC).replace(tzinfo=None) if raw.tzinfo is not None else raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo is not None else parsed
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


@dataclass(frozen=True)
class FinancialFactSchemaProfile:
    """The financial_facts column-presence probes, hoisted to once per caller.

    `insert_with_restatement_detection` re-derives its INSERT shape from these
    booleans; computing them once per ingest batch (the schema cannot change
    mid-transaction) removes the per-fact ``PRAGMA table_info`` probes from
    the admission hot loop.
    """

    has_supersedes_id: bool
    has_extracted_by: bool
    has_locator: bool

    @property
    def has_audit_cols(self) -> bool:
        return self.has_supersedes_id and self.has_extracted_by


def probe_financial_fact_schema(conn: sqlite3.Connection) -> FinancialFactSchemaProfile:
    """Probe financial_facts column presence once for a whole batch of inserts."""

    return FinancialFactSchemaProfile(
        has_supersedes_id=_table_has_column(conn, "financial_facts", "supersedes_id"),
        has_extracted_by=_table_has_column(conn, "financial_facts", "extracted_by"),
        has_locator=_table_has_column(conn, "financial_facts", "locator"),
    )


# Schema-tolerant public read seam for legacy writers during staged migrations.
table_has_column = _table_has_column


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
        return new_fetched is not None and inc_fetched is not None and new_fetched > inc_fetched
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
    period_day = period_end.strftime("%Y-%m-%d")
    try:
        if table == "financial_facts":
            row = conn.execute(
                "SELECT id FROM financial_facts WHERE ticker = ? AND date(period_end) = date(?) "
                "AND fiscal_period_type = ? AND line_item = ? ORDER BY id DESC LIMIT 1",
                (ticker.upper(), period_day, fiscal_period_type, line_item),
            ).fetchone()
        elif table == "kpi_facts":
            row = conn.execute(
                "SELECT id FROM kpi_facts WHERE ticker = ? AND date(period_end) = date(?) "
                "AND fiscal_period_type = ? AND kpi_definition_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (ticker.upper(), period_day, fiscal_period_type, line_item),
            ).fetchone()
        else:
            raise ValueError(f"Unsupported table for restatement detection: {table!r}")
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


def _decimal_eq(stored: object, new: Decimal) -> bool:
    """True iff the incumbent's stored value equals `new` numerically.

    `financial_facts.value` is persisted as ``str(Decimal)`` (SQLite NUMERIC),
    so a textual `3677000000` vs `3677000000.0` must compare equal. Falls back
    to a string compare only when the stored value can't be parsed as a
    Decimal (never expected for a real fact row)."""
    try:
        return Decimal(str(stored)) == new
    except (InvalidOperation, ValueError, TypeError):
        return str(stored) == str(new)


def _heal_or_detect_same_document_fact(
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
    extracted_by: str | None,
    schema_profile: FinancialFactSchemaProfile | None = None,
) -> tuple[int | None, bool]:
    """Return ``(corrected_row_id, existed_with_same_doc_date)`` using one lookup."""
    if schema_profile is not None:
        has_extracted_by = schema_profile.has_extracted_by
    else:
        has_extracted_by = _table_has_column(conn, "financial_facts", "extracted_by")
    if not has_extracted_by:
        return None, False
    period_day = period_end.strftime("%Y-%m-%d")
    try:
        row = conn.execute(
            """
            SELECT id, value, currency, unit, extracted_by
            FROM financial_facts
            WHERE ticker = ?
              AND date(period_end) = date(?)
              AND fiscal_period_type = ?
              AND line_item = ?
              AND source_doc_id = ?
            """,
            (ticker.upper(), period_day, fiscal_period_type, line_item, source_doc_id),
        ).fetchone()
    except sqlite3.Error as exc:
        log.error(
            {
                "event": "restatement_same_doc_lookup_failed",
                "outcome": "raised",
                "ticker": ticker,
                "line_item": line_item,
                "error": str(exc),
            }
        )
        raise
    if row is None:
        return None, False
    inc_id = int(row["id"] if hasattr(row, "keys") else row[0])
    inc_value = row["value"] if hasattr(row, "keys") else row[1]
    inc_currency = row["currency"] if hasattr(row, "keys") else row[2]
    inc_unit = row["unit"] if hasattr(row, "keys") else row[3]
    inc_extracted_by = row["extracted_by"] if hasattr(row, "keys") else row[4]

    if inc_extracted_by != extracted_by:
        return None, True

    unchanged = (
        _decimal_eq(inc_value, value)
        and (inc_currency == currency)
        and (str(inc_unit) == str(unit))
    )
    if unchanged:
        return None, True

    new_value_str = str(value)
    try:
        conn.execute(
            "UPDATE financial_facts SET value = ?, currency = ?, unit = ? WHERE id = ?",
            (new_value_str, currency, unit, inc_id),
        )
    except sqlite3.Error as exc:
        log.error(
            {
                "event": "restatement_same_doc_correction_failed",
                "outcome": "raised",
                "ticker": ticker,
                "line_item": line_item,
                "error": str(exc),
            }
        )
        raise

    log.info(
        {
            "event": "financial_fact_same_doc_correction",
            "ticker": ticker.upper(),
            "period_end": str(period_end),
            "fiscal_period_type": fiscal_period_type,
            "line_item": line_item,
            "source_doc_id": source_doc_id,
            "row_id": inc_id,
            "old_value": str(inc_value),
            "new_value": new_value_str,
            "old_currency": inc_currency,
            "new_currency": currency,
            "old_unit": str(inc_unit),
            "new_unit": str(unit),
        }
    )
    resolve_fact_row(conn, fact_table="financial_facts", fact_row_id=inc_id)
    return inc_id, True


def _correct_same_document_fact(
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
    extracted_by: str | None,
    schema_profile: FinancialFactSchemaProfile | None = None,
) -> int | None:
    """Heal an incumbent `financial_facts` row that shares this write's exact
    provenance key but carries a stale value/currency/unit.

    `uq_financial_facts_provenance` is UNIQUE on (ticker, period_end,
    fiscal_period_type, line_item, source_doc_id), so a re-extraction of the
    SAME source document is an INSERT-OR-IGNORE no-op — which silently freezes
    the first-pulled value even when a later re-pull of that same accession
    yields a corrected number (a tag-ladder reorder, or an aggregated SEC
    companyfacts payload that gained a preferred concept after the first pull).
    Supersession never fires here because it requires a *different, later*
    filing. This UPDATEs the incumbent in place so same-document corrections
    land instead of being dropped.

    Safety rail: only a row written by the SAME extractor is overwritten. A
    row sharing the doc id but authored by a different extractor (e.g. a manual
    override) is left untouched — corrections never clobber a human edit.

    Returns the corrected row id when a heal was applied; None when there is no
    incumbent for the key, the incumbent already matches (identical replay — a
    true no-op), the safety rail blocked the write, or the schema predates the
    `extracted_by` audit column (synthetic pre-0054 fixtures). Database and
    invariant failures propagate to the transaction owner; they are not replays."""
    corrected_id, _ = _heal_or_detect_same_document_fact(
        conn,
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=fiscal_period_type,
        line_item=line_item,
        value=value,
        currency=currency,
        unit=unit,
        source_doc_id=source_doc_id,
        extracted_by=extracted_by,
        schema_profile=schema_profile,
    )
    return corrected_id


def _validate_financial_fact_plausibility(line_item: str, value: Decimal, unit: str) -> None:
    """Pre-persist plausibility gate for financial facts to prevent extreme scale errors."""
    try:
        val_float = float(value)
    except (TypeError, ValueError):
        return
    item_lower = line_item.lower()
    if (
        item_lower in ("revenue", "net_income", "total_assets", "operating_income")
        and abs(val_float) > 1e14
    ):
        log.error(
            "implausible_magnitude_rejected line_item=%s value=%s unit=%s", line_item, value, unit
        )
        raise ValueError(f"Implausible financial fact magnitude for {line_item}: {value} {unit}")
    if "shares" in item_lower and val_float < 0:
        log.error("implausible_negative_shares_rejected line_item=%s value=%s", line_item, value)
        raise ValueError(f"Negative share count implausible for {line_item}: {value}")


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
    before_resolve: Callable[[int], None] | None = None,
    evidence_resolution_timing_sink: Callable[[int], None] | None = None,
    monotonic_ns: Callable[[], int] = time.perf_counter_ns,
    schema_profile: FinancialFactSchemaProfile | None = None,
) -> tuple[int | None, int | None]:
    """Insert one financial_facts row, setting `supersedes_id` when this is
    a restatement of an existing row from an earlier filing.

    Returns `(new_row_id, superseded_row_id)`:
      - `new_row_id` is the id of the inserted row, or None when no NEW row
        was written — either a true no-op (identical same-document replay) or
        a same-document *correction* (the incumbent row for this exact
        provenance key was UPDATEd in place because the re-extraction changed
        its value/currency/unit; see `_correct_same_document_fact`). In both
        cases the row count is unchanged, so callers that tally inserts do not
        double-count a heal.
      - `superseded_row_id` is the id of the predecessor in the chain,
        or None if this is the first row for the logical key OR the new
        document is NOT a later filing than the incumbent (in which case
        the row is inserted standalone, no chain link).

    `locator` is the pre-serialized sub-document locator JSON (alembic 0075;
    serialize via models.facts.FactLocator.to_json). Like extracted_by /
    supersedes_id, it is silently dropped when the schema predates its
    column — acceptable for synthetic test fixtures.

    `before_resolve`, when provided, establishes source-specific provenance
    for the new row. A successful insert always resolves after that callback
    returns; callback or resolution failures propagate to the transaction
    owner so the entire admission can be rolled back.

    `evidence_resolution_timing_sink`, when provided, receives the elapsed
    nanoseconds for the source-specific provenance callback plus mandatory
    resolution. It is observational only and does not own transaction state.

    `schema_profile`, when provided, carries the once-per-batch column-
    presence probes (see `probe_financial_fact_schema`) so batch callers
    avoid re-running ``PRAGMA table_info`` for every fact row; when None the
    probes run per call exactly as before.

    Callers should use this in place of a raw INSERT OR IGNORE when the
    extractor wants the restatement chain populated. Existing call sites
    (compute/_common.py::insert_financial_facts) keep their plain INSERT
    OR IGNORE — adopting this helper is opt-in to avoid behavior changes
    in the load path until extractor-by-extractor wiring lands.
    """
    _validate_financial_fact_plausibility(line_item, value, unit)
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

    _, same_doc_exists = _heal_or_detect_same_document_fact(
        conn,
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=fiscal_period_type,
        line_item=line_item,
        value=value,
        currency=currency,
        unit=unit,
        source_doc_id=source_doc_id,
        extracted_by=extracted_by,
        schema_profile=schema_profile,
    )
    if same_doc_exists:
        return (None, None)

    if schema_profile is not None:
        has_audit_cols = schema_profile.has_audit_cols
        has_locator = schema_profile.has_locator
    else:
        has_audit_cols = _table_has_column(
            conn, "financial_facts", "supersedes_id"
        ) and _table_has_column(conn, "financial_facts", "extracted_by")
        has_locator = _table_has_column(conn, "financial_facts", "locator")
    write_locator = locator is not None and has_locator
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
        log.error(
            {
                "event": "restatement_insert_failed",
                "outcome": "raised",
                "ticker": ticker,
                "line_item": line_item,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        raise

    if cur.rowcount == 0:
        # UNIQUE conflict on (ticker, period_end, fiscal_period_type,
        # line_item, source_doc_id) — the SAME source document already has a
        # row for this key. Usually a true no-op, but when the re-extraction
        # yields a changed value/currency/unit (a tag-ladder reorder, or a
        # companyfacts payload that gained a preferred concept after the first
        # pull) the incumbent is stale — heal it in place. Identical replays
        # stay no-ops; a row authored by a different extractor is left alone.
        # supersedes_id is irrelevant here (that path needs a later filing).
        _correct_same_document_fact(
            conn,
            ticker=ticker,
            period_end=period_end,
            fiscal_period_type=fiscal_period_type,
            line_item=line_item,
            value=value,
            currency=currency,
            unit=unit,
            source_doc_id=source_doc_id,
            extracted_by=extracted_by,
            schema_profile=schema_profile,
        )
        return (None, None)
    new_row_id = int(cur.lastrowid) if cur.lastrowid is not None else None
    if new_row_id is not None:
        if evidence_resolution_timing_sink is None:
            if before_resolve is not None:
                before_resolve(new_row_id)
            resolve_fact_row(conn, fact_table="financial_facts", fact_row_id=new_row_id)
        else:
            evidence_started_ns = monotonic_ns()
            try:
                if before_resolve is not None:
                    before_resolve(new_row_id)
                resolve_fact_row(conn, fact_table="financial_facts", fact_row_id=new_row_id)
            finally:
                evidence_resolution_timing_sink(max(0, monotonic_ns() - evidence_started_ns))
    return (new_row_id, supersedes_id)


DERIVED_RECOMPUTE_EXTRACTOR = "metrics_engine"


class _SameDocumentKpiHead(NamedTuple):
    id: int
    value: object
    unit: str
    currency: str | None
    extracted_by: str | None


def _same_document_kpi_head(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: datetime,
    fiscal_period_type: str,
    kpi_definition_id: int,
    source_doc_id: int,
) -> _SameDocumentKpiHead | None:
    """Return the current head among same-document rows for one KPI key.

    Rows are append-only and a successor is always inserted after the row it
    supersedes, so the highest id is the head of the same-document chain.
    """
    row = conn.execute(
        "SELECT id, value, unit, currency, extracted_by FROM kpi_facts "
        "WHERE ticker=? AND date(period_end)=date(?) AND fiscal_period_type=? "
        "AND kpi_definition_id=? AND source_doc_id=? ORDER BY id DESC LIMIT 1",
        (
            ticker.upper(),
            period_end.strftime("%Y-%m-%d"),
            fiscal_period_type,
            kpi_definition_id,
            source_doc_id,
        ),
    ).fetchone()
    if row is None:
        return None
    return _SameDocumentKpiHead(
        id=int(row[0]),
        value=row[1],
        unit=str(row[2]),
        currency=None if row[3] is None else str(row[3]),
        extracted_by=None if row[4] is None else str(row[4]),
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
    currency: str | None = None,
    source_doc_id: int,
    confidence: float = 1.0,
    extracted_by: str | None = None,
    locator: str | None = None,
    source_excerpt: str | None = None,
    computed_from: str | None = None,
    formula_id: int | None = None,
    formula_version: int | None = None,
    before_resolve: Callable[[int], None] | None = None,
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

    `formula_id`/`formula_version` (alembic 0162, both nullable) tag a row
    produced by `compute.metrics_engine` — same drop-on-legacy tolerance as
    every other tail column; a pre-0162 `kpi_facts` (or a caller outside the
    metrics engine that never passes them) simply omits the columns.

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
    # them (locator: 0075; source_excerpt: 0033; computed_from: 0087;
    # formula_id/formula_version: 0162) — same drop-on-legacy tolerance as
    # the audit columns.
    tail_cols: list[str] = []
    tail_vals: list[str | int | None] = []
    if _table_has_column(conn, "kpi_facts", "currency"):
        tail_cols.append("currency")
        tail_vals.append(currency)
    if locator is not None and _table_has_column(conn, "kpi_facts", "locator"):
        tail_cols.append("locator")
        tail_vals.append(locator)
    if source_excerpt is not None and _table_has_column(conn, "kpi_facts", "source_excerpt"):
        tail_cols.append("source_excerpt")
        tail_vals.append(source_excerpt)
    if computed_from is not None and _table_has_column(conn, "kpi_facts", "computed_from"):
        tail_cols.append("computed_from")
        tail_vals.append(computed_from)
    if formula_id is not None and _table_has_column(conn, "kpi_facts", "formula_id"):
        tail_cols.append("formula_id")
        tail_vals.append(formula_id)
    if formula_version is not None and _table_has_column(conn, "kpi_facts", "formula_version"):
        tail_cols.append("formula_version")
        tail_vals.append(formula_version)
    has_currency_col = _table_has_column(conn, "kpi_facts", "currency")
    derived_recompute = (
        extracted_by == DERIVED_RECOMPUTE_EXTRACTOR and computed_from is not None and has_audit_cols
    )
    same_doc_head = (
        _same_document_kpi_head(
            conn,
            ticker=ticker,
            period_end=period_end,
            fiscal_period_type=fiscal_period_type,
            kpi_definition_id=kpi_definition_id,
            source_doc_id=source_doc_id,
        )
        if has_currency_col and (derived_recompute or currency is not None)
        else None
    )
    if (
        derived_recompute
        and same_doc_head is not None
        and same_doc_head.extracted_by == DERIVED_RECOMPUTE_EXTRACTOR
    ):
        # A deterministic recomputation from the same anchor document. The
        # persisted head is a prior calculation, not management wording, so a
        # changed value, unit or newly admitted currency appends a successor
        # that names its predecessor. The predecessor row stays unchanged.
        stored_currency = same_doc_head.currency
        if stored_currency is not None and stored_currency != currency:
            raise ValueError("same-document derived KPI currency conflicts with persisted fact")
        if (
            _decimal_eq(same_doc_head.value, value)
            and same_doc_head.unit == unit
            and stored_currency == currency
        ):
            return (None, None)
        supersedes_id = same_doc_head.id
    elif currency is not None and has_currency_col:
        if same_doc_head is not None:
            stored = same_doc_head.currency
            if stored is None:
                if not _decimal_eq(same_doc_head.value, value) or same_doc_head.unit != unit:
                    raise ValueError(
                        "same-document KPI currency backfill conflicts with persisted fact identity"
                    )
                raise ValueError(
                    "same-document KPI currency enrichment requires source-reviewed supersession"
                )
            if str(stored) != currency:
                raise ValueError("same-document KPI currency conflicts with persisted fact")
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
        raise

    if cur.rowcount == 0:
        # Conflict on UNIQUE (same source_doc_id under provenance; same
        # logical key under legacy logical-only). No row written.
        return (None, None)
    new_row_id = int(cur.lastrowid) if cur.lastrowid is not None else None
    if new_row_id is not None:
        if before_resolve is not None:
            before_resolve(new_row_id)
        resolve_fact_row(conn, fact_table="kpi_facts", fact_row_id=new_row_id)
    return (new_row_id, supersedes_id)


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
            if table == "financial_facts":
                row = conn.execute(
                    "SELECT id FROM financial_facts WHERE supersedes_id = ?", (current,)
                ).fetchone()
            elif table == "kpi_facts":
                row = conn.execute(
                    "SELECT id FROM kpi_facts WHERE supersedes_id = ?", (current,)
                ).fetchone()
            else:
                raise ValueError(f"Unsupported table for restatement chain: {table!r}")
        except sqlite3.Error:
            break
        if row is None:
            return current
        current = int(row["id"] if hasattr(row, "keys") else row[0])
    return current
