"""Decision P&L scoring arithmetic + due-scoring (monthly_red_team.md Phase 3,
PR7) — ``src/redteam/decision_pnl.py``."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from redteam.decision_pnl import (
    DEFAULT_MIN_QUARTERS,
    build_decision_pnl,
    build_yearly_scorecard,
)

_SCHEMA = """
CREATE TABLE red_team_items (
    id INTEGER PRIMARY KEY, run_key TEXT NOT NULL, ticker TEXT, lens TEXT NOT NULL,
    kind TEXT NOT NULL, attack_md TEXT NOT NULL, question_md TEXT NOT NULL,
    proposed_change_md TEXT NOT NULL, severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', defer_count INTEGER NOT NULL DEFAULT 0,
    response_md TEXT, responded_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY, ticker TEXT, live_price REAL, created_at TEXT,
    is_latest INTEGER DEFAULT 1, segment_name TEXT DEFAULT ''
);
"""

_OLD = "2026-01-10T00:00:00"
_NEW = "2026-07-10T00:00:00"
_NOW = datetime(2026, 7, 11)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _add_item(
    db: Path,
    *,
    ticker: str | None,
    lens: str,
    kind: str,
    status: str,
    responded_at: str | None,
) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO red_team_items (run_key, ticker, lens, kind, attack_md, question_md, "
        "proposed_change_md, severity, status, responded_at, created_at) "
        "VALUES ('red_team_2026_01', ?, ?, ?, 'a', 'q', 'p', 'high', ?, ?, ?)",
        (ticker, lens, kind, status, responded_at, responded_at or _OLD),
    )
    conn.commit()
    conn.close()


def _add_price(db: Path, ticker: str, price: float, created_at: str, *, is_latest: int = 1) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO dcf_runs (ticker, live_price, created_at, is_latest, segment_name) "
        "VALUES (?, ?, ?, ?, '')",
        (ticker, price, created_at, is_latest),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# scoring arithmetic
# ---------------------------------------------------------------------------


def test_refute_scores_positive_when_price_holds(db: Path) -> None:
    _add_item(db, ticker="MELI", lens="shared_factor", kind="per_name", status="refuted", responded_at=_OLD)
    _add_price(db, "MELI", 1000, _OLD, is_latest=0)
    _add_price(db, "MELI", 1100, _NEW, is_latest=1)
    report = build_decision_pnl(db_path=db, now=_NOW)
    assert report.n_due == 1
    row = report.rows[0]
    assert row.price_move_pct == pytest.approx(0.10)
    assert row.scored_pct == pytest.approx(0.10)  # unweighted (no materialized weights)
    assert "REFUTE" in row.note


def test_refute_scores_negative_when_price_falls(db: Path) -> None:
    _add_item(db, ticker="MELI", lens="shared_factor", kind="per_name", status="refuted", responded_at=_OLD)
    _add_price(db, "MELI", 1000, _OLD, is_latest=0)
    _add_price(db, "MELI", 800, _NEW, is_latest=1)
    report = build_decision_pnl(db_path=db, now=_NOW)
    row = report.rows[0]
    assert row.price_move_pct == pytest.approx(-0.20)
    assert row.scored_pct == pytest.approx(-0.20)


def test_accept_scores_positive_when_price_falls(db: Path) -> None:
    _add_item(db, ticker="NU", lens="fx_translation", kind="per_name", status="accepted", responded_at=_OLD)
    _add_price(db, "NU", 10, _OLD, is_latest=0)
    _add_price(db, "NU", 8, _NEW, is_latest=1)
    report = build_decision_pnl(db_path=db, now=_NOW)
    row = report.rows[0]
    assert row.price_move_pct == pytest.approx(-0.20)
    assert row.scored_pct == pytest.approx(0.20)  # de-risking avoided the fall
    assert "ACCEPT" in row.note


def test_accept_scores_negative_when_price_rises(db: Path) -> None:
    _add_item(db, ticker="NU", lens="fx_translation", kind="per_name", status="accepted", responded_at=_OLD)
    _add_price(db, "NU", 10, _OLD, is_latest=0)
    _add_price(db, "NU", 12, _NEW, is_latest=1)
    report = build_decision_pnl(db_path=db, now=_NOW)
    row = report.rows[0]
    assert row.scored_pct == pytest.approx(-0.20)  # de-risking cost the upside


def test_defer_is_informational_not_scored(db: Path) -> None:
    _add_item(db, ticker="WIX", lens="model_vs_market", kind="per_name", status="deferred", responded_at=_OLD)
    _add_price(db, "WIX", 100, _OLD, is_latest=0)
    _add_price(db, "WIX", 120, _NEW, is_latest=1)
    report = build_decision_pnl(db_path=db, now=_NOW)
    row = report.rows[0]
    assert row.scored_pct is None
    assert row.price_move_pct == pytest.approx(0.20)
    assert "DEFER" in row.note


def test_cross_book_item_is_not_price_scorable(db: Path) -> None:
    _add_item(db, ticker=None, lens="style_drift", kind="cross_book", status="refuted", responded_at=_OLD)
    report = build_decision_pnl(db_path=db, now=_NOW)
    row = report.rows[0]
    assert row.ticker is None
    assert row.scored_pct is None
    assert "cross-book" in row.note


def test_missing_price_data_is_unscorable_not_a_crash(db: Path) -> None:
    _add_item(db, ticker="ORPHAN", lens="fx_translation", kind="per_name", status="refuted", responded_at=_OLD)
    report = build_decision_pnl(db_path=db, now=_NOW)
    row = report.rows[0]
    assert row.scored_pct is None
    assert report.n_unscorable == 1
    assert "unscorable" in row.note


# ---------------------------------------------------------------------------
# due / not-yet-due
# ---------------------------------------------------------------------------


def test_recent_response_is_not_yet_due(db: Path) -> None:
    recent = "2026-07-01T00:00:00"  # < 2 quarters before _NOW
    _add_item(db, ticker="MELI", lens="shared_factor", kind="per_name", status="refuted", responded_at=recent)
    report = build_decision_pnl(db_path=db, now=_NOW, min_quarters=DEFAULT_MIN_QUARTERS)
    assert report.n_due == 0
    assert report.n_not_yet_due == 1
    assert report.rows == []


def test_min_quarters_is_configurable(db: Path) -> None:
    _add_item(db, ticker="MELI", lens="shared_factor", kind="per_name", status="refuted", responded_at="2026-06-01T00:00:00")
    report = build_decision_pnl(db_path=db, now=_NOW, min_quarters=0)
    assert report.n_due == 1


# ---------------------------------------------------------------------------
# yearly scorecard — honest-empty states
# ---------------------------------------------------------------------------


def test_yearly_scorecard_all_honest_empty_on_thin_db(db: Path) -> None:
    sc = build_yearly_scorecard(db_path=db)
    assert sc.brier_trend.available is False
    assert sc.brier_trend.value_text == "no data yet"
    assert sc.cut_discipline_hit_rate.available is False
    assert sc.rule_execution_fidelity.available is False
    assert "not yet captured" in sc.rule_execution_fidelity.detail
