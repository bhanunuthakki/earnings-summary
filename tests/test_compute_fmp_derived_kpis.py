"""Tests for src/compute/fmp_derived_kpis.py — KPI derivation from financial_facts."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from compute.fmp_derived_kpis import (
    KPI_GROSS_MARGIN_GAAP,
    KPI_NET_MARGIN_GAAP,
    KPI_OPERATING_MARGIN_GAAP,
    KPI_REVENUE_YOY_USD,
    QuarterlyFacts,
    derive_for_facts,
    derive_for_ticker,
)
from models.facts import FiscalPeriodType, Unit


def _create_schema(conn: sqlite3.Connection) -> None:
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
            confidence REAL DEFAULT 1.0
        );
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            unit TEXT NOT NULL,
            primary_source TEXT NOT NULL,
            fallback_source TEXT,
            ir_url TEXT,
            threshold_tier TEXT,
            threshold_low REAL,
            threshold_high REAL,
            notes TEXT,
            UNIQUE(ticker, name)
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            kpi_definition_id INTEGER NOT NULL,
            value NUMERIC(24, 6) NOT NULL,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX uq_kpi_facts_provenance
        ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id);
        """
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def _insert_doc(
    conn: sqlite3.Connection, ticker: str, file_path: str
) -> int:
    cur = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES (?, 'fmp', 'fmp_income_statement', ?, ?, ?, 'ok', 1)",
        (ticker, file_path, "a" * 64, datetime.now()),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _insert_fact(
    conn: sqlite3.Connection,
    *,
    ticker: str, period_end: datetime, fpt: str, line_item: str,
    value: float, source_doc_id: int,
) -> None:
    conn.execute(
        "INSERT INTO financial_facts "
        "(ticker, period_end, fiscal_period_type, line_item, value, currency, unit, source_doc_id) "
        "VALUES (?, ?, ?, ?, ?, 'USD', 'actual', ?)",
        (ticker, period_end, fpt, line_item, str(value), source_doc_id),
    )


def test_derive_for_facts_emits_three_margin_metrics_per_period() -> None:
    """One QuarterlyFacts -> 3 margin rows (no YoY without prior year)."""
    facts = [
        QuarterlyFacts(
            ticker="MELI", period_end=datetime(2024, 12, 31),
            fiscal_period_type=FiscalPeriodType.Q4,
            revenue=Decimal("6059000000"),
            operating_income=Decimal("820000000"),
            net_income=Decimal("639000000"),
            gross_profit=Decimal("2749000000"),
            source_doc_id=42,
        )
    ]
    rows = derive_for_facts(facts)
    by_name = {r.name: r for r in rows}
    assert KPI_OPERATING_MARGIN_GAAP in by_name
    assert KPI_NET_MARGIN_GAAP in by_name
    assert KPI_GROSS_MARGIN_GAAP in by_name
    # YoY needs a prior year — not present yet
    assert KPI_REVENUE_YOY_USD not in by_name


def test_derive_operating_margin_value_is_correct() -> None:
    """820M / 6059M = 13.535...% — matches the MELI Q4'24 published number 13.5%."""
    facts = [
        QuarterlyFacts(
            ticker="MELI", period_end=datetime(2024, 12, 31),
            fiscal_period_type=FiscalPeriodType.Q4,
            revenue=Decimal("6059000000"),
            operating_income=Decimal("820000000"),
            net_income=Decimal("639000000"),
            gross_profit=Decimal("2749000000"),
            source_doc_id=42,
        )
    ]
    rows = derive_for_facts(facts)
    op_margin = next(r for r in rows if r.name == KPI_OPERATING_MARGIN_GAAP)
    assert Decimal("13.5") < op_margin.value < Decimal("13.6")
    assert op_margin.unit == Unit.PERCENT
    assert op_margin.fiscal_period_type == FiscalPeriodType.Q4


