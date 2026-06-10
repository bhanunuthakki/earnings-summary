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

from integrations.portfolio_tracker_client import (  # noqa: E402
    LivePortfolio,
    LivePosition,
    PositionAlpha,
    PositionAlphaRow,
)
from pipeline.allocation_decisions_panel import (  # noqa: E402
    SizingAuditRow,
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
