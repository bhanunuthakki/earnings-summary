"""Junction-model writer for segment dimensions.

Backs the additive segment_periods + segment_dimensions schema (migration
0053). A single call writes one period row (upserted on the uniq tuple) plus
N dimension rows under it — each dimension row carrying its own
(dim_type, dim_name, metric, value) cell.

Usage path:
  - Backfill: scratch/backfill_segment_junction.py walks segment_facts and
    invokes this writer once per (ticker, period, source_doc_id) tuple with
    one dim per existing row.
  - Forward (FMP): src/compute/segments.py also calls this writer after it
    writes segment_facts, mirroring each row into the junction shape.

The contract intentionally takes pre-shaped SegmentDimension objects so the
caller controls the mapping from a source-specific schema (FMP, 10-K narrative,
manual entry) into the junction's dim_type/metric conventions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal

from models.facts import (
    Currency,
    FiscalPeriodType,
    SegmentDimType,
    SegmentDimension,
    SegmentFact,
    Unit,
)


def _ensure_period(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: datetime,
    fiscal_period_type: FiscalPeriodType,
    source_doc_id: int,
    currency: Currency | None,
    unit: Unit,
) -> tuple[int, bool]:
    """Upsert a segment_periods row; return (period_id, inserted)."""
    cur = conn.execute(
        """
        SELECT id FROM segment_periods
        WHERE ticker = ?
          AND period_end = ?
          AND fiscal_period_type = ?
          AND source_doc_id = ?
        """,
        (
            ticker.upper(),
            period_end,
            fiscal_period_type.value,
            source_doc_id,
        ),
    )
    row = cur.fetchone()
    if row is not None:
        return (int(row[0]), False)
    cur = conn.execute(
        """
        INSERT INTO segment_periods
          (ticker, period_end, fiscal_period_type, source_doc_id,
           currency, unit, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker.upper(),
            period_end,
            fiscal_period_type.value,
            source_doc_id,
            currency.value if currency is not None else None,
            unit.value,
            datetime.now(timezone.utc).replace(tzinfo=None),
        ),
    )
    new_id = cur.lastrowid
    if new_id is None:
        raise RuntimeError(
            "segment_periods INSERT returned no lastrowid — DB connection "
            "is in an unexpected state"
        )
    return (int(new_id), True)


def _dimension_exists(
    conn: sqlite3.Connection,
    *,
    period_id: int,
    dim_type: SegmentDimType,
    dim_name: str,
    metric: str,
    value: Decimal,
) -> bool:
    """A dim row is considered a duplicate when (period_id, dim_type, dim_name,
    metric, value) all match. Re-running the writer on the same source is a no-op.

    The per-dim `unit` column is intentionally NOT part of the dedup key —
    a metric's unit is determined by the metric itself (capex → actual,
    headcount → count, etc.), so a unit flip on a re-run would indicate a
    bug upstream rather than legitimate new data."""
    cur = conn.execute(
        """
        SELECT 1 FROM segment_dimensions
        WHERE period_id = ?
          AND dim_type = ?
          AND dim_name = ?
          AND metric = ?
          AND value = ?
        LIMIT 1
        """,
        (period_id, dim_type.value, dim_name, metric, str(value)),
    )
    return cur.fetchone() is not None


def _segment_dimensions_has_unit_column(conn: sqlite3.Connection) -> bool:
    """True if migration 0057 has added the per-dim unit column.

    Cached as a single PRAGMA call to keep the writer hot path light; the
    inspection runs once per `write_segment_facts_junction` invocation. On
    pre-0057 schemas the writer silently falls back to omitting the column,
    matching the pre-extension behavior."""
    try:
        rows = conn.execute("PRAGMA table_info(segment_dimensions)").fetchall()
    except sqlite3.Error:
        return False
    return any(str(r[1]) == "unit" for r in rows)


