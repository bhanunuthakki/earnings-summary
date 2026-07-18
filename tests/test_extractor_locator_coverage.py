"""CI guard for the provenance click-through program (Phase A, section 4.2)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from models.facts import LocatorKind  # noqa: E402

REGISTERED_EXTRACTOR_LOCATOR_VERSIONS: dict[str, int] = {
    "table_extractors.generic_xbrl_capture": 2,
    "compute._common": 2,
    "compute.as_reported": 2,
    "compute.s1_financials": 1,
    "ir_pipeline.ingest": 1,
    "compute.kpi_extract_summaries": 1,
    "competitive.transcript_mentions": 1,
    "competitive.category_share": 1,
}

_WRITER_CALL_RX = re.compile(r"(?<!class )\b(KpiValue|FinancialFact)\(")


def _discover_writer_modules() -> set[str]:
    discovered: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if _WRITER_CALL_RX.search(text):
            rel = path.relative_to(SRC_ROOT).with_suffix("").as_posix().replace("/", ".")
            discovered.add(rel)
    return discovered


def test_every_locator_writer_is_registered() -> None:
    discovered = _discover_writer_modules()
    registered = set(REGISTERED_EXTRACTOR_LOCATOR_VERSIONS)
    new_unregistered = discovered - registered
    assert not new_unregistered, sorted(new_unregistered)
    stale = registered - discovered
    assert not stale, sorted(stale)


def test_generic_xbrl_capture_locator_is_v2_on_fixture() -> None:
    from collections import defaultdict
    from datetime import datetime

    from table_extractors import generic_xbrl_capture as g

    section: list[dict[str, object]] = [
        {"Debt - Long-Term Debt (Details) - USD ($) $ in Millions": ["Dec. 31, 2024"]},
        {"Term loan, net": [500]},
    ]
    per_period: dict[datetime, list[object]] = defaultdict(list)
    audit = g.CaptureAudit()
    g._walk_section("Debt - Long-Term", section, per_period, audit, fye_md=(12, 31))
    values = next(iter(per_period.values()))
    loc = values[0].locator
    assert loc is not None
    assert loc.locator_version >= 2
    assert loc.effective_kind() == LocatorKind.FMP_JSON_TABLE
    assert loc.table_cell is not None
    assert loc.table_cell.row_label == "Term loan, net"
    assert loc.table_cell.column_header == "Dec. 31, 2024"
    assert loc.verbatim_snippet


def test_common_extract_facts_with_spec_locator_is_v2_on_fixture() -> None:
    from compute._common import extract_facts_with_spec
    from models.facts import Unit
    from models.fmp_payloads import FmpIncomeStatementRecord

    record = FmpIncomeStatementRecord.model_validate(
        {
            "date": "2025-12-31",
            "symbol": "GOOG",
            "reportedCurrency": "USD",
            "period": "FY",
            "revenue": 402963000000,
        }
    )
    facts = extract_facts_with_spec(
        record, 1, [("revenue", "revenue", Unit.ACTUAL)], record_index=3
    )
    loc = facts[0].locator
    assert loc is not None
    assert loc.locator_version >= 2
    assert loc.effective_kind() == LocatorKind.FMP_JSON_TABLE
    assert loc.json_path == "[3].revenue"
    assert loc.table_cell is not None
    assert loc.table_cell.row_label == "revenue"
    assert loc.table_cell.column_header == "2025-12-31"
    assert loc.verbatim_snippet


def test_as_reported_locator_is_v2_on_fixture() -> None:
    from compute.as_reported import extract_facts_from_record
    from models.fmp_payloads import FmpAsReportedRecord

    record = FmpAsReportedRecord.model_validate(
        {
            "date": "2025-09-30",
            "symbol": "NOW",
            "reportedCurrency": "USD",
            "period": "Q3",
            "data": {"revenueremainingperformanceobligation": 21500000000},
        }
    )
    facts = extract_facts_from_record(record, source_doc_id=9, record_index=2)
    loc = facts[0].locator
    assert loc is not None
    assert loc.locator_version >= 2
    assert loc.effective_kind() == LocatorKind.FMP_JSON_TABLE
    assert loc.json_path == "[2].data.revenueremainingperformanceobligation"
    assert loc.table_cell is not None
    assert loc.table_cell.row_label == "revenueremainingperformanceobligation"


def test_sec_xbrl_locator_is_v2_on_fixture() -> None:
    """pipeline.sec_xbrl is enriched but writes via a lower-level SQL insert
    path rather than constructing a FinancialFact/KpiValue instance, so it
    sits outside the discovery scan above -- fixture-tested directly."""
    import sqlite3
    from datetime import datetime

    from models.facts import FactLocator
    from pipeline.sec_xbrl import insert_facts_from_companyfacts

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ticker TEXT NOT NULL, source_type TEXT NOT NULL, doc_type TEXT NOT NULL, "
        "period_end TIMESTAMP, file_path TEXT NOT NULL, sha256 TEXT NOT NULL, "
        "fetched_at TIMESTAMP NOT NULL, fetch_status TEXT NOT NULL, "
        "raw_bytes_size INTEGER NOT NULL);"
        "CREATE TABLE financial_facts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL, "
        "fiscal_period_type TEXT NOT NULL, line_item TEXT NOT NULL, "
        "value NUMERIC(24, 6) NOT NULL, currency TEXT, unit TEXT NOT NULL, "
        "source_doc_id INTEGER NOT NULL, confidence REAL NOT NULL DEFAULT 1.0, "
        "extracted_by TEXT, supersedes_id INTEGER, locator TEXT);"
        "CREATE UNIQUE INDEX uq_financial_facts_provenance ON financial_facts "
        "(ticker, period_end, fiscal_period_type, line_item, source_doc_id);"
    )
    conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, file_path, sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES ('GOOG', 'sec_xbrl', 'sec_10q', "
        "'data/historical/sec/GOOG_companyfacts.json#accn=0001-01-000001', 'b', ?, 'ok', 1)",
        (datetime.now(),),
    )
    doc_id = conn.execute("SELECT id FROM documents").fetchone()["id"]
    payload: dict[str, object] = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "accn": "0001-01-000001",
                                "end": "2025-03-31",
                                "start": "2025-01-01",
                                "val": 80500000000,
                                "fp": "Q1",
                            }
                        ]
                    }
                }
            }
        }
    }
    inserted = insert_facts_from_companyfacts(
        conn, ticker="GOOG", payload=payload, accession_to_doc_id={"0001-01-000001": doc_id}
    )
    assert inserted == 1
    row = conn.execute("SELECT locator FROM financial_facts").fetchone()
    loc = FactLocator.from_json(row["locator"])
    assert loc is not None
    assert loc.locator_version >= 2
    assert loc.effective_kind() == LocatorKind.FMP_JSON_TABLE
    assert loc.table_cell is not None
    assert loc.table_cell.row_label == "Revenues"
    assert loc.table_cell.column_header == "2025-03-31"
    conn.close()


def test_legacy_writer_stays_unenriched_by_construction() -> None:
    """The floor=1 s1_financials entry genuinely doesn't emit a locator at
    all today -- pinning it at 1 isn't an unverified claim."""
    from datetime import datetime
    from decimal import Decimal

    from compute.s1_financials import S1Datum, build_financial_facts
    from models.facts import FiscalPeriodType, Unit

    datum = S1Datum(
        line_item="revenue",
        period_end=datetime(2025, 12, 31),
        fiscal_period_type=FiscalPeriodType.FY,
        value=Decimal("100"),
        unit=Unit.ACTUAL,
        currency=None,
    )
    facts = build_financial_facts([datum], source_doc_id=1, ticker="TST")
    assert facts[0].locator is None
