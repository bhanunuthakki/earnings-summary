"""DB-backed tests for src/compute/metrics_engine/io.py.

Synthetic in-memory sqlite schema only (no prod data, per repo convention --
mirrors tests/test_compute_fmp_derived_kpis.py's _create_schema pattern).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from compute.metrics_engine.io import compute_for_ticker, resolve_classification
from compute.metrics_engine.registry import ReasonCode
from models.companies import AccountingStandard, BusinessModelClass


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
            raw_bytes_size INTEGER NOT NULL,
            source_quality_tier VARCHAR(32) DEFAULT 'fmp_normalized' NOT NULL
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
            source_doc_id INTEGER NOT NULL,
            confidence REAL DEFAULT 1.0,
            extracted_by VARCHAR(64),
            supersedes_id INTEGER,
            locator TEXT,
            computed_from TEXT,
            formula_id INTEGER,
            formula_version INTEGER
        );
        CREATE UNIQUE INDEX uq_kpi_facts_provenance
        ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id);
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            business_model_class TEXT NOT NULL DEFAULT 'operating_company',
            accounting_standard TEXT NOT NULL DEFAULT 'us_gaap'
        );
        CREATE TABLE formula_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            formula_key TEXT NOT NULL,
            version INTEGER NOT NULL,
            category TEXT NOT NULL,
            display_formula TEXT NOT NULL,
            method_notes TEXT NOT NULL,
            period_grid TEXT NOT NULL,
            unit TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            UNIQUE(formula_key, version)
        );
        CREATE TABLE metric_computation_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            formula_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            reason_code TEXT,
            reason_detail TEXT,
            kpi_fact_id INTEGER,
            input_fingerprint TEXT NOT NULL,
            engine_version TEXT NOT NULL,
            computed_at TIMESTAMP NOT NULL
        );
        CREATE UNIQUE INDEX uq_metric_computation_attempts_logical
        ON metric_computation_attempts (ticker, period_end, fiscal_period_type, formula_id);
        """
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def _insert_doc(conn: sqlite3.Connection, ticker: str, period_end: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size) "
        "VALUES (?, 'fmp', 'fmp_income_statement', ?, ?, ?, 'ok', 1)",
        (ticker, f"{ticker}_{period_end.date()}.json", "a" * 64, datetime.now()),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _insert_fact(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: datetime,
    fpt: str,
    line_item: str,
    value: str,
    source_doc_id: int,
) -> None:
    conn.execute(
        "INSERT INTO financial_facts "
        "(ticker, period_end, fiscal_period_type, line_item, value, currency, unit, source_doc_id) "
        "VALUES (?, ?, ?, ?, ?, 'USD', 'actual', ?)",
        (ticker, period_end, fpt, line_item, value, source_doc_id),
    )


# 8 consecutive calendar quarters (2 years), revenue growing 10% YoY on
# each same-quarter comparison, so revenue_yoy has a real value from the
# 5th quarter onward. Values are small round numbers for easy assertion.
_QUARTERS: list[tuple[str, str]] = [
    ("2022-03-31", "Q1"),
    ("2022-06-30", "Q2"),
    ("2022-09-30", "Q3"),
    ("2022-12-31", "Q4"),
    ("2023-03-31", "Q1"),
    ("2023-06-30", "Q2"),
    ("2023-09-30", "Q3"),
    ("2023-12-31", "Q4"),
]


