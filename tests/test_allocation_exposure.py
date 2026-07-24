"""C2 — deterministic exposure rollup over the segment junction (allocation.exposure)."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from allocation.exposure import (
    UNMAPPED,
    book_exposure,
    currency_exposure,
    latest_revenue_mix,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Pinned "today" so the TTM trailing window is deterministic in tests.
_NOW = date(2026, 7, 1)

# Hand-built junction schema (the segment-suite convention —
# tests/test_compute_segments.py does the same): the 0055/0166 shape reduced
# to the columns exposure.py reads, with source_doc_id relaxed to nullable so
# seeds don't need a documents row.
_SCHEMA = """
CREATE TABLE segment_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type VARCHAR(8) NOT NULL,
    source_doc_id INTEGER,
    currency VARCHAR(8),
    unit VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    period_basis VARCHAR(16)
);
CREATE TABLE segment_dimensions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id INTEGER NOT NULL REFERENCES segment_periods(id),
    dim_type VARCHAR(16) NOT NULL,
    dim_name VARCHAR(128) NOT NULL,
    value NUMERIC(20, 4) NOT NULL,
    metric VARCHAR(32) NOT NULL,
    unit VARCHAR(16)
);
"""


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return db


def _seed_period(
    db: Path,
    *,
    ticker: str,
    period_end: str,
    fiscal_period_type: str = "FY",
    period_basis: str = "annual",
    currency: str | None = "USD",
    dims: list[tuple[str, str, float, str]],  # (dim_type, dim_name, value, metric)
) -> None:
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "INSERT INTO segment_periods (ticker, period_end, fiscal_period_type, "
            "currency, unit, created_at, period_basis) VALUES (?, ?, ?, ?, 'actual', "
            "datetime('now'), ?)",
            (ticker, period_end, fiscal_period_type, currency, period_basis),
        )
        pid = int(cur.lastrowid or 0)
        for dim_type, dim_name, value, metric in dims:
            conn.execute(
                "INSERT INTO segment_dimensions (period_id, dim_type, dim_name, value, "
                "metric, unit) VALUES (?, ?, ?, ?, ?, 'actual')",
                (pid, dim_type, dim_name, value, metric),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_quarters(db: Path, ticker: str, quarters: list[str]) -> None:
    for q_end in quarters:
        _seed_period(
            db,
            ticker=ticker,
            period_end=q_end,
            fiscal_period_type="Q",
            period_basis="quarterly",
            currency="USD",
            dims=[
                ("geography", "Brazil", 60.0, "revenue"),
                ("geography", "Mexico", 40.0, "revenue"),
            ],
        )


def test_ttm_quarterly_path_sums_last_four_quarters(db_path: Path) -> None:
    _seed_quarters(db_path, "MELI", ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"])
    mix = latest_revenue_mix("MELI", db_path=db_path, now=_NOW)
    assert mix is not None
    assert mix.basis == "ttm_quarterly"
    assert mix.period_end == "2026-06-30"
    assert mix.shares == pytest.approx({"Brazil": 0.6, "Mexico": 0.4})
    assert mix.currency == "USD"


def test_annual_fallback_when_too_few_quarters(db_path: Path) -> None:
    _seed_quarters(db_path, "NOW", ["2026-03-31"])  # one quarter only
    _seed_period(
        db_path,
        ticker="NOW",
        period_end="2025-12-31",
        dims=[
            ("geography", "North America", 800.0, "revenue"),
            ("geography", "EMEA", 200.0, "revenue"),
        ],
    )
    mix = latest_revenue_mix("NOW", db_path=db_path, now=_NOW)
    assert mix is not None
    assert mix.basis == "annual"
    assert mix.period_end == "2025-12-31"
    assert mix.shares == pytest.approx({"North America": 0.8, "EMEA": 0.2})


def test_stale_quarters_outside_window_fall_back_to_annual(db_path: Path) -> None:
    _seed_quarters(db_path, "OLD", ["2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31"])
    _seed_period(
        db_path,
        ticker="OLD",
        period_end="2024-12-31",
        dims=[("geography", "United States", 10.0, "revenue")],
    )
    mix = latest_revenue_mix("OLD", db_path=db_path, now=_NOW)
    assert mix is not None
    assert mix.basis == "annual"


def test_multi_metric_revenue_summing_and_non_revenue_exclusion(db_path: Path) -> None:
    """NVO-style geography rows: several *_revenue metrics per dim plus a
    non-revenue metric that must not contaminate the mix."""
    _seed_period(
        db_path,
        ticker="NVO",
        period_end="2025-12-31",
        currency="DKK",
        dims=[
            ("geography", "North America operations", 70.0, "glp1_revenue"),
            ("geography", "North America operations", 10.0, "insulin_revenue"),
            ("geography", "International operations", 20.0, "glp1_revenue"),
            ("geography", "North America operations", 999.0, "non_current_assets"),
        ],
    )
    mix = latest_revenue_mix("NVO", db_path=db_path, now=_NOW)
    assert mix is not None
    assert mix.shares == pytest.approx(
        {"North America operations": 0.8, "International operations": 0.2}
    )


def test_garbage_disclosure_rejected(db_path: Path) -> None:
    """Negative-heavy mixes (subtotals grabbed as rows) degrade to None."""
    _seed_period(
        db_path,
        ticker="BAD",
        period_end="2025-12-31",
        dims=[
            ("geography", "Segment A", 100.0, "revenue"),
            ("geography", "Eliminations", -60.0, "revenue"),
        ],
    )
    assert latest_revenue_mix("BAD", db_path=db_path, now=_NOW) is None


def test_currency_exposure_collapse_with_unmapped(db_path: Path) -> None:
    _seed_period(
        db_path,
        ticker="MELI",
        period_end="2025-12-31",
        dims=[
            ("geography", "Brazil", 55.0, "revenue"),
            ("geography", "Mexico", 25.0, "revenue"),
            ("geography", "Other countries", 20.0, "revenue"),
        ],
    )
    mix = latest_revenue_mix("MELI", db_path=db_path, now=_NOW)
    assert mix is not None
    ccy = currency_exposure(mix)
    assert ccy == pytest.approx({"BRL": 0.55, "MXN": 0.25, UNMAPPED: 0.20})


def test_currency_map_strips_crosstab_segment_suffix(db_path: Path) -> None:
    """Prod crosstab labels read "Brazil Segment" — the currency lookup strips
    the suffix; the disclosed label survives verbatim in shares."""
    _seed_period(
        db_path,
        ticker="MELI",
        period_end="2025-12-31",
        dims=[
            ("geography", "Brazil Segment", 70.0, "revenue"),
            ("geography", "Argentina Segment", 30.0, "revenue"),
        ],
    )
    mix = latest_revenue_mix("MELI", db_path=db_path, now=_NOW)
    assert mix is not None
    assert set(mix.shares) == {"Brazil Segment", "Argentina Segment"}
    assert currency_exposure(mix) == pytest.approx({"BRL": 0.7, "ARS": 0.3})


def test_book_exposure_aggregates_and_reports_unattributed(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_period(
        db_path,
        ticker="MELI",
        period_end="2025-12-31",
        dims=[("geography", "Brazil", 100.0, "revenue")],
    )
    import portfolio_weights

    monkeypatch.setattr(
        portfolio_weights,
        "read_materialized_weights",
        lambda repo_root: {"MELI": 0.6, "FLKR": 0.4},
    )
    book = book_exposure(dim_type="geography", db_path=db_path, repo_root=tmp_path, now=_NOW)
    assert book.shares == pytest.approx({"Brazil": 0.6})
    assert "FLKR" in book.unattributed
    assert book.unattributed_weight == pytest.approx(0.4)
    assert "MELI" in book.by_name
    # Placed shares deliberately do NOT sum to 1 — unattributed is visible.
    assert sum(book.shares.values()) == pytest.approx(1.0 - book.unattributed_weight)


def test_never_raises_on_empty_db(db_path: Path, tmp_path: Path) -> None:
    assert latest_revenue_mix("NOPE", db_path=db_path, now=_NOW) is None
    book = book_exposure(db_path=db_path, repo_root=tmp_path, now=_NOW)
    assert book.shares == {}
    assert book.unattributed == {}
