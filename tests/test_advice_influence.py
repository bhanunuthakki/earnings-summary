"""Tests for ``decision_calibration.compute_advice_influence`` (tenet-2 Phase
5 §5.2) — the advice-influence read riding ``v_decision_journal``.

Builds the same alembic-migrated DB pattern as ``test_decision_journal_view.py``
so the partition is exercised against the real view. Covers: the four-cell
partition, the min-n "too thin" note, the missing-view degrade, and the
CLI wiring in ``execution/run_calibration_scorecard.py``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from calibration_guard import MIN_CONFIDENT_N  # noqa: E402
from decision_calibration import compute_advice_influence  # noqa: E402

# Verbatim copy of the three tables db.py's init_db() creates outside alembic
# (every migration from 0001 on assumes these already exist) — see the
# identical constant + rationale in test_decision_journal_view.py.
_BOOTSTRAP_DDL = """
CREATE TABLE IF NOT EXISTS tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    list_type TEXT NOT NULL CHECK(list_type IN (
        'portfolio', 'watchlist', 'evaluation', 'none', 'etf', 'index_member'
    )),
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sec_validated BOOLEAN DEFAULT 0,
    ir_url TEXT DEFAULT NULL,
    model_url TEXT DEFAULT NULL,
    publishes_release BOOLEAN DEFAULT 0,
    publishes_slides BOOLEAN DEFAULT 0,
    publishes_transcript BOOLEAN DEFAULT 0,
    fmp_data_upto TEXT DEFAULT NULL,
    manual_data_quarters TEXT DEFAULT '[]',
    fmp_data_saved BOOLEAN DEFAULT 0,
    UNIQUE(user_id, ticker)
);
CREATE TABLE IF NOT EXISTS quarterly_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    year INTEGER NOT NULL,
    quarter TEXT NOT NULL,
    has_release_file    BOOLEAN DEFAULT 0,
    has_slides_file     BOOLEAN DEFAULT 0,
    has_transcript_file BOOLEAN DEFAULT 0,
    has_audio_file      BOOLEAN DEFAULT 0,
    step_audio_transcribed BOOLEAN DEFAULT 0,
    step_llm_summarized    BOOLEAN DEFAULT 0,
    step_saydo_analyzed    BOOLEAN DEFAULT 0,
    step_thesis_updated    BOOLEAN DEFAULT 0,
    UNIQUE(ticker, year, quarter)
);
CREATE TABLE IF NOT EXISTS fmp_endpoint_status (
    ticker         TEXT    NOT NULL,
    endpoint       TEXT    NOT NULL,
    period         TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL,
    http_code      INTEGER,
    record_count   INTEGER,
    earliest_date  TEXT,
    latest_date    TEXT,
    file_path      TEXT,
    file_bytes     INTEGER,
    error_msg      TEXT,
    last_pulled    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, endpoint, period)
);
"""


def _bootstrap_base_tables(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_BOOTSTRAP_DDL)
        conn.commit()
    finally:
        conn.close()


def _build_db(tmp_path: Path) -> Path:
    db_dir = tmp_path / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "portfolio.db"
    _bootstrap_base_tables(db_path)
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    return db_path


def _insert_memo(conn: sqlite3.Connection, *, ticker: str, created_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO advisor_memos (user_id, kind, ticker, title, body_md, stance, "
        "score_status, created_at) VALUES ('bhanu','position_review',?,'t','b','hold',"
        "'scored',?)",
        (ticker, created_at),
    )
    return int(cur.lastrowid or 0)


def _insert_decision(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    made_at: str,
    outcome_label: str | None,
    user_action_kind: str | None,
    source_memo_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, decided_by, made_at, created_at, "
        "outcome_label, user_action_kind, source_memo_id) VALUES (?, 'hold', 'owner', ?, ?, "
        "?, ?, ?)",
        (ticker, made_at, made_at, outcome_label, user_action_kind, source_memo_id),
    )


def test_missing_view_returns_none(tmp_path: Path) -> None:
    db_path = tmp_path / "bare.db"
    sqlite3.connect(str(db_path)).close()
    assert compute_advice_influence(db_path) is None


def test_no_decisions_yet_returns_all_zero_with_thin_note(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    stats = compute_advice_influence(db_path)
    assert stats is not None
    assert stats.total_graded == 0
    assert stats.advice_before_n == 0 and stats.no_advice_n == 0
    assert any("too thin" in n for n in stats.notes)
    assert all(b.n == 0 for b in stats.buckets)


def test_partition_crosses_advice_before_and_followed_overridden(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        memo_id = _insert_memo(conn, ticker="NU", created_at="2026-06-01T00:00:00")
        # advice-before, followed, correct
        _insert_decision(
            conn, ticker="NU", made_at="2026-06-10T00:00:00", outcome_label="correct",
            user_action_kind="followed", source_memo_id=memo_id,
        )  # fmt: skip
        memo_id2 = _insert_memo(conn, ticker="META", created_at="2026-06-01T00:00:00")
        # advice-before, overridden, wrong
        _insert_decision(
            conn, ticker="META", made_at="2026-06-10T00:00:00", outcome_label="wrong",
            user_action_kind="reversed", source_memo_id=memo_id2,
        )  # fmt: skip
        # no advice, followed, correct
        _insert_decision(
            conn, ticker="WIX", made_at="2026-07-01T00:00:00", outcome_label="correct",
            user_action_kind="followed",
        )  # fmt: skip
        # no advice, unknown action -> excluded from the 4 cells
        _insert_decision(
            conn, ticker="FLKR", made_at="2026-07-01T00:00:00", outcome_label="mixed",
            user_action_kind=None,
        )  # fmt: skip
        # ungraded (pending) -> excluded entirely
        _insert_decision(
            conn, ticker="RBRK", made_at="2026-07-01T00:00:00", outcome_label=None,
            user_action_kind="followed",
        )  # fmt: skip
        conn.commit()
    finally:
        conn.close()

    stats = compute_advice_influence(db_path)
    assert stats is not None
    assert stats.total_graded == 4  # NU, META, WIX, FLKR — RBRK excluded (ungraded)
    assert stats.advice_before_n == 2  # NU, META
    assert stats.no_advice_n == 2  # WIX, FLKR
    assert stats.unknown_action_n == 1  # FLKR

    by_label = {b.label: b for b in stats.buckets}
    ab_followed = by_label["advice-before, followed"]
    assert ab_followed.n == 1 and ab_followed.correct == 1
    ab_overridden = by_label["advice-before, overridden"]
    assert ab_overridden.n == 1 and ab_overridden.correct == 0
    no_advice_followed = by_label["no advice, followed"]
    assert no_advice_followed.n == 1 and no_advice_followed.correct == 1
    no_advice_overridden = by_label["no advice, overridden"]
    assert no_advice_overridden.n == 0


def test_above_floor_has_no_thin_note(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        for i in range(MIN_CONFIDENT_N):
            _insert_decision(
                conn,
                ticker=f"T{i}",
                made_at="2026-06-10T00:00:00",
                outcome_label="correct",
                user_action_kind="followed",
            )
        conn.commit()
    finally:
        conn.close()
    stats = compute_advice_influence(db_path)
    assert stats is not None
    assert stats.total_graded == MIN_CONFIDENT_N
    assert not any("too thin" in n for n in stats.notes)


def test_run_calibration_scorecard_persists_advice_influence_sibling_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The monthly scorecard CLI computes + persists the advice-influence read
    as a sibling JSON artifact, without touching the eval-gated
    CalibrationScorecard machinery (no LLM call for this piece)."""
    import calibration_coach as cc
    from calibration_coach import CalibrationScorecard
    from execution import run_calibration_scorecard

    db_path = _build_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        for i in range(MIN_CONFIDENT_N):
            _insert_decision(
                conn,
                ticker=f"T{i}",
                made_at="2026-06-10T00:00:00",
                outcome_label="correct",
                user_action_kind="followed",
            )
        conn.commit()
    finally:
        conn.close()

    def _card(*_a: object, **_k: object) -> CalibrationScorecard:
        return CalibrationScorecard(
            period="2026-06", generated_at="2026-06-14T00:00:00", granularity="quarter",
            can_coach=False, n_graded=3, overall_hit_rate=0.5, improving=None,
            hit_rate_delta=None, latest_period=None, latest_hit_rate=None,
            selection_usd=None, sizing_usd=None, timing_usd=None, biases=[],
            experiment=None, coach_quality_ok=None, coach_quality_score=None,
        )  # fmt: skip

    monkeypatch.setattr(cc, "build_scorecard", _card)
    rc = run_calibration_scorecard.main(["--repo-root", str(tmp_path), "--period", "2026-06"])
    assert rc == 0
    advice_path = tmp_path / "data" / "calibration_scorecard" / "2026-06_advice_influence.json"
    assert advice_path.exists()
    payload = json.loads(advice_path.read_text(encoding="utf-8"))
    assert payload["total_graded"] == MIN_CONFIDENT_N
    assert len(payload["buckets"]) == 4
