"""Tests for src/compute/segments.py."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from compute.segments import (
    _passes_reconciliation,
    extract_facts_from_record,
    extract_segment_facts,
    insert_segment_facts,
)
from models.facts import (
    Currency,
    FactLocator,
    FiscalPeriodType,
    LocatorKind,
    SegmentDimension,
    SegmentDimType,
    Unit,
)
from models.fmp_payloads import FmpSegmentRecord
from pipeline.segment_junction_writer import write_segment_facts_junction

_PRODUCT_SAMPLE = {
    "date": "2025-12-31",
    "symbol": "GOOG",
    "reportedCurrency": "USD",
    "period": "FY",
    "fiscalYear": 2025,
    "data": {
        "Google Search & Other": 224_532_000_000,
        "YouTube Advertising Revenue": 40_367_000_000,
        "Google Cloud": 58_705_000_000,
        "Other Bets": 1_537_000_000,
    },
}

_GEO_SAMPLE = {
    "date": "2025-12-31",
    "symbol": "GOOG",
    "reportedCurrency": "USD",
    "period": "FY",
    "fiscalYear": 2025,
    "data": {
        "UNITED STATES": 194_229_000_000,
        "EMEA": 117_152_000_000,
        "Asia Pacific": 67_680_000_000,
    },
}


def _create_schema(conn: sqlite3.Connection) -> None:
    """Bootstrap the post-0056 schema: documents + financial_facts + the junction."""
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL,
            raw_bytes_size INTEGER NOT NULL
        );
        CREATE TABLE segment_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            period_end DATETIME NOT NULL,
            fiscal_period_type VARCHAR(8) NOT NULL,
            source_doc_id INTEGER NOT NULL REFERENCES documents(id),
            currency VARCHAR(8),
            unit VARCHAR(16) NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_segment_periods_provenance UNIQUE
              (ticker, period_end, fiscal_period_type, source_doc_id)
        );
        CREATE TABLE segment_dimensions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_id INTEGER NOT NULL REFERENCES segment_periods(id),
            dim_type VARCHAR(16) NOT NULL,
            dim_name VARCHAR(128) NOT NULL,
            value NUMERIC(20, 4) NOT NULL,
            metric VARCHAR(32) NOT NULL,
            segment_entity_id INTEGER
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            line_item TEXT NOT NULL,
            value NUMERIC(24, 6) NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0
        );
        """
    )
    conn.commit()


def _seed_revenue(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: str,
    period_type: str,
    value: int,
) -> None:
    conn.execute(
        "INSERT INTO financial_facts "
        "(ticker, period_end, fiscal_period_type, line_item, value, unit, source_doc_id) "
        "VALUES (?, ?, ?, 'revenue', ?, 'actual', 1)",
        (ticker, period_end, period_type, value),
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def test_extract_product_segment_facts() -> None:
    """One product segment record produces one row per segment with metric=revenue_by_product."""
    record = FmpSegmentRecord.model_validate(_PRODUCT_SAMPLE)
    facts = extract_facts_from_record(record, source_doc_id=10, metric="revenue_by_product")

    assert len(facts) == 4
    by_segment = {f.segment_name: f for f in facts}
    assert by_segment["Google Cloud"].value == Decimal("58705000000")
    assert by_segment["Google Cloud"].metric == "revenue_by_product"
    assert by_segment["Google Cloud"].currency == Currency.USD
    assert by_segment["Google Cloud"].unit == Unit.ACTUAL
    assert by_segment["Google Cloud"].fiscal_period_type == FiscalPeriodType.FY
    assert by_segment["Google Cloud"].period_end == datetime(2025, 12, 31)


def test_extract_geographic_segment_facts() -> None:
    """One geographic segment record produces one row per region."""
    record = FmpSegmentRecord.model_validate(_GEO_SAMPLE)
    facts = extract_facts_from_record(record, source_doc_id=11, metric="revenue_by_geography")

    assert len(facts) == 3
    segment_names = {f.segment_name for f in facts}
    assert "UNITED STATES" in segment_names
    assert "EMEA" in segment_names
    assert all(f.metric == "revenue_by_geography" for f in facts)


def test_extract_segment_facts_rejects_wrong_doc_type(
    conn: sqlite3.Connection,
) -> None:
    """Calling on a non-segment document_id raises."""
    conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, file_path, sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES ('GOOG', 'fmp', 'fmp_income_statement', 'x', 'a' || replace(hex(randomblob(31)), 'a', 'b'), ?, 'ok', 1)",
        (datetime.now(),),
    )
    conn.commit()
    document_id = conn.execute("SELECT id FROM documents").fetchone()["id"]

    with pytest.raises(ValueError, match="not one of"):
        extract_segment_facts(conn, document_id, project_root=Path("."))