def test_derive_revenue_yoy_compares_same_quarter_one_year_apart() -> None:
    """Q4'24 vs Q4'23: 96% if revenue went 100 -> 196."""
    facts = [
        QuarterlyFacts(
            ticker="X", period_end=datetime(2023, 12, 31),
            fiscal_period_type=FiscalPeriodType.Q4,
            revenue=Decimal("100"), operating_income=Decimal("10"),
            net_income=Decimal("8"), gross_profit=Decimal("40"), source_doc_id=1,
        ),
        QuarterlyFacts(
            ticker="X", period_end=datetime(2024, 12, 31),
            fiscal_period_type=FiscalPeriodType.Q4,
            revenue=Decimal("196"), operating_income=Decimal("20"),
            net_income=Decimal("15"), gross_profit=Decimal("80"), source_doc_id=2,
        ),
    ]
    rows = derive_for_facts(facts)
    yoy = [r for r in rows if r.name == KPI_REVENUE_YOY_USD]
    assert len(yoy) == 1
    assert yoy[0].value == Decimal("96")
    assert yoy[0].period_end == datetime(2024, 12, 31)


def test_derive_skips_yoy_when_year_gap_is_not_exactly_one() -> None:
    """If only Q4'23 and Q4'25 exist (gap year), no YoY emitted."""
    facts = [
        QuarterlyFacts(
            ticker="X", period_end=datetime(2023, 12, 31),
            fiscal_period_type=FiscalPeriodType.Q4,
            revenue=Decimal("100"), operating_income=Decimal("10"),
            net_income=Decimal("8"), gross_profit=Decimal("40"), source_doc_id=1,
        ),
        QuarterlyFacts(
            ticker="X", period_end=datetime(2025, 12, 31),
            fiscal_period_type=FiscalPeriodType.Q4,
            revenue=Decimal("200"), operating_income=Decimal("20"),
            net_income=Decimal("15"), gross_profit=Decimal("80"), source_doc_id=2,
        ),
    ]
    rows = derive_for_facts(facts)
    yoy = [r for r in rows if r.name == KPI_REVENUE_YOY_USD]
    assert yoy == []


def test_derive_for_ticker_filters_to_quarterly_files(conn: sqlite3.Connection) -> None:
    """Only documents whose file_path ends in _quarterly.json contribute to derivation."""
    quarterly_doc = _insert_doc(conn, "X", "data/historical/fmp/X_income_statement_quarterly.json")
    annual_doc = _insert_doc(conn, "X", "data/historical/fmp/X_income_statement.json")
    ttm_doc = _insert_doc(conn, "X", "data/historical/fmp/X_income_statement_ttm.json")

    pe = datetime(2024, 12, 31)
    for line, val in [("revenue", 100), ("operating_income", 20), ("net_income", 15), ("gross_profit", 40)]:
        _insert_fact(conn, ticker="X", period_end=pe, fpt="Q4", line_item=line, value=val, source_doc_id=quarterly_doc)
        # Annual + TTM rows would pollute if not filtered
        _insert_fact(conn, ticker="X", period_end=pe, fpt="Q4", line_item=line, value=val * 10, source_doc_id=annual_doc)
        _insert_fact(conn, ticker="X", period_end=pe, fpt="Q4", line_item=line, value=val * 4, source_doc_id=ttm_doc)
    conn.commit()

    emitted, inserted = derive_for_ticker(conn, "X")
    assert emitted == 3  # OpMargin + NetMargin + GrossMargin (no YoY without prior year)
    assert inserted == 3
    rows = conn.execute(
        "SELECT kd.name, kf.value FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
        "WHERE kf.ticker='X' ORDER BY kd.name"
    ).fetchall()
    by_name = {dict(r)["name"]: Decimal(str(dict(r)["value"])) for r in rows}
    assert by_name[KPI_OPERATING_MARGIN_GAAP] == Decimal("20")  # 20/100*100
    assert by_name[KPI_NET_MARGIN_GAAP] == Decimal("15")        # 15/100*100
    assert by_name[KPI_GROSS_MARGIN_GAAP] == Decimal("40")      # 40/100*100


def test_derive_for_ticker_idempotent(conn: sqlite3.Connection) -> None:
    """Re-running derivation inserts no duplicates (UNIQUE index dedupes)."""
    doc_id = _insert_doc(conn, "Y", "data/historical/fmp/Y_income_statement_quarterly.json")
    pe = datetime(2024, 12, 31)
    for line, val in [("revenue", 100), ("operating_income", 20), ("net_income", 15), ("gross_profit", 40)]:
        _insert_fact(conn, ticker="Y", period_end=pe, fpt="Q4", line_item=line, value=val, source_doc_id=doc_id)
    conn.commit()

    _, inserted_first = derive_for_ticker(conn, "Y")
    _, inserted_second = derive_for_ticker(conn, "Y")
    assert inserted_first == 3
    assert inserted_second == 0


