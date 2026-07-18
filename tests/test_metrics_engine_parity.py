"""Parity harness tests (docs/design/bottoms_up_metrics_engine.md section 4).

Synthetic fixtures ONLY -- an in-memory kpi_facts/kpi_definitions pair and a
hand-written FMP cache JSON in tmp_path. No prod data, no network call. The
real-cache sweep against MAIN's data/portfolio.db + data/historical/fmp/ is
a separate, manually-invoked CLI mode
(`execution/compute_derived_metrics.py --parity`), not exercised by this
test file.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from compute.metrics_engine.parity import ParityOutcome, compare_ticker, load_fmp_reference
from compute.metrics_engine.parity_known_mismatches import tolerance_for, within_tolerance
from compute.metrics_engine.registry import MetricCategory
from models.facts import Unit


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            unit TEXT NOT NULL,
            primary_source TEXT NOT NULL,
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
        """
    )
    return c


def _seed_kpi_fact(conn: sqlite3.Connection, *, ticker: str, formula_key: str, value: str) -> None:
    cur = conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source) VALUES (?, ?, 'percent', 'fmp')",
        (ticker, formula_key),
    )
    kpi_def_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id) VALUES (?, ?, 'TTM', ?, ?, 'percent', 1)",
        (ticker, datetime(2024, 12, 31), kpi_def_id, value),
    )
    conn.commit()