def test_reconciliation_accepts_well_formed_record(conn: sqlite3.Connection) -> None:
    """Segment sum within ±10% of revenue passes the gate."""
    _seed_revenue(
        conn,
        ticker="GOOG",
        period_end="2025-12-31 00:00:00",
        period_type="FY",
        value=350_018_000_000,
    )
    record = FmpSegmentRecord.model_validate(_PRODUCT_SAMPLE)  # sum ≈ 325B; ratio ≈ 0.93
    assert _passes_reconciliation(conn, record, "revenue_by_product", source_doc_id=42) is True


def test_reconciliation_rejects_contaminated_record(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """The UBER Q4-2025 contamination pattern (sum ≈ 3.4x revenue) is rejected."""
    _seed_revenue(
        conn,
        ticker="UBER",
        period_end="2025-12-31 00:00:00",
        period_type="Q4",
        value=14_366_000_000,
    )
    contaminated = {
        "date": "2025-12-31",
        "symbol": "UBER",
        "reportedCurrency": "USD",
        "period": "Q4",
        "fiscalYear": 2025,
        "data": {
            "United States And Canada": 30_762_000_000,
            "EMEA": 15_367_000_000,
            "Asia Pacific": 1_625_000_000,
            "Latin America": 992_000_000,
        },
    }
    record = FmpSegmentRecord.model_validate(contaminated)
    assert _passes_reconciliation(conn, record, "revenue_by_geography", source_doc_id=99) is False
    err = capsys.readouterr().err
    log = json.loads(err.strip().splitlines()[-1])
    assert log["event"] == "segment_record_rejected"
    assert log["reason"] == "sum_exceeds_revenue"
    assert log["ticker"] == "UBER"
    assert log["ratio"] > 3.0


def test_reconciliation_accepts_under_revenue(conn: sqlite3.Connection) -> None:
    """Sum < revenue (missing 'Other' bucket case) always passes."""
    _seed_revenue(
        conn, ticker="ACN", period_end="2024-08-31 00:00:00", period_type="FY", value=64_896_000_000
    )
    record = FmpSegmentRecord.model_validate(
        {
            "date": "2024-08-31",
            "symbol": "ACN",
            "reportedCurrency": "USD",
            "period": "FY",
            "fiscalYear": 2024,
            "data": {"North America": 30_000_000_000, "EMEA": 18_000_000_000},  # ratio 0.74
        }
    )
    assert _passes_reconciliation(conn, record, "revenue_by_geography", source_doc_id=7) is True


def test_reconciliation_no_revenue_accepts(conn: sqlite3.Connection) -> None:
    """If income_statement hasn't been ingested yet, can't disprove — accept."""
    record = FmpSegmentRecord.model_validate(_GEO_SAMPLE)
    assert _passes_reconciliation(conn, record, "revenue_by_geography", source_doc_id=1) is True


def test_insert_segment_facts_writes_to_junction(conn: sqlite3.Connection) -> None:
    """insert_segment_facts now persists into segment_periods + segment_dimensions.

    Regression for the 0056 cutover — before the rewrite this wrote to a
    `segment_facts` table that no longer exists. The function still accepts
    legacy `SegmentFact` objects so callers (e.g. compute.segment_oi_10k) keep
    their existing pydantic-shaped extractors; the persistence path is what
    changed.
    """
    # Document FK target for the junction's source_doc_id constraint.
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, file_path, sha256, fetched_at, "
        " fetch_status, raw_bytes_size) "
        "VALUES (?, 'GOOG', 'fmp', 'fmp_segment_product', 'x.json', "
        " '0000000000000000000000000000000000000000000000000000000000000000', "
        " ?, 'ok', 1)",
        (42, datetime.now()),
    )
    conn.commit()

    record = FmpSegmentRecord.model_validate(_PRODUCT_SAMPLE)
    facts = extract_facts_from_record(record, source_doc_id=42, metric="revenue_by_product")
    inserted = insert_segment_facts(conn, facts)
    conn.commit()

    assert inserted == 4  # one dim per segment in the FMP record
    assert conn.execute("SELECT COUNT(*) FROM segment_periods").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM segment_dimensions").fetchone()[0] == 4
    # dim_type should reflect the legacy → junction mapping for product revenue
    dims = conn.execute(
        "SELECT dim_type, dim_name, metric FROM segment_dimensions ORDER BY dim_name"
    ).fetchall()
    assert all(d["dim_type"] == "product" and d["metric"] == "revenue" for d in dims)