def _seed_operating_company(conn: sqlite3.Connection, ticker: str) -> None:
    """Full line-item set for every Phase-1 formula, growing simply per quarter."""
    for i, (pe_str, fpt) in enumerate(_QUARTERS):
        pe = datetime.fromisoformat(pe_str)
        doc_id = _insert_doc(conn, ticker, pe)
        # Revenue grows 10% YoY (quarter i vs quarter i-4); a flat 100 base
        # scaled by 1.1 per elapsed year keeps every same-quarter comparison
        # an exact +10%.
        year_index = i // 4
        revenue = Decimal("1000") * (Decimal("1.1") ** year_index)
        facts: dict[str, str] = {
            "revenue": str(revenue),
            "gross_profit": str(revenue * Decimal("0.4")),
            "operating_income": str(revenue * Decimal("0.2")),
            "net_income": str(revenue * Decimal("0.1")),
            "depreciation_and_amortization": str(revenue * Decimal("0.05")),
            "free_cash_flow": str(revenue * Decimal("0.08")),
            "stock_based_compensation": str(revenue * Decimal("0.02")),
            "eps_diluted": str(Decimal("1.00") * (Decimal("1.1") ** year_index)),
            "total_current_assets": "500",
            "total_current_liabilities": "250",
            "cash_and_equivalents": "200",
            "total_debt": "300",
            "total_stockholders_equity": "2000",
            "total_assets": "3000",
        }
        for line_item, value in facts.items():
            _insert_fact(
                conn,
                ticker=ticker,
                period_end=pe,
                fpt=fpt,
                line_item=line_item,
                value=value,
                source_doc_id=doc_id,
            )
    conn.commit()


def _set_classification(
    conn: sqlite3.Connection, ticker: str, business_model: str, standard: str
) -> None:
    conn.execute(
        "INSERT INTO tracked_companies (ticker, business_model_class, accounting_standard) "
        "VALUES (?, ?, ?)",
        (ticker, business_model, standard),
    )
    conn.commit()


def _attempts(conn: sqlite3.Connection, ticker: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT mca.*, fd.formula_key FROM metric_computation_attempts mca "
        "JOIN formula_definitions fd ON fd.id = mca.formula_id "
        "WHERE mca.ticker = ?",
        (ticker,),
    ).fetchall()


def _latest_attempt(conn: sqlite3.Connection, ticker: str, formula_key: str) -> sqlite3.Row:
    rows = [r for r in _attempts(conn, ticker) if r["formula_key"] == formula_key]
    rows.sort(key=lambda r: str(r["period_end"]))
    return rows[-1]


def test_resolve_classification_defaults_on_legacy_schema() -> None:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE tracked_companies (id INTEGER PRIMARY KEY, ticker TEXT)")
    business_model, standard = resolve_classification(c, "XYZ")
    assert business_model is BusinessModelClass.OPERATING_COMPANY
    assert standard is AccountingStandard.US_GAAP


def test_resolve_classification_reads_seeded_row(conn: sqlite3.Connection) -> None:
    _set_classification(conn, "NU", "bank", "ifrs")
    business_model, standard = resolve_classification(conn, "NU")
    assert business_model is BusinessModelClass.BANK
    assert standard is AccountingStandard.IFRS


def test_compute_for_ticker_writes_expected_gross_margin(conn: sqlite3.Connection) -> None:
    _seed_operating_company(conn, "TEST")
    _set_classification(conn, "TEST", "operating_company", "us_gaap")
    summary = compute_for_ticker(conn, "TEST")
    assert summary.quarters_seen == 8
    assert summary.computed_ok > 0

    row = _latest_attempt(conn, "TEST", "gross_margin")
    assert row["status"] == "ok"
    kpi_fact = conn.execute(
        "SELECT value, formula_id, formula_version FROM kpi_facts WHERE id = ?",
        (row["kpi_fact_id"],),
    ).fetchone()
    # gross_profit is 40% of revenue in the fixture -> gross_margin = 40%.
    assert Decimal(str(kpi_fact["value"])) == Decimal("40")
    assert kpi_fact["formula_id"] == row["formula_id"]
    assert kpi_fact["formula_version"] == 1


def test_compute_for_ticker_revenue_yoy_is_10_percent(conn: sqlite3.Connection) -> None:
    _seed_operating_company(conn, "TEST")
    _set_classification(conn, "TEST", "operating_company", "us_gaap")
    compute_for_ticker(conn, "TEST")

    row = _latest_attempt(conn, "TEST", "revenue_yoy")
    assert row["status"] == "ok"
    kpi_fact = conn.execute(
        "SELECT value FROM kpi_facts WHERE id = ?", (row["kpi_fact_id"],)
    ).fetchone()
    assert Decimal(str(kpi_fact["value"])) == Decimal("10.0")


