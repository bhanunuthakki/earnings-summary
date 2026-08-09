"""Round-trip + join-correctness tests for ``v_decision_journal`` (alembic
0179, tenet-2 Phase 5 §5.1).

Builds a fully alembic-migrated DB (every real migration executes, in order,
against the bootstrap tables ``db.py``'s ``init_db()`` normally creates) so
the view is validated against the true production schema rather than a
hand-guessed shape. The bootstrap DDL below is a verbatim copy of the three
tables ``db.py``'s ``_create_tracked_companies``/``_create_quarterly_artifacts``/
``_create_fmp_endpoint_status`` build (inlined rather than calling those
private module functions directly, and without mutating the module-global
``db.DB_PATH`` other tests rely on).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

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
    """The three tables ``db.py:init_db()`` creates outside alembic (every
    migration from 0001 on assumes these already exist)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_BOOTSTRAP_DDL)
        conn.commit()
    finally:
        conn.close()


def _build_db(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    db_dir = tmp_path / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "portfolio.db"
    return migrated_db(db_path)


def _insert_memo(
    conn: sqlite3.Connection,
    *,
    kind: str,
    ticker: str | None,
    created_at: str,
    stance: str | None = None,
    context: dict[str, object] | None = None,
    score_status: str = "pending",
) -> int:
    cur = conn.execute(
        "INSERT INTO advisor_memos (user_id, kind, ticker, title, body_md, context_json, "
        "stance, score_status, created_at) VALUES ('bhanu', ?, ?, 't', 'b', ?, ?, ?, ?)",
        (
            kind,
            ticker,
            json.dumps(context) if context is not None else None,
            stance,
            score_status,
            created_at,
        ),
    )
    return int(cur.lastrowid or 0)


def _insert_decision(
    conn: sqlite3.Connection,
    *,
    ticker: str | None,
    kind: str,
    made_at: str,
    decided_by: str = "advisor",
    source_memo_id: int | None = None,
    user_action_kind: str | None = None,
    outcome_label: str | None = None,
    scope: str = "ticker",
) -> int:
    cur = conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, decided_by, made_at, created_at, "
        "source_memo_id, user_action_kind, outcome_label, scope) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ticker,
            kind,
            decided_by,
            made_at,
            made_at,
            source_memo_id,
            user_action_kind,
            outcome_label,
            scope,
        ),
    )
    return int(cur.lastrowid or 0)


def _row(conn: sqlite3.Connection, decision_id: int) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM v_decision_journal WHERE decision_id = ?", (decision_id,)
    ).fetchone()
    assert row is not None
    return row