def test_reconciliation_at_tolerance_boundary(conn: sqlite3.Connection) -> None:
    """Exactly 10% over revenue is the cap — values at or below the cap accept."""
    _seed_revenue(
        conn, ticker="TEST", period_end="2024-12-31 00:00:00", period_type="FY", value=100_000_000
    )
    record = FmpSegmentRecord.model_validate(
        {
            "date": "2024-12-31",
            "symbol": "TEST",
            "reportedCurrency": "USD",
            "period": "FY",
            "fiscalYear": 2024,
            "data": {"A": 60_000_000, "B": 50_000_000},  # sum=110M; ratio=1.10 (==cap)
        }
    )
    assert _passes_reconciliation(conn, record, "revenue_by_product", source_doc_id=1) is True

    record_over = FmpSegmentRecord.model_validate(
        {
            "date": "2024-12-31",
            "symbol": "TEST",
            "reportedCurrency": "USD",
            "period": "FY",
            "fiscalYear": 2024,
            "data": {"A": 60_000_000, "B": 51_000_000},  # sum=111M; ratio=1.11 (>cap)
        }
    )
    assert _passes_reconciliation(conn, record_over, "revenue_by_product", source_doc_id=1) is False


# ---------------------------------------------------------------------------
# Locator provenance — this extractor is the writer used for legacy per-ticker
# quarterly segment JSON ingest (e.g. data/historical/fmp/MELI_product_
# segments_quarterly.json / MELI_geo_segments_quarterly.json). It predates the
# locator contract (migration 0165), so every row it ever wrote landed with
# segment_dimensions.locator NULL. These tests prove (a) a fresh ingest now
# stamps a renderable v2 `fmp_json_table` locator, and (b) re-ingesting over
# an already-landed pre-fix row (locator NULL) heals it in place rather than
# duplicating the row — the actual point of this change, per
# docs/design/provenance_clickthrough.md's "do not invent a second locator
# shape for segments" and its segment_dimensions.locator gap.
# ---------------------------------------------------------------------------

_PROVENANCE_SCHEMA = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL,
    raw_bytes_size INTEGER NOT NULL
);
CREATE TABLE segment_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type VARCHAR(8) NOT NULL,
    source_doc_id INTEGER NOT NULL REFERENCES documents(id),
    currency VARCHAR(8),
    unit VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    period_basis VARCHAR(16) NOT NULL DEFAULT 'discrete',
    raw_period_label TEXT,
    method_version VARCHAR(32),
    CONSTRAINT uq_segment_periods_provenance UNIQUE
      (ticker, period_end, fiscal_period_type, source_doc_id)
);
CREATE TABLE segment_dimensions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id INTEGER NOT NULL REFERENCES segment_periods(id),
    dim_type VARCHAR(16) NOT NULL,
    dim_name VARCHAR(128) NOT NULL,
    value NUMERIC(20, 4) NOT NULL,
    metric VARCHAR(32) NOT NULL,
    unit VARCHAR(16),
    disclosure_status VARCHAR(16) NOT NULL DEFAULT 'reported',
    method_version VARCHAR(32),
    confidence REAL NOT NULL DEFAULT 1.0,
    extracted_by VARCHAR(64),
    locator TEXT,
    derived_from TEXT,
    supersedes_id INTEGER
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value NUMERIC(24, 6) NOT NULL,
    currency TEXT,
    unit TEXT NOT NULL,
    source_doc_id INTEGER NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0
);
"""


@pytest.fixture
def prov_conn() -> sqlite3.Connection:
    """A post-0165/0166 schema (segment_periods + segment_dimensions carry
    the full provenance column set) — distinct from the module-level `conn`
    fixture, whose schema predates those migrations and is kept as-is so the
    existing pre-provenance-column tests above keep exercising the writer's
    graceful-degrade path."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_PROVENANCE_SCHEMA)
    c.commit()
    return c


def _write_meli_cache(project_root: Path, record: dict[str, object]) -> None:
    fmp_dir = project_root / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)
    (fmp_dir / "MELI_product_segments_quarterly.json").write_text(
        json.dumps([record]), encoding="utf-8"
    )