def test_derive_for_ticker_skips_period_missing_required_line_items(
    conn: sqlite3.Connection,
) -> None:
    """If a period is missing one of revenue/op_income/net_income/gross_profit, it's skipped (no partial derivation)."""
    doc_id = _insert_doc(conn, "Z", "data/historical/fmp/Z_income_statement_quarterly.json")
    pe = datetime(2024, 12, 31)
    # Missing gross_profit
    for line, val in [("revenue", 100), ("operating_income", 20), ("net_income", 15)]:
        _insert_fact(conn, ticker="Z", period_end=pe, fpt="Q4", line_item=line, value=val, source_doc_id=doc_id)
    conn.commit()

    emitted, inserted = derive_for_ticker(conn, "Z")
    assert emitted == 0
    assert inserted == 0


def test_derive_for_ticker_skips_when_revenue_zero(conn: sqlite3.Connection) -> None:
    """Zero revenue would divide by zero — period is skipped, no validation error."""
    doc_id = _insert_doc(conn, "Z", "data/historical/fmp/Z_income_statement_quarterly.json")
    pe = datetime(2024, 12, 31)
    for line, val in [("revenue", 0), ("operating_income", 20), ("net_income", 15), ("gross_profit", 40)]:
        _insert_fact(conn, ticker="Z", period_end=pe, fpt="Q4", line_item=line, value=val, source_doc_id=doc_id)
    conn.commit()
    emitted, _inserted = derive_for_ticker(conn, "Z")
    assert emitted == 0


def test_derive_drops_cashflow_when_all_three_line_items_are_zero(
    conn: sqlite3.Connection,
) -> None:
    """FMP coverage-gap signature: OCF=Capex=FCF=0 with non-zero revenue means
    the upstream parser failed (Nintendo's JP filing format, HDFC Bank's IN
    format). Treat all three as missing so FCF Margin / Capex Ratio / OCF YoY
    are NOT emitted as 0% / 0% / -100% — instead they're omitted entirely.
    Income-statement margins still emit normally."""
    doc_id = _insert_doc(conn, "NTDOY", "data/historical/fmp/NTDOY_income_statement_quarterly.json")
    pe = datetime(2024, 12, 31)
    for line, val in [
        ("revenue", 432_919_000_000),
        ("operating_income", 100_000_000_000),
        ("net_income", 80_000_000_000),
        ("gross_profit", 200_000_000_000),
        ("operating_cash_flow", 0),
        ("capital_expenditure", 0),
        ("free_cash_flow", 0),
    ]:
        _insert_fact(conn, ticker="NTDOY", period_end=pe, fpt="Q3", line_item=line, value=val, source_doc_id=doc_id)
    conn.commit()

    emitted, inserted = derive_for_ticker(conn, "NTDOY")
    # Should emit only the three income-statement margins, NOT FCF margin
    # or Capex/Revenue. (No YoY without a prior year.)
    assert emitted == 3
    assert inserted == 3
    names = [
        r["name"] for r in conn.execute(
            "SELECT kd.name FROM kpi_facts kf JOIN kpi_definitions kd "
            "ON kd.id = kf.kpi_definition_id WHERE kf.ticker='NTDOY'"
        ).fetchall()
    ]
    assert "FCF Margin (GAAP)" not in names
    assert "Capex / Revenue (GAAP)" not in names


def test_derive_does_not_drop_cashflow_when_only_capex_is_zero(
    conn: sqlite3.Connection,
) -> None:
    """An asset-light business with capex=0 but real OCF and FCF should NOT be
    treated as a coverage gap. Only ALL THREE simultaneously zero is the FMP
    parser-failure signature."""
    doc_id = _insert_doc(conn, "Y", "data/historical/fmp/Y_income_statement_quarterly.json")
    pe = datetime(2024, 12, 31)
    for line, val in [
        ("revenue", 100),
        ("operating_income", 20),
        ("net_income", 15),
        ("gross_profit", 40),
        ("operating_cash_flow", 18),  # real
        ("capital_expenditure", 0),   # asset-light, legitimately zero
        ("free_cash_flow", 18),       # real
    ]:
        _insert_fact(conn, ticker="Y", period_end=pe, fpt="Q4", line_item=line, value=val, source_doc_id=doc_id)
    conn.commit()

    derive_for_ticker(conn, "Y")
    names = [
        r["name"] for r in conn.execute(
            "SELECT kd.name FROM kpi_facts kf JOIN kpi_definitions kd "
            "ON kd.id = kf.kpi_definition_id WHERE kf.ticker='Y'"
        ).fetchall()
    ]
    # FCF Margin must still be emitted (FCF is real, just capex happens to be 0)
    assert "FCF Margin (GAAP)" in names
    # Capex / Revenue can still be emitted as 0% — that's a legitimate value
    # for an asset-light quarter
    assert "Capex / Revenue (GAAP)" in names


