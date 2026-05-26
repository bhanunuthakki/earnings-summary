"""Tests for src/timeseries/ — primitives + loaders.

Golden-data tests on synthetic series so the expected behavior is
deterministic and independent of the live portfolio.db state. Mirrors
the fixture shape used in test_predictions_store.py."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from timeseries import (
    Observation,
    correlation_matrix,
    detect_inflection,
    detect_trend,
    load_financial_series,
    load_kpi_series,
    load_segment_series,
    rolling_zscore_anomalies,
    seasonal_decompose,
    yoy_acceleration,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _series(values: Sequence[float], start: str = "2020-03-31") -> list[Observation]:
    """Build a quarterly Observation list with sequential 90-day cadence."""
    base = datetime.fromisoformat(start)
    return [
        Observation(period_end=base + timedelta(days=90 * i), value=float(v))
        for i, v in enumerate(values)
    ]


def _facts_db(tmp_path: Path) -> Path:
    """Build a minimal portfolio.db with financial_facts / kpi_facts /
    kpi_definitions / segment_facts so the loaders have something to read.
    """
    p = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(p))
    try:
        conn.executescript(
            """
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR NOT NULL,
                period_end DATETIME NOT NULL,
                fiscal_period_type VARCHAR NOT NULL,
                line_item VARCHAR NOT NULL,
                value NUMERIC(24,6) NOT NULL,
                currency VARCHAR(3),
                unit VARCHAR NOT NULL,
                source_doc_id INTEGER NOT NULL,
                confidence FLOAT NOT NULL DEFAULT 1.0
            );
            CREATE TABLE kpi_definitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR NOT NULL,
                name TEXT NOT NULL,
                unit VARCHAR NOT NULL DEFAULT 'actual',
                primary_source VARCHAR NOT NULL,
                fallback_source VARCHAR,
                ir_url VARCHAR,
                threshold_tier VARCHAR,
                threshold_low FLOAT,
                threshold_high FLOAT,
                notes TEXT,
                UNIQUE(ticker, name)
            );
            CREATE TABLE kpi_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR NOT NULL,
                period_end DATETIME NOT NULL,
                fiscal_period_type VARCHAR NOT NULL,
                kpi_definition_id INTEGER NOT NULL,
                value NUMERIC(24,6) NOT NULL,
                unit VARCHAR NOT NULL,
                source_doc_id INTEGER NOT NULL
            );
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
        conn.commit()
    finally:
        conn.close()
    return p


def _insert_financial(
    db: Path,
    ticker: str,
    line_item: str,
    values: Sequence[float],
    *,
    start: str = "2020-03-31",
    source_doc_ids: list[int] | None = None,
) -> None:
    """Insert a synthetic financial_facts series. Optionally repeat across
    multiple source_doc_ids per period to exercise the dedup path."""
    base = datetime.fromisoformat(start)
    sids = source_doc_ids or [1]
    conn = sqlite3.connect(str(db))
    try:
        for i, v in enumerate(values):
            period_end = (base + timedelta(days=90 * i)).strftime("%Y-%m-%d %H:%M:%S")
            quarter = f"Q{(i % 4) + 1}"
            for sid in sids:
                conn.execute(
                    "INSERT INTO financial_facts(ticker, period_end, fiscal_period_type, "
                    "line_item, value, unit, source_doc_id) VALUES (?,?,?,?,?,?,?)",
                    (ticker, period_end, quarter, line_item, float(v), "USD", sid),
                )
        conn.commit()
    finally:
        conn.close()


def _insert_kpi(
    db: Path, ticker: str, kpi_name: str, values: list[float], *, start: str = "2020-03-31"
) -> None:
    """Insert kpi_definitions row (if new) + kpi_facts series."""
    base = datetime.fromisoformat(start)
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO kpi_definitions(ticker, name, unit, primary_source) "
            "VALUES (?,?,?,?)",
            (ticker, kpi_name, "actual", "ir_doc"),
        )
        _ = cur
        kpi_def_id = conn.execute(
            "SELECT id FROM kpi_definitions WHERE ticker = ? AND name = ?", (ticker, kpi_name)
        ).fetchone()[0]
        for i, v in enumerate(values):
            period_end = (base + timedelta(days=90 * i)).strftime("%Y-%m-%d %H:%M:%S")
            quarter = f"Q{(i % 4) + 1}"
            conn.execute(
                "INSERT INTO kpi_facts(ticker, period_end, fiscal_period_type, "
                "kpi_definition_id, value, unit, source_doc_id) VALUES (?,?,?,?,?,?,?)",
                (ticker, period_end, quarter, int(kpi_def_id), float(v), "actual", 1),
            )
        conn.commit()
    finally:
        conn.close()


