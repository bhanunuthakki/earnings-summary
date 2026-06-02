"""Tests for the category-2 KPI transform derivers in compute.fmp_derived_kpis.

These derive a same-fiscal-quarter YoY *transform* (NIM YoY change in bps,
Revenue YoY-growth deceleration, NPL YoY change in pp) over an existing
kpi_facts LEVEL series, materializing the series a thesis break_rule references
by name so the rule resolves to real observations instead of evaluating against
nothing (the silent-OK / UNRESOLVED case). The end-to-end tests pin that a
derived NIM-YoY series flips the NU break rule from "no observations" to a real
BREACH / OK verdict carrying observations.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from compute.fmp_derived_kpis import (
    KPI_NPL_15D_TOTAL_YOY_PP,
    KPI_REVENUE_YOY_DECELERATION,
    KPI_RISK_ADJ_NIM_YOY_BPS,
    KpiSeriesPoint,
    TransformKind,
    compute_yoy_transform,
    derive_for_ticker,
    derive_kpi_transforms,
)
from compute.kpi_resolver import resolve_kpi_definition_name
from compute.thesis_evaluator import evaluate_ticker_thesis
from models.facts import FiscalPeriodType, Unit
from models.kpis import BreachStatus

_NIM_BASE = "Risk-adjusted NIM (NIM minus cost of risk)"
_NPL_BASE_FULL = (
    "NPL 15d+ ratio (consolidated total: early-stage 15-90d + severe 90d+, YoY-tracked)"
)


# ---------------------------------------------------------------------------
# Schema + seed helpers (mirror the prod kpi_facts shape, incl. the post-0054
# audit columns so persist tags extracted_by).
# ---------------------------------------------------------------------------


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
            period_end TIMESTAMP
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
            extracted_by TEXT,
            supersedes_id INTEGER
        );
        CREATE UNIQUE INDEX uq_kpi_facts_provenance
        ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id);
        """
    )
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size, period_end) "
        "VALUES (1, 'NU', 'ir_doc', 'ir_pdf', 'ir/nu.pdf', ?, ?, 'ok', 1, ?)",
        ("a" * 64, datetime(2026, 4, 1), datetime(2026, 3, 31)),
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def _seed_base_kpi(
    conn: sqlite3.Connection,
    ticker: str,
    name: str,
    points: list[tuple[str, str, float]],
    *,
    unit: str = "percent",
    source_doc_id: int = 1,
) -> None:
    """Insert a kpi_definitions row + one kpi_facts row per (period_end, fpt, value)."""
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source) "
        "VALUES (?, ?, ?, 'ir_doc')",
        (ticker, name, unit),
    )
    def_id = conn.execute(
        "SELECT id FROM kpi_definitions WHERE ticker = ? AND name = ?", (ticker, name)
    ).fetchone()["id"]
    for period_end, fpt, value in points:
        conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, "
            "kpi_definition_id, value, unit, source_doc_id, extracted_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'ir')",
            (ticker, period_end, fpt, def_id, str(value), unit, source_doc_id),
        )
    conn.commit()


def _seed_quarterly_income(
    conn: sqlite3.Connection, ticker: str, rows: list[tuple[str, str, float]]
) -> None:
    """Seed FMP quarterly income statement facts (revenue + the 3 other required
    line items) so phase-1 derivation produces Revenue YoY Growth (USD)."""
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size, period_end) "
        "VALUES (2, ?, 'fmp', 'fmp_income_statement', "
        "'data/historical/fmp/NU_income_statement_quarterly.json', ?, ?, 'ok', 1, ?)",
        (ticker, "b" * 64, datetime(2026, 4, 1), datetime(2026, 3, 31)),
    )
    for period_end, fpt, revenue in rows:
        for line_item, value in (
            ("revenue", revenue),
            ("operating_income", revenue * 0.2),
            ("net_income", revenue * 0.15),
            ("gross_profit", revenue * 0.5),
        ):
            conn.execute(
                "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, "
                "line_item, value, currency, unit, source_doc_id) "
                "VALUES (?, ?, ?, ?, ?, 'USD', 'actual', 2)",
                (ticker, period_end, fpt, line_item, str(value)),
            )
    conn.commit()


