"""Extract segment_facts from FMP product / geographic segment documents.

Each FmpSegmentRecord has a `data: dict[segment_name -> revenue]` field. We
emit one segment_facts row per segment_name. The `metric` column distinguishes
product vs geographic dimensions ('revenue_by_product' vs 'revenue_by_geography').

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
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from compute._common import (
    load_document_row,
    parse_currency,
    read_records_json,
)
from models.facts import FiscalPeriodType, SegmentFact, Unit
from models.fmp_payloads import FmpSegmentRecord

_DOC_TYPE_TO_METRIC: dict[str, str] = {
    "fmp_segment_product": "revenue_by_product",
    "fmp_segment_geographic": "revenue_by_geography",
}

# Reject a segment record if sum(values) > revenue * (1 + tolerance). 0.10
# absorbs FX/rounding noise while still catching the 1.4x+ contamination
# pattern. sum < revenue is always accepted (missing-bucket case).
RECONCILE_TOLERANCE_OVER: float = 0.10


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
    seg_total = sum(
        (Decimal(str(v)) for v in record.data.values() if v is not None),
        start=Decimal("0"),
    )
    cap = revenue * Decimal(str(1 + RECONCILE_TOLERANCE_OVER))
    if seg_total <= cap:
        return True
    sys.stderr.write(
        json.dumps({
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
        }) + "\n"
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
    """Bulk-insert segment facts via INSERT OR IGNORE. Returns rowcount."""
    insert_sql = (
        "INSERT OR IGNORE INTO segment_facts "
        "(ticker, period_end, fiscal_period_type, segment_name, metric, value, "
        " currency, unit, source_doc_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    inserted = 0
    for f in facts:
        result = conn.execute(
            insert_sql,
            (
                f.ticker,
                f.period_end,
                f.fiscal_period_type.value,
                f.segment_name,
                f.metric,
                str(f.value),
                f.currency.value if f.currency is not None else None,
                f.unit.value,
                f.source_doc_id,
            ),
        )
        if result.rowcount > 0:
            inserted += 1
    return inserted


def extract_segment_facts(conn: sqlite3.Connection, document_id: int, project_root: Path) -> int:
    """Read documents[document_id]'s segment file, write SegmentFact rows.

    Auto-detects product vs geographic from doc_type. Idempotent. Rejects
    records that fail the revenue-reconciliation gate (see module docstring).
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
    records = read_records_json(project_root / file_path_str)

    inserted = 0
    for rec_data in records:
        rec = FmpSegmentRecord.model_validate(rec_data)
        if not _passes_reconciliation(conn, rec, metric, document_id):
            continue
        facts = extract_facts_from_record(rec, source_doc_id=document_id, metric=metric)
        inserted += insert_segment_facts(conn, facts)
    conn.commit()
    return inserted
