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
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from decision_calibration import CalibrationStats  # noqa: E402
from integrations.portfolio_tracker_client import (  # noqa: E402
    LivePortfolio,
    LivePosition,
    PositionAlpha,
    PositionAlphaRow,
)
from pipeline.allocation_decisions_panel import (  # noqa: E402
    COACH_CHANGED_TARGET,
    SizingAuditRow,
    _coach_digest_section,
    _coach_mutes_section,
    _coach_pings_section,
    _coach_pnl_section,
    _query_coach_pnl,
    build_decisions_timeline,
    build_sizing_audit_rows,
    compose_decisions_page,
    score_row,
)
from user_state.ledger import append_entry  # noqa: E402
from user_state.notes import create_note  # noqa: E402
from user_state.sizing import append_intent, list_intents  # noqa: E402

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
def client(tmp_path: Path) -> FlaskClient:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")
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


# --------------------------------------------------------------------------- #
# Coach P&L (REQ-7) — the guard's public scoreboard
# --------------------------------------------------------------------------- #


# ``decisions`` predates the 0059 stamp point (db.init_db() territory, same as
# test_open_loops.py's _DECISIONS_DDL / test_governor.py's _PRE_DDL) — 0130's
# extension is _has_table-guarded, so stamping past 0059 and upgrading to head
# never creates it; hand-build the modern (post-0130) shape afterward so the
# heuristic's decided_by / recommendation_kind / made_at columns are present.
_MEMO_TEST_DECISIONS_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    made_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL
);
"""


def _memo_db(tmp_path: Path, name: str = "coach.db") -> Path:
    """Alembic-built DB through head — carries advisor_memos (0077, widened to
    admit 'position_review' by 0140), stance_scores (0078), and
    coach_pings/coach_mutes (0131). Stamps past baseline first (the ``client``
    fixture's pattern above) — 0000_baseline assumes the db.py:init_db() shape
    already exists, which an empty file doesn't have. ``decisions`` is hand-
    built post-upgrade (see ``_MEMO_TEST_DECISIONS_DDL``)."""
    db = tmp_path / name
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_MEMO_TEST_DECISIONS_DDL)
        conn.commit()
    finally:
        conn.close()
    return db


def _insert_review_memo(
    db: Path,
    *,
    ticker: str,
    verdict_source: str,
    created_at: str,
    user_id: str = "bhanu",
) -> int:
    import json

    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "INSERT INTO advisor_memos (user_id, kind, ticker, title, body_md, context_json, "
            "stance, score_status, created_at) VALUES (?, 'position_review', ?, ?, 'body', ?, "
            "'hold', 'pending', ?)",
            (
                user_id,
                ticker,
                f"Position review: {ticker}",
                json.dumps({"verdict_source": verdict_source}),
                created_at,
            ),
        )
        memo_id = int(cur.lastrowid or 0)
        conn.commit()
        return memo_id
    finally:
        conn.close()


def _insert_stance_score(db: Path, memo_id: int, verdict: str) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO stance_scores (memo_id, user_id, verdict, benchmark_basis, "
            "horizon_days, created_at) VALUES (?, 'bhanu', ?, 'none', 90, '2026-07-01')",
            (memo_id, verdict),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_owner_decision(db: Path, *, ticker: str, kind: str, made_at: str) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, made_at, "
            "created_at) VALUES (?, ?, 'owner', ?, ?)",
            (ticker, kind, made_at, made_at),
        )
        conn.commit()
    finally:
        conn.close()


def test_coach_pnl_all_zero_is_honest(tmp_path: Path) -> None:
    db = _memo_db(tmp_path)
    pnl = _query_coach_pnl(db, user_id="bhanu")
    assert pnl.reviews_run == 0
    assert pnl.guard_fired == 0 and pnl.overridden == 0
    assert pnl.graded_right == 0 and pnl.graded_total == 0
    assert pnl.changed == 0
    html = _coach_pnl_section(db, user_id="bhanu")
    assert "Reviews run: <b>0</b> — the guard has never been exercised." in html


def test_coach_pnl_counts_reviews_guard_fires_and_grades(tmp_path: Path) -> None:
    db = _memo_db(tmp_path)
    _insert_review_memo(db, ticker="NU", verdict_source="llm", created_at="2026-06-01T00:00:00")
    m2 = _insert_review_memo(
        db, ticker="META", verdict_source="guard_override", created_at="2026-06-02T00:00:00"
    )
    _insert_stance_score(db, m2, "correct")

    pnl = _query_coach_pnl(db, user_id="bhanu")
    assert pnl.reviews_run == 2
    assert pnl.guard_fired == 1 and pnl.overridden == 1
    assert pnl.graded_total == 1 and pnl.graded_right == 1

    html = _coach_pnl_section(db, user_id="bhanu")
    assert "Reviews run: <b>2</b>" in html
    assert "guard fired: <b>1</b>" in html
    assert "overridden: <b>1</b>" in html
    assert "1/1 graded right" in html
    assert f"Q3&rsquo;26 target: <b>{COACH_CHANGED_TARGET}</b>" in html
    assert "/api/peek/memo/position_review" in html


def test_coach_pnl_heuristic_counts_heeded_not_unheeded(tmp_path: Path) -> None:
    """A guard_override with no contradicting owner sell/trim within 30d counts
    as a changed decision; one immediately contradicted by an owner SELL does
    not (the owner overrode the guard right back)."""
    db = _memo_db(tmp_path)
    # Heeded: guard said hold on NU, no sell/trim followed.
    _insert_review_memo(
        db, ticker="NU", verdict_source="guard_override", created_at="2026-06-01T00:00:00"
    )
    # Unheeded: guard said hold on META, owner sold 5 days later anyway.
    _insert_review_memo(
        db, ticker="META", verdict_source="guard_override", created_at="2026-06-01T00:00:00"
    )
    _insert_owner_decision(db, ticker="META", kind="sell", made_at="2026-06-06T00:00:00")

    pnl = _query_coach_pnl(db, user_id="bhanu")
    assert pnl.guard_fired == 2
    assert pnl.changed == 1  # only NU's hold stuck


def test_coach_pnl_section_never_raises_on_missing_tables(tmp_path: Path) -> None:
    """A thin/hand-rolled DB with no advisor_memos table degrades to the
    honest all-zero line, never a 500."""
    db = tmp_path / "thin.db"
    conn = sqlite3.connect(str(db))
    conn.close()
    html = _coach_pnl_section(db, user_id="bhanu")
    assert "Reviews run: <b>0</b>" in html


# --------------------------------------------------------------------------- #
# Pings / mutes / digest (REQ-12) — first production renderers
# --------------------------------------------------------------------------- #


def _insert_ping(
    db: Path, *, class_: str, key: str, ticker: str | None, status: str, created_at: str
) -> int:
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "INSERT INTO coach_pings (class_, key, ticker, body, status, created_at, "
            "updated_at) VALUES (?, ?, ?, 'body', ?, ?, ?)",
            (class_, key, ticker, status, created_at, created_at),
        )
        pid = int(cur.lastrowid or 0)
        conn.commit()
        return pid
    finally:
        conn.close()


def _insert_mute(db: Path, *, class_: str, muted_at: str, reason: str | None = None) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO coach_mutes (class_, muted_at, reason) VALUES (?, ?, ?)",
            (class_, muted_at, reason),
        )
        conn.commit()
    finally:
        conn.close()


def test_coach_pings_section_renders_seeded_rows_this_month(tmp_path: Path) -> None:
    from datetime import datetime

    db = _memo_db(tmp_path)
    now = datetime(2026, 7, 10)
    _insert_ping(
        db,
        class_="falsifier_breach",
        key="alert:1",
        ticker="NU",
        status="sent",
        created_at="2026-07-05T00:00:00",
    )
    # Out of window (June) — must not appear.
    _insert_ping(
        db,
        class_="intent_followup",
        key="intent:1:0",
        ticker=None,
        status="dismissed",
        created_at="2026-06-01T00:00:00",
    )
    html = _coach_pings_section(db, now=now)
    assert "falsifier_breach" in html and "NU" in html and "sent" in html
    assert "intent_followup" not in html


def test_coach_pings_section_empty_state_is_one_line(tmp_path: Path) -> None:
    db = _memo_db(tmp_path)
    html = _coach_pings_section(db)
    assert "No pings this month." in html


def test_coach_mutes_section_renders_row_with_unmute_button(tmp_path: Path) -> None:
    db = _memo_db(tmp_path)
    _insert_mute(db, class_="falsifier_breach", muted_at="2026-07-02T00:00:00")
    html = _coach_mutes_section(db)
    assert "falsifier_breach" in html
    assert "muted since 2026-07-02" in html
    assert "cpnl-unmute-btn" in html
    assert "/api/coach/unmute" in html  # wired via _UNMUTE_JS


def test_coach_mutes_section_empty_state_is_one_line(tmp_path: Path) -> None:
    db = _memo_db(tmp_path)
    html = _coach_mutes_section(db)
    assert "No active mutes." in html


def test_coach_digest_section_renders_and_empty_state(tmp_path: Path) -> None:
    db = _memo_db(tmp_path)
    empty_html = _coach_digest_section(db)
    assert "Digest is empty." in empty_html

    _insert_ping(
        db,
        class_="intent_followup",
        key="intent:2:0",
        ticker="WIX",
        status="digest",
        created_at="2026-07-01T00:00:00",
    )
    html = _coach_digest_section(db)
    assert "intent_followup" in html and "WIX" in html


def test_coach_unmute_route_calls_governor_and_row_disappears(
    client: FlaskClient, tmp_path: Path
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    _insert_mute(db, class_="falsifier_breach", muted_at="2026-07-02T00:00:00")
    resp = client.post("/api/coach/unmute", json={"class_": "falsifier_breach"})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True and payload["unmuted"] is True

    from research.governor import unmute as _unmute  # sanity: same function the route calls

    assert _unmute("falsifier_breach", db_path=db) is False  # already gone

    html = _coach_mutes_section(db)
    assert "No active mutes." in html
    assert "falsifier_breach" not in html


def test_coach_unmute_route_options_preflight(client: FlaskClient) -> None:
    resp = client.options("/api/coach/unmute")
    assert resp.status_code == 204


def test_coach_unmute_route_validates_body(client: FlaskClient) -> None:
    resp = client.post("/api/coach/unmute", json={})
    assert resp.status_code == 400


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