def _insert_segment(
    db: Path,
    ticker: str,
    segment: str,
    metric: str,
    values: Sequence[float],
    *,
    start: str = "2020-03-31",
) -> None:
    """Seed a quarterly segment series in the junction model.

    Translates legacy `metric` strings into (dim_type, junction_metric); the
    test passes `metric="revenue"` directly (the post-junction metric), in
    which case the legacy table is empty and the fallback (business_unit
    dim_type, metric verbatim) applies — same path as
    `pipeline.segment_junction_writer.segment_fact_to_dimension`.
    """
    if metric == "revenue_by_product":
        dim_type, junction_metric = ("product", "revenue")
    elif metric == "revenue_by_geography":
        dim_type, junction_metric = ("geography", "revenue")
    elif metric == "operating_income":
        dim_type, junction_metric = ("business_unit", "operating_income")
    else:
        dim_type, junction_metric = ("business_unit", metric)
    base = datetime.fromisoformat(start)
    conn = sqlite3.connect(str(db))
    try:
        for i, v in enumerate(values):
            period_end = (base + timedelta(days=90 * i)).strftime("%Y-%m-%d %H:%M:%S")
            quarter = f"Q{(i % 4) + 1}"
            cur = conn.execute(
                "SELECT id FROM segment_periods "
                "WHERE ticker = ? AND period_end = ? AND fiscal_period_type = ? "
                "AND source_doc_id = 1",
                (ticker, period_end, quarter),
            )
            row = cur.fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO segment_periods "
                    "(ticker, period_end, fiscal_period_type, source_doc_id, "
                    " currency, unit) VALUES (?, ?, ?, 1, 'USD', 'actual')",
                    (ticker, period_end, quarter),
                )
                period_id = cur.lastrowid
            else:
                period_id = row[0]
            conn.execute(
                "INSERT INTO segment_dimensions "
                "(period_id, dim_type, dim_name, metric, value) "
                "VALUES (?, ?, ?, ?, ?)",
                (period_id, dim_type, segment, junction_metric, float(v)),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# detect_trend
# ---------------------------------------------------------------------------


def test_detect_trend_monotonic_linear_is_flat_acceleration() -> None:
    """Constant slope → 'flat' (second derivative is zero). Slope value
    still carries the direction info."""
    s = _series([100.0 + 5 * i for i in range(16)])
    r = detect_trend(s)
    assert r["direction"] == "flat"
    assert r["slope"] == pytest.approx(5.0)
    assert r["r2"] == pytest.approx(1.0)
    assert r["statistical_significance"] is True


def test_detect_trend_quadratic_is_accelerating() -> None:
    s = _series([100.0 + i * i for i in range(16)])
    r = detect_trend(s)
    assert r["direction"] == "accelerating"
    s1 = cast(float, r["first_half_slope"])
    s2 = cast(float, r["second_half_slope"])
    assert s2 > s1


def test_detect_trend_log_growth_is_decelerating() -> None:
    s = _series([100.0 + 10 * math.log(i + 1) for i in range(16)])
    r = detect_trend(s)
    assert r["direction"] == "decelerating"


def test_detect_trend_v_shape_is_inflecting() -> None:
    vals = [100.0 - 5 * i for i in range(8)] + [60.0 + 5 * i for i in range(8)]
    s = _series(vals)
    r = detect_trend(s)
    assert r["direction"] == "inflecting"


def test_detect_trend_flat_level_is_flat() -> None:
    s = _series([100.0 + 0.05 * (i % 2) for i in range(16)])
    r = detect_trend(s)
    assert r["direction"] == "flat"


def test_detect_trend_short_series_returns_insufficient_data() -> None:
    s = _series([100.0, 105.0, 110.0])
    r = detect_trend(s)
    assert r.get("insufficient_data") is True
    assert r["n"] == 3


# ---------------------------------------------------------------------------
# detect_inflection
# ---------------------------------------------------------------------------


def test_detect_inflection_two_regime_series_locates_break() -> None:
    """Series goes flat for 8 quarters then accelerates — changepoint
    should land near index 8 (give or take 1)."""
    vals = [100.0] * 8 + [100.0 + 10 * (i + 1) for i in range(8)]
    s = _series(vals)
    r = detect_inflection(s)
    assert r["inflection_period"] is not None
    assert abs(cast(int, r["inflection_index"]) - 8) <= 2
    assert cast(float, r["magnitude"]) > 1.0
    assert r["method"] in {"ruptures", "rolling_window"}


def test_detect_inflection_flat_series_returns_no_inflection() -> None:
    """A perfectly flat series should NOT spuriously detect a changepoint."""
    s = _series([100.0 + 0.1 * (i % 2) for i in range(16)])
    r = detect_inflection(s)
    assert r["inflection_period"] is None or cast(float, r["magnitude"]) < 0.5


def test_detect_inflection_too_short_returns_insufficient() -> None:
    s = _series([1.0, 2.0, 3.0])
    r = detect_inflection(s)
    assert r.get("insufficient_data") is True


# ---------------------------------------------------------------------------
# rolling_zscore_anomalies
# ---------------------------------------------------------------------------


def test_rolling_zscore_detects_known_spike() -> None:
    """Baseline of ~100 for 8 quarters then a 200-spike — z-score on
    the spike against the prior 8 must exceed the threshold."""
    vals = [100.0, 101.0, 99.0, 100.5, 100.0, 99.5, 101.0, 100.0, 200.0]
    s = _series(vals)
    anomalies = rolling_zscore_anomalies(s, window=8, threshold=2.5)
    assert len(anomalies) == 1
    assert anomalies[0]["period_end"] == s[-1].period_end.date().isoformat()
    assert cast(float, anomalies[0]["zscore"]) > 50  # huge spike vs near-zero std


def test_rolling_zscore_clean_series_returns_empty() -> None:
    s = _series([100.0 + 0.5 * (i % 3 - 1) for i in range(16)])
    anomalies = rolling_zscore_anomalies(s, window=8, threshold=2.5)
    assert anomalies == []


def test_rolling_zscore_short_series_returns_empty() -> None:
    s = _series([1.0, 2.0, 3.0])
    assert rolling_zscore_anomalies(s, window=8) == []


# ---------------------------------------------------------------------------
# seasonal_decompose
# ---------------------------------------------------------------------------


def test_seasonal_decompose_recovers_quarterly_pattern() -> None:
    """A clear seasonal signal: trend = constant 100 + seasonal pattern
    of (+10, -5, -5, 0) every 4 quarters. Strength should be high."""
    pattern = [10.0, -5.0, -5.0, 0.0]
    vals = [100.0 + pattern[i % 4] for i in range(20)]
    s = _series(vals)
    r = seasonal_decompose(s, period=4)
    assert r["n"] == 20
    assert r["period"] == 4
    assert r["method"] in {"stl", "classical"}
    assert cast(float, r["seasonal_strength"]) > 0.5  # strong seasonal signal


def test_seasonal_decompose_short_series_insufficient_data() -> None:
    s = _series([100.0] * 5)
    r = seasonal_decompose(s, period=4)
    assert r.get("insufficient_data") is True


def test_seasonal_decompose_invalid_period_insufficient_data() -> None:
    s = _series([100.0] * 8)
    r = seasonal_decompose(s, period=1)
    assert r.get("insufficient_data") is True


# ---------------------------------------------------------------------------
# yoy_acceleration
# ---------------------------------------------------------------------------


def test_yoy_acceleration_shape_and_count() -> None:
    """Linear growth with constant absolute increment → 12 YoY observations
    from a 16-quarter input (lose the first 4 to the YoY lag)."""
    s = _series([100.0 + 5 * i for i in range(16)])
    r = yoy_acceleration(s)
    assert "insufficient_data" not in r
    yoy_series = cast(list[object], r["yoy_series"])
    assert isinstance(yoy_series, list)
    assert len(yoy_series) == 12  # 16 - 4 lag = 12 YoY points


def test_yoy_acceleration_decelerating_signal() -> None:
    """Slowing growth (each delta < prior delta) → clear deceleration."""
    vals = [100.0]
    delta = 20.0
    for _ in range(15):
        vals.append(vals[-1] + delta)
        delta *= 0.85  # next increment is 15% smaller
    s = _series(vals)
    r = yoy_acceleration(s)
    assert r["trend"] == "decelerating"


def test_yoy_acceleration_short_series_insufficient_data() -> None:
    s = _series([100.0, 105.0, 110.0, 115.0])
    r = yoy_acceleration(s)
    assert r.get("insufficient_data") is True


def test_yoy_acceleration_accelerating_signal() -> None:
    """Compound growth at increasing rate → YoY accelerates."""
    vals = [100.0]
    growth = 0.02
    for _ in range(15):
        growth += 0.005  # rate is increasing each quarter
        vals.append(vals[-1] * (1 + growth))
    s = _series(vals)
    r = yoy_acceleration(s)
    assert r["trend"] == "accelerating"


# ---------------------------------------------------------------------------
# correlation_matrix
# ---------------------------------------------------------------------------


def test_correlation_matrix_perfect_correlation_diagonal_and_off_diagonal(
    tmp_path: Path,
) -> None:
    """Two identical KPI series → correlation should be 1.0 off-diagonal."""
    db = _facts_db(tmp_path)
    vals = [100.0 + 5 * i for i in range(12)]
    _insert_kpi(db, "TEST", "kpi_a", vals)
    _insert_kpi(db, "TEST", "kpi_b", vals)
    r = correlation_matrix("TEST", ["kpi_a", "kpi_b"], db_path=db)
    assert "insufficient_data" not in r
    matrix = cast("dict[str, dict[str, float]]", r["matrix"])
    assert isinstance(matrix, dict)
    assert matrix["kpi_a"]["kpi_a"] == pytest.approx(1.0)
    assert matrix["kpi_a"]["kpi_b"] == pytest.approx(1.0)


def test_correlation_matrix_anti_correlation(tmp_path: Path) -> None:
    """Inverted series → correlation should approach -1.0."""
    db = _facts_db(tmp_path)
    vals_a = [100.0 + 5 * i for i in range(12)]
    vals_b = [200.0 - 5 * i for i in range(12)]
    _insert_kpi(db, "TEST", "kpi_up", vals_a)
    _insert_kpi(db, "TEST", "kpi_down", vals_b)
    r = correlation_matrix("TEST", ["kpi_up", "kpi_down"], db_path=db)
    matrix = cast("dict[str, dict[str, float]]", r["matrix"])
    assert isinstance(matrix, dict)
    assert matrix["kpi_up"]["kpi_down"] == pytest.approx(-1.0)


def test_correlation_matrix_missing_kpis_insufficient_data(tmp_path: Path) -> None:
    db = _facts_db(tmp_path)
    r = correlation_matrix("TEST", ["nope_a", "nope_b"], db_path=db)
    assert r.get("insufficient_data") is True


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def test_load_financial_series_returns_sorted_observations(tmp_path: Path) -> None:
    db = _facts_db(tmp_path)
    _insert_financial(db, "TEST", "revenue", [100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0])
    s = load_financial_series("TEST", "revenue", db_path=db)
    assert len(s) == 8
    # Strictly ascending period_end
    assert all(s[i].period_end < s[i + 1].period_end for i in range(len(s) - 1))
    # Values preserved in order
    assert [obs.value for obs in s] == [100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0]


def test_load_financial_series_dedupes_across_source_docs(tmp_path: Path) -> None:
    """Multiple source_doc_id rows per period collapse to one observation."""
    db = _facts_db(tmp_path)
    _insert_financial(
        db, "TEST", "revenue", [100.0, 110.0, 120.0, 130.0], source_doc_ids=[1, 2, 3]
    )
    s = load_financial_series("TEST", "revenue", db_path=db)
    assert len(s) == 4  # not 12
    assert [obs.value for obs in s] == [100.0, 110.0, 120.0, 130.0]


def test_load_financial_series_missing_db_returns_empty(tmp_path: Path) -> None:
    s = load_financial_series("TEST", "revenue", db_path=tmp_path / "no.db")
    assert s == []


def test_load_kpi_series_joins_through_definitions(tmp_path: Path) -> None:
    db = _facts_db(tmp_path)
    _insert_kpi(db, "TEST", "Operating Margin (GAAP)", [0.20, 0.22, 0.24, 0.25, 0.26, 0.27])
    s = load_kpi_series("TEST", "Operating Margin (GAAP)", db_path=db)
    assert len(s) == 6
    assert s[0].value == pytest.approx(0.20)
    assert s[-1].value == pytest.approx(0.27)


def test_load_kpi_series_unknown_kpi_returns_empty(tmp_path: Path) -> None:
    db = _facts_db(tmp_path)
    _insert_kpi(db, "TEST", "real_kpi", [1.0, 2.0, 3.0, 4.0])
    s = load_kpi_series("TEST", "Not A KPI", db_path=db)
    assert s == []


def test_load_segment_series_filters_segment_and_metric(tmp_path: Path) -> None:
    db = _facts_db(tmp_path)
    _insert_segment(db, "TEST", "Cloud", "revenue", [10.0, 12.0, 14.0, 16.0, 18.0])
    _insert_segment(db, "TEST", "Services", "revenue", [50.0, 51.0, 52.0, 53.0, 54.0])
    s = load_segment_series("TEST", "Cloud", "revenue", db_path=db)
    assert len(s) == 5
    assert [obs.value for obs in s] == [10.0, 12.0, 14.0, 16.0, 18.0]


def test_loaders_with_repo_root_argument(tmp_path: Path) -> None:
    """Both `repo_root` and `db_path` work as ways to find the DB. When
    `repo_root` is passed, the loader looks for repo_root/data/portfolio.db."""
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    db = _facts_db(repo / "data")
    # _facts_db put it at repo/data/portfolio.db — verify
    assert db == repo / "data" / "portfolio.db"
    _insert_financial(db, "TEST", "revenue", [100.0, 110.0, 120.0, 130.0, 140.0])
    s = load_financial_series("TEST", "revenue", repo_root=repo)
    assert len(s) == 5
