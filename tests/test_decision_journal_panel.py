"""Tests for the decision-journal panel section (tenet-2 Phase 5 §5.2) --
``pipeline.allocation_decisions_panel._decision_journal_section``.

Builds the same alembic-migrated DB pattern as ``test_decision_journal_view.py``
(so the section renders against the real ``v_decision_journal`` view) and
covers: the empty state, a populated row with every advice-before marker
present, the NULL-tolerant bare row, and that ``compose_decisions_page``
wires the section in at all (default "" doesn't break existing callers).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from integrations.portfolio_tracker_client import LivePortfolio  # noqa: E402
from pipeline.allocation_decisions_panel import (  # noqa: E402
    _decision_journal_section,
    compose_decisions_page,
)

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


def test_empty_state_renders_one_line(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    html = _decision_journal_section(db_path)
    assert "Owner Decision journal" in html
    assert "No Owner Decisions recorded yet." in html


def test_populated_row_renders_advice_disposition_outcome(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO advisor_memos (user_id, kind, ticker, title, body_md, context_json, "
            "stance, score_status, created_at) VALUES ('bhanu','position_review','NU','t','b',"
            "?,'hold','scored','2026-06-01T00:00:00')",
            (json.dumps({"verdict_source": "guard_override", "owner_attested_change": True}),),
        )
        memo_id = cur.lastrowid
        conn.execute(
            "INSERT INTO coach_pings (class_, key, ticker, body, status, created_at, "
            "updated_at) VALUES ('falsifier_breach','k1','NU','b','sent',"
            "'2026-06-05T00:00:00','2026-06-05T00:00:00')"
        )
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, made_at, "
            "created_at, source_memo_id, user_action_kind, outcome_label, outcome_pct) VALUES "
            "('NU','hold','owner','2026-06-10T00:00:00','2026-06-10T00:00:00', ?, "
            "'followed', 'correct', 5.5)",
            (memo_id,),
        )
        dec_id = conn.execute("SELECT id FROM decisions WHERE ticker='NU'").fetchone()[0]
        conn.execute(
            "INSERT INTO decision_nudges (decision_id, status, sent_at) VALUES (?, 'sent', "
            "'2026-06-10T00:00:00')",
            (dec_id,),
        )
        conn.commit()
    finally:
        conn.close()

    html = _decision_journal_section(db_path)
    assert "NU" in html
    assert "owner: hold" in html
    assert "position_review" in html
    assert "guard" in html
    assert "attested" in html
    assert "ping" in html
    assert "nudge" in html
    assert "followed" in html
    assert "correct" in html
    assert "+5.5%" in html
    assert 'data-peek-url="/api/peek/memo/position_review"' in html


def test_bare_decision_with_no_advice_renders_dashes(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, made_at, "
            "created_at) VALUES ('WIX','sell','owner','2026-07-01T00:00:00',"
            "'2026-07-01T00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    html = _decision_journal_section(db_path)
    assert "WIX" in html
    assert "owner: sell" in html
    assert "pending" in html  # no outcome yet
    assert "&mdash;" in html  # no advice, no disposition


def test_missing_view_degrades_to_empty_state(tmp_path: Path) -> None:
    """A DB with no v_decision_journal (pre-0179, or a bare hand-DDL fixture)
    must render the section's empty state, never raise."""
    db_path = tmp_path / "bare.db"
    sqlite3.connect(str(db_path)).close()
    html = _decision_journal_section(db_path)
    assert "No Owner Decisions recorded yet." in html


def test_compose_page_wires_journal_section_when_provided() -> None:
    offline = LivePortfolio(available=False, api_url="http://x", error="down")
    page_with = compose_decisions_page(
        [],
        [],
        offline,
        None,
        decision_journal_html='<section class="panel">JOURNAL_MARKER</section>',
    )
    assert "JOURNAL_MARKER" in page_with

    page_without = compose_decisions_page([], [], offline, None)
    assert "JOURNAL_MARKER" not in page_without