def test_derive_segment_kpis_amzn(conn: sqlite3.Connection) -> None:
    # 1. Create the junction tables — the post-0056 home for segment data.
    conn.executescript(
        """
        CREATE TABLE segment_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            period_end DATETIME NOT NULL,
            fiscal_period_type VARCHAR(8) NOT NULL,
            source_doc_id INTEGER NOT NULL,
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
        """
    )

    doc_id = _insert_doc(conn, "AMZN", "data/historical/fmp/AMZN_income_statement.json")

    # Seed the junction with AWS + North America cells for Q4 2023 and Q4 2024.
    # Each (ticker, period_end, fpt, source_doc_id) tuple gets one
    # segment_periods row; each dim cell is one segment_dimensions row.
    # The reader's CASE maps (product, revenue) → "revenue_by_product" and
    # (business_unit, operating_income) → "operating_income".
    periods = [
        ("AMZN", datetime(2023, 12, 31), "Q4"),
        ("AMZN", datetime(2024, 12, 31), "Q4"),
    ]
    period_ids: dict[tuple[str, datetime, str], int] = {}
    for ticker, pe, fpt in periods:
        cur = conn.execute(
            "INSERT INTO segment_periods "
            "(ticker, period_end, fiscal_period_type, source_doc_id, currency, unit) "
            "VALUES (?, ?, ?, ?, 'USD', 'actual')",
            (ticker, pe, fpt, doc_id),
        )
        assert cur.lastrowid is not None
        period_ids[(ticker, pe, fpt)] = cur.lastrowid

    dims = [
        # (period_key, dim_type, dim_name, junction_metric, value)
        (("AMZN", datetime(2023, 12, 31), "Q4"), "product", "AWS", "revenue", 20_000_000_000),
        (("AMZN", datetime(2023, 12, 31), "Q4"), "business_unit", "AWS", "operating_income", 5_000_000_000),
        (("AMZN", datetime(2024, 12, 31), "Q4"), "product", "AWS", "revenue", 30_000_000_000),
        (("AMZN", datetime(2024, 12, 31), "Q4"), "business_unit", "AWS", "operating_income", 12_000_000_000),
        (("AMZN", datetime(2024, 12, 31), "Q4"), "product", "North America", "revenue", 100_000_000_000),
        (("AMZN", datetime(2024, 12, 31), "Q4"), "business_unit", "North America", "operating_income", 5_000_000_000),
    ]
    conn.executemany(
        "INSERT INTO segment_dimensions "
        "(period_id, dim_type, dim_name, metric, value) VALUES (?, ?, ?, ?, ?)",
        [
            (period_ids[k], dim_type, dim_name, metric, value)
            for (k, dim_type, dim_name, metric, value) in dims
        ],
    )
    conn.commit()

    from compute.fmp_derived_kpis import _derive_segment_kpis
    derived = _derive_segment_kpis(conn, "AMZN")
    
    by_name_and_pe: dict[tuple[str, datetime], Decimal] = {
        (r.name, r.period_end): r.value for r in derived
    }
    
    assert by_name_and_pe[("AWS operating margin", datetime(2023, 12, 31))] == Decimal("25")
    assert by_name_and_pe[("AWS Operating Margin", datetime(2023, 12, 31))] == Decimal("25")
    
    assert by_name_and_pe[("AWS operating margin", datetime(2024, 12, 31))] == Decimal("40")
    assert by_name_and_pe[("AWS Operating Margin", datetime(2024, 12, 31))] == Decimal("40")
    
    assert by_name_and_pe[("North America retail operating margin", datetime(2024, 12, 31))] == Decimal("5")
    assert by_name_and_pe[("North America Retail Operating Margin", datetime(2024, 12, 31))] == Decimal("5")
    
    assert by_name_and_pe[("AWS revenue growth (YoY)", datetime(2024, 12, 31))] == Decimal("50")
    assert by_name_and_pe[("AWS Revenue YoY Growth", datetime(2024, 12, 31))] == Decimal("50")
