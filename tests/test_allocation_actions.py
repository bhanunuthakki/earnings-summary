"""Tests for src/allocation/actions.py — the ONE action core for the
Incremental Dollar Recommendation's owner verbs (save_intent /
hold_accountable / dismiss).

Hand-rolled minimal decisions + llm_artifacts schema (post-0188 shape, no
alembic-head fixture), mirroring tests/test_recommendation_frontier.py's
style. No LLM call anywhere on this path.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from allocation.actions import (
    RecommendationActionError,
    act_on_recommendation,
)

_PURPOSE = "incremental_dollar_recommendation"


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16),
            recommendation_kind VARCHAR(32) NOT NULL,
            recommendation_value REAL,
            conviction VARCHAR(16),
            source_artifact_id INTEGER,
            source_memo_id INTEGER,
            rationale_excerpt TEXT,
            made_at DATETIME NOT NULL,
            user_acted_at DATETIME,
            user_action_kind VARCHAR(32),
            user_notes TEXT,
            outcome_at DATETIME,
            outcome_label VARCHAR(16),
            created_at DATETIME NOT NULL,
            decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
            scope VARCHAR(16) NOT NULL DEFAULT 'ticker',
            advice_artifact_id INTEGER
        );
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16),
            scope VARCHAR(64) NOT NULL DEFAULT 'ticker',
            purpose VARCHAR(64) NOT NULL,
            content_md TEXT,
            content_json TEXT,
            input_sha256 VARCHAR(64) NOT NULL,
            generated_at DATETIME NOT NULL,
            superseded_by_id INTEGER,
            dirty BOOLEAN NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_artifact(
    db_path: Path, *, artifact_id: int, purpose: str = _PURPOSE, status: str = "deploy_all"
) -> None:
    content = {
        "status": status,
        "central_hypothesis": "NU is the best risk-adjusted use of this cash.",
    }
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO llm_artifacts (id, scope, purpose, content_json, input_sha256, generated_at) "
        "VALUES (?, 'portfolio', ?, ?, 'sha', '2026-07-20T00:00:00')",
        (artifact_id, purpose, json.dumps(content)),
    )
    conn.commit()
    conn.close()


def _decisions(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM decisions").fetchall()
    finally:
        conn.close()


def test_save_intent_creates_linked_decision(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1, status="deploy_all")

    status = act_on_recommendation(1, "save_intent", db_path=db_path)

    assert status == "saved"
    rows = _decisions(db_path)
    assert len(rows) == 1
    assert rows[0]["advice_artifact_id"] == 1
    assert rows[0]["decided_by"] == "owner"
    assert rows[0]["scope"] == "portfolio"
    assert rows[0]["recommendation_kind"] == "allocate"
    assert "NU" in rows[0]["rationale_excerpt"]


def test_save_intent_retain_all_maps_to_hold(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=2, status="retain_all")

    act_on_recommendation(2, "save_intent", db_path=db_path)

    rows = _decisions(db_path)
    assert rows[0]["recommendation_kind"] == "hold"


def test_double_save_intent_is_idempotent(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)

    first = act_on_recommendation(1, "save_intent", db_path=db_path)
    second = act_on_recommendation(1, "save_intent", db_path=db_path)

    assert first == "saved"
    assert second == "already_saved"
    assert len(_decisions(db_path)) == 1


def test_hold_accountable_marks_and_is_idempotent(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)

    first = act_on_recommendation(1, "hold_accountable", db_path=db_path)
    second = act_on_recommendation(1, "hold_accountable", db_path=db_path)

    assert first == "marked_accountable"
    assert second == "already_marked"
    rows = _decisions(db_path)
    assert len(rows) == 1
    assert rows[0]["user_action_kind"] == "hold_accountable"


def test_save_intent_and_hold_accountable_are_distinct_verbs(tmp_path: Path) -> None:
    """save_intent and hold_accountable each write their own decision row —
    idempotency is scoped per-verb, not per-artifact."""
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)

    act_on_recommendation(1, "save_intent", db_path=db_path)
    act_on_recommendation(1, "hold_accountable", db_path=db_path)

    rows = _decisions(db_path)
    assert len(rows) == 2
    kinds = {r["user_action_kind"] for r in rows}
    assert kinds == {None, "hold_accountable"}


def test_dismiss_creates_no_decision(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)

    status = act_on_recommendation(1, "dismiss", db_path=db_path)

    assert status == "dismissed"
    assert _decisions(db_path) == []


def test_unknown_verb_raises(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)

    with pytest.raises(RecommendationActionError):
        act_on_recommendation(1, "bogus_verb", db_path=db_path)


def test_missing_artifact_raises(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)

    with pytest.raises(RecommendationActionError):
        act_on_recommendation(999, "save_intent", db_path=db_path)


def test_wrong_purpose_artifact_raises(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1, purpose="bear_case")

    with pytest.raises(RecommendationActionError):
        act_on_recommendation(1, "save_intent", db_path=db_path)


def test_notes_persisted_on_save_intent(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)

    act_on_recommendation(1, "save_intent", db_path=db_path, notes="funding from bonus")

    rows = _decisions(db_path)
    assert rows[0]["user_notes"] == "funding from bonus"
