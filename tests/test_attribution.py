"""S15 PR2(b) — per-position attribution narratives.

The deterministic skeleton: tracker dollar-alpha (PositionAlphaRow, never
recomputed) + the open lifecycle row (entry date/price/conviction) + the
thesis/alert/decision events inside the window, phrased as plain sentences.
Covers the joins, every degradation path (tracker offline, no entry, no
events), the book-level builder's selection/ordering, and both renderers
(holding-page section with the tracker fetch monkeypatched; Portfolio-tab
block + its compose_portfolio_page mount).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import pipeline.attribution_panel as attribution_panel
from attribution import (
    attributions_for_book,
    build_position_attribution,
    compose_narrative,
    gather_window_events,
)
from integrations.portfolio_tracker_client import (
    LivePortfolio,
    PortfolioAnalytics,
    PositionAlpha,
    PositionAlphaRow,
)
from pipeline.attribution_panel import (
    render_attribution_section,
    render_book_attribution_section,
)
from pipeline.portfolio_panel import compose_portfolio_page
from position_lifecycle import list_entries

# Hand-rolled minimal substrate: every column the SELECT * decoders touch.
_SCHEMA = """
CREATE TABLE position_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    entry_date TEXT, entry_price REAL, entry_conviction TEXT,
    entry_thesis_excerpt TEXT, entry_conditions TEXT,
    exit_date TEXT, exit_price REAL, exit_reason TEXT, lessons TEXT,
    outcome_vs_thesis TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE thesis_ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL, ticker TEXT NOT NULL,
    entry_kind TEXT NOT NULL, body TEXT, created_at TEXT NOT NULL
);
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL, ticker TEXT NOT NULL,
    trigger_kind TEXT NOT NULL, fired_at TEXT NOT NULL
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, recommendation_kind TEXT NOT NULL,
    made_at TEXT NOT NULL, outcome_at TEXT, outcome_label TEXT
);
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "attr.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _seed(db: Path, sql: str, params: tuple[object, ...]) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def _open_position(
    db: Path,
    ticker: str = "NU",
    *,
    entry_date: str | None = "2026-01-15",
    entry_price: float | None = 11.20,
    conviction: str | None = "high",
) -> None:
    _seed(
        db,
        "INSERT INTO position_entries (user_id, ticker, entry_date, entry_price, "
        "entry_conviction, created_at, updated_at) VALUES ('bhanu', ?, ?, ?, ?, 'x', 'x')",
        (ticker, entry_date, entry_price, conviction),
    )


def _alpha_row(ticker: str = "NU", alpha: float | None = 4210.0) -> PositionAlphaRow:
    return PositionAlphaRow(
        ticker=ticker,
        name=None,
        value_at_start=10_000.0,
        bought_in_window=0.0,
        sold_in_window=0.0,
        value_at_end=16_100.0,
        actual_pl=6100.0,
        spy_counterfactual_pl=1890.0,
        alpha=alpha,
        alpha_vs_qqq=None,
        alpha_vs_policy=None,
        incomplete=False,
    )


# ---------------------------------------------------------------------------
# Window events
# ---------------------------------------------------------------------------


