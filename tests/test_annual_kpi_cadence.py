"""Annual-cadence KPI support — resolver, evaluator, persistence, rendering.

Covers the cross-cutting behaviour that lets a KPI an issuer discloses only
annually (NU's Basel III capital adequacy ratio) be first-class: the
``reporting_cadence`` lookup, the FY-only break-rule history (so
``consecutive_periods`` counts YEARS), the cadence-aware persistence stamp, the
financials annual-series builder, and the cadence-parametrized YoY heatmap.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from compute.kpi_resolver import reporting_cadence_for
from compute.thesis_evaluator import (
    BreakRule,
    Comparator,
    _fetch_kpi_history,  # pyright: ignore[reportPrivateUsage]  # internal seam
    evaluate_rule,
)
from models.documents import SourceType
from models.facts import FiscalPeriodType, Unit
from models.kpis import BreachStatus, ReportingCadence
from pipeline.kpi_persistence import (
    KpiExtractionManifest,
    KpiValue,
    find_or_create_kpi_definition,
    persist_manifest,
)
from report.renderers.charts_v2 import MatrixRow, yoy_heatmap_table
from report.sections.financials import (
    _align_annual_kpis,  # pyright: ignore[reportPrivateUsage]
    _annual_kpi_raw_for,  # pyright: ignore[reportPrivateUsage]
    _resolve_priorities,  # pyright: ignore[reportPrivateUsage]
)

_CAR = "Capital adequacy ratio (CET1 / Basel III total)"

# NU CAR: 3 fiscal-year-end (annual) prints + interim Q prints (preserved).
_CAR_FY = [("2023-12-31", "FY", 13.7), ("2024-12-31", "FY", 18.1), ("2025-12-31", "FY", 16.6)]
_CAR_INTERIM = [("2024-09-30", "Q3", 15.8), ("2025-03-31", "Q1", 16.9)]


def _schema(conn: sqlite3.Connection, *, with_cadence: bool = True) -> None:
    cadence_col = "reporting_cadence TEXT NOT NULL DEFAULT 'quarterly'," if with_cadence else ""
    conn.executescript(
        f"""
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            unit TEXT NOT NULL,
            primary_source TEXT NOT NULL,
            fallback_source TEXT,
            ir_url TEXT,
            threshold_tier TEXT,
            threshold_low FLOAT,
            threshold_high FLOAT,
            notes TEXT,
            {cadence_col}
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
    conn.commit()


def _seed(
    conn: sqlite3.Connection,
    ticker: str,
    name: str,
    cadence: str,
    rows: list[tuple[str, str, float]],
    *,
    source_doc_id: int = 1,
) -> int:
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source, reporting_cadence) "
        "VALUES (?, ?, 'percent', 'ir_doc', ?)",
        (ticker, name, cadence),
    )
    kid = int(
        conn.execute(
            "SELECT id FROM kpi_definitions WHERE ticker = ? AND name = ?", (ticker, name)
        ).fetchone()[0]
    )
    for period_end, fpt, value in rows:
        conn.execute(
            "INSERT INTO kpi_facts "
            "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, source_doc_id) "
            "VALUES (?, ?, ?, ?, ?, 'percent', ?)",
            (ticker, period_end, fpt, kid, str(value), source_doc_id),
        )
    conn.commit()
    return kid


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _schema(c)
    return c


# --------------------------------------------------------------------------- #
# reporting_cadence_for
# --------------------------------------------------------------------------- #
def test_reporting_cadence_for_annual_resolves_via_short_label(conn: sqlite3.Connection) -> None:
    _seed(conn, "NU", _CAR, "annual", _CAR_FY)
    assert reporting_cadence_for(conn, "NU", _CAR) == "annual"
    # a short break-rule label normalizes to the parenthetical-qualified definition
    assert reporting_cadence_for(conn, "NU", "Capital adequacy ratio") == "annual"


def test_reporting_cadence_for_quarterly_default_and_unknown(conn: sqlite3.Connection) -> None:
    _seed(conn, "NU", "NIM", "quarterly", [("2025-03-31", "Q1", 18.0)])
    assert reporting_cadence_for(conn, "NU", "NIM") == "quarterly"
    assert reporting_cadence_for(conn, "NU", "No such metric") == "quarterly"