def test_view_created_with_full_column_set(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = _build_db(tmp_path, migrated_db)
    conn = sqlite3.connect(str(db_path))
    try:
        cols = {d[0] for d in conn.execute("SELECT * FROM v_decision_journal LIMIT 0").description}
    finally:
        conn.close()
    assert {
        "decision_id",
        "ticker",
        "scope",
        "decided_by",
        "recommendation_kind",
        "conviction",
        "made_at",
        "user_action_kind",
        "outcome_label",
        "outcome_pct",
        "process_quality",
        "linked_memo_id",
        "linked_memo_kind",
        "linked_memo_verdict_source",
        "advice_before_memo_id",
        "advice_before_memo_kind",
        "advice_before_memo_at",
        "guard_override_flag",
        "owner_attested_change",
        "coach_ping_class",
        "coach_ping_status",
        "decision_nudge_id",
        "decision_nudge_status",
        "owner_profile_active_fact_count",
        "owner_profile_last_affirmed_at",
        "stance_verdict",
        "stance_horizon_days",
    } <= cols


def test_decision_with_no_advice_reads_all_null(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    """The majority historical case (Phases 1-4 didn't exist yet): a decision
    with zero advice-machinery footprint must surface with honest NULLs, not
    be dropped from the view or fabricated a false join."""
    db_path = _build_db(tmp_path, migrated_db)
    conn = sqlite3.connect(str(db_path))
    try:
        dec_id = _insert_decision(
            conn, ticker="WIX", kind="sell", made_at="2026-07-01T00:00:00", decided_by="owner"
        )
        conn.commit()
        row = _row(conn, dec_id)
        assert row["ticker"] == "WIX"
        assert row["linked_memo_id"] is None
        assert row["advice_before_memo_id"] is None
        assert row["guard_override_flag"] == 0
        assert row["owner_attested_change"] is None
        assert row["coach_ping_class"] is None
        assert row["decision_nudge_id"] is None
        assert row["stance_verdict"] is None
    finally:
        conn.close()


def test_linked_memo_and_guard_override_and_attestation(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = _build_db(tmp_path, migrated_db)
    conn = sqlite3.connect(str(db_path))
    try:
        memo_id = _insert_memo(
            conn,
            kind="position_review",
            ticker="NU",
            created_at="2026-06-01T00:00:00",
            stance="hold",
            context={"verdict_source": "guard_override", "owner_attested_change": True},
            score_status="scored",
        )
        conn.execute(
            "INSERT INTO stance_scores (memo_id, user_id, verdict, benchmark_basis, "
            "horizon_days, created_at) VALUES (?, 'bhanu', 'correct', 'none', 90, "
            "'2026-09-01T00:00:00')",
            (memo_id,),
        )
        dec_id = _insert_decision(
            conn,
            ticker="NU",
            kind="hold",
            made_at="2026-06-10T00:00:00",
            source_memo_id=memo_id,
            user_action_kind="followed",
            outcome_label="correct",
        )
        conn.commit()
        row = _row(conn, dec_id)
        assert row["linked_memo_id"] == memo_id
        assert row["linked_memo_kind"] == "position_review"
        assert row["linked_memo_verdict_source"] == "guard_override"
        assert row["guard_override_flag"] == 1
        assert row["owner_attested_change"] == 1
        assert row["stance_verdict"] == "correct"
        # The same memo also qualifies as the windowed "advice before" pick,
        # since it predates the decision within the lookback window.
        assert row["advice_before_memo_id"] == memo_id
    finally:
        conn.close()


def test_advice_before_window_respects_lookback_and_agent_source(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    """A position_review memo more than 30 days before the decision does not
    count as advice-before; one tagged context.source='agent' is excluded
    even inside the window (verification/CI runs never count as advice
    delivered to the owner)."""
    db_path = _build_db(tmp_path, migrated_db)
    conn = sqlite3.connect(str(db_path))
    try:
        # Too old — 45 days before the decision.
        _insert_memo(
            conn, kind="socratic", ticker="META", created_at="2026-05-01T00:00:00", stance="hold"
        )
        # Agent-sourced — inside the window but must be excluded.
        _insert_memo(
            conn,
            kind="position_review",
            ticker="META",
            created_at="2026-06-20T00:00:00",
            stance="hold",
            context={"source": "agent"},
        )
        dec_id = _insert_decision(
            conn, ticker="META", kind="trim", made_at="2026-06-25T00:00:00", decided_by="owner"
        )
        conn.commit()
        row = _row(conn, dec_id)
        assert row["advice_before_memo_id"] is None
    finally:
        conn.close()


def test_advice_before_picks_most_recent_within_window(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = _build_db(tmp_path, migrated_db)
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_memo(
            conn, kind="socratic", ticker="RBRK", created_at="2026-06-01T00:00:00", stance="hold"
        )
        newer_id = _insert_memo(
            conn,
            kind="position_review",
            ticker="RBRK",
            created_at="2026-06-15T00:00:00",
            stance="trim",
        )
        dec_id = _insert_decision(
            conn,
            ticker="RBRK",
            kind="trim",
            made_at="2026-06-20T00:00:00",
            decided_by="owner",
        )
        conn.commit()
        row = _row(conn, dec_id)
        assert row["advice_before_memo_id"] == newer_id
        assert row["advice_before_memo_kind"] == "position_review"
    finally:
        conn.close()


def test_coach_ping_and_decision_nudge_and_profile_point_in_time(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = _build_db(tmp_path, migrated_db)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO coach_pings (class_, key, ticker, body, status, created_at, "
            "updated_at) VALUES ('falsifier_breach', 'k1', 'FLKR', 'b', 'sent', "
            "'2026-06-05T00:00:00', '2026-06-05T00:00:00')"
        )
        # A profile fact affirmed AFTER the decision must not count as active.
        conn.execute(
            "INSERT INTO owner_profile_facts (user_id, category, key, value_json, narrative, "
            "provenance, status, affirmed_at, created_at) VALUES ('bhanu', 'capacity', 'k1', "
            "'{}', 'n', 'owner', 'affirmed', '2026-05-01T00:00:00', '2026-05-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO owner_profile_facts (user_id, category, key, value_json, narrative, "
            "provenance, status, affirmed_at, created_at) VALUES ('bhanu', 'capacity', 'k2', "
            "'{}', 'n', 'owner', 'affirmed', '2026-07-01T00:00:00', '2026-07-01T00:00:00')"
        )
        dec_id = _insert_decision(
            conn,
            ticker="FLKR",
            kind="add",
            made_at="2026-06-10T00:00:00",
            decided_by="owner",
        )
        conn.execute(
            "INSERT INTO decision_nudges (decision_id, status, sent_at) VALUES (?, 'sent', "
            "'2026-06-10T00:00:00')",
            (dec_id,),
        )
        conn.commit()
        row = _row(conn, dec_id)
        assert row["coach_ping_class"] == "falsifier_breach"
        assert row["decision_nudge_id"] is not None
        assert row["decision_nudge_status"] == "sent"
        # Only the k1 fact (affirmed before made_at) is active — the k2 fact
        # affirmed 2026-07-01 is after the 2026-06-10 decision.
        assert row["owner_profile_active_fact_count"] == 1
        assert row["owner_profile_last_affirmed_at"] == "2026-05-01T00:00:00"
    finally:
        conn.close()


def test_missing_view_degrades_gracefully_on_pre_0179_db(tmp_path: Path) -> None:
    """A DB stamped before 0179 (or any hand-DDL fixture) has no view at all —
    querying it must raise OperationalError, never silently return []; the
    caller (panel/scorecard reader) is the one responsible for the try/except
    degrade, matching every other view/table reader in this codebase."""
    db_path = tmp_path / "bare.db"
    conn = sqlite3.connect(str(db_path))
    conn.close()
    conn = sqlite3.connect(str(db_path))
    try:
        raised = False
        try:
            conn.execute("SELECT * FROM v_decision_journal").fetchall()
        except sqlite3.OperationalError:
            raised = True
        assert raised
    finally:
        conn.close()


def test_downgrade_drops_view(tmp_path: Path, migrated_db: Callable[..., Path]) -> None:
    db_path = migrated_db(
        tmp_path / "data" / "portfolio.db",
        stamp="0059_kpi_facts_restatement",
        archived=True,
    )
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.downgrade(cfg, "0174_behavior_distill_budget")
    conn = sqlite3.connect(str(db_path))
    try:
        view = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' AND name='v_decision_journal'"
        ).fetchone()
        assert view is None
    finally:
        conn.close()
