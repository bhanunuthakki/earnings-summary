"""Tests for decisions write path — idempotency, user-action recording,
outcome grading, and the batch recorder over llm_artifacts."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from decision_extractor import (
    hit_rate_by_kind,
    history,
    outcome_curve_by_conviction,
    pending_for_grading,
    record_decision,
    record_decisions_from_artifacts,
    record_outcome,
    record_user_action,
)


def _create_schema(conn: sqlite3.Connection) -> None:
    """Mirror migrations 0035 (llm_artifacts) + 0046 (decisions) inline for
    self-contained tests."""
    conn.executescript(
        """
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16),
            scope VARCHAR(64) NOT NULL DEFAULT 'ticker',
            purpose VARCHAR(64) NOT NULL,
            fiscal_period VARCHAR(10),
            content_md TEXT,
            content_json TEXT,
            input_sha256 VARCHAR(64) NOT NULL,
            output_sha256 VARCHAR(64),
            model VARCHAR(64),
            prompt_version VARCHAR(32) NOT NULL DEFAULT 'v1',
            generated_at DATETIME NOT NULL,
            expires_at DATETIME,
            superseded_by_id INTEGER REFERENCES llm_artifacts(id),
            dirty BOOLEAN NOT NULL DEFAULT 0,
            dirty_reason VARCHAR(128),
            source_doc_ids TEXT,
            parent_artifact_ids TEXT,
            llm_call_id INTEGER
        );
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            recommendation_kind VARCHAR(32) NOT NULL,
            recommendation_value FLOAT,
            conviction VARCHAR(16),
            source_artifact_id INTEGER NOT NULL,
            source_lens VARCHAR(64),
            rationale_excerpt TEXT,
            made_at DATETIME NOT NULL,
            user_acted_at DATETIME,
            user_action_kind VARCHAR(32),
            user_notes TEXT,
            outcome_at DATETIME,
            outcome_label VARCHAR(16),
            outcome_pct FLOAT,
            outcome_notes TEXT,
            created_at DATETIME NOT NULL
        );
        CREATE UNIQUE INDEX idx_decisions_source_artifact
            ON decisions(source_artifact_id);
        CREATE INDEX idx_decisions_ticker ON decisions(ticker);
        CREATE INDEX idx_decisions_made_at ON decisions(made_at);
        CREATE INDEX idx_decisions_outcome_label ON decisions(outcome_label);
        """
    )
    conn.commit()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(path))
    try:
        _create_schema(conn)
    finally:
        conn.close()
    return path


@pytest.fixture()
def repo_root(tmp_path: Path, db: Path) -> Path:
    """A repo-root layout where data/portfolio.db points at the test DB.
    Used by record_decisions_from_artifacts."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    # Move the test db into data/portfolio.db so the batch recorder finds it
    target = data_dir / "portfolio.db"
    target.write_bytes(db.read_bytes())
    return tmp_path


# ---------------------------------------------------------------------------
# record_decision — basic insert + idempotency on source_artifact_id
# ---------------------------------------------------------------------------


def test_record_decision_inserts_row(db: Path) -> None:
    pid = record_decision(
        ticker="GOOG",
        recommendation_kind="trim",
        recommendation_value=20.0,
        conviction="high",
        source_artifact_id=42,
        source_lens="five_min_reread",
        rationale_excerpt="Premium too wide.",
        made_at=datetime(2026, 5, 1, tzinfo=UTC),
        db_path=db,
    )
    assert pid is not None and pid > 0


def test_record_decision_idempotent_on_source_artifact_id(db: Path) -> None:
    args = dict(
        ticker="META",
        recommendation_kind="add",
        recommendation_value=15.0,
        conviction="medium",
        source_artifact_id=99,
        source_lens="five_min_reread",
        rationale_excerpt="Q4 cleared every KPI.",
        made_at=datetime(2026, 5, 1, tzinfo=UTC),
        db_path=db,
    )
    pid1 = record_decision(**args)  # type: ignore[arg-type]
    pid2 = record_decision(**args)  # type: ignore[arg-type]
    assert pid1 == pid2
    # Confirm only one row was inserted
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM decisions WHERE source_artifact_id = 99").fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_record_decision_returns_none_on_missing_db(tmp_path: Path) -> None:
    out = record_decision(
        ticker="X",
        recommendation_kind="hold",
        recommendation_value=None,
        conviction=None,
        source_artifact_id=1,
        source_lens=None,
        rationale_excerpt=None,
        made_at=datetime.now(UTC),
        db_path=tmp_path / "nonexistent.db",
    )
    assert out is None


# ---------------------------------------------------------------------------
# User action + outcome write paths
# ---------------------------------------------------------------------------


def test_record_user_action_writes_columns(db: Path) -> None:
    pid = record_decision(
        ticker="AMZN",
        recommendation_kind="add",
        recommendation_value=8.0,
        conviction=None,
        source_artifact_id=10,
        source_lens="five_min_reread",
        rationale_excerpt="MoS bar respected.",
        made_at=datetime(2026, 4, 1, tzinfo=UTC),
        db_path=db,
    )
    assert pid is not None
    ok = record_user_action(
        decision_id=pid,
        user_action_kind="followed",
        user_notes="Added 8% at $266.",
        db_path=db,
    )
    assert ok is True

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT user_action_kind, user_notes, user_acted_at FROM decisions WHERE id = ?",
            (pid,),
        ).fetchone()
        assert row[0] == "followed"
        assert "Added 8%" in row[1]
        assert row[2] is not None  # timestamp populated
    finally:
        conn.close()


