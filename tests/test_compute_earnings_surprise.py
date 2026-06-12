"""Tests for src/compute/earnings_surprise.py — beat-rate aggregation, NULL
handling, lookback windowing, and ordering semantics.

These focus on the pure-compute layer; integration with HoldingScorecard is
exercised in test_compute_holding_scorecard.py.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from compute.earnings_surprise import (
    EMPTY_SCORECARD,
    SurpriseMetric,
    _aggregate_one_side,
    _to_decimal,
    surprise_scorecard_for,
)

# --- _to_decimal coercion ---------------------------------------------------


def test_to_decimal_string_value() -> None:
    assert _to_decimal("30.29") == Decimal("30.29")


def test_to_decimal_int_value() -> None:
    """SQLite NUMERIC strips trailing zeros — round-trip can return int."""
    assert _to_decimal(-1800) == Decimal("-1800")


def test_to_decimal_float_value() -> None:
    assert _to_decimal(1.5) == Decimal("1.5")


def test_to_decimal_none() -> None:
    assert _to_decimal(None) is None


def test_to_decimal_bool_rejected() -> None:
    """bool is an int subclass; must not coerce to 0/1."""
    assert _to_decimal(True) is None


def test_to_decimal_nan_inf() -> None:
    assert _to_decimal(float("nan")) is None
    assert _to_decimal(float("inf")) is None


def test_to_decimal_empty_string() -> None:
    assert _to_decimal("") is None
    assert _to_decimal("  ") is None


def test_to_decimal_garbage_returns_none() -> None:
    assert _to_decimal("not a decimal") is None


# --- _aggregate_one_side ----------------------------------------------------


def test_aggregate_pure_beats() -> None:
    metric = _aggregate_one_side(
        [Decimal("10"), Decimal("5"), Decimal("2")],
        latest_value=Decimal("10"),
    )
    assert metric.beats == 3
    assert metric.misses == 0
    assert metric.no_data == 0
    assert metric.beat_rate_pct == Decimal("100.0")
    assert metric.avg_surprise_pct == Decimal("5.67")
    assert metric.latest_surprise_pct == Decimal("10")


def test_aggregate_zero_counts_as_beat() -> None:
    """In-line (surprise == 0) counts as a beat by industry convention —
    meeting consensus exactly is read as a non-miss, not a miss."""
    metric = _aggregate_one_side(
        [Decimal("0")],
        latest_value=Decimal("0"),
    )
    assert metric.beats == 1
    assert metric.misses == 0
    assert metric.beat_rate_pct == Decimal("100.0")


def test_aggregate_mixed_with_nulls() -> None:
    """Nulls bucket into no_data and exclude from both beat-rate denom AND avg."""
    metric = _aggregate_one_side(
        [Decimal("20"), None, Decimal("-5"), None, Decimal("10")],
        latest_value=Decimal("20"),
    )
    assert metric.beats == 2  # 20, 10
    assert metric.misses == 1  # -5
    assert metric.no_data == 2
    # 2 / (2+1) * 100 = 66.7
    assert metric.beat_rate_pct == Decimal("66.7")
    # avg over the 3 non-null: (20 + (-5) + 10) / 3 = 8.33
    assert metric.avg_surprise_pct == Decimal("8.33")


def test_aggregate_all_nulls_yields_none_rates() -> None:
    """No matched records (all NULL) -> beat_rate is None, not 0%.

    Critical for post-FMP-lapse revenue side: caller mustn't display 0% beat
    rate when the source is simply absent."""
    metric = _aggregate_one_side([None, None, None], latest_value=None)
    assert metric.no_data == 3
    assert metric.beats == 0
    assert metric.beat_rate_pct is None
    assert metric.avg_surprise_pct is None
    assert metric.latest_surprise_pct is None


def test_aggregate_empty_list() -> None:
    metric = _aggregate_one_side([], latest_value=None)
    assert metric == SurpriseMetric(
        beats=0,
        misses=0,
        no_data=0,
        beat_rate_pct=None,
        avg_surprise_pct=None,
        latest_surprise_pct=None,
    )


def test_aggregate_beat_rate_rounding() -> None:
    """Beat rate quantized to 1 dp; e.g. 5 beats / 7 matched = 71.4..."""
    metric = _aggregate_one_side(
        [Decimal("1")] * 5 + [Decimal("-1")] * 2,
        latest_value=Decimal("1"),
    )
    # 5/7 = 0.71428... * 100 = 71.4
    assert metric.beat_rate_pct == Decimal("71.4")


# --- surprise_scorecard_for DB integration ----------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    """In-memory DB with just the earnings_surprises table — same shape as
    the migration produces."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE earnings_surprises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            release_date TEXT NOT NULL,
            eps_estimate NUMERIC,
            eps_actual NUMERIC,
            revenue_estimate NUMERIC,
            revenue_actual NUMERIC,
            eps_surprise_pct NUMERIC,
            revenue_surprise_pct NUMERIC,
            num_analysts_eps INTEGER,
            num_analysts_revenue INTEGER,
            source_name TEXT NOT NULL,
            source_url TEXT,
            fetched_at TEXT NOT NULL,
            ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    return c


