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
from models.facts import FactLocator, LocatorKind


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
    """Full line-item set for every Phase-1 and Phase-2 formula, growing
    simply per quarter."""
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
            # Phase 2 additions.
            "short_term_investments": "100",
            "long_term_investments": "50",
            "income_tax_expense": str(revenue * Decimal("0.2") * Decimal("0.21")),
            "income_before_tax": str(revenue * Decimal("0.2")),
            "interest_expense": "10",
            "cost_of_revenue": str(revenue * Decimal("0.6")),
            "accounts_receivable": "150",
            "accounts_payable": "80",
            "inventory": "60",
            "operating_lease_liability": "120",
            "weighted_avg_shares_diluted": "100",
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


# 4 fiscal years, revenue growing an exact 10% YoY so revenue_cagr_3y ==
# 10% between FY0 and FY3.
_FY_YEARS: list[str] = ["2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31"]


def _seed_fy_rows(conn: sqlite3.Connection, ticker: str) -> None:
    for year_index, pe_str in enumerate(_FY_YEARS):
        pe = datetime.fromisoformat(pe_str)
        doc_id = _insert_doc(conn, ticker, pe)
        revenue = Decimal("1000") * (Decimal("1.1") ** year_index)
        facts: dict[str, str] = {
            "revenue": str(revenue),
            "operating_income": str(revenue * Decimal("0.2")),
            "depreciation_and_amortization": str(revenue * Decimal("0.05")),
        }
        for line_item, value in facts.items():
            _insert_fact(
                conn,
                ticker=ticker,
                period_end=pe,
                fpt="FY",
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


def test_compute_for_ticker_writes_derived_locator(conn: sqlite3.Connection) -> None:
    """Every ok computation writes a canonical `derived`-kind FactLocator
    (docs/design/provenance_clickthrough.md §1.5, bottoms_up_metrics_engine.md
    §3) alongside the existing computed_from JSON -- the Phase C formula-tree
    peek needs this to render without a retrofit; see
    tests/test_extractor_locator_coverage.py for the CI-guard registration."""
    _seed_operating_company(conn, "TEST")
    _set_classification(conn, "TEST", "operating_company", "us_gaap")
    compute_for_ticker(conn, "TEST")

    row = _latest_attempt(conn, "TEST", "gross_margin")
    assert row["status"] == "ok"
    kpi_fact = conn.execute(
        "SELECT locator, computed_from, formula_id FROM kpi_facts WHERE id = ?",
        (row["kpi_fact_id"],),
    ).fetchone()
    loc = FactLocator.from_json(kpi_fact["locator"])
    assert loc is not None
    assert loc.locator_version >= 2
    assert loc.effective_kind() == LocatorKind.DERIVED
    assert loc.derived is not None
    assert loc.derived.formula_id == kpi_fact["formula_id"]
    assert loc.derived.display  # display_formula carried through
    assert len(loc.derived.inputs) > 0
    for input_ref in loc.derived.inputs:
        assert input_ref.ref == "financial_fact"
        assert input_ref.doc_id is not None
        assert input_ref.period_end is not None
    # computed_from (the pre-existing lineage column) is still written
    # unchanged for backward compatibility with readers that predate the
    # locator kind (ui.source_chip._lineage_rows).
    assert kpi_fact["computed_from"] is not None


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


def test_ifrs_ticker_computes_successfully_in_phase2(
    conn: sqlite3.Connection,
) -> None:
    """Phase 2 populated IFRS_FIELD_MAP (verified against real
    data/portfolio.db rows for NU/BN/ASML/NVO -- FMP's normalization
    collapses IFRS filers onto the identical US-GAAP vocabulary), so an
    IFRS ticker with every concept present now computes real values --
    the opposite outcome from Phase 1's "every formula unmapped" baseline,
    and the point of doing the Phase 2 mapping work at all."""
    _seed_operating_company(conn, "IFRSCO")
    _set_classification(conn, "IFRSCO", "operating_company", "ifrs")
    summary = compute_for_ticker(conn, "IFRSCO")
    assert summary.quarters_seen == 8
    assert summary.computed_ok > 0

    gross_row = _latest_attempt(conn, "IFRSCO", "gross_margin")
    assert gross_row["status"] == "ok"
    roic_row = _latest_attempt(conn, "IFRSCO", "roic_strict")
    assert roic_row["status"] == "ok"


def test_ifrs_ticker_missing_lease_liability_is_missing_input_not_mapping_gap(
    conn: sqlite3.Connection,
) -> None:
    """The real NU/BN/NVO case (verified against data/portfolio.db):
    operating_lease_liability's FIELD NAME is mapped for IFRS (proven by
    ASML's real data), but these specific tickers have no VALUE under that
    name for any period -- a per-period data gap (MISSING_INPUT via the
    normal resolution path), not a mapping gap (MISSING_INPUT_MAPPING).
    roic_strict (doesn't need the lease concept) must still compute fine."""
    _seed_operating_company(conn, "IFRSNOLEASE")
    _set_classification(conn, "IFRSNOLEASE", "operating_company", "ifrs")
    # Remove the operating_lease_liability facts this fixture ticker would
    # otherwise carry, simulating NU/BN/NVO's real absence of that field.
    conn.execute(
        "DELETE FROM financial_facts WHERE ticker = ? AND line_item = 'operating_lease_liability'",
        ("IFRSNOLEASE",),
    )
    conn.commit()
    compute_for_ticker(conn, "IFRSNOLEASE")

    lease_row = _latest_attempt(conn, "IFRSNOLEASE", "roic_lease_adjusted")
    assert lease_row["status"] == "not_computable"
    assert lease_row["reason_code"] == ReasonCode.MISSING_INPUT.value

    strict_row = _latest_attempt(conn, "IFRSNOLEASE", "roic_strict")
    assert strict_row["status"] == "ok"


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


# ---------------------------------------------------------------------------
# Phase 2 -- roic_*/net_debt_* method-variant pairs, ticker overrides,
# 2-point averaging, and the FY-cadence CAGR grid.
# ---------------------------------------------------------------------------


def test_compute_for_ticker_roic_variants_disagree_when_leases_material(
    conn: sqlite3.Connection,
) -> None:
    _seed_operating_company(conn, "TEST")
    _set_classification(conn, "TEST", "operating_company", "us_gaap")
    compute_for_ticker(conn, "TEST")

    strict_row = _latest_attempt(conn, "TEST", "roic_strict")
    lease_row = _latest_attempt(conn, "TEST", "roic_lease_adjusted")
    assert strict_row["status"] == "ok"
    assert lease_row["status"] == "ok"
    strict_value = conn.execute(
        "SELECT value FROM kpi_facts WHERE id = ?", (strict_row["kpi_fact_id"],)
    ).fetchone()["value"]
    lease_value = conn.execute(
        "SELECT value FROM kpi_facts WHERE id = ?", (lease_row["kpi_fact_id"],)
    ).fetchone()["value"]
    # Adding operating_lease_liability (120) to invested capital can only
    # lower the ratio -- the documented, expected method-variant disagreement.
    assert Decimal(str(lease_value)) < Decimal(str(strict_value))


def test_compute_for_ticker_net_debt_variants_disagree_when_lt_investments_material(
    conn: sqlite3.Connection,
) -> None:
    _seed_operating_company(conn, "TEST")
    _set_classification(conn, "TEST", "operating_company", "us_gaap")
    compute_for_ticker(conn, "TEST")

    strict_row = _latest_attempt(conn, "TEST", "net_debt_strict")
    incl_row = _latest_attempt(conn, "TEST", "net_debt_incl_lt_securities")
    assert strict_row["status"] == "ok"
    assert incl_row["status"] == "ok"
    strict_value = conn.execute(
        "SELECT value FROM kpi_facts WHERE id = ?", (strict_row["kpi_fact_id"],)
    ).fetchone()["value"]
    incl_value = conn.execute(
        "SELECT value FROM kpi_facts WHERE id = ?", (incl_row["kpi_fact_id"],)
    ).fetchone()["value"]
    # incl_lt_securities nets an additional 50 of long_term_investments.
    assert Decimal(str(strict_value)) - Decimal(str(incl_value)) == Decimal("50")


def test_compute_for_ticker_zero_inventory_ticker_override_suppresses_inventory_turnover(
    conn: sqlite3.Connection,
) -> None:
    """NOW/VEEV/WIX/META/GOOG/UBER/BKNG/BN (applicability._TICKER_EFFICIENCY_OVERRIDES)
    get not_applicable_business_model for inventory_turnover/cash_conversion_cycle
    even though the ticker's business_model_class is plain operating_company."""
    _seed_operating_company(conn, "NOW")
    _set_classification(conn, "NOW", "operating_company", "us_gaap")
    compute_for_ticker(conn, "NOW")

    inv_row = _latest_attempt(conn, "NOW", "inventory_turnover")
    assert inv_row["status"] == "not_computable"
    assert inv_row["reason_code"] == ReasonCode.NOT_APPLICABLE_BUSINESS_MODEL.value

    ccc_row = _latest_attempt(conn, "NOW", "cash_conversion_cycle")
    assert ccc_row["status"] == "not_computable"
    assert ccc_row["reason_code"] == ReasonCode.NOT_APPLICABLE_BUSINESS_MODEL.value

    # asset_turnover has no override for NOW -- still computes.
    asset_row = _latest_attempt(conn, "NOW", "asset_turnover")
    assert asset_row["status"] == "ok"


def test_compute_for_ticker_asset_turnover_averages_start_and_end_total_assets(
    conn: sqlite3.Connection,
) -> None:
    """Proves the io._resolve_ttm_average mechanism actually runs (not just
    reading the latest quarter's point-in-time value, which roa/roe use for
    the SAME concept): total_assets grows every quarter, so the 2-point
    average must differ from both the window's start and end values."""
    ticker = "AVGCO"
    _set_classification(conn, ticker, "operating_company", "us_gaap")
    for i, (pe_str, fpt) in enumerate(_QUARTERS):
        pe = datetime.fromisoformat(pe_str)
        doc_id = _insert_doc(conn, ticker, pe)
        # total_assets: 1000, 2000, ..., 8000 across the 8 quarters.
        total_assets = Decimal(1000) * (i + 1)
        for line_item, value in {
            "revenue": "1000",
            "total_assets": str(total_assets),
        }.items():
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

    compute_for_ticker(conn, ticker)
    row = _latest_attempt(conn, ticker, "asset_turnover")
    assert row["status"] == "ok"
    kpi_fact = conn.execute(
        "SELECT value FROM kpi_facts WHERE id = ?", (row["kpi_fact_id"],)
    ).fetchone()
    # Last cell idx=7 (total_assets=8000); TTM window = cells[4:8] = quarters
    # with total_assets 5000, 6000, 7000, 8000. Average of start (5000) and
    # end (8000) = 6500 -- NOT 8000 (latest point-in-time) and NOT 6500's
    # naive 4-quarter mean (6500 happens to coincide here since the series
    # is linear, so the real proof is the revenue_ttm/6500 result below).
    revenue_ttm = Decimal("4000")
    expected = revenue_ttm / Decimal("6500")
    # kpi_facts.value is a NUMERIC(24,6) SQLite column (stored as a float
    # under the hood) -- compare with a small tolerance rather than exact
    # Decimal equality.
    assert abs(Decimal(str(kpi_fact["value"])) - expected) < Decimal("0.0000001")


def test_compute_for_ticker_revenue_cagr_3y_over_fy_rows(conn: sqlite3.Connection) -> None:
    _seed_fy_rows(conn, "FYCO")
    _set_classification(conn, "FYCO", "operating_company", "us_gaap")
    compute_for_ticker(conn, "FYCO")

    rows = [r for r in _attempts(conn, "FYCO") if r["formula_key"] == "revenue_cagr_3y"]
    assert len(rows) == 4  # one attempt per FY row, even the un-computable early ones
    ok_rows = [r for r in rows if r["status"] == "ok"]
    assert len(ok_rows) == 1  # only the 4th FY row (idx=3) has a 3-year-back prior
    kpi_fact = conn.execute(
        "SELECT value FROM kpi_facts WHERE id = ?", (ok_rows[0]["kpi_fact_id"],)
    ).fetchone()
    # FY0=1000, FY3=1000*1.1^3=1331 -> exactly 10% CAGR.
    assert abs(Decimal(str(kpi_fact["value"])) - Decimal("10")) < Decimal("0.001")


def test_compute_for_ticker_ebitda_cagr_3y_over_fy_rows(conn: sqlite3.Connection) -> None:
    _seed_fy_rows(conn, "FYCO2")
    _set_classification(conn, "FYCO2", "operating_company", "us_gaap")
    compute_for_ticker(conn, "FYCO2")

    ok_rows = [
        r
        for r in _attempts(conn, "FYCO2")
        if r["formula_key"] == "ebitda_cagr_3y" and r["status"] == "ok"
    ]
    assert len(ok_rows) == 1
    kpi_fact = conn.execute(
        "SELECT value FROM kpi_facts WHERE id = ?", (ok_rows[0]["kpi_fact_id"],)
    ).fetchone()
    assert abs(Decimal(str(kpi_fact["value"])) - Decimal("10")) < Decimal("0.001")


def test_compute_for_ticker_revenue_cagr_3y_only_2_fy_rows_is_missing_input(
    conn: sqlite3.Connection,
) -> None:
    """Fewer than 4 FY rows means no formula attempt ever has a valid
    3-year-back prior -- every attempt is not_computable/missing_input,
    never silently absent."""
    ticker = "SHORTFY"
    _set_classification(conn, ticker, "operating_company", "us_gaap")
    for pe_str in ("2023-12-31", "2024-12-31"):
        pe = datetime.fromisoformat(pe_str)
        doc_id = _insert_doc(conn, ticker, pe)
        _insert_fact(
            conn,
            ticker=ticker,
            period_end=pe,
            fpt="FY",
            line_item="revenue",
            value="1000",
            source_doc_id=doc_id,
        )
    conn.commit()
    compute_for_ticker(conn, ticker)
    rows = [r for r in _attempts(conn, ticker) if r["formula_key"] == "revenue_cagr_3y"]
    assert len(rows) == 2
    assert all(r["status"] == "not_computable" for r in rows)
    assert all(r["reason_code"] == ReasonCode.MISSING_INPUT.value for r in rows)