def write_segment_facts_junction(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: datetime,
    fiscal_period_type: FiscalPeriodType,
    source_doc_id: int,
    currency: Currency | None,
    unit: Unit,
    dimensions: Iterable[SegmentDimension],
) -> tuple[int, int]:
    """Write one (ticker, period, source_doc) anchor + N dimension cells.

    Returns (period_inserted, dimensions_inserted). `period_inserted` is 1
    when a fresh period row was created and 0 when it already existed.
    Caller manages the surrounding transaction; this function does not commit.

    Per-dim unit handling: if a `SegmentDimension.unit` is set AND differs
    from the period's `unit` argument, the per-dim value is stored on the
    segment_dimensions row. This lets a single period anchor host mixed-unit
    cells (e.g. ACTUAL revenue + COUNT headcount under the same period).
    Requires migration 0057 to have added the `unit` column; on pre-0057
    schemas the writer silently omits the per-dim unit (legacy single-unit
    behavior).
    """
    period_id, period_inserted = _ensure_period(
        conn,
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=fiscal_period_type,
        source_doc_id=source_doc_id,
        currency=currency,
        unit=unit,
    )
    has_unit_column = _segment_dimensions_has_unit_column(conn)

    dims_inserted = 0
    for dim in dimensions:
        if _dimension_exists(
            conn,
            period_id=period_id,
            dim_type=dim.dim_type,
            dim_name=dim.dim_name,
            metric=dim.metric,
            value=dim.value,
        ):
            continue
        # Store the dim's unit only when (a) the column exists and (b) it
        # differs from the period's unit — keeps the column NULL when the
        # dim's unit is just the period's unit, matching the "inherit"
        # default the reader expects.
        dim_unit_value: str | None = None
        if has_unit_column and dim.unit is not None and dim.unit != unit:
            dim_unit_value = dim.unit.value
        if has_unit_column:
            conn.execute(
                """
                INSERT INTO segment_dimensions
                  (period_id, dim_type, dim_name, value, metric, unit)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    period_id,
                    dim.dim_type.value,
                    dim.dim_name,
                    str(dim.value),
                    dim.metric,
                    dim_unit_value,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO segment_dimensions
                  (period_id, dim_type, dim_name, value, metric)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    period_id,
                    dim.dim_type.value,
                    dim.dim_name,
                    str(dim.value),
                    dim.metric,
                ),
            )
        dims_inserted += 1
    return (1 if period_inserted else 0, dims_inserted)


# ---------------------------------------------------------------------------
# Mapping helpers — translate a legacy segment_facts row into the junction shape
# ---------------------------------------------------------------------------


_LEGACY_METRIC_TO_DIM_TYPE: dict[str, SegmentDimType] = {
    "revenue_by_product": SegmentDimType.PRODUCT,
    "revenue_by_geography": SegmentDimType.GEOGRAPHY,
    "operating_income": SegmentDimType.BUSINESS_UNIT,
    # Capex + headcount are emitted by the 10-K segment-note extractor under
    # the business-unit axis (segment_name = "AWS" / "North America" / etc.).
    # Keeps the fallback path's dim_type stable for these well-known metrics
    # rather than letting them slip through as a generic BUSINESS_UNIT default.
    "capex": SegmentDimType.BUSINESS_UNIT,
    "headcount": SegmentDimType.BUSINESS_UNIT,
}

_LEGACY_METRIC_TO_JUNCTION_METRIC: dict[str, str] = {
    "revenue_by_product": "revenue",
    "revenue_by_geography": "revenue",
    "operating_income": "operating_income",
    "capex": "capex",
    "headcount": "headcount",
}


def segment_fact_to_dimension(
    segment_name: str,
    legacy_metric: str,
    value: Decimal,
    *,
    unit: Unit | None = None,
) -> SegmentDimension:
    """Translate a legacy `segment_facts` row into a single junction dimension.

    Falls back to dim_type=BUSINESS_UNIT for unknown legacy metrics and keeps
    the legacy metric string verbatim — keeps backfill total-loss-free for
    non-standard metrics emitted by future extractors.

    `unit` is optional: when set (and different from the period anchor's unit
    at write time) it tells the writer to store the dim's unit on the
    segment_dimensions row, allowing mixed-unit cells under one period
    anchor — needed for headcount (Unit.COUNT) alongside ACTUAL-unit cells.
    """
    dim_type = _LEGACY_METRIC_TO_DIM_TYPE.get(legacy_metric, SegmentDimType.BUSINESS_UNIT)
    metric = _LEGACY_METRIC_TO_JUNCTION_METRIC.get(legacy_metric, legacy_metric)
    return SegmentDimension(
        dim_type=dim_type,
        dim_name=segment_name,
        value=value,
        metric=metric,
        unit=unit,
    )


def write_segment_facts_via_junction(
    conn: sqlite3.Connection, facts: Iterable[SegmentFact]
) -> int:
    """Write a batch of legacy-shaped SegmentFact objects through the junction.

    Groups facts on the segment_periods natural key (ticker, period_end,
    fiscal_period_type, source_doc_id) — currency and unit are NOT part of
    the group key. Per-fact currency and unit travel onto the SegmentDimension
    objects; the period anchor's currency/unit come from the first fact in
    the group (and the writer stores the dim's unit on the dim row whenever
    it differs from the period's). This lets a single 10-K extraction batch
    that mixes Unit.ACTUAL (OI / capex) with Unit.COUNT (headcount) share
    one period anchor instead of fighting the natural-key uniqueness.

    Returns the total number of NEW dimension rows inserted (period rows
    already in place are not counted, matching the legacy
    `_insert_segment_facts` semantics).

    Used by both `compute.segment_oi_10k._insert_segment_facts` and
    `compute.segments_nu._insert_segment_facts` after the segment_facts
    table was retired. Caller manages the transaction.
    """
    grouped: dict[
        tuple[str, datetime, FiscalPeriodType, int],
        list[SegmentFact],
    ] = {}
    for f in facts:
        key = (
            f.ticker.upper(),
            f.period_end,
            f.fiscal_period_type,
            f.source_doc_id,
        )
        grouped.setdefault(key, []).append(f)

    total_dims_inserted = 0
    for (
        ticker,
        period_end,
        period_type,
        source_doc_id,
    ), group in grouped.items():
        # Period anchor's currency/unit come from the first fact. The
        # remaining facts in the group MAY have a different unit — in that
        # case `write_segment_facts_junction` stores their unit on the dim
        # row (via SegmentDimension.unit).
        anchor = group[0]
        dimensions = [
            segment_fact_to_dimension(
                f.segment_name, f.metric, f.value, unit=f.unit
            )
            for f in group
        ]
        _, dims_inserted = write_segment_facts_junction(
            conn,
            ticker=ticker,
            period_end=period_end,
            fiscal_period_type=period_type,
            source_doc_id=source_doc_id,
            currency=anchor.currency,
            unit=anchor.unit,
            dimensions=dimensions,
        )
        total_dims_inserted += dims_inserted
    return total_dims_inserted