def test_reporting_cadence_for_annual_wins_over_fragmented_duplicate(
    conn: sqlite3.Connection,
) -> None:
    """A sparse quarterly duplicate must not mask the annual cadence marker."""
    _seed(conn, "NU", "Capital adequacy ratio", "quarterly", [("2025-03-31", "Q1", 16.9)])
    _seed(conn, "NU", _CAR, "annual", _CAR_FY)
    assert reporting_cadence_for(conn, "NU", "Capital adequacy ratio") == "annual"


def test_reporting_cadence_for_missing_column_defaults_quarterly() -> None:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _schema(c, with_cadence=False)
    c.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source) "
        "VALUES ('NU', ?, 'percent', 'ir_doc')",
        (_CAR,),
    )
    c.commit()
    assert reporting_cadence_for(c, "NU", _CAR) == "quarterly"


# --------------------------------------------------------------------------- #
# _fetch_kpi_history — FY-only for annual, unchanged for quarterly
# --------------------------------------------------------------------------- #
def test_fetch_history_annual_reads_fy_only_counts_years(conn: sqlite3.Connection) -> None:
    _seed(conn, "NU", _CAR, "annual", _CAR_FY + _CAR_INTERIM)
    obs = _fetch_kpi_history(conn, "NU", "Capital adequacy ratio", 2)
    assert obs is not None
    # 2 consecutive periods == 2 YEARS (latest two FY rows), never an interim print
    assert [str(o.value) for o in obs] == ["16.6", "18.1"]
    assert [o.period_end.year for o in obs] == [2025, 2024]
    assert 16.9 not in [float(o.value) for o in obs]  # interim Q1'25 excluded


def test_fetch_history_quarterly_unchanged_reads_all_types(conn: sqlite3.Connection) -> None:
    _seed(
        conn,
        "MELI",
        "GMV growth",
        "quarterly",
        [("2025-03-31", "Q1", 30.0), ("2024-12-31", "Q4", 28.0), ("2024-09-30", "Q3", 26.0)],
    )
    obs = _fetch_kpi_history(conn, "MELI", "GMV growth", 2)
    assert obs is not None
    assert [str(o.value) for o in obs] == ["30", "28"]  # latest two, any period type


def test_evaluate_annual_rule_breaches_on_two_years(conn: sqlite3.Connection) -> None:
    _seed(conn, "NU", _CAR, "annual", _CAR_FY + _CAR_INTERIM)
    rule = BreakRule(
        rule_id="car_below_20",
        kpi_name="Capital adequacy ratio",
        comparator=Comparator.LT,
        threshold=Decimal("20"),
        unit=Unit.PERCENT,
        consecutive_periods=2,
        narrative="CAR < 20% for two years",
    )
    history = _fetch_kpi_history(conn, "NU", rule.kpi_name, rule.consecutive_periods)
    result = evaluate_rule(rule, history)
    assert result.status is BreachStatus.BREACH
    # the evidence is two ANNUAL prints, not a year-end + interim mix
    assert [o.period_end.year for o in result.observations] == [2025, 2024]


def test_evaluate_annual_rule_ok_when_latest_year_passes(conn: sqlite3.Connection) -> None:
    _seed(conn, "NU", _CAR, "annual", _CAR_FY + _CAR_INTERIM)
    rule = BreakRule(
        rule_id="car_below_13",
        kpi_name="Capital adequacy ratio",
        comparator=Comparator.LT,
        threshold=Decimal("13"),
        unit=Unit.PERCENT,
        consecutive_periods=1,
        narrative="CAR < 13%",
    )
    history = _fetch_kpi_history(conn, "NU", rule.kpi_name, rule.consecutive_periods)
    result = evaluate_rule(rule, history)
    assert result.status is BreachStatus.OK
    assert [str(o.value) for o in result.observations] == ["16.6"]  # latest FY


# --------------------------------------------------------------------------- #
# persistence — cadence stamp (authoritative), defensive on missing column
# --------------------------------------------------------------------------- #
def test_persist_manifest_stamps_annual_cadence(conn: sqlite3.Connection) -> None:
    manifest = KpiExtractionManifest(
        ticker="NU",
        period_end=datetime(2025, 12, 31),  # naive-UTC convention
        fiscal_period_type=FiscalPeriodType.FY,
        source_doc_id=42,
        primary_source=SourceType.IR_DOC,
        cadences={_CAR: ReportingCadence.ANNUAL},
        values=[KpiValue(name=_CAR, value=Decimal("16.6"), unit=Unit.PERCENT)],
    )
    result = persist_manifest(conn, run_id="t", manifest=manifest)
    assert result.inserted == 1
    row = conn.execute(
        "SELECT reporting_cadence FROM kpi_definitions WHERE ticker='NU' AND name=?", (_CAR,)
    ).fetchone()
    assert row["reporting_cadence"] == "annual"
    fact = conn.execute("SELECT fiscal_period_type FROM kpi_facts WHERE ticker='NU'").fetchone()
    assert fact["fiscal_period_type"] == "FY"