def test_record_outcome_writes_columns(db: Path) -> None:
    pid = record_decision(
        ticker="NVO",
        recommendation_kind="hold",
        recommendation_value=None,
        conviction=None,
        source_artifact_id=20,
        source_lens="five_min_reread",
        rationale_excerpt="Fair value, no MoS.",
        made_at=datetime(2026, 1, 1, tzinfo=UTC),
        db_path=db,
    )
    assert pid is not None
    ok = record_outcome(
        decision_id=pid,
        outcome_label="correct",
        outcome_pct=0.02,
        outcome_notes="Flat band — HOLD was right.",
        db_path=db,
    )
    assert ok is True

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT outcome_label, outcome_pct, outcome_notes, outcome_at FROM decisions WHERE id = ?",
            (pid,),
        ).fetchone()
        assert row[0] == "correct"
        assert float(row[1]) == pytest.approx(0.02)
        assert "HOLD was right" in row[2]
        assert row[3] is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read paths — history, pending, aggregates
# ---------------------------------------------------------------------------


def test_history_returns_newest_first(db: Path) -> None:
    record_decision(
        ticker="GOOG",
        recommendation_kind="trim",
        recommendation_value=20.0,
        conviction="high",
        source_artifact_id=1,
        source_lens="five_min_reread",
        rationale_excerpt="r1",
        made_at=datetime(2026, 3, 1, tzinfo=UTC),
        db_path=db,
    )
    record_decision(
        ticker="GOOG",
        recommendation_kind="hold",
        recommendation_value=None,
        conviction=None,
        source_artifact_id=2,
        source_lens="five_min_reread",
        rationale_excerpt="r2",
        made_at=datetime(2026, 5, 1, tzinfo=UTC),
        db_path=db,
    )
    out = history(ticker="GOOG", db_path=db)
    assert len(out) == 2
    # Newest first
    assert out[0].recommendation_kind == "hold"
    assert out[1].recommendation_kind == "trim"


def test_pending_for_grading_filters_age(db: Path) -> None:
    now = datetime.now(UTC)
    # Old → grade-eligible
    record_decision(
        ticker="AMZN",
        recommendation_kind="add",
        recommendation_value=8.0,
        conviction=None,
        source_artifact_id=1,
        source_lens="five_min_reread",
        rationale_excerpt="old",
        made_at=now - timedelta(days=60),
        db_path=db,
    )
    # Fresh → too new to grade
    record_decision(
        ticker="AMZN",
        recommendation_kind="hold",
        recommendation_value=None,
        conviction=None,
        source_artifact_id=2,
        source_lens="five_min_reread",
        rationale_excerpt="fresh",
        made_at=now - timedelta(days=3),
        db_path=db,
    )
    pending = pending_for_grading(older_than_days=30, db_path=db)
    assert len(pending) == 1
    assert pending[0].source_artifact_id == 1


def test_pending_for_grading_skips_already_graded(db: Path) -> None:
    pid = record_decision(
        ticker="META",
        recommendation_kind="add",
        recommendation_value=15.0,
        conviction="high",
        source_artifact_id=5,
        source_lens="five_min_reread",
        rationale_excerpt="r",
        made_at=datetime(2026, 1, 1, tzinfo=UTC),
        db_path=db,
    )
    assert pid is not None
    record_outcome(decision_id=pid, outcome_label="correct", outcome_pct=0.10, db_path=db)
    pending = pending_for_grading(older_than_days=1, db_path=db)
    assert pending == []


def test_hit_rate_by_kind_aggregates(db: Path) -> None:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    cases = [
        ("add", "correct", 1),
        ("add", "wrong", 2),
        ("trim", "correct", 3),
        ("trim", "mixed", 4),
        ("hold", "correct", 5),
    ]
    for kind, outcome_label, source_id in cases:
        pid = record_decision(
            ticker="X",
            recommendation_kind=kind,  # type: ignore[arg-type]
            recommendation_value=None,
            conviction=None,
            source_artifact_id=source_id,
            source_lens="five_min_reread",
            rationale_excerpt="r",
            made_at=base,
            db_path=db,
        )
        assert pid is not None
        record_outcome(decision_id=pid, outcome_label=outcome_label, db_path=db)  # type: ignore[arg-type]

    agg = hit_rate_by_kind(db_path=db)
    assert agg["add"]["correct"] == 1
    assert agg["add"]["wrong"] == 1
    assert agg["trim"]["correct"] == 1
    assert agg["trim"]["mixed"] == 1
    assert agg["hold"]["correct"] == 1