def test_gather_window_events_counts_and_bounds(db: Path) -> None:
    _seed(db, "INSERT INTO thesis_ledger_entries (user_id, ticker, entry_kind, created_at) "
              "VALUES ('bhanu', 'NU', 'thesis_update', '2026-04-01T10:00:00')", ())  # fmt: skip
    _seed(db, "INSERT INTO thesis_ledger_entries (user_id, ticker, entry_kind, created_at) "
              "VALUES ('bhanu', 'NU', 'thesis_update', '2026-06-11T23:59:00')", ())  # fmt: skip
    # Outside the window (before start) — excluded.
    _seed(db, "INSERT INTO thesis_ledger_entries (user_id, ticker, entry_kind, created_at) "
              "VALUES ('bhanu', 'NU', 'bear_append', '2026-02-01T10:00:00')", ())  # fmt: skip
    # Other ticker — excluded.
    _seed(db, "INSERT INTO thesis_ledger_entries (user_id, ticker, entry_kind, created_at) "
              "VALUES ('bhanu', 'MELI', 'thesis_update', '2026-04-01T10:00:00')", ())  # fmt: skip
    _seed(db, "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at) "
              "VALUES ('bhanu', 'NU', 'kpi_inflection', '2026-05-01T08:00:00')", ())  # fmt: skip
    _seed(db, "INSERT INTO decisions (ticker, recommendation_kind, made_at, outcome_at, "
              "outcome_label) VALUES ('NU', 'trim', '2026-03-01', '2026-05-20', 'correct')",
          ())  # fmt: skip
    # Pending decision — not "graded in window".
    _seed(db, "INSERT INTO decisions (ticker, recommendation_kind, made_at, outcome_label) "
              "VALUES ('NU', 'add', '2026-05-01', 'pending')", ())  # fmt: skip

    events = gather_window_events(
        "nu", db_path=db, window_start="2026-03-12", window_end="2026-06-11"
    )
    assert events.ledger_counts == {"thesis_update": 2}  # same-day end timestamp included
    assert events.alert_counts == {"kpi_inflection": 1}
    assert events.decisions_graded == [("trim", "correct")]
    assert events.empty is False


def test_gather_window_events_missing_tables(tmp_path: Path) -> None:
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()
    events = gather_window_events(
        "NU", db_path=bare, window_start="2026-01-01", window_end="2026-02-01"
    )
    assert events.empty is True


# ---------------------------------------------------------------------------
# Narrative skeleton
# ---------------------------------------------------------------------------


def test_full_narrative_sentences(db: Path) -> None:
    _open_position(db)
    _seed(db, "INSERT INTO thesis_ledger_entries (user_id, ticker, entry_kind, created_at) "
              "VALUES ('bhanu', 'NU', 'thesis_update', '2026-04-01T10:00:00')", ())  # fmt: skip
    _seed(db, "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at) "
              "VALUES ('bhanu', 'NU', 'kpi_inflection', '2026-05-01T08:00:00')", ())  # fmt: skip

    attribution = build_position_attribution(
        "NU",
        db_path=db,
        alpha_row=_alpha_row(),
        window_start="2026-03-12",
        window_end="2026-06-11",
    )
    n = attribution.narrative
    assert "Beat its SPY counterfactual by $4,210 over 2026-03-12 → 2026-06-11" in n
    assert "(P&L +$6,100 vs SPY-matched +$1,890)" in n
    assert "Held since 2026-01-15 (entry $11.20, high conviction)." in n
    assert "1 thesis update" in n and "1 alert (1 kpi_inflection)" in n
    assert attribution.alpha_usd == pytest.approx(4210.0)
    assert attribution.entry is not None and attribution.entry.is_open


def test_narrative_negative_alpha_and_no_events(db: Path) -> None:
    _open_position(db, conviction=None, entry_price=None)
    row = _alpha_row(alpha=-1500.0)
    attribution = build_position_attribution(
        "NU", db_path=db, alpha_row=row,
        window_start="2026-03-12", window_end="2026-06-11",
    )  # fmt: skip
    assert "Trailed its SPY counterfactual by $1,500" in attribution.narrative
    assert "market/flow-driven" in attribution.narrative
    assert "Held since 2026-01-15." in attribution.narrative  # no price/conviction detail


def test_narrative_degrades_without_tracker_and_entry(db: Path) -> None:
    attribution = build_position_attribution(
        "NU", db_path=db, alpha_row=None, window_start=None, window_end=None
    )
    assert attribution.narrative.startswith("No tracker alpha for this window")
    # Default 90d window applied for the events scan.
    assert attribution.window_start is not None and attribution.window_end is not None
    assert "Held" not in attribution.narrative  # no lifecycle row


