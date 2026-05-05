"""Extract segment_facts from FMP product / geographic segment documents.

Each FmpSegmentRecord has a `data: dict[segment_name -> revenue]` field. We
emit one segment_facts row per segment_name. The `metric` column distinguishes
product vs geographic dimensions ('revenue_by_product' vs 'revenue_by_geography').
"""

from __future__ import annotations

import sqlite3
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

    Auto-detects product vs geographic from doc_type. Idempotent.
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
        facts = extract_facts_from_record(rec, source_doc_id=document_id, metric=metric)
        inserted += insert_segment_facts(conn, facts)
    conn.commit()
    return inserted
