"""Decision P&L (Red Team) panel render — src/pipeline/redteam_pnl_panel.py."""

from __future__ import annotations

from pipeline.redteam_pnl_panel import REDTEAM_PNL_CSS, render_redteam_pnl_section
from redteam.decision_pnl import (
    DecisionPnlReport,
    DecisionPnlRow,
    PriceRead,
    ScorecardNumber,
    YearlyScorecard,
)


def _row(**overrides: object) -> DecisionPnlRow:
    base: dict[str, object] = dict(
        item_id=1,
        ticker="MELI",
        kind="per_name",
        lens="shared_factor",
        status="refuted",
        severity="high",
        responded_at="2026-01-10T00:00:00",
        weight_pct=0.05,
        price_then=PriceRead(1000, "2026-01-10"),
        price_now=PriceRead(1100, "2026-07-10"),
        price_move_pct=0.10,
        scored_pct=0.10,
        note="REFUTE: price held/rose",
    )
    base.update(overrides)
    return DecisionPnlRow(**base)  # type: ignore[arg-type]


def _scorecard(*, available: bool = False) -> YearlyScorecard:
    n = ScorecardNumber("x", available, "12%" if available else "no data yet", "detail")
    return YearlyScorecard(brier_trend=n, cut_discipline_hit_rate=n, rule_execution_fidelity=n)


def test_render_none_report_is_honest_not_blank() -> None:
    html = render_redteam_pnl_section(None, None)
    assert "unavailable" in html
    assert "<section" in html


def test_render_empty_rows_shows_due_counts() -> None:
    report = DecisionPnlReport(
        min_quarters=2, as_of="2026-07-11", rows=[], n_due=0, n_not_yet_due=3
    )
    html = render_redteam_pnl_section(report, None)
    assert "0 responses due for scoring" in html
    assert "3 responded but not yet 2 quarters old" in html


def test_render_rows_include_ticker_status_and_score() -> None:
    report = DecisionPnlReport(
        min_quarters=2, as_of="2026-07-11", rows=[_row()], n_due=1, n_not_yet_due=0
    )
    html = render_redteam_pnl_section(report, None)
    assert "MELI" in html
    assert "REFUTED" in html
    assert "+10.0%" in html
    assert "k-chip" in html
    assert "k-pill" in html


def test_render_cross_book_row_shows_cross_book_label() -> None:
    row = _row(
        ticker=None,
        kind="cross_book",
        price_then=None,
        price_now=None,
        price_move_pct=None,
        scored_pct=None,
        note="cross-book item — not price-scorable",
    )
    report = DecisionPnlReport(min_quarters=2, as_of="x", rows=[row], n_due=1, n_not_yet_due=0)
    html = render_redteam_pnl_section(report, None)
    assert "Cross-book" in html


def test_render_deferred_row_shows_informational_pill() -> None:
    row = _row(status="deferred", scored_pct=None, price_move_pct=0.05, note="DEFER: informational")
    report = DecisionPnlReport(min_quarters=2, as_of="x", rows=[row], n_due=1, n_not_yet_due=0)
    html = render_redteam_pnl_section(report, None)
    assert "informational" in html


def test_render_scorecard_numbers_honest_empty() -> None:
    report = DecisionPnlReport(min_quarters=2, as_of="x", rows=[], n_due=0, n_not_yet_due=0)
    html = render_redteam_pnl_section(report, _scorecard(available=False))
    assert html.count("no data yet") == 3


def test_render_scorecard_numbers_available() -> None:
    report = DecisionPnlReport(min_quarters=2, as_of="x", rows=[], n_due=0, n_not_yet_due=0)
    html = render_redteam_pnl_section(report, _scorecard(available=True))
    assert "12%" in html


def test_css_has_no_raw_hex_colors() -> None:
    import re

    assert "var(--" in REDTEAM_PNL_CSS
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", REDTEAM_PNL_CSS) is None
