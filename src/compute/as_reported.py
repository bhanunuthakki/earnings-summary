"""Extract canonical line items from FMP as-reported (XBRL-tagged) documents.

The four FMP as-reported endpoints (income, balance, cashflow, financial) all
share the same envelope shape: `data` is a dict of XBRL tag names to values.
We extract a curated subset of numeric tags relevant to the analytical aims —
RPO, contract liabilities, operating-lease detail, restructuring, impairment.

Same XBRL tag can appear in multiple as-reported files for the same period
(income, balance, financial overlap). The UNIQUE financial_facts index keeps
duplicates out; first writer wins per source_doc_id.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from compute._common import (
    insert_financial_facts,
    load_document_row,
    parse_currency,
    read_records_json,
)
from models.documents import SourceQualityTier
from models.facts import FinancialFact, FiscalPeriodType, Unit
from models.fmp_payloads import FmpAsReportedRecord
from pipeline import locators

_AS_REPORTED_DOC_TYPES: frozenset[str] = frozenset(
    {
        "fmp_as_reported_income",
        "fmp_as_reported_balance",
        "fmp_as_reported_cashflow",
        "fmp_as_reported_financial",
    }
)

# (xbrl_tag, canonical_line_item)
_XBRL_TAG_MAP: list[tuple[str, str]] = [
    ("revenueremainingperformanceobligation", "rpo"),
    ("contractwithcustomerliability", "contract_liabilities"),
    ("contractwithcustomerliabilitycurrent", "contract_liabilities_current"),
    ("contractwithcustomerliabilitynoncurrent", "contract_liabilities_non_current"),
    ("contractwithcustomerasset", "contract_assets"),
    (
        "revenuefromcontractwithcustomerexcludingassessedtax",
        "revenue_excl_assessed_tax",
    ),
    ("operatingleaseliability", "operating_lease_liability"),
    ("operatingleaseliabilitycurrent", "operating_lease_liability_current"),
    ("operatingleaseliabilitynoncurrent", "operating_lease_liability_non_current"),
    ("operatingleaserightofuseasset", "operating_lease_rou_asset"),
    ("paymentsforrepurchaseofcommonstock", "repurchase_of_common_stock"),
    ("paymentsofdividendscommonstock", "dividends_paid_common_xbrl"),
    ("goodwillimpairmentloss", "goodwill_impairment_loss"),
    ("restructuringcharges", "restructuring_charges"),
    ("stockholdersequity", "stockholders_equity_xbrl"),
]


def extract_facts_from_record(
    record: FmpAsReportedRecord, source_doc_id: int, record_index: int | None = None
) -> list[FinancialFact]:
    """Walk the curated XBRL tag map; emit a FinancialFact per numeric value found.

    `record_index` (the record's position in the source JSON array) makes each
    fact carry a locator pointing at the exact tag of the cached response:
    `[<i>].data.<xbrl_tag>` (data_provenance.md §7).
    """
    period_end = datetime.fromisoformat(record.date)
    period_type = FiscalPeriodType(record.period)
    currency = parse_currency(record.reportedCurrency)

    facts: list[FinancialFact] = []
    for xbrl_tag, canonical in _XBRL_TAG_MAP:
        value = record.data.get(xbrl_tag)
        if value is None:
            continue
        if not isinstance(value, (int, float)):
            continue
        locator = (
            locators.table_cell_locator(
                section=None,
                row_label=xbrl_tag,
                column_header=record.date,
                json_path=f"[{record_index}].data.{xbrl_tag}",
                cell_value_as_extracted=str(value),
            )
            if record_index is not None
            else None
        )
        facts.append(
            FinancialFact(
                ticker=record.symbol.upper(),
                period_end=period_end,
                fiscal_period_type=period_type,
                line_item=canonical,
                value=Decimal(str(value)),
                currency=currency,
                unit=Unit.ACTUAL,
                source_doc_id=source_doc_id,
                confidence=1.0,
                locator=locator,
            )
        )
    return facts


def extract_as_reported_facts(
    conn: sqlite3.Connection, document_id: int, project_root: Path
) -> int:
    """Read documents[document_id]'s file, write FinancialFact rows. Idempotent.

    Auto-detects which of the four as-reported doc_types it is by looking up
    the document; same extractor handles all four because the JSON envelope
    and tag namespace are identical.
    """
    cur = conn.cursor()
    cur.execute("SELECT doc_type FROM documents WHERE id = ?", (document_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"No document with id={document_id}")
    doc_type = row["doc_type"]
    if doc_type not in _AS_REPORTED_DOC_TYPES:
        raise ValueError(
            f"Document {document_id} is doc_type={doc_type!r}, "
            f"not one of {sorted(_AS_REPORTED_DOC_TYPES)}"
        )

    _ticker, file_path_str = load_document_row(conn, document_id, doc_type)
    records = read_records_json(project_root / file_path_str)

    inserted = 0
    for idx, rec_data in enumerate(records):
        rec = FmpAsReportedRecord.model_validate(rec_data)
        facts = extract_facts_from_record(rec, source_doc_id=document_id, record_index=idx)
        inserted += insert_financial_facts(
            conn, facts, extracted_by="fmp_as_reported", tier=SourceQualityTier.FMP_NORMALIZED
        )
    conn.commit()
    return inserted
