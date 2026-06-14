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
    decompose_alpha,
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


# ---------------------------------------------------------------------------
# Shared skill-decomposition engine (L8 §6)
# ---------------------------------------------------------------------------


def _row(
    ticker: str,
    *,
    value_start: float | None,
    alpha: float | None,
    bought: float = 0.0,
    sold: float = 0.0,
    value_end: float | None = None,
) -> PositionAlphaRow:
    return PositionAlphaRow(
        ticker=ticker,
        name=None,
        value_at_start=value_start,
        bought_in_window=bought,
        sold_in_window=sold,
        value_at_end=value_end if value_end is not None else value_start,
        actual_pl=None,
        spy_counterfactual_pl=None,
        alpha=alpha,
        alpha_vs_qqq=None,
        alpha_vs_policy=None,
        incomplete=False,
    )


def _alpha(
    rows: list[PositionAlphaRow], *, start: str = "2026-01-01", end: str = "2026-03-31"
) -> PositionAlpha:
    return PositionAlpha(
        start_date=start, end_date=end, has_policy=False,
        total_actual_pl=None, total_spy_pl=None, total_alpha=None,
        total_alpha_vs_qqq=None, total_alpha_vs_policy=None, rows=rows,
    )  # fmt: skip


def test_decompose_selection_sizing_additive_identity() -> None:
    # A: $100 -> +$20 (rate 0.2). B: $300 -> +$15 (rate 0.05). W=400, N=2.
    # selection = (W/N)*sum(r) = 200*0.25 = 50; sizing = total - selection = 35 - 50 = -15
    # (you put more capital in the lower-rate name -- weighting diluted selection).
    d = decompose_alpha(_alpha([_row("A", value_start=100, alpha=20),
                                _row("B", value_start=300, alpha=15)]))  # fmt: skip
    assert d is not None
    assert d.n_names == 2
    assert d.selection_usd is not None and d.sizing_usd is not None
    assert d.total_alpha_usd == pytest.approx(35.0)
    assert d.selection_usd == pytest.approx(50.0)
    assert d.sizing_usd == pytest.approx(-15.0)
    # exact additive split
    assert d.selection_usd + d.sizing_usd == pytest.approx(d.total_alpha_usd)
    assert d.window_start == "2026-01-01" and d.window_end == "2026-03-31"


def test_decompose_equal_rates_zero_sizing() -> None:
    # Same alpha rate in both names → weighting can't add or subtract → sizing 0.
    d = decompose_alpha(_alpha([_row("A", value_start=100, alpha=10),
                                _row("B", value_start=300, alpha=30)]))  # fmt: skip
    assert d is not None
    assert d.sizing_usd == pytest.approx(0.0)
    assert d.selection_usd == pytest.approx(40.0)


def test_decompose_timing_flow_lean() -> None:
    # A: net buyer (+50) into a winner (+20) → good timing (+20).
    # B: net seller (-100) out of a winner (+15) → trimmed a winner → bad (-15).
    d = decompose_alpha(_alpha([
        _row("A", value_start=100, alpha=20, bought=50),
        _row("B", value_start=300, alpha=15, sold=100),
    ]))  # fmt: skip
    assert d is not None
    assert d.n_timed == 2
    assert d.timing_usd == pytest.approx(5.0)  # +20 - 15


def test_decompose_no_flows_timing_none() -> None:
    d = decompose_alpha(_alpha([_row("A", value_start=100, alpha=20)]))
    assert d is not None
    assert d.n_timed == 0
    assert d.timing_usd is None


def test_decompose_conviction_join() -> None:
    d = decompose_alpha(
        _alpha([_row("NU", value_start=100, alpha=20), _row("MELI", value_start=100, alpha=-10)]),
        conviction_by_ticker={"NU": 5.0, "MELI": 2.0},
    )
    assert d is not None
    by = {c.conviction: c for c in d.by_conviction}
    assert by["high"].n == 1 and by["high"].alpha_usd == pytest.approx(20.0)
    assert by["high"].mean_conviction == pytest.approx(5.0)
    assert by["low"].alpha_usd == pytest.approx(-10.0)


def test_decompose_offline_returns_none() -> None:
    assert decompose_alpha(None) is None


def test_decompose_excludes_unpriced_names() -> None:
    # One name has no usable value (start and end both 0) → out of the split,
    # counted, noted; the priced name still decomposes.
    d = decompose_alpha(_alpha([
        _row("A", value_start=100, alpha=20),
        _row("B", value_start=0, alpha=99, value_end=0),
    ]))  # fmt: skip
    assert d is not None
    assert d.n_names == 1
    assert d.excluded_no_value == 1
    assert any("no usable window value" in n for n in d.notes)


def test_decompose_value_at_end_fallback_for_new_position() -> None:
    # Opened inside the window (start 0) → end value is the capital-at-risk proxy.
    d = decompose_alpha(_alpha([
        _row("A", value_start=0, alpha=20, value_end=200),
        _row("B", value_start=200, alpha=10),
    ]))  # fmt: skip
    assert d is not None
    assert d.n_names == 2  # neither excluded


def test_decompose_min_n_guard_hedges_thin_book() -> None:
    d = decompose_alpha(_alpha([_row("A", value_start=100, alpha=20)]))
    assert d is not None
    assert d.confident is False  # n=1 < the shared floor
    assert any("low confidence" in n for n in d.notes)


def test_decompose_empty_when_no_priced_names() -> None:
    assert decompose_alpha(_alpha([_row("A", value_start=0, alpha=5, value_end=0)])) is None
    assert decompose_alpha(_alpha([])) is None


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