def test_narrative_unknown_opening(db: Path) -> None:
    _open_position(db, entry_date=None, entry_price=None, conviction="medium")
    narrative = compose_narrative(
        window_start="2026-03-12",
        window_end="2026-06-11",
        alpha_row=None,
        entry=list_entries(db_path=db, ticker="NU")[0],
        events=gather_window_events(
            "NU", db_path=db, window_start="2026-03-12", window_end="2026-06-11"
        ),
    )
    assert "opening predates the lifecycle ledger (medium conviction)." in narrative


# ---------------------------------------------------------------------------
# Book-level builder
# ---------------------------------------------------------------------------


def test_attributions_for_book_selection_and_order(db: Path) -> None:
    _open_position(db, "NU")
    _open_position(db, "MELI")
    # Closed stint — out of scope for the book view.
    _seed(
        db,
        "INSERT INTO position_entries (user_id, ticker, entry_date, exit_date, "
        "created_at, updated_at) VALUES ('bhanu', 'RBRK', '2025-01-01', '2026-01-01', 'x', 'x')",
        (),
    )
    alpha = PositionAlpha(
        start_date="2026-03-12",
        end_date="2026-06-11",
        has_policy=False,
        total_actual_pl=None,
        total_spy_pl=None,
        total_alpha=None,
        total_alpha_vs_qqq=None,
        total_alpha_vs_policy=None,
        rows=[_alpha_row("NU", alpha=500.0), _alpha_row("MELI", alpha=-4000.0)],
    )
    out = attributions_for_book(db_path=db, alpha=alpha)
    assert [a.ticker for a in out] == ["MELI", "NU"]  # |alpha| descending
    assert all(a.window_start == "2026-03-12" for a in out)
    # Tracker fully missing → still one per open position, alpha-less.
    offline = attributions_for_book(db_path=db, alpha=None)
    assert {a.ticker for a in offline} == {"MELI", "NU"}
    assert all(a.alpha_usd is None for a in offline)


def test_attributions_for_book_empty_ledger(db: Path) -> None:
    assert attributions_for_book(db_path=db, alpha=None) == []


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def test_render_attribution_section_monkeypatched_tracker(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_position(db)
    alpha = PositionAlpha(
        start_date="2026-03-12", end_date="2026-06-11", has_policy=False,
        total_actual_pl=None, total_spy_pl=None, total_alpha=None,
        total_alpha_vs_qqq=None, total_alpha_vs_policy=None,
        rows=[_alpha_row("NU")],
    )  # fmt: skip

    def fake_fetch(**_kwargs: object) -> PortfolioAnalytics:
        return PortfolioAnalytics(available=True, api_url="http://t", position_alpha=alpha)

    monkeypatch.setattr(attribution_panel, "fetch_portfolio_analytics", fake_fetch)
    html = render_attribution_section(db, "NU")
    assert "<h2>Attribution</h2>" in html
    assert "Beat its SPY counterfactual" in html
    assert "+$4,210" in html  # alpha chip
    assert "atr-narrative" in html


def test_render_attribution_section_tracker_offline(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_position(db)

    def fake_fetch(**_kwargs: object) -> PortfolioAnalytics:
        return PortfolioAnalytics(available=False, api_url="http://t")

    monkeypatch.setattr(attribution_panel, "fetch_portfolio_analytics", fake_fetch)
    html = render_attribution_section(db, "NU")
    assert "No tracker alpha for this window" in html
    assert "Held since 2026-01-15" in html  # entry context still narrated


def test_book_section_and_portfolio_mount(db: Path) -> None:
    _open_position(db)
    attributions = attributions_for_book(db_path=db, alpha=None)
    block = render_book_attribution_section(attributions)
    assert "Attribution narratives" in block
    assert 'href="/ticker/NU"' in block
    assert render_book_attribution_section([]) == ""

    live = LivePortfolio(available=False, api_url="http://t", error="down")
    analytics = PortfolioAnalytics(available=False, api_url="http://t")
    page = compose_portfolio_page(analytics, live, attributions=attributions)
    assert "Attribution narratives" in page
    # None keeps the pre-S15 page byte-shape (no section).
    page_without = compose_portfolio_page(analytics, live)
    assert "Attribution narratives" not in page_without