def _insert_meli_document(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, file_path, sha256, fetched_at, "
        " fetch_status, raw_bytes_size) "
        "VALUES (1, 'MELI', 'fmp', 'fmp_segment_product', "
        "'data/historical/fmp/MELI_product_segments_quarterly.json', "
        "'0000000000000000000000000000000000000000000000000000000000000000', "
        "?, 'ok', 1)",
        (datetime.now(),),
    )
    conn.commit()


_MELI_RECORD: dict[str, object] = {
    "date": "2025-12-31",
    "symbol": "MELI",
    "reportedCurrency": "USD",
    "period": "Q4",
    "fiscalYear": 2025,
    "data": {"Commerce": 4_000_000_000, "Fintech": 2_000_000_000},
}


def test_extract_segment_facts_emits_v2_fmp_json_table_locator(
    prov_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A fresh ingest stamps a renderable `fmp_json_table` locator on every
    dim row -- `json_path` points at the exact cell of the cached FMP
    response, same convention as `compute.as_reported.extract_facts_from_record`
    for the identical `data: {name -> value}` envelope shape."""
    conn = prov_conn
    _insert_meli_document(conn)
    _write_meli_cache(tmp_path, _MELI_RECORD)

    inserted = extract_segment_facts(conn, 1, tmp_path)
    assert inserted == 2

    rows = conn.execute(
        "SELECT dim_name, locator, extracted_by FROM segment_dimensions ORDER BY dim_name"
    ).fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["extracted_by"] == "fmp_segment_quarterly_v1"
        loc = FactLocator.from_json(row["locator"])
        assert loc is not None
        assert loc.locator_version >= 2
        assert loc.effective_kind() == LocatorKind.FMP_JSON_TABLE
        assert loc.table_cell is not None
        assert loc.table_cell.row_label == row["dim_name"]
        assert loc.table_cell.column_header == "2025-12-31"
        assert loc.json_path == f"[0].data.{row['dim_name']}"
        assert loc.verbatim_snippet


def test_extract_segment_facts_backfills_null_locator_on_reingest(
    prov_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The point of this change: a row landed by the PRE-fix version of this
    same extractor (locator NULL — the legacy-ingested shape the bug report
    described) gets healed in place the next time the source document is
    re-ingested. No duplicate row is created (`inserted == 0`, since the
    natural key already matches), and a locator that already exists is never
    clobbered by a later write."""
    conn = prov_conn
    _insert_meli_document(conn)
    _write_meli_cache(tmp_path, _MELI_RECORD)

    # Simulate the PRE-fix legacy state directly: same (ticker, period,
    # source_doc, dim) natural key the real extractor will compute below, but
    # written with no locator at all -- exactly what every row this extractor
    # ever wrote before this change looked like.
    write_segment_facts_junction(
        conn,
        ticker="MELI",
        period_end=datetime(2025, 12, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        source_doc_id=1,
        currency=Currency.USD,
        unit=Unit.ACTUAL,
        dimensions=[
            SegmentDimension(
                dim_type=SegmentDimType.PRODUCT,
                dim_name="Commerce",
                value=Decimal("4000000000"),
                metric="revenue",
            ),
            SegmentDimension(
                dim_type=SegmentDimType.PRODUCT,
                dim_name="Fintech",
                value=Decimal("2000000000"),
                metric="revenue",
            ),
        ],
    )
    conn.commit()
    pre_rows = conn.execute(
        "SELECT dim_name, locator FROM segment_dimensions ORDER BY dim_name"
    ).fetchall()
    assert len(pre_rows) == 2
    assert all(r["locator"] is None for r in pre_rows), "precondition: legacy rows carry no locator"

    # Re-ingest the SAME source document through the (now-fixed) extractor.
    inserted = extract_segment_facts(conn, 1, tmp_path)
    assert inserted == 0, "no NEW dim rows -- the natural key already existed"

    post_rows = conn.execute(
        "SELECT dim_name, locator FROM segment_dimensions ORDER BY dim_name"
    ).fetchall()
    assert len(post_rows) == 2, "re-ingest must not duplicate rows"
    for row in post_rows:
        loc = FactLocator.from_json(row["locator"])
        assert loc is not None, f"{row['dim_name']} locator was not backfilled"
        assert loc.effective_kind() == LocatorKind.FMP_JSON_TABLE
        assert loc.table_cell is not None
        assert loc.table_cell.row_label == row["dim_name"]