def _q4(year: int, value: float) -> KpiSeriesPoint:
    return KpiSeriesPoint(
        period_end=datetime(year, 12, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        value=Decimal(str(value)),
        source_doc_id=1,
    )


# ---------------------------------------------------------------------------
# Pure transform math
# ---------------------------------------------------------------------------


def test_yoy_change_bps_multiplies_pp_delta_by_100() -> None:
    """NIM 10% -> 9% -> 8% (same Q4) yields -100 bps each YoY step."""
    points = [_q4(2023, 10.0), _q4(2024, 9.0), _q4(2025, 8.0)]
    rows = compute_yoy_transform(
        points, kind=TransformKind.YOY_CHANGE_BPS, name=KPI_RISK_ADJ_NIM_YOY_BPS, unit=Unit.BPS
    )
    by_year = {r.period_end.year: r for r in rows}
    assert set(by_year) == {2024, 2025}  # 2023 has no prior-year Q4
    assert by_year[2024].value == Decimal("-100")
    assert by_year[2025].value == Decimal("-100")
    assert all(r.unit is Unit.BPS for r in rows)


def test_yoy_change_pp_is_raw_level_delta() -> None:
    """NPL 10 -> 12 -> 13.6 yields +2.0 and +1.6 pp (no x100)."""
    points = [_q4(2023, 10.0), _q4(2024, 12.0), _q4(2025, 13.6)]
    rows = compute_yoy_transform(
        points, kind=TransformKind.YOY_CHANGE_PP, name=KPI_NPL_15D_TOTAL_YOY_PP, unit=Unit.PERCENT
    )
    by_year = {r.period_end.year: r.value for r in rows}
    assert by_year[2024] == Decimal("2.0")
    assert by_year[2025] == Decimal("1.6")


def test_yoy_deceleration_skips_nonpositive_prior() -> None:
    """Decel = (prior - curr)/prior*100; a prior growth rate <= 0 is skipped."""
    # Growth-RATE series: -10% (2022), 100% (2023), 50% (2024), 5% (2025).
    points = [_q4(2022, -10.0), _q4(2023, 100.0), _q4(2024, 50.0), _q4(2025, 5.0)]
    rows = compute_yoy_transform(
        points,
        kind=TransformKind.YOY_DECELERATION,
        name=KPI_REVENUE_YOY_DECELERATION,
        unit=Unit.PERCENT,
    )
    by_year = {r.period_end.year: r.value for r in rows}
    # 2023 skipped: its prior (2022) growth rate is -10% <= 0.
    assert set(by_year) == {2024, 2025}
    assert by_year[2024] == Decimal("50")  # (100-50)/100*100
    assert by_year[2025] == Decimal("90")  # (50-5)/50*100


def test_yoy_transform_skips_points_without_same_quarter_prior() -> None:
    """Prod NPL shape: 4 prints scattered across distinct fiscal quarters in
    non-adjacent years -> no same-quarter YoY pair -> empty output."""
    points = [
        KpiSeriesPoint(datetime(2023, 12, 31), FiscalPeriodType.Q4, Decimal("10.2"), 1),
        KpiSeriesPoint(datetime(2025, 6, 30), FiscalPeriodType.Q2, Decimal("11.3"), 1),
        KpiSeriesPoint(datetime(2025, 9, 30), FiscalPeriodType.Q3, Decimal("11.0"), 1),
        KpiSeriesPoint(datetime(2026, 3, 31), FiscalPeriodType.Q1, Decimal("11.5"), 1),
    ]
    rows = compute_yoy_transform(
        points, kind=TransformKind.YOY_CHANGE_PP, name=KPI_NPL_15D_TOTAL_YOY_PP, unit=Unit.PERCENT
    )
    assert rows == []


# ---------------------------------------------------------------------------
# DB-backed: base resolution + derive_for_ticker materialization
# ---------------------------------------------------------------------------


def test_derive_kpi_transforms_materializes_nim_series(conn: sqlite3.Connection) -> None:
    """Base level series in kpi_facts -> the NIM-YoY-change derived rows, found
    via base_label resolution against the canonical definition."""
    _seed_base_kpi(
        conn,
        "NU",
        _NIM_BASE,
        [("2023-12-31", "Q4", 10.0), ("2024-12-31", "Q4", 9.0), ("2025-12-31", "Q4", 8.0)],
    )
    rows = derive_kpi_transforms(conn, "NU")
    nim = [r for r in rows if r.name == KPI_RISK_ADJ_NIM_YOY_BPS]
    assert len(nim) == 2
    assert {r.value for r in nim} == {Decimal("-100")}
    assert all(r.unit is Unit.BPS for r in nim)


def test_derive_for_ticker_persists_transform_series(conn: sqlite3.Connection) -> None:
    """End-to-end persist: the derived NIM series lands in kpi_facts under the
    canonical name, tagged extracted_by='kpi_transform_derived'."""
    _seed_base_kpi(
        conn,
        "NU",
        _NIM_BASE,
        [("2023-12-31", "Q4", 10.0), ("2024-12-31", "Q4", 9.0), ("2025-12-31", "Q4", 8.0)],
    )
    derive_for_ticker(conn, "NU")
    rows = conn.execute(
        "SELECT kf.value, kf.unit, kf.extracted_by FROM kpi_facts kf "
        "JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
        "WHERE kf.ticker = 'NU' AND kd.name = ? ORDER BY kf.period_end",
        (KPI_RISK_ADJ_NIM_YOY_BPS,),
    ).fetchall()
    assert len(rows) == 2
    assert all(dict(r)["unit"] == "bps" for r in rows)
    assert all(dict(r)["extracted_by"] == "kpi_transform_derived" for r in rows)


def test_derive_for_ticker_transforms_are_idempotent(conn: sqlite3.Connection) -> None:
    """Re-running inserts no duplicate derived rows (UNIQUE index dedupes)."""
    _seed_base_kpi(
        conn,
        "NU",
        _NIM_BASE,
        [("2023-12-31", "Q4", 10.0), ("2024-12-31", "Q4", 9.0), ("2025-12-31", "Q4", 8.0)],
    )
    _, inserted_first = derive_for_ticker(conn, "NU")
    _, inserted_second = derive_for_ticker(conn, "NU")
    assert inserted_first >= 2  # at least the 2 NIM-YoY rows
    assert inserted_second == 0


def test_revenue_deceleration_derives_over_phase1_revenue_yoy(
    conn: sqlite3.Connection,
) -> None:
    """Phase ordering: phase-1 derives Revenue YoY Growth from financial_facts,
    then phase-2 derives its deceleration from that freshly-persisted base."""
    _seed_quarterly_income(
        conn,
        "NU",
        [
            ("2023-12-31", "Q4", 100.0),
            ("2024-12-31", "Q4", 200.0),  # YoY +100%
            ("2025-12-31", "Q4", 260.0),  # YoY +30%
            ("2026-12-31", "Q4", 312.0),  # YoY +20%
        ],
    )
    derive_for_ticker(conn, "NU")
    decel = conn.execute(
        "SELECT kf.period_end, kf.value FROM kpi_facts kf "
        "JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
        "WHERE kf.ticker = 'NU' AND kd.name = ? ORDER BY kf.period_end",
        (KPI_REVENUE_YOY_DECELERATION,),
    ).fetchall()
    by_year = {
        datetime.fromisoformat(str(dict(r)["period_end"])).year: Decimal(str(dict(r)["value"]))
        for r in decel
    }
    assert 2025 in by_year and 2026 in by_year
    assert by_year[2025] == Decimal("70")  # (100-30)/100*100


def test_sparse_npl_base_resolves_but_yields_no_derived_rows(
    conn: sqlite3.Connection,
) -> None:
    """The NPL base label resolves to the verbose extracted def (proving base
    resolution works), but the 4 scattered prints have no same-quarter YoY pair,
    so no derived rows are produced — the honest data-limited outcome."""
    _seed_base_kpi(
        conn,
        "NU",
        _NPL_BASE_FULL,
        [
            ("2023-12-31", "Q4", 10.2),
            ("2025-06-30", "Q2", 11.3),
            ("2025-09-30", "Q3", 11.0),
            ("2026-03-31", "Q1", 11.5),
        ],
    )
    # Base resolution succeeds: the short spec label reaches the verbose def that
    # the rule's own label ("NPL 15d+ total") does NOT normalize-match.
    assert resolve_kpi_definition_name(conn, "NU", "NPL 15d+ ratio") == _NPL_BASE_FULL
    # But no YoY pairs -> no derived NPL series materialized.
    derive_for_ticker(conn, "NU")
    npl_def = conn.execute(
        "SELECT 1 FROM kpi_definitions WHERE ticker = 'NU' AND name = ?",
        (KPI_NPL_15D_TOTAL_YOY_PP,),
    ).fetchone()
    assert npl_def is None


# ---------------------------------------------------------------------------
# Regression: a derived series flips a break rule from unresolved to evaluated.
# ---------------------------------------------------------------------------


def _write_nim_holdings(tmp_path: Path) -> None:
    payload = {
        "ticker": "NU",
        "thesis": "...",
        "break_rules": [
            {
                "rule_id": "risk_adjusted_nim_yoy_compression",
                "kpi_name": KPI_RISK_ADJ_NIM_YOY_BPS,
                "comparator": "lt",
                "threshold": 0,
                "unit": "bps",
                "consecutive_periods": 2,
                "narrative": "Risk-adjusted NIM contracting YoY for 2 consecutive quarters.",
            }
        ],
    }
    (tmp_path / "NU.json").write_text(json.dumps(payload), encoding="utf-8")


def test_break_rule_unresolved_before_derivation_then_breaches_after(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Regression + BREACH: before derivation the rule's kpi_name resolves to NO
    kpi_facts definition, so it evaluates UNRESOLVED (the gap #271 surfaces, not a
    silent OK). After derivation the materialized YoY series gives it real
    observations and — the base contracting 10->9->8 YoY — a BREACH verdict."""
    _seed_base_kpi(
        conn,
        "NU",
        _NIM_BASE,
        [("2023-12-31", "Q4", 10.0), ("2024-12-31", "Q4", 9.0), ("2025-12-31", "Q4", 8.0)],
    )
    _write_nim_holdings(tmp_path)
    # Before: the derived series doesn't exist -> rule resolves to nothing -> UNRESOLVED.
    before = evaluate_ticker_thesis(conn, ticker="NU", holdings_dir=tmp_path)
    before_nim = before.rule_evaluations[0]
    assert before_nim.status is BreachStatus.UNRESOLVED
    assert before_nim.observations == ()
    # After: the derived series resolves the rule to real, breaching observations.
    derive_for_ticker(conn, "NU")
    after = evaluate_ticker_thesis(conn, ticker="NU", holdings_dir=tmp_path)
    after_nim = after.rule_evaluations[0]
    assert after.overall_status is BreachStatus.BREACH
    assert after_nim.status is BreachStatus.BREACH
    assert len(after_nim.observations) == 2
    assert all(o.value == Decimal("-100") for o in after_nim.observations)


def test_derived_nim_series_warns_on_single_breaching_quarter(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Most-recent YoY change positive but the prior one negative -> only 1 of 2
    consecutive periods matches lt-0 -> WARN (not BREACH), still with observations."""
    # 10 -> 9 (YoY -100 bps, matches) -> 9.5 (YoY +50 bps vs 9, no match); newest +50.
    _seed_base_kpi(
        conn,
        "NU",
        _NIM_BASE,
        [("2023-12-31", "Q4", 10.0), ("2024-12-31", "Q4", 9.0), ("2025-12-31", "Q4", 9.5)],
    )
    _write_nim_holdings(tmp_path)
    derive_for_ticker(conn, "NU")
    verdict = evaluate_ticker_thesis(conn, ticker="NU", holdings_dir=tmp_path)
    assert verdict.overall_status is BreachStatus.WARN
    nim_eval = verdict.rule_evaluations[0]
    assert nim_eval.status is BreachStatus.WARN
    assert len(nim_eval.observations) == 2


def test_derived_nim_series_evaluates_ok_with_observations(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A NIM base expanding YoY -> derived series -> the rule evaluates OK, but
    crucially WITH observations: it resolved rather than silently defaulting to
    the no-data OK a missing series would have produced."""
    _seed_base_kpi(
        conn,
        "NU",
        _NIM_BASE,
        [("2023-12-31", "Q4", 8.0), ("2024-12-31", "Q4", 9.0), ("2025-12-31", "Q4", 10.0)],
    )
    _write_nim_holdings(tmp_path)
    derive_for_ticker(conn, "NU")
    verdict = evaluate_ticker_thesis(conn, ticker="NU", holdings_dir=tmp_path)
    assert verdict.overall_status is BreachStatus.OK
    nim_eval = verdict.rule_evaluations[0]
    assert nim_eval.status is BreachStatus.OK
    assert len(nim_eval.observations) == 2  # OK *with* observations, not no-data
    assert "no kpi_facts" not in nim_eval.detail
