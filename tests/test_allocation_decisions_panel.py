"""Tests for the allocation-decisions record (master build P2.2).

Covers the three layers separately:

* the mismatch scorer (pure — every heuristic gated on its inputs and
  emitting an explainable chip),
* the audit/timeline composers over hand-built tracker payloads + a
  hand-rolled SQLite substrate (tracker offline degradation included), and
* the server seams — ``GET /api/panel/decisions_record`` and the append-only
  ``POST /api/sizing-intents`` recorder (alembic-built DB, real Flask client).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import comments_server
import pytest
from flask.testing import FlaskClient

import pipeline.allocation_decisions_panel as adp
from attribution import ConvictionAlphaRow, SkillDecomposition
from decision_calibration import CalibrationStats
from integrations.portfolio_tracker_client import (
    BetaStats,
    LivePortfolio,
    LivePosition,
    PortfolioAnalytics,
    PositionAlpha,
    PositionAlphaRow,
)
from pipeline.allocation_decisions_panel import (
    SizingAuditRow,
    SQLiteConnectionRole,
    build_decisions_timeline,
    build_sizing_audit_rows,
    compose_decisions_page,
    render_decision_journal_section,
    score_row,
)
from user_state.ledger import append_entry
from user_state.notes import create_note
from user_state.sizing import append_intent, list_intents

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Scorer
# --------------------------------------------------------------------------- #


def testscore_row_no_inputs_is_aligned() -> None:
    pts, reasons = score_row(
        verdict=None,
        conviction=None,
        target_weight_pct=None,
        weight_pct=None,
        fv_gap_pct=None,
        alpha_usd=None,
        alpha_frac=None,
        median_weight=None,
    )
    assert pts == 0.0
    assert reasons == []


def testscore_row_target_drift() -> None:
    pts, reasons = score_row(
        verdict="ok",
        conviction=None,
        target_weight_pct=5.0,
        weight_pct=9.0,
        fv_gap_pct=None,
        alpha_usd=None,
        alpha_frac=None,
        median_weight=6.0,
    )
    assert pts == pytest.approx(4.0)
    assert any("+4.0pp vs stated target 5.0%" in r for r in reasons)


def testscore_row_high_conviction_underweight() -> None:
    pts, reasons = score_row(
        verdict="ok",
        conviction=5.0,
        target_weight_pct=None,
        weight_pct=2.0,
        fv_gap_pct=None,
        alpha_usd=None,
        alpha_frac=None,
        median_weight=8.0,
    )
    assert pts > 0
    assert any("conviction 5/5 but 2.0% of book" in r for r in reasons)


def testscore_row_thesis_stress_at_size() -> None:
    pts, reasons = score_row(
        verdict="breach",
        conviction=None,
        target_weight_pct=None,
        weight_pct=12.0,
        fv_gap_pct=None,
        alpha_usd=None,
        alpha_frac=None,
        median_weight=6.0,
    )
    assert pts == pytest.approx(12.0)  # base 6 * min(rel=2, 2)
    assert any("thesis breach at 12.0% of book" in r for r in reasons)


def testscore_row_rich_at_size_and_cheap_small_conviction() -> None:
    rich_pts, rich_reasons = score_row(
        verdict="ok",
        conviction=None,
        target_weight_pct=None,
        weight_pct=10.0,
        fv_gap_pct=50.0,
        alpha_usd=None,
        alpha_frac=None,
        median_weight=5.0,
    )
    assert rich_pts > 0
    assert any("+50% vs DCF FV at 10.0% of book" in r for r in rich_reasons)

    cheap_pts, cheap_reasons = score_row(
        verdict="ok",
        conviction=4.0,
        target_weight_pct=None,
        weight_pct=3.0,
        fv_gap_pct=-40.0,
        alpha_usd=None,
        alpha_frac=None,
        median_weight=6.0,
    )
    assert cheap_pts > 0
    assert any("-40% vs DCF FV with conviction 4/5" in r for r in cheap_reasons)


def testscore_row_scenario_range_defaults_are_inert() -> None:
    # No bull/bear gaps → identical to the base-only score (no asymmetry chips).
    base_pts, base_reasons = score_row(
        verdict="ok",
        conviction=None,
        target_weight_pct=None,
        weight_pct=10.0,
        fv_gap_pct=10.0,
        alpha_usd=None,
        alpha_frac=None,
        median_weight=5.0,
    )
    assert base_pts == 0.0 and base_reasons == []


def testscore_row_above_bull_case_at_size() -> None:
    # Price above even the bull fair value, held at size → an extra tension chip.
    pts, reasons = score_row(
        verdict="ok",
        conviction=None,
        target_weight_pct=None,
        weight_pct=10.0,
        fv_gap_pct=40.0,
        alpha_usd=None,
        alpha_frac=None,
        median_weight=5.0,
        bull_gap_pct=15.0,
        bear_gap_pct=80.0,
    )
    assert pts > 0
    assert any("vs the bull case (no upside even if it works)" in r for r in reasons)


def testscore_row_below_bear_case_underweight_high_conviction() -> None:
    # Price below even the bear fair value (downside-protected) but sized below
    # the median despite high conviction → an extra tension chip.
    pts, reasons = score_row(
        verdict="ok",
        conviction=5.0,
        target_weight_pct=None,
        weight_pct=3.0,
        fv_gap_pct=-30.0,
        alpha_usd=None,
        alpha_frac=None,
        median_weight=6.0,
        bull_gap_pct=-60.0,
        bear_gap_pct=-10.0,
    )
    assert pts > 0
    assert any("vs the bear case (downside-protected)" in r for r in reasons)


def testscore_row_alpha_drag_needs_size_and_magnitude() -> None:
    pts, reasons = score_row(
        verdict="ok",
        conviction=None,
        target_weight_pct=None,
        weight_pct=8.0,
        fv_gap_pct=None,
        alpha_usd=-3000.0,
        alpha_frac=-0.25,
        median_weight=6.0,
    )
    assert pts == pytest.approx(1.5)
    assert any("window alpha" in r for r in reasons)
    # Below-median weight: one bad window on a small position is not a mismatch.
    pts_small, _ = score_row(
        verdict="ok",
        conviction=None,
        target_weight_pct=None,
        weight_pct=2.0,
        fv_gap_pct=None,
        alpha_usd=-3000.0,
        alpha_frac=-0.25,
        median_weight=6.0,
    )
    assert pts_small == 0.0


# --------------------------------------------------------------------------- #
# Audit composer
# --------------------------------------------------------------------------- #


def _live(positions: list[LivePosition]) -> LivePortfolio:
    return LivePortfolio(available=True, api_url="http://x", positions=positions)


def _pos(ticker: str, pct: float, mv: float) -> LivePosition:
    return LivePosition(
        ticker=ticker,
        name=None,
        quantity=1.0,
        market_value=mv,
        cost_basis=None,
        unrealized_pnl=None,
        percent_of_portfolio=pct,
    )


def _alpha_row(ticker: str, alpha: float, value: float) -> PositionAlphaRow:
    return PositionAlphaRow(
        ticker=ticker,
        name=None,
        value_at_start=None,
        bought_in_window=None,
        sold_in_window=None,
        value_at_end=value,
        actual_pl=None,
        spy_counterfactual_pl=None,
        alpha=alpha,
        alpha_vs_qqq=None,
        alpha_vs_policy=None,
        incomplete=False,
    )


def _intent_db(tmp_path: Path) -> Path:
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            CREATE TABLE position_sizing_intent (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'bhanu',
                ticker TEXT NOT NULL, intent_kind TEXT NOT NULL,
                intent_value REAL, narrative TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_build_sizing_audit_ranks_and_joins(tmp_path: Path) -> None:
    db = _intent_db(tmp_path)
    # Stale then current conviction — only the latest (5) must surface.
    append_intent(
        user_id="bhanu", ticker="NU", intent_kind="conviction", intent_value=2, db_path=db
    )
    append_intent(
        user_id="bhanu", ticker="NU", intent_kind="conviction", intent_value=5, db_path=db
    )
    append_intent(
        user_id="bhanu",
        ticker="META",
        intent_kind="target_weight_pct",
        intent_value=5.0,
        narrative="cap the single-name risk",
        db_path=db,
    )
    intents = list_intents(user_id="bhanu", db_path=db)

    holdings: list[tuple[str, str | None]] = [("NU", "Nu Holdings"), ("META", None), ("WIX", None)]
    verdicts = {"META": "warn", "NU": "ok", "WIX": "ok"}
    dcf_gaps: dict[str, tuple[float | None, float | None, float | None, str | None]] = {
        "NU": (-30.0, 14.0, 9.8, "2026-06-01"),
        "META": (35.0, 500.0, 675.0, "2026-06-01"),
    }
    live = _live([_pos("NU", 2.0, 2000.0), _pos("META", 10.0, 10000.0), _pos("WIX", 4.0, 4000.0)])
    alpha = PositionAlpha(
        start_date="2025-06-10",
        end_date="2026-06-10",
        has_policy=False,
        total_actual_pl=None,
        total_spy_pl=None,
        total_alpha=None,
        total_alpha_vs_qqq=None,
        total_alpha_vs_policy=None,
        rows=[_alpha_row("META", -2600.0, 10000.0), _alpha_row("NU", 400.0, 2000.0)],
    )

    rows = build_sizing_audit_rows(holdings, verdicts, dcf_gaps, intents, live, alpha)
    by_ticker = {r.ticker: r for r in rows}

    nu = by_ticker["NU"]
    assert nu.conviction == 5.0  # latest wins over the stale 2
    assert nu.weight_pct == 2.0
    assert nu.fv_gap_pct == -30.0
    assert nu.alpha_usd == 400.0
    # high conviction + cheap + below-median weight → mismatch with reasons
    assert nu.mismatch_score > 0
    assert any("conviction 5/5" in c for c in nu.mismatch_reasons)

    meta = by_ticker["META"]
    assert meta.target_weight_pct == 5.0
    assert meta.alpha_frac == pytest.approx(-0.26)
    # target drift + warn-at-size + rich-at-size + alpha drag all fire
    assert len(meta.mismatch_reasons) >= 3

    wix = by_ticker["WIX"]
    assert wix.mismatch_score == 0.0 and wix.mismatch_reasons == []

    # Mismatches ranked first, aligned row last.
    assert rows[-1].ticker == "WIX"
    assert rows[0].mismatch_score >= rows[1].mismatch_score


def test_build_sizing_audit_threads_scenario_range(tmp_path: Path) -> None:
    db = _intent_db(tmp_path)
    intents = list_intents(user_id="bhanu", db_path=db)
    holdings: list[tuple[str, str | None]] = [("NU", None)]
    verdicts: dict[str, str] = {"NU": "ok"}
    # Rich at size: price 14.0 above the base FV 9.8 AND above the bull FV 12.0.
    dcf_gaps: dict[str, tuple[float | None, float | None, float | None, str | None]] = {
        "NU": (42.86, 9.8, 14.0, "2026-06-01")
    }
    dcf_scenarios: dict[str, tuple[float | None, float | None]] = {"NU": (12.0, 7.0)}
    live = _live([_pos("NU", 10.0, 10000.0)])
    rows = build_sizing_audit_rows(holdings, verdicts, dcf_gaps, intents, live, None, dcf_scenarios)
    (nu,) = rows
    # price/bull - 1 = 14/12 - 1 = +16.7%; price/bear - 1 = 14/7 - 1 = +100%.
    assert nu.bull_gap_pct == pytest.approx(16.667, abs=0.01)
    assert nu.bear_gap_pct == pytest.approx(100.0, abs=0.01)
    assert any("vs the bull case" in c for c in nu.mismatch_reasons)


def test_build_sizing_audit_tracker_offline(tmp_path: Path) -> None:
    db = _intent_db(tmp_path)
    append_intent(
        user_id="bhanu", ticker="NU", intent_kind="conviction", intent_value=4, db_path=db
    )
    intents = list_intents(user_id="bhanu", db_path=db)
    offline = LivePortfolio(available=False, api_url="http://x", error="ConnectionError: down")
    rows = build_sizing_audit_rows(
        [("NU", None)], {"NU": "breach"}, {"NU": (-10.0, 1.0, 0.9, None)}, intents, offline, None
    )
    (nu,) = rows
    assert nu.weight_pct is None and nu.alpha_usd is None
    assert nu.conviction == 4.0 and nu.verdict == "breach" and nu.fv_gap_pct == -10.0
    # Weight-gated heuristics must not fire without a live book.
    assert nu.mismatch_score == 0.0


# --------------------------------------------------------------------------- #
# Timeline + page composition
# --------------------------------------------------------------------------- #


def _full_state_db(tmp_path: Path) -> Path:
    db = _intent_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            CREATE TABLE thesis_ledger_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, ticker TEXT NOT NULL, entry_kind TEXT NOT NULL,
                body TEXT NOT NULL, source_alert_id INTEGER,
                created_at TEXT NOT NULL, accepted_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE analyst_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'bhanu', ticker TEXT,
                kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                body TEXT NOT NULL, anchor_type TEXT, anchor_key TEXT,
                source TEXT NOT NULL, source_ref TEXT, supersedes_id INTEGER,
                resolution_note TEXT, context_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_timeline_merges_three_sources_newest_first(tmp_path: Path) -> None:
    db = _full_state_db(tmp_path)
    append_entry(
        user_id="bhanu", ticker="NU", entry_kind="thesis_update", body="NIM stabilized", db_path=db
    )
    append_intent(
        user_id="bhanu",
        ticker="META",
        intent_kind="conviction",
        intent_value=4,
        narrative="ad cycle turning",
        db_path=db,
    )
    create_note(
        user_id="bhanu", ticker="WIX", kind="decision", body="hold through the print", db_path=db
    )
    events = build_decisions_timeline(db, user_id="bhanu")
    assert [e.ticker for e in events] == ["WIX", "META", "NU"]  # insertion order, newest first
    assert events[1].kind == "sizing_intent"
    assert "conviction 4/5 — ad cycle turning" in events[1].body
    assert events[0].label == "Decision note"
    # Non-decision notes stay out of the decisions record.
    create_note(
        user_id="bhanu", ticker="WIX", kind="observation", body="checkout UX improved", db_path=db
    )
    events = build_decisions_timeline(db, user_id="bhanu")
    assert all("checkout UX" not in e.body for e in events)


def test_compose_page_renders_audit_timeline_and_editor(tmp_path: Path) -> None:
    db = _full_state_db(tmp_path)
    append_intent(
        user_id="bhanu", ticker="NU", intent_kind="conviction", intent_value=5, db_path=db
    )
    intents = list_intents(user_id="bhanu", db_path=db)
    live = _live([_pos("NU", 3.0, 3000.0), _pos("META", 9.0, 9000.0)])
    rows = build_sizing_audit_rows(
        [("NU", None), ("META", None)],
        {"NU": "ok", "META": "warn"},
        {"NU": (-30.0, 14.0, 9.8, None)},
        intents,
        live,
        None,
    )
    timeline = build_decisions_timeline(db, user_id="bhanu", intents=intents)
    html = compose_decisions_page(rows, timeline, live, None)
    assert "Sizing audit" in html and "Decisions timeline" in html
    assert "ad-edit-btn" in html and "/api/sizing-intents" in html
    assert "conviction 5/5" in html  # the intent surfaced in the timeline
    assert "alpha column dashed" in html  # tracker up, alpha payload missing


def test_compose_page_offline_note() -> None:
    offline = LivePortfolio(available=False, api_url="http://localhost:8000", error="down")
    rows = [
        SizingAuditRow(
            ticker="NU",
            name=None,
            verdict="ok",
            conviction=None,
            conviction_at=None,
            target_weight_pct=None,
            target_at=None,
            weight_pct=None,
            market_value=None,
            fv_gap_pct=None,
            alpha_usd=None,
            alpha_frac=None,
        )
    ]
    html = compose_decisions_page(rows, [], offline, None)
    assert "Tracker offline" in html
    assert "No sizing intents recorded yet" in html


# --------------------------------------------------------------------------- #
# Server seams (alembic-built DB, real Flask client)
# --------------------------------------------------------------------------- #

_PRIOR_HEAD = "0059_kpi_facts_restatement"


@pytest.fixture
def client(tmp_path: Path, migrated_db: Callable[..., Path]) -> FlaskClient:
    migrated_db(tmp_path / "data" / "portfolio.db", stamp=_PRIOR_HEAD)
    return comments_server.create_app(tmp_path).test_client()


def test_post_sizing_intent_appends_history(client: FlaskClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/sizing-intents",
        json={"ticker": "nu", "conviction": 4, "target_weight_pct": 6.5, "narrative": "size up"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ticker"] == "NU" and len(payload["created_ids"]) == 2
    rows = list_intents(user_id="bhanu", ticker="NU", db_path=tmp_path / "data" / "portfolio.db")
    assert {r.intent_kind for r in rows} == {"conviction", "target_weight_pct"}
    assert all(r.narrative == "size up" for r in rows)

    append_intent(
        user_id="bhanu",
        ticker="NU",
        intent_kind="target_weight_pct",
        intent_value=6.0,
        narrative="legacy duplicate",
        db_path=tmp_path / "data" / "portfolio.db",
    )

    replacement = client.post(
        "/api/sizing-intents",
        json={"ticker": "NU", "target_weight_pct": 5.5, "narrative": "new DCF ladder"},
    )
    assert replacement.status_code == 200
    active = list_intents(user_id="bhanu", ticker="NU", db_path=tmp_path / "data" / "portfolio.db")
    target = next(row for row in active if row.intent_kind == "target_weight_pct")
    assert target.intent_value == 5.5
    history = list_intents(
        user_id="bhanu",
        ticker="NU",
        include_superseded=True,
        db_path=tmp_path / "data" / "portfolio.db",
    )
    assert [row.intent_value for row in history if row.intent_kind == "target_weight_pct"] == [
        5.5,
        6.0,
        6.5,
    ]


def test_post_sizing_intent_validates(client: FlaskClient) -> None:
    assert client.post("/api/sizing-intents", json={"conviction": 4}).status_code == 400
    assert client.post("/api/sizing-intents", json={"ticker": "NU"}).status_code == 400
    assert (
        client.post("/api/sizing-intents", json={"ticker": "NU", "conviction": 9}).status_code
        == 400
    )
    assert (
        client.post(
            "/api/sizing-intents", json={"ticker": "NU", "target_weight_pct": "abc"}
        ).status_code
        == 400
    )


def test_decisions_record_panel_route(client: FlaskClient) -> None:
    client.post("/api/sizing-intents", json={"ticker": "NU", "conviction": 5})
    resp = client.get("/api/panel/decisions_record")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    body = resp.data.decode()
    assert "Sizing audit" in body
    assert "Decisions timeline" in body
    assert "conviction 5/5" in body


def test_decision_journal_lists_confirmed_owner_decisions_only(tmp_path: Path) -> None:
    db = tmp_path / "journal.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE VIEW v_decision_journal AS "
            "SELECT 1 AS decision_id, 'NU' AS ticker, 'owner' AS decided_by, "
            "'watch' AS recommendation_kind, '2026-07-25' AS made_at, "
            "'followed' AS user_action_kind, 'correct' AS outcome_label, 4.0 AS outcome_pct, 'sound' AS process_quality "
            "UNION ALL SELECT 2, 'WIX', 'advisor', 'buy', '2026-07-24', "
            "NULL, NULL, NULL, NULL"
        )

    html = render_decision_journal_section(db)

    assert "owner: watch" in html
    assert "followed" in html
    assert "correct" in html
    assert "+4.0%" in html
    assert "advisor: buy" not in html
    assert "advisor history" not in html


# --------------------------------------------------------------------------- #
# Mirror-first KPI-strip hoist
# --------------------------------------------------------------------------- #


def _stats(*, total: int, graded: int, overall_hit_rate: float | None) -> CalibrationStats:
    return CalibrationStats(
        total=total,
        graded=graded,
        overall_hit_rate=overall_hit_rate,
        by_conviction=[],
        action_mix={},
        reversals=[],
        reversals_vindicated=0,
        reversals_cost=0,
        time_to_outcome=[],
    )


_HOIST_MARKER = '<div class="cpnl-hoist">'


def test_kpi_strip_hoisted_above_audit_section_when_calibration_present() -> None:
    offline = LivePortfolio(available=False, api_url="http://x", error="down")
    stats = _stats(total=5, graded=3, overall_hit_rate=0.6)
    page = compose_decisions_page([], [], offline, None, calibration=stats)
    kpi_pos = page.find(_HOIST_MARKER)
    audit_pos = page.find("Sizing audit")
    assert kpi_pos != -1 and audit_pos != -1
    assert kpi_pos < audit_pos


def test_kpi_strip_absent_without_calibration() -> None:
    offline = LivePortfolio(available=False, api_url="http://x", error="down")
    page = compose_decisions_page([], [], offline, None)
    assert _HOIST_MARKER not in page


def test_kpi_strip_absent_when_calibration_total_zero() -> None:
    offline = LivePortfolio(available=False, api_url="http://x", error="down")
    stats = _stats(total=0, graded=0, overall_hit_rate=None)
    page = compose_decisions_page([], [], offline, None, calibration=stats)
    assert _HOIST_MARKER not in page


# --------------------------------------------------------------------------- #
# Wave 1 (surface_density_jit_redesign.md D4/D7): skill decomposition leads
# with an honest verdict; an all-unstated conviction join never renders as a
# one-row "unstated" table.
# --------------------------------------------------------------------------- #


def _decomp(
    by_conviction: list[ConvictionAlphaRow] | None = None,
) -> SkillDecomposition:
    if by_conviction is None:
        by_conviction = [
            ConvictionAlphaRow(
                conviction="unstated",
                n=21,
                alpha_usd=41_774.0,
                sizing_usd=11_774.0,
                mean_conviction=None,
            )
        ]
    return SkillDecomposition(
        window_start="2025-07-24",
        window_end="2026-07-24",
        n_names=21,
        total_alpha_usd=41_774.0,
        selection_usd=30_000.0,
        sizing_usd=11_774.0,
        timing_usd=-17_619.0,
        n_timed=4,
        by_conviction=by_conviction,
        top_contributors=[],
        excluded_no_value=0,
        confident=True,
        notes=[],
    )


def _beta(significant: bool | None, t: float | None = -0.7) -> BetaStats:
    return BetaStats(
        benchmark="SPY",
        start_date=None,
        end_date=None,
        sample_size=252,
        risk_free_annual=None,
        beta=1.4,
        alpha_annualized_pct=-41.8,
        alpha_t_stat=t,
        alpha_std_error_annualized_pct=None,
        alpha_significant=significant,
        r_squared=0.1,
        correlation=None,
        sharpe=None,
        sortino=None,
        information_ratio=None,
        portfolio_volatility_annualized=None,
        benchmark_volatility_annualized=None,
        tracking_error_annualized=None,
        notes=[],
    )


def test_skill_verdict_reconciles_dollars_with_luck_test() -> None:
    """The prod symptom: "+$41,774 total alpha" and "not distinguishable from
    zero — could be luck" rendered as adjacent, unreconciled lines. The verdict
    must say both in one sentence, before any KPI."""
    from pipeline.allocation_decisions_panel import render_skill_decomposition_section

    html = render_skill_decomposition_section(_decomp(), _beta(significant=False))
    assert 'class="adc-line sk-verdict"' in html
    v_start = html.index("sk-verdict")
    assert "you made" in html[v_start : v_start + 400]
    assert "not yet statistically distinguishable from luck" in html[v_start : v_start + 400]
    assert "t=-0.7" in html
    assert "Jensen &alpha; -41.8% annualized" in html
    # The verdict leads: it appears before the KPI strip.
    assert html.index("sk-verdict") < html.index("sk-kpis")


def test_skill_verdict_significant_alpha_reads_as_skill() -> None:
    from pipeline.allocation_decisions_panel import render_skill_decomposition_section

    html = render_skill_decomposition_section(_decomp(), _beta(significant=True, t=2.4))
    assert "real skill, not luck" in html
    assert "could be luck" not in html


def test_all_unstated_conviction_join_renders_unlock_line_not_table() -> None:
    """A one-row table whose only cell reads "unstated" is an empty ritual —
    D4 replaces it with the line naming what unlocks the join."""
    from pipeline.allocation_decisions_panel import render_skill_decomposition_section

    html = render_skill_decomposition_section(_decomp(), None)
    assert "Conviction &rarr; outcome is locked" in html
    assert "<th>Conviction</th>" not in html


def test_stated_conviction_join_still_renders_the_table() -> None:
    from pipeline.allocation_decisions_panel import render_skill_decomposition_section

    rows = [
        ConvictionAlphaRow(
            conviction="high", n=3, alpha_usd=9_000.0, sizing_usd=1_000.0, mean_conviction=4.7
        ),
        ConvictionAlphaRow(
            conviction="unstated",
            n=18,
            alpha_usd=32_774.0,
            sizing_usd=10_774.0,
            mean_conviction=None,
        ),
    ]
    html = render_skill_decomposition_section(_decomp(by_conviction=rows), None)
    assert "<th>Conviction</th>" in html
    assert "Conviction &rarr; outcome is locked" not in html


# --------------------------------------------------------------------------- #
# Pooled panel-local reads: one READ_ONLY conn for four sections
# --------------------------------------------------------------------------- #


def _make_pool_db(path: Path) -> Path:
    con = sqlite3.connect(str(path))
    try:
        con.executescript(
            "CREATE TABLE decisions(id INTEGER PRIMARY KEY, ticker TEXT,"
            " recommendation_kind TEXT, decided_by TEXT, made_at TEXT, created_at TEXT);"
            "CREATE TABLE v_decision_journal(decision_id INTEGER, ticker TEXT, decided_by TEXT,"
            " recommendation_kind TEXT, made_at TEXT, user_action_kind TEXT,"
            " outcome_label TEXT, outcome_pct REAL);"
            "CREATE TABLE tracked_companies(ticker TEXT, name TEXT, list_type TEXT, archived_at TEXT);"
        )
        con.execute(
            "INSERT INTO v_decision_journal VALUES(1,'NU','owner','watch','2026-07-25',"
            "'followed','correct',4.0)"
        )
        con.execute(
            "INSERT INTO v_decision_journal VALUES(2,'WIX','advisor','buy','2026-07-24',"
            "NULL,NULL,NULL)"
        )
        con.execute("INSERT INTO tracked_companies VALUES('NU','Nu','portfolio',NULL)")
        con.commit()
    finally:
        con.close()
    return path


def _assert_usable(con: sqlite3.Connection) -> None:
    assert con.execute("SELECT 1").fetchone()[0] == 1


def _assert_closed(con: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.ProgrammingError):
        con.execute("SELECT 1")


def test_pool_owned_supplied_equivalence(tmp_path: Path) -> None:
    db = _make_pool_db(tmp_path / "pool.db")
    exp_journal = adp.render_decision_journal_section(db)
    con = adp.connect_sqlite(db, role=SQLiteConnectionRole.READ_ONLY)
    con.row_factory = sqlite3.Row
    try:
        _assert_usable(con)
        assert adp.render_decision_journal_section(db, conn=con) == exp_journal
        _assert_usable(con)
    finally:
        con.close()
    _assert_closed(con)


def test_pool_thin_missing_tables(tmp_path: Path) -> None:
    db = tmp_path / "thin.db"
    sqlite3.connect(str(db)).close()
    con = adp.connect_sqlite(db, role=SQLiteConnectionRole.READ_ONLY)
    con.row_factory = sqlite3.Row
    try:
        assert "No Owner Decisions recorded yet." in adp.render_decision_journal_section(
            db, conn=con
        )
        _assert_usable(con)
    finally:
        con.close()


def test_pool_render_two_conns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _make_pool_db(tmp_path / "render.db")
    exp_journal = adp.render_decision_journal_section(db)
    conns: list[sqlite3.Connection] = []
    orig = adp.connect_sqlite

    def _record(path: Path, *, role: SQLiteConnectionRole) -> sqlite3.Connection:
        c = orig(path, role=role)
        conns.append(c)
        return c

    monkeypatch.setattr(adp, "connect_sqlite", _record)

    def _live(*a: object, **k: object) -> LivePortfolio:
        for c in conns:
            _assert_closed(c)
        return LivePortfolio(available=False, api_url="http://x", error="down")

    def _analytics(*a: object, **k: object) -> PortfolioAnalytics:
        for c in conns:
            _assert_closed(c)
        return PortfolioAnalytics(
            available=False,
            api_url="http://x",
            position_alpha=None,
            exit_quality=None,
            beta=None,
        )

    monkeypatch.setattr(adp, "fetch_live_portfolio", _live)
    monkeypatch.setattr(adp, "fetch_portfolio_analytics", _analytics)

    def _fake_build_calibration(
        *,
        db_path: Path | str,
        cohort_granularity: str = "",
        magnitudes_by_ticker: dict[str, float] | None = None,
    ) -> adp.CalibrationStats | None:
        return None

    def _fake_latest_dcf_runs(
        conn: sqlite3.Connection,
    ) -> dict[str, tuple[float | None, float | None, float | None, str | None]]:
        return {}

    def _fake_latest_dcf_scenarios(
        conn: sqlite3.Connection,
    ) -> dict[str, tuple[float | None, float | None]]:
        return {}

    monkeypatch.setattr(adp, "build_calibration", _fake_build_calibration)
    monkeypatch.setattr(adp, "latest_dcf_runs", _fake_latest_dcf_runs)
    monkeypatch.setattr(adp, "latest_dcf_scenarios", _fake_latest_dcf_scenarios)
    html = adp.render_allocation_decisions_panel(db, user_id="bhanu")
    assert exp_journal in html
    assert len(conns) == 2
    for c in conns:
        _assert_closed(c)