def _insert(
    conn: sqlite3.Connection,
    ticker: str,
    release_date: str,
    eps: str | None,
    rev: str | None,
) -> None:
    conn.execute(
        "INSERT INTO earnings_surprises "
        "(ticker, release_date, eps_surprise_pct, revenue_surprise_pct, "
        " source_name, fetched_at) VALUES (?, ?, ?, ?, 'fmp_calendar', '2026-05-13T12:00:00')",
        (ticker, release_date, eps, rev),
    )
    conn.commit()


def test_scorecard_no_rows_returns_empty(conn: sqlite3.Connection) -> None:
    """Empty table -> EMPTY_SCORECARD sentinel; callers can short-circuit on
    total_quarters == 0."""
    sc = surprise_scorecard_for(conn, "X")
    assert sc is EMPTY_SCORECARD
    assert sc.total_quarters == 0


def test_scorecard_orders_by_release_date_desc(conn: sqlite3.Connection) -> None:
    """Most-recent record contributes the `latest_surprise_pct` regardless
    of insertion order."""
    _insert(conn, "X", "2024-08-06", "5.0", "1.0")
    _insert(conn, "X", "2025-05-21", "-3.0", "-1.0")  # latest
    _insert(conn, "X", "2024-11-19", "10.0", "2.0")
    sc = surprise_scorecard_for(conn, "X")
    assert sc.total_quarters == 3
    assert sc.eps.latest_surprise_pct == Decimal("-3.0")
    assert sc.revenue.latest_surprise_pct == Decimal("-1.0")


def test_scorecard_lookback_caps_window(conn: sqlite3.Connection) -> None:
    """Lookback=2 takes only the most recent 2; older history excluded."""
    _insert(conn, "X", "2023-08-06", "-50.0", None)  # oldest, big miss
    _insert(conn, "X", "2023-11-19", "-40.0", None)  # second oldest, miss
    _insert(conn, "X", "2024-08-06", "10.0", None)
    _insert(conn, "X", "2024-11-19", "15.0", None)  # latest
    sc = surprise_scorecard_for(conn, "X", lookback_quarters=2)
    assert sc.total_quarters == 2
    # Only the 2 most recent (both beats); older misses excluded
    assert sc.eps.beats == 2
    assert sc.eps.misses == 0
    assert sc.eps.beat_rate_pct == Decimal("100.0")


def test_scorecard_zero_lookback_returns_empty(conn: sqlite3.Connection) -> None:
    """Defensive: lookback=0 returns EMPTY_SCORECARD without hitting the DB.
    Negative values likewise."""
    _insert(conn, "X", "2024-08-06", "10.0", "5.0")
    assert surprise_scorecard_for(conn, "X", lookback_quarters=0) is EMPTY_SCORECARD
    assert surprise_scorecard_for(conn, "X", lookback_quarters=-1) is EMPTY_SCORECARD


def test_scorecard_filters_by_ticker(conn: sqlite3.Connection) -> None:
    """Records for another ticker must not leak in."""
    _insert(conn, "X", "2024-08-06", "10.0", "5.0")
    _insert(conn, "Y", "2024-08-06", "-50.0", "-30.0")
    sc = surprise_scorecard_for(conn, "X")
    assert sc.total_quarters == 1
    assert sc.eps.beats == 1


def test_scorecard_normalizes_ticker_uppercase(conn: sqlite3.Connection) -> None:
    """Calls with lowercase ticker still find uppercase-stored rows."""
    _insert(conn, "WIX", "2024-08-06", "10.0", "5.0")
    sc = surprise_scorecard_for(conn, "wix")
    assert sc.total_quarters == 1


def test_scorecard_post_fmp_lapse_scenario(conn: sqlite3.Connection) -> None:
    """End-to-end shape after FMP coverage drops: EPS via yfinance (full),
    revenue all NULL. EPS beat-rate computes; revenue beat-rate is None."""
    _insert(conn, "X", "2024-08-06", "10.0", None)
    _insert(conn, "X", "2024-11-19", "-5.0", None)
    _insert(conn, "X", "2025-02-19", "12.0", None)
    _insert(conn, "X", "2025-05-21", "8.0", None)
    sc = surprise_scorecard_for(conn, "X")
    assert sc.total_quarters == 4
    assert sc.eps.beats == 3
    assert sc.eps.misses == 1
    assert sc.eps.beat_rate_pct == Decimal("75.0")
    assert sc.revenue.no_data == 4
    assert sc.revenue.beat_rate_pct is None  # NOT 0%
    assert sc.revenue.avg_surprise_pct is None
