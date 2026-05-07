"""Tests for src/compute/batch_dcf.py — default-profile derivation + linear decay."""

from __future__ import annotations

from itertools import pairwise

from compute.batch_dcf import (
    _GROWTH_CEILING,
    _GROWTH_FLOOR,
    derive_default_profile,
    linear_decay_to_terminal,
)


def test_linear_decay_endpoints() -> None:
    """First value = base, last value = terminal."""
    series = linear_decay_to_terminal(0.10, 0.025, horizon_years=10)
    assert len(series) == 10
    assert abs(series[0] - 0.10) < 1e-9
    assert abs(series[-1] - 0.025) < 1e-9


def test_linear_decay_monotone_decreasing() -> None:
    series = linear_decay_to_terminal(0.20, 0.03, horizon_years=10)
    for prev, curr in pairwise(series):
        assert curr <= prev


def test_linear_decay_zero_horizon_returns_empty() -> None:
    assert linear_decay_to_terminal(0.10, 0.03, horizon_years=0) == []


def test_linear_decay_one_horizon_returns_just_base() -> None:
    assert linear_decay_to_terminal(0.10, 0.03, horizon_years=1) == [0.10]


def test_default_profile_returns_none_for_empty_db() -> None:
    """No KPI rows -> returns DefaultProfile with None values + a notes string."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY,
            ticker TEXT, name TEXT, unit TEXT, primary_source TEXT,
            UNIQUE(ticker, name)
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY,
            ticker TEXT, period_end TIMESTAMP, fiscal_period_type TEXT,
            kpi_definition_id INTEGER, value NUMERIC, unit TEXT, source_doc_id INTEGER
        );
        """
    )
    profile = derive_default_profile(conn, "X")
    assert profile.base_growth is None
    assert profile.fcf_margin is None
    assert "no derived KPI" in profile.notes


def test_default_profile_clamps_growth_to_ceiling() -> None:
    """A 200% YoY input clamps to the ceiling (50%) so we don't blow up the DCF."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY,
            ticker TEXT, name TEXT, unit TEXT, primary_source TEXT,
            UNIQUE(ticker, name)
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY,
            ticker TEXT, period_end TIMESTAMP, fiscal_period_type TEXT,
            kpi_definition_id INTEGER, value NUMERIC, unit TEXT, source_doc_id INTEGER
        );
        """
    )
    cur = conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source) VALUES "
        "('X', 'Revenue YoY Growth (USD)', 'percent', 'fmp')"
    )
    g_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    cur = conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source) VALUES "
        "('X', 'Net Income Margin (GAAP)', 'percent', 'fmp')"
    )
    m_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id) VALUES ('X', '2024-12-31', 'Q4', ?, '200', 'percent', 1)",
        (g_id,),
    )
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id) VALUES ('X', '2024-12-31', 'Q4', ?, '15', 'percent', 1)",
        (m_id,),
    )
    conn.commit()
    profile = derive_default_profile(conn, "X")
    assert profile.base_growth == _GROWTH_CEILING
    assert _GROWTH_FLOOR <= profile.base_growth <= _GROWTH_CEILING
    assert profile.fcf_margin == 0.15