def _write_fmp_cache(tmp_path: Path, ticker: str, suffix: str, field: str, value: float) -> Path:
    path = tmp_path / f"{ticker}_{suffix}.json"
    path.write_text(json.dumps([{"symbol": ticker, field: value}]), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# tolerance bands
# ---------------------------------------------------------------------------


def test_percent_band_passes_within_50bps() -> None:
    assert within_tolerance(MetricCategory.MARGIN, Decimal("40.3"), Decimal("40.0")) is True


def test_percent_band_passes_within_3pct_relative_even_if_over_50bps() -> None:
    # 3% relative of 40 is 1.2 pts -- outside 50bps absolute but within the
    # looser relative bound, so it must still pass ("whichever is looser").
    assert within_tolerance(MetricCategory.MARGIN, Decimal("41.0"), Decimal("40.0")) is True


def test_percent_band_fails_outside_both_bounds() -> None:
    assert within_tolerance(MetricCategory.MARGIN, Decimal("50.0"), Decimal("40.0")) is False


def test_ratio_band_5pct_relative() -> None:
    assert within_tolerance(MetricCategory.LIQUIDITY, Decimal("1.04"), Decimal("1.00")) is True
    assert within_tolerance(MetricCategory.LIQUIDITY, Decimal("1.10"), Decimal("1.00")) is False


# ---------------------------------------------------------------------------
# load_fmp_reference
# ---------------------------------------------------------------------------


def test_load_fmp_reference_rescales_fraction_to_percent(tmp_path: Path) -> None:
    _write_fmp_cache(tmp_path, "MELI", "financial_ratios_quarterly", "grossProfitMargin", 0.4386)
    value = load_fmp_reference(tmp_path, "MELI", "gross_margin")
    assert value is not None
    assert value == Decimal("43.86")


def test_load_fmp_reference_ratio_field_not_rescaled(tmp_path: Path) -> None:
    _write_fmp_cache(tmp_path, "MELI", "financial_ratios_quarterly", "currentRatio", 1.161)
    value = load_fmp_reference(tmp_path, "MELI", "current_ratio")
    assert value == Decimal("1.161")


def test_load_fmp_reference_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_fmp_reference(tmp_path, "NOPE", "gross_margin") is None


def test_load_fmp_reference_unmapped_formula_returns_none(tmp_path: Path) -> None:
    assert load_fmp_reference(tmp_path, "MELI", "revenue_yoy") is None


# ---------------------------------------------------------------------------
# compare_ticker
# ---------------------------------------------------------------------------


def test_compare_ticker_reports_match(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_kpi_fact(conn, ticker="MELI", formula_key="gross_margin", value="43.9")
    _write_fmp_cache(tmp_path, "MELI", "financial_ratios_quarterly", "grossProfitMargin", 0.4386)
    results = compare_ticker(conn, tmp_path, "MELI")
    gross = next(r for r in results if r.formula_key == "gross_margin")
    assert gross.outcome == ParityOutcome.MATCH


def test_compare_ticker_reports_fail_when_no_known_mismatch(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_kpi_fact(conn, ticker="MELI", formula_key="gross_margin", value="10.0")
    _write_fmp_cache(tmp_path, "MELI", "financial_ratios_quarterly", "grossProfitMargin", 0.4386)
    results = compare_ticker(conn, tmp_path, "MELI")
    gross = next(r for r in results if r.formula_key == "gross_margin")
    assert gross.outcome == ParityOutcome.FAIL


def test_compare_ticker_reports_no_data_when_computed_missing(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _write_fmp_cache(tmp_path, "MELI", "financial_ratios_quarterly", "grossProfitMargin", 0.4386)
    results = compare_ticker(conn, tmp_path, "MELI")
    gross = next(r for r in results if r.formula_key == "gross_margin")
    assert gross.outcome == ParityOutcome.NO_DATA


def test_compare_ticker_known_mismatch_overrides_fail(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import compute.metrics_engine.parity as parity_mod

    monkeypatch.setitem(
        parity_mod.KNOWN_MISMATCHES,
        ("MELI", "gross_margin"),
        "test-only synthetic divergence",
    )
    _seed_kpi_fact(conn, ticker="MELI", formula_key="gross_margin", value="10.0")
    _write_fmp_cache(tmp_path, "MELI", "financial_ratios_quarterly", "grossProfitMargin", 0.4386)
    results = compare_ticker(conn, tmp_path, "MELI")
    gross = next(r for r in results if r.formula_key == "gross_margin")
    assert gross.outcome == ParityOutcome.KNOWN_MISMATCH
    assert gross.detail == "test-only synthetic divergence"


# ---------------------------------------------------------------------------
# Phase 2 -- unit-aware EFFICIENCY tolerance band (turnovers/CCC are ratios,
# not percentages, unlike Phase 1's sole EFFICIENCY formula sbc_pct_revenue)
# ---------------------------------------------------------------------------


def test_efficiency_band_defaults_to_percent_when_unit_omitted() -> None:
    """Backward compatible: existing Phase 1 call sites never pass `unit`."""
    band = tolerance_for(MetricCategory.EFFICIENCY)
    assert band.abs_bps == Decimal("50")


def test_efficiency_band_is_ratio_band_for_non_percent_unit() -> None:
    band = tolerance_for(MetricCategory.EFFICIENCY, unit=Unit.RATIO)
    assert band.abs_bps is None
    assert band.rel_pct == Decimal("5")


def test_efficiency_band_stays_percent_for_percent_unit() -> None:
    band = tolerance_for(MetricCategory.EFFICIENCY, unit=Unit.PERCENT)
    assert band.abs_bps == Decimal("50")


def test_within_tolerance_turnover_5pct_relative_not_50bps_absolute() -> None:
    """A 0.3x absolute gap on a ~4x turnover ratio is way over 50bps but well
    within 5% relative -- must pass via the ratio band, not fail via a
    wrongly-applied percent band."""
    assert (
        within_tolerance(MetricCategory.EFFICIENCY, Decimal("4.1"), Decimal("4.0"), unit=Unit.RATIO)
        is True
    )
    assert (
        within_tolerance(MetricCategory.EFFICIENCY, Decimal("4.5"), Decimal("4.0"), unit=Unit.RATIO)
        is False
    )


# ---------------------------------------------------------------------------
# Phase 2 -- FMP field map additions (grid-matched TTM vs quarterly files)
# ---------------------------------------------------------------------------


def test_load_fmp_reference_roic_strict_uses_ttm_file_and_rescales(tmp_path: Path) -> None:
    _write_fmp_cache(tmp_path, "MELI", "key_metrics_ttm", "returnOnInvestedCapitalTTM", 0.1664)
    value = load_fmp_reference(tmp_path, "MELI", "roic_strict")
    assert value is not None
    assert value == Decimal("16.64")


def test_load_fmp_reference_net_debt_to_ebitda_not_rescaled(tmp_path: Path) -> None:
    _write_fmp_cache(tmp_path, "MELI", "key_metrics_ttm", "netDebtToEBITDATTM", -3.79)
    value = load_fmp_reference(tmp_path, "MELI", "net_debt_to_ebitda")
    assert value == Decimal("-3.79")


def test_load_fmp_reference_asset_turnover_uses_ttm_ratios_file(tmp_path: Path) -> None:
    _write_fmp_cache(tmp_path, "MELI", "financial_ratios_ttm", "assetTurnoverTTM", 0.6776)
    value = load_fmp_reference(tmp_path, "MELI", "asset_turnover")
    assert value == Decimal("0.6776")


def test_load_fmp_reference_interest_coverage_uses_plain_quarterly_file(tmp_path: Path) -> None:
    """interest_coverage is period_grid='quarterly', not 'ttm' -- must read
    the plain financial_ratios_quarterly file, not a _ttm one."""
    _write_fmp_cache(tmp_path, "MELI", "financial_ratios_quarterly", "interestCoverageRatio", 9.4)
    value = load_fmp_reference(tmp_path, "MELI", "interest_coverage")
    assert value == Decimal("9.4")


def test_load_fmp_reference_net_debt_strict_has_no_mapping() -> None:
    """FMP publishes netDebtToEBITDA (a ratio) but never a standalone net
    debt dollar figure -- net_debt_strict/incl_lt_securities stay unmapped."""
    assert load_fmp_reference(Path("/nonexistent"), "MELI", "net_debt_strict") is None
    assert load_fmp_reference(Path("/nonexistent"), "MELI", "net_debt_incl_lt_securities") is None
    assert load_fmp_reference(Path("/nonexistent"), "MELI", "roic_lease_adjusted") is None
    assert load_fmp_reference(Path("/nonexistent"), "MELI", "eps_adjusted_ex_sbc") is None
    assert load_fmp_reference(Path("/nonexistent"), "MELI", "revenue_cagr_3y") is None
    assert load_fmp_reference(Path("/nonexistent"), "MELI", "ebitda_cagr_3y") is None


def test_compare_ticker_inventory_turnover_ratio_band_not_percent_band(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """End-to-end: compare_ticker must apply the RATIO band (5% relative)
    to inventory_turnover, not the PERCENT band its EFFICIENCY category
    would default to without the unit-aware override. Chosen so the two
    bands actually disagree: computed=26.0 vs reference=25.0 is a 1.0
    absolute gap (fails the percent band's 50bps bound) and 4% relative
    (fails the percent band's 3% bound too, so the percent band rejects
    this outright) but clears the ratio band's 5% relative bound."""
    _seed_kpi_fact(conn, ticker="MELI", formula_key="inventory_turnover", value="26.0")
    _write_fmp_cache(tmp_path, "MELI", "financial_ratios_ttm", "inventoryTurnoverTTM", 25.0)
    results = compare_ticker(conn, tmp_path, "MELI")
    inv = next(r for r in results if r.formula_key == "inventory_turnover")
    assert inv.outcome == ParityOutcome.MATCH