def test_compute_for_ticker_roe_uses_ttm_net_income(conn: sqlite3.Connection) -> None:
    _seed_operating_company(conn, "TEST")
    _set_classification(conn, "TEST", "operating_company", "us_gaap")
    compute_for_ticker(conn, "TEST")

    row = _latest_attempt(conn, "TEST", "roe")
    assert row["status"] == "ok"
    kpi_fact = conn.execute(
        "SELECT value, unit FROM kpi_facts WHERE id = ?", (row["kpi_fact_id"],)
    ).fetchone()
    # TTM net_income = sum of the last 4 quarters' net_income (all in the
    # second year, so 4 * revenue_year2 * 0.1); equity is flat 2000.
    revenue_year2 = Decimal("1000") * Decimal("1.1")
    ttm_net_income = revenue_year2 * Decimal("0.1") * 4
    expected_roe = (ttm_net_income / Decimal("2000")) * Decimal(100)
    assert Decimal(str(kpi_fact["value"])) == expected_roe


def test_bank_excludes_gross_margin_but_keeps_roe(conn: sqlite3.Connection) -> None:
    _seed_operating_company(conn, "NU")
    _set_classification(conn, "NU", "bank", "us_gaap")
    compute_for_ticker(conn, "NU")

    gross_row = _latest_attempt(conn, "NU", "gross_margin")
    assert gross_row["status"] == "not_computable"
    assert gross_row["reason_code"] == ReasonCode.NOT_APPLICABLE_BUSINESS_MODEL.value

    roe_row = _latest_attempt(conn, "NU", "roe")
    assert roe_row["status"] == "ok"


def test_ifrs_ticker_gets_missing_input_mapping_for_every_formula(
    conn: sqlite3.Connection,
) -> None:
    """Phase 1 has no IFRS field mapping -- every applicable formula must
    still get a real metric_computation_attempts row (period discovery is
    unrestricted by mapping, per _build_quarter_cells's docstring), tagged
    not_computable/missing_input_mapping -- never silently zero rows, which
    would be indistinguishable from "never ran"."""
    _seed_operating_company(conn, "IFRSCO")
    _set_classification(conn, "IFRSCO", "operating_company", "ifrs")
    summary = compute_for_ticker(conn, "IFRSCO")
    assert summary.quarters_seen == 8
    assert summary.computed_ok == 0
    rows = _attempts(conn, "IFRSCO")
    assert len(rows) > 0
    assert all(r["status"] == "not_computable" for r in rows)
    assert all(r["reason_code"] == ReasonCode.MISSING_INPUT_MAPPING.value for r in rows)


def test_recompute_is_idempotent_without_force(conn: sqlite3.Connection) -> None:
    _seed_operating_company(conn, "TEST")
    _set_classification(conn, "TEST", "operating_company", "us_gaap")
    first = compute_for_ticker(conn, "TEST")
    assert first.attempts_written > 0
    assert first.skipped_unchanged == 0

    second = compute_for_ticker(conn, "TEST")
    assert second.attempts_written == 0
    assert second.skipped_unchanged == first.attempts_written

    kpi_fact_count = conn.execute("SELECT COUNT(*) AS n FROM kpi_facts").fetchone()["n"]
    # Re-running must not duplicate kpi_facts rows.
    first_count = conn.execute("SELECT COUNT(*) AS n FROM kpi_facts").fetchone()["n"]
    assert kpi_fact_count == first_count


def test_force_recompute_rewrites_attempts_without_duplicating(
    conn: sqlite3.Connection,
) -> None:
    _seed_operating_company(conn, "TEST")
    _set_classification(conn, "TEST", "operating_company", "us_gaap")
    first = compute_for_ticker(conn, "TEST")
    attempts_before = conn.execute(
        "SELECT COUNT(*) AS n FROM metric_computation_attempts"
    ).fetchone()["n"]

    second = compute_for_ticker(conn, "TEST", force=True)
    assert second.attempts_written == first.attempts_written
    attempts_after = conn.execute(
        "SELECT COUNT(*) AS n FROM metric_computation_attempts"
    ).fetchone()["n"]
    # ON CONFLICT DO UPDATE -- same logical rows, no growth in row count.
    assert attempts_after == attempts_before
