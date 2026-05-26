"""Tests for src/llm/calibration.py + the prompt_calibration_scores table.

The graders (grade_bear_cases, grade_decisions) call record_score to
durable-log the LLM output quality; the dashboard groups them via
summarize_by_prompt_version to surface "is v3 better than v2".
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm.calibration import (  # noqa: E402
    CalibrationScore,
    record_score,
    summarize_by_prompt_version,
)


def _build_schema(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE prompt_calibration_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purpose TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                ticker TEXT,
                score REAL NOT NULL,
                reason TEXT,
                scored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                scored_by TEXT,
                artifact_id INTEGER
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_record_score_inserts_row(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _build_schema(db)
    rid = record_score(
        CalibrationScore(
            purpose="bear_case",
            prompt_version="v3",
            score=0.85,
            ticker="META",
            reason="non-consensus failure mode landed",
            scored_by="auto:bear_case_grader",
        ),
        db_path=db,
    )
    assert rid is not None and rid > 0

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT purpose, prompt_version, ticker, score FROM prompt_calibration_scores"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "bear_case"
    assert row[1] == "v3"
    assert row[2] == "META"
    assert row[3] == 0.85


def test_record_score_silently_skips_when_table_missing(tmp_path: Path) -> None:
    """Synthetic env without migration 0058 — must not raise."""
    db = tmp_path / "portfolio.db"
    sqlite3.connect(str(db)).close()  # empty DB
    rid = record_score(
        CalibrationScore(purpose="bear_case", prompt_version="v3", score=0.5),
        db_path=db,
    )
    assert rid is None


def test_summarize_groups_by_purpose_and_version(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _build_schema(db)

    # Seed: v2 (lower scores) and v3 (higher scores) for bear_case,
    # plus a few pairwise_analysis rows on v1.
    scores = [
        ("bear_case", "v2", 0.4),
        ("bear_case", "v2", 0.5),
        ("bear_case", "v3", 0.8),
        ("bear_case", "v3", 0.85),
        ("bear_case", "v3", 0.9),
        ("pairwise_analysis", "v1", 0.6),
        ("pairwise_analysis", "v1", 0.65),
    ]
    for purpose, version, score in scores:
        record_score(
            CalibrationScore(
                purpose=purpose,
                prompt_version=version,
                score=score,
            ),
            db_path=db,
        )

    summaries = summarize_by_prompt_version(db_path=db)
    by_key = {(s.purpose, s.prompt_version): s for s in summaries}

    v2 = by_key[("bear_case", "v2")]
    assert v2.score_count == 2
    assert v2.avg_score == 0.45
    assert v2.min_score == 0.4
    assert v2.max_score == 0.5

    v3 = by_key[("bear_case", "v3")]
    assert v3.score_count == 3
    assert v3.avg_score > 0.84  # ~0.85
    assert v3.min_score == 0.8
    assert v3.max_score == 0.9

    pair_v1 = by_key[("pairwise_analysis", "v1")]
    assert pair_v1.score_count == 2


def test_summarize_filters_by_purpose(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _build_schema(db)
    record_score(
        CalibrationScore(purpose="bear_case", prompt_version="v3", score=0.7),
        db_path=db,
    )
    record_score(
        CalibrationScore(purpose="pairwise_analysis", prompt_version="v1", score=0.5),
        db_path=db,
    )

    bear_only = summarize_by_prompt_version(db_path=db, purpose="bear_case")
    assert len(bear_only) == 1
    assert bear_only[0].purpose == "bear_case"


def test_summarize_filters_by_since_cutoff(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _build_schema(db)
    record_score(
        CalibrationScore(purpose="bear_case", prompt_version="v3", score=0.5),
        db_path=db,
    )
    # The since cutoff is naive in this codebase; recent inserts have
    # scored_at = utcnow(). A future cutoff should exclude them.
    future = datetime.utcnow() + timedelta(days=1)
    out = summarize_by_prompt_version(db_path=db, since=future)
    assert out == []


def test_summarize_returns_empty_for_missing_db(tmp_path: Path) -> None:
    """No DB file at all — read returns []."""
    out = summarize_by_prompt_version(db_path=tmp_path / "does-not-exist.db")
    assert out == []
