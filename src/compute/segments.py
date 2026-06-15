"""Extract segment data from FMP product / geographic segment documents.

Each FmpSegmentRecord has a `data: dict[segment_name -> revenue]` field. We
emit one segment_dimensions cell per segment_name, hung off one
segment_periods anchor per (ticker, period_end, fiscal_period_type, source_doc).
`dim_type` distinguishes product vs geographic dimensions.

Reconciliation gate: FMP's revenue-geographic-segments endpoint (and to a lesser
extent revenue-product-segmentation) occasionally returns inflated values for
the most-recent fiscal year — typically the Q4 or FY record carries a per-segment
additive contamination from the prior FY's annual figure. The signature is
distinctive: sum-of-segments runs 1.4x–3.4x the period's reported revenue.
Legitimate cases of sum < revenue (an "Other" bucket not in the FMP feed) are
common and accepted; sum substantially > revenue is mechanically impossible.
So we drop records whose segment sum exceeds the period's revenue by more than
RECONCILE_TOLERANCE_OVER. Requires the income_statement extractor to have
already run for the same ticker — quarterly_refresh orders docs so that holds.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from compute._common import (
    load_document_row,
    parse_currency,
    read_records_json,
)
from compute.segment_cache import apply_overrides
from models.facts import (
    FiscalPeriodType,
    SegmentDimension,
    SegmentDimType,
    SegmentFact,
    Unit,
)
from models.fmp_payloads import FmpSegmentRecord
from pipeline.segment_junction_writer import (
    write_segment_facts_junction,
    write_segment_facts_via_junction,
)

_DOC_TYPE_TO_METRIC: dict[str, str] = {
    "fmp_segment_product": "revenue_by_product",
    "fmp_segment_geographic": "revenue_by_geography",
}

_DOC_TYPE_TO_DIM_TYPE: dict[str, SegmentDimType] = {
    "fmp_segment_product": SegmentDimType.PRODUCT,
    "fmp_segment_geographic": SegmentDimType.GEOGRAPHY,
}

# Reject a segment record if sum(values) > revenue * (1 + tolerance). 0.10
# absorbs FX/rounding noise while still catching the 1.4x+ contamination
# pattern. sum < revenue is always accepted (missing-bucket case).
RECONCILE_TOLERANCE_OVER: float = 0.10


def segment_sum_exceeds_revenue(
    values: Iterable[object],
    revenue: Decimal,
    *,
    tolerance: float = RECONCILE_TOLERANCE_OVER,
) -> tuple[bool, Decimal]:
    """Pure reconciliation predicate: does the segment sum exceed reported revenue?

    Shared by the DB-ingest gate (`_passes_reconciliation`) and the offline cache
    sanity scanner (execution/check_segment_cache_sanity.py) so both apply the
    identical rule and the same `RECONCILE_TOLERANCE_OVER` threshold. Returns
    ``(exceeds, seg_sum)`` where ``exceeds`` is True iff the sum of non-null
    segment values is more than ``revenue * (1 + tolerance)`` — the signature of
    FMP's recurring "Q4/FY record carries a per-segment additive contamination
    from the prior FY's annual figure" bug. ``sum < revenue`` is never flagged
    (the common, legitimate missing-"Other"-bucket case).
    """
    seg_sum = sum((Decimal(str(v)) for v in values if v is not None), start=Decimal("0"))
    cap = revenue * Decimal(str(1 + tolerance))
    return (seg_sum > cap, seg_sum)


def _lookup_period_revenue(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: datetime,
    period_type: FiscalPeriodType,
) -> Decimal | None:
    """Revenue for (ticker, period_end, period_type) from financial_facts, or None.

    None means the income-statement extractor hasn't populated this period yet
    — the caller treats this as "skip the gate, accept the record" since we
    can't disprove it. quarterly_refresh orders docs so income_statement runs
    first; the None branch only triggers on bootstrap / partial-data states.
    """
    cur = conn.execute(
        "SELECT value FROM financial_facts "
        "WHERE ticker = ? AND period_end = ? AND fiscal_period_type = ? "
        "AND line_item = 'revenue' LIMIT 1",
        (ticker.upper(), period_end, period_type.value),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return Decimal(str(row[0]))


def _passes_reconciliation(
    conn: sqlite3.Connection,
    record: FmpSegmentRecord,
    metric: str,
    source_doc_id: int,
) -> bool:
    """True if record's segment sum is within tolerance of reported revenue.

    Emits a single-line JSON warning to stderr when rejecting so cron / onboard
    logs surface the drop. Doesn't raise — bad data is not a refresh failure.
    """
    period_end = datetime.fromisoformat(record.date)
    period_type = FiscalPeriodType(record.period)
    revenue = _lookup_period_revenue(
        conn, ticker=record.symbol, period_end=period_end, period_type=period_type
    )
    if revenue is None or revenue == 0:
        return True
    exceeds, seg_total = segment_sum_exceeds_revenue(record.data.values(), revenue)
    if not exceeds:
        return True
    sys.stderr.write(
        json.dumps(
            {
                "event": "segment_record_rejected",
                "reason": "sum_exceeds_revenue",
                "ticker": record.symbol.upper(),
                "period_end": record.date,
                "period_type": record.period,
                "metric": metric,
                "segment_sum": str(seg_total),
                "revenue": str(revenue),
                "ratio": float(seg_total / revenue),
                "source_doc_id": source_doc_id,
            }
        )
        + "\n"
    )
    return False


def extract_facts_from_record(
    record: FmpSegmentRecord, source_doc_id: int, metric: str
) -> list[SegmentFact]:
    """Convert one validated segment record to SegmentFact rows (one per segment)."""
    period_end = datetime.fromisoformat(record.date)
    period_type = FiscalPeriodType(record.period)
    currency = parse_currency(record.reportedCurrency)

    facts: list[SegmentFact] = []
    for segment_name, value in record.data.items():
        facts.append(
            SegmentFact(
                ticker=record.symbol.upper(),
                period_end=period_end,
                fiscal_period_type=period_type,
                segment_name=segment_name,
                metric=metric,
                value=Decimal(str(value)),
                currency=currency,
                unit=Unit.ACTUAL,
                source_doc_id=source_doc_id,
            )
        )
    return facts


def insert_segment_facts(conn: sqlite3.Connection, facts: list[SegmentFact]) -> int:
    """Persist a batch of segment facts via the segment_periods + segment_dimensions junction.

    Returns the number of NEW dimension rows inserted. Idempotent — re-running
    over the same source is a no-op (writer dedupes on the natural key).
    """
    return write_segment_facts_via_junction(conn, facts)


def extract_segment_facts(conn: sqlite3.Connection, document_id: int, project_root: Path) -> int:
    """Read documents[document_id]'s segment file, write junction rows.

    Auto-detects product vs geographic from doc_type. Idempotent. Rejects
    records that fail the revenue-reconciliation gate (see module docstring).
    Writes into segment_periods + segment_dimensions; the legacy segment_facts
    table was retired in migration 0056.
    """
    cur = conn.cursor()
    cur.execute("SELECT doc_type FROM documents WHERE id = ?", (document_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"No document with id={document_id}")
    doc_type = row["doc_type"]
    if doc_type not in _DOC_TYPE_TO_METRIC:
        raise ValueError(
            f"Document {document_id} is doc_type={doc_type!r}, "
            f"not one of {sorted(_DOC_TYPE_TO_METRIC)}"
        )

    _ticker, file_path_str = load_document_row(conn, document_id, doc_type)
    metric = _DOC_TYPE_TO_METRIC[doc_type]
    dim_type = _DOC_TYPE_TO_DIM_TYPE[doc_type]
    records = read_records_json(project_root / file_path_str)
    # Company-doc overrides win over FMP at the ingest gate too: a record-level
    # replace (e.g. the GOOG Q4'25 8-K product segmentation) is applied here so a
    # re-fetched contaminated record is corrected BEFORE the reconciliation gate —
    # it then reconciles and lands the 8-K figures instead of being dropped.
    records = apply_overrides(records, ticker=_ticker, dim_type=dim_type.value, conn=conn)

    inserted = 0
    for rec_data in records:
        rec = FmpSegmentRecord.model_validate(rec_data)
        if not _passes_reconciliation(conn, rec, metric, document_id):
            continue
        inserted += _write_record_to_junction(
            conn,
            rec,
            source_doc_id=document_id,
            dim_type=dim_type,
            junction_metric="revenue",
        )
    conn.commit()
    return inserted


def _write_record_to_junction(
    conn: sqlite3.Connection,
    record: FmpSegmentRecord,
    *,
    source_doc_id: int,
    dim_type: SegmentDimType,
    junction_metric: str = "revenue",
) -> int:
    """Persist one FMP segment record into segment_periods + segment_dimensions.

    Returns the number of NEW dimension rows inserted. Replaces the previous
    additive `insert_segment_facts` + `_mirror_record_to_junction` pair —
    only the junction is written now.
    """
    period_end = datetime.fromisoformat(record.date)
    period_type = FiscalPeriodType(record.period)
    currency = parse_currency(record.reportedCurrency)
    dimensions = [
        SegmentDimension(
            dim_type=dim_type,
            dim_name=segment_name,
            value=Decimal(str(value)),
            metric=junction_metric,
        )
        for segment_name, value in record.data.items()
    ]
    if not dimensions:
        return 0
    _, dims_inserted = write_segment_facts_junction(
        conn,
        ticker=record.symbol.upper(),
        period_end=period_end,
        fiscal_period_type=period_type,
        source_doc_id=source_doc_id,
        currency=currency,
        unit=Unit.ACTUAL,
        dimensions=dimensions,
    )
    return dims_inserted
