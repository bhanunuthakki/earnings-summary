"""Tests for ``research.investment_decision_card.act_on_card`` — the ONE
action core for the P1.1 Investment Decision Card's three explicit owner
actions (pass/watch/promote — PRD §8.1 disposition semantics).

Hand-rolled minimal decisions + llm_artifacts + tracked_companies +
research_tasks schema, mirroring tests/test_allocation_actions.py's style.
No LLM call anywhere on this path.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from identity import DEFAULT_USER_ID
from research.investment_decision_card import CardActionError, act_on_card

_PURPOSE = "investment_decision_card"
TICKER = "ACME"

_DDL = """
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    list_type TEXT NOT NULL,
    brief_dirty BOOLEAN DEFAULT 0,
    archived_at TEXT
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
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    scope VARCHAR(16) NOT NULL DEFAULT 'ticker',
    advice_artifact_id INTEGER,
    rationale_excerpt TEXT,
    user_notes TEXT,
    user_action_kind VARCHAR(32),
    user_acted_at DATETIME,
    made_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE TABLE research_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER,
    claim TEXT NOT NULL,
    ticker TEXT,
    status TEXT NOT NULL,
    -- cost_usd/run_id are the legacy physical columns used for stated cost and
    -- JSON metadata — the hand DDL must
    -- track the real schema or every disposition test breaks on INSERT.
    cost_usd FLOAT,
    run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _make_db(tmp_path: Path, *, list_type: str = "evaluation") -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, list_type) VALUES (?, ?, ?)",
        (DEFAULT_USER_ID, TICKER, list_type),
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_artifact(db_path: Path, *, artifact_id: int, ticker: str = TICKER) -> None:
    content = {
        "company_hypothesis": {
            "directional_thesis": "ACME dominates checkout via network effects."
        },
    }
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO llm_artifacts (id, ticker, scope, purpose, content_json, "
        "input_sha256, generated_at) VALUES (?, ?, 'ticker', ?, ?, 'sha', '2026-07-20T00:00:00')",
        (artifact_id, ticker, _PURPOSE, json.dumps(content)),
    )
    conn.commit()
    conn.close()


def _tracked_row(db_path: Path) -> tuple[str, str | None]:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT list_type, archived_at FROM tracked_companies WHERE ticker = ?", (TICKER,)
        ).fetchone()
        return (str(row[0]), row[1])
    finally:
        conn.close()


def _decisions(db_path: Path) -> list[tuple[str, str, int, str, str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT ticker, recommendation_kind, advice_artifact_id, decided_by, scope "
            "FROM decisions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Unknown verb / missing / wrong-purpose artifact
# --------------------------------------------------------------------------- #


def test_unknown_verb_raises(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)
    with pytest.raises(CardActionError):
        act_on_card(1, "buy", db_path=db_path)


def test_missing_artifact_raises(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with pytest.raises(CardActionError):
        act_on_card(999, "pass", db_path=db_path)


def test_wrong_purpose_artifact_raises(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO llm_artifacts (id, ticker, scope, purpose, content_json, "
        "input_sha256, generated_at) VALUES (1, ?, 'ticker', 'bear_case', '{}', 'sha', "
        "'2026-07-20T00:00:00')",
        (TICKER,),
    )
    conn.commit()
    conn.close()
    with pytest.raises(CardActionError):
        act_on_card(1, "pass", db_path=db_path)


# --------------------------------------------------------------------------- #
# pass — archives + records decision
# --------------------------------------------------------------------------- #


def test_pass_archives_and_records_decision(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)

    status = act_on_card(1, "pass", db_path=db_path)

    assert status == "pass_recorded"
    list_type, archived_at = _tracked_row(db_path)
    assert list_type == "evaluation"
    assert archived_at is not None
    rows = _decisions(db_path)
    assert rows == [(TICKER, "pass", 1, "owner", "ticker")]


def test_pass_idempotent_double_post(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)

    first = act_on_card(1, "pass", db_path=db_path)
    second = act_on_card(1, "pass", db_path=db_path)

    assert first == "pass_recorded"
    assert second == "already_pass"
    assert len(_decisions(db_path)) == 1


# --------------------------------------------------------------------------- #
# watch — retains/moves to watchlist + records decision
# --------------------------------------------------------------------------- #


def test_watch_sets_watchlist_and_records_decision(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)

    status = act_on_card(1, "watch", db_path=db_path)

    assert status == "watch_recorded"
    list_type, archived_at = _tracked_row(db_path)
    assert list_type == "watchlist"
    assert archived_at is None
    rows = _decisions(db_path)
    assert rows == [(TICKER, "watch", 1, "owner", "ticker")]


def test_watch_idempotent_double_post(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)

    act_on_card(1, "watch", db_path=db_path)
    second = act_on_card(1, "watch", db_path=db_path)

    assert second == "already_watch"
    assert len(_decisions(db_path)) == 1


# --------------------------------------------------------------------------- #
# promote — list_type stays 'evaluation'; the decision row IS the marker
# --------------------------------------------------------------------------- #


def test_promote_leaves_list_type_evaluation(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)

    status = act_on_card(1, "promote", db_path=db_path)

    assert status == "promote_recorded"
    list_type, archived_at = _tracked_row(db_path)
    assert list_type == "evaluation"  # unchanged — promote never touches list_type
    assert archived_at is None
    rows = _decisions(db_path)
    assert rows == [(TICKER, "promote", 1, "owner", "ticker")]


def test_promote_idempotent_double_post(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)

    act_on_card(1, "promote", db_path=db_path)
    second = act_on_card(1, "promote", db_path=db_path)

    assert second == "already_promote"
    assert len(_decisions(db_path)) == 1


# --------------------------------------------------------------------------- #
# Retired research_further action — stale calls must not write either store.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Reconciliation safety — pass/watch/promote are never presumed buy/sell
# --------------------------------------------------------------------------- #
