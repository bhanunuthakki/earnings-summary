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
    metric, value) all match. Re-running the writer on the same source is a no-op."""
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
}

_LEGACY_METRIC_TO_JUNCTION_METRIC: dict[str, str] = {
    "revenue_by_product": "revenue",
    "revenue_by_geography": "revenue",
    "operating_income": "operating_income",
}


def segment_fact_to_dimension(
    segment_name: str, legacy_metric: str, value: Decimal
) -> SegmentDimension:
    """Translate a legacy `segment_facts` row into a single junction dimension.

    Falls back to dim_type=BUSINESS_UNIT for unknown legacy metrics and keeps
    the legacy metric string verbatim — keeps backfill total-loss-free for
    non-standard metrics emitted by future extractors.
    """
    dim_type = _LEGACY_METRIC_TO_DIM_TYPE.get(legacy_metric, SegmentDimType.BUSINESS_UNIT)
    metric = _LEGACY_METRIC_TO_JUNCTION_METRIC.get(legacy_metric, legacy_metric)
    return SegmentDimension(
        dim_type=dim_type,
        dim_name=segment_name,
        value=value,
        metric=metric,
    )