def test_outcome_curve_by_conviction(db: Path) -> None:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    for source_id, conv, outcome_label in [
        (10, "high", "correct"),
        (11, "high", "wrong"),
        (12, "low", "correct"),
    ]:
        pid = record_decision(
            ticker="X",
            recommendation_kind="add",
            recommendation_value=5.0,
            conviction=conv,
            source_artifact_id=source_id,
            source_lens="five_min_reread",
            rationale_excerpt="r",
            made_at=base,
            db_path=db,
        )
        assert pid is not None
        record_outcome(decision_id=pid, outcome_label=outcome_label, db_path=db)  # type: ignore[arg-type]
    curve = outcome_curve_by_conviction(db_path=db)
    assert curve["high"]["correct"] == 1
    assert curve["high"]["wrong"] == 1
    assert curve["low"]["correct"] == 1


# ---------------------------------------------------------------------------
# Batch recorder — walks llm_artifacts, idempotent, supersession-aware
# ---------------------------------------------------------------------------


def _seed_artifact(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    purpose: str,
    content_md: str,
    generated_at: datetime,
    superseded_by_id: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO llm_artifacts(
            ticker, scope, purpose, content_md, input_sha256,
            generated_at, superseded_by_id, dirty
        ) VALUES (?, 'ticker', ?, ?, 'h', ?, ?, 0)
        """,
        (ticker, purpose, content_md, generated_at.isoformat(), superseded_by_id),
    )
    return int(cur.lastrowid or 0)


def test_batch_recorder_inserts_from_lens_artifacts(repo_root: Path) -> None:
    db_path = repo_root / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        _seed_artifact(
            conn,
            ticker="GOOG",
            purpose="lens:five_min_reread",
            content_md=(
                "## 1. What changed\n\nstuff\n\n"
                "## 2. Recommended Action\n\n**TRIM 20%**\n\nPremium too wide.\n\n"
                "## 3. What would change my mind\n..."
            ),
            generated_at=datetime.now(UTC) - timedelta(days=2),
        )
        _seed_artifact(
            conn,
            ticker="META",
            purpose="lens:five_min_reread",
            content_md=(
                "## 2. Recommended Action\n\n**ADD 15%**\n\nThesis intact, discount real."
            ),
            generated_at=datetime.now(UTC) - timedelta(days=5),
        )
        conn.commit()
    finally:
        conn.close()

    tally = record_decisions_from_artifacts(repo_root=repo_root, since_days=30)
    assert tally["inserted"] == 2
    assert tally["skipped_existing"] == 0

    # Re-run — idempotent
    tally2 = record_decisions_from_artifacts(repo_root=repo_root, since_days=30)
    assert tally2["inserted"] == 0
    assert tally2["skipped_existing"] == 2


def test_batch_recorder_skips_artifacts_without_recommendation(repo_root: Path) -> None:
    db_path = repo_root / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        _seed_artifact(
            conn,
            ticker="GOOG",
            purpose="lens:five_min_reread",
            content_md="## 1. Notes\n\nNo recommendation section here.",
            generated_at=datetime.now(UTC) - timedelta(days=2),
        )
        conn.commit()
    finally:
        conn.close()

    tally = record_decisions_from_artifacts(repo_root=repo_root, since_days=30)
    assert tally["inserted"] == 0
    assert tally["no_recommendation"] == 1


def test_batch_recorder_ignores_superseded_artifacts(repo_root: Path) -> None:
    db_path = repo_root / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        # Newer artifact (current)
        new_id = _seed_artifact(
            conn,
            ticker="AMZN",
            purpose="lens:five_min_reread",
            content_md="## 2. Recommended Action\n\n**ADD 5%**\n\nFresh take.",
            generated_at=datetime.now(UTC) - timedelta(days=1),
        )
        # Older artifact (superseded by the new one)
        _seed_artifact(
            conn,
            ticker="AMZN",
            purpose="lens:five_min_reread",
            content_md="## 2. Recommended Action\n\n**TRIM 10%**\n\nOld take.",
            generated_at=datetime.now(UTC) - timedelta(days=15),
            superseded_by_id=new_id,
        )
        conn.commit()
    finally:
        conn.close()

    tally = record_decisions_from_artifacts(repo_root=repo_root, since_days=30)
    assert tally["inserted"] == 1
    # Only the current (non-superseded) artifact's recommendation should land
    out = history(ticker="AMZN", db_path=db_path)
    assert len(out) == 1
    assert out[0].recommendation_kind == "add"


def test_batch_recorder_respects_since_days_window(repo_root: Path) -> None:
    db_path = repo_root / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        # Inside window
        _seed_artifact(
            conn,
            ticker="X",
            purpose="lens:five_min_reread",
            content_md="## 2. Recommended Action\n\n**HOLD**\n\nIn window.",
            generated_at=datetime.now(UTC) - timedelta(days=2),
        )
        # Outside window
        _seed_artifact(
            conn,
            ticker="Y",
            purpose="lens:five_min_reread",
            content_md="## 2. Recommended Action\n\n**ADD 5%**\n\nToo old.",
            generated_at=datetime.now(UTC) - timedelta(days=60),
        )
        conn.commit()
    finally:
        conn.close()

    tally = record_decisions_from_artifacts(repo_root=repo_root, since_days=30)
    assert tally["inserted"] == 1