def test_find_or_create_cadence_defensive_without_column() -> None:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _schema(c, with_cadence=False)
    # Must not raise even though the column is absent.
    kid = find_or_create_kpi_definition(
        c,
        ticker="NU",
        name=_CAR,
        unit=Unit.PERCENT,
        primary_source=SourceType.IR_DOC,
        reporting_cadence=ReportingCadence.ANNUAL,
    )
    assert isinstance(kid, int) and kid > 0


# --------------------------------------------------------------------------- #
# financials — annual KPI series builder + cadence routing
# --------------------------------------------------------------------------- #
def test_annual_kpi_raw_for_excludes_interim(conn: sqlite3.Connection) -> None:
    _seed(conn, "NU", _CAR, "annual", _CAR_FY + _CAR_INTERIM)
    raw = _annual_kpi_raw_for(conn, "NU", "Capital adequacy ratio")
    assert raw is not None
    name, unit, by_year, src_by_year = raw
    assert name == _CAR
    assert unit == "%"
    assert by_year == {2023: 13.7, 2024: 18.1, 2025: 16.6}  # interim years not added
    # No documents table in this fixture -> provenance degrades to empty (S2 PR2).
    assert src_by_year == {}


def test_align_annual_kpis_builds_shared_year_axis() -> None:
    series, years = _align_annual_kpis([(_CAR, "%", {2023: 13.7, 2024: 18.1, 2025: 16.6}, {})])
    assert years == [2023, 2024, 2025]
    assert len(series) == 1
    assert series[0].values == [13.7, 18.1, 16.6]
    assert series[0].years == [2023, 2024, 2025]


def test_resolve_priorities_routes_annual_off_quarterly_axis(tmp_path: Path) -> None:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    _schema(c)
    _seed(c, "NU", _CAR, "annual", _CAR_FY + _CAR_INTERIM)
    _seed(c, "NU", "NIM", "quarterly", [("2025-03-31", "Q1", 18.0), ("2024-12-31", "Q4", 17.5)])
    c.close()

    resolved, kpi_series, annual_series, annual_years = _resolve_priorities(
        ["Capital adequacy ratio", "NIM"],
        line_items=[],
        ticker="NU",
        repo_root=tmp_path,
        quarter_labels=["2025 Q1"],
        quarter_labels_full=["2024 Q4", "2025 Q1"],
    )
    annual_names = {s.name for s in annual_series}
    quarterly_names = {s.name for s in kpi_series}
    assert _CAR in annual_names  # annual KPI lands on the annual axis
    assert _CAR not in quarterly_names  # and NOT on the gappy quarterly axis
    assert "NIM" in quarterly_names  # quarterly KPI stays quarterly
    assert annual_years == [2023, 2024, 2025]
    assert _CAR in resolved and "NIM" in resolved


# --------------------------------------------------------------------------- #
# charts_v2 — cadence-parametrized YoY heatmap
# --------------------------------------------------------------------------- #
def test_yoy_heatmap_annual_stride_renders_year_axis() -> None:
    html = yoy_heatmap_table(
        [MatrixRow(name=_CAR, levels=[13.7, 18.1, 16.6], unit="%")],
        ["2023", "2024", "2025"],
        title="",
        display_quarters=3,
        cagr_periods=(1, 2),
        period_stride=1,
        periods_per_year=1,
    )
    # fiscal-year headers + level cells render (percent => absolute level mode)
    for token in ("2023", "2024", "2025", "13.7", "18.1", "16.6", "1y CAGR"):
        assert token in html


def test_yoy_heatmap_quarterly_defaults_unchanged() -> None:
    """A flow series with the default (quarterly) stride still computes YoY at
    4 periods back — the defaults preserve the pre-existing behaviour."""
    levels = [100.0, 110.0, 120.0, 130.0, 200.0]  # Q5 vs Q1 => +100% YoY
    html = yoy_heatmap_table(
        [MatrixRow(name="Revenue", levels=levels, unit="USD millions")],
        ["2024 Q1", "2024 Q2", "2024 Q3", "2024 Q4", "2025 Q1"],
        title="",
        display_quarters=12,
    )
    assert "+100.0%" in html  # 200/100 - 1, YoY at stride 4
