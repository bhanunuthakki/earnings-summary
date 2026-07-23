"""Tests for ``research.investment_decision_card.act_on_card`` — the ONE
action core for the P1.1 Investment Decision Card's four owner verbs
(pass/watch/research_further/promote — PRD §8.1 disposition semantics).

Hand-rolled minimal decisions + llm_artifacts + tracked_companies +
research_tasks schema, mirroring tests/test_allocation_actions.py's style.
No LLM call anywhere on this path.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from decision_extractor import (
    _KIND_DIRECTION,  # pyright: ignore[reportPrivateUsage]  # internal seam under test
    reconcile_decision_actions,
)
from identity import DEFAULT_USER_ID
from integrations.portfolio_tracker_client import LivePortfolio, LiveTransaction
from research.investment_decision_card import CardActionError, act_on_card

_PURPOSE = "investment_decision_card"
TICKER = "ACME"

_DDL = """
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    list_type TEXT NOT NULL,
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
# research_further — research_tasks row, NO decision row
# --------------------------------------------------------------------------- #


def test_research_further_creates_task_no_decision_row(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)

    status = act_on_card(1, "research_further", db_path=db_path)

    assert status.startswith("research_task_created:")
    assert _decisions(db_path) == []
    conn = sqlite3.connect(str(db_path))
    try:
        task = conn.execute("SELECT ticker, status, claim FROM research_tasks").fetchone()
    finally:
        conn.close()
    assert task is not None
    assert task[0] == TICKER
    assert task[1] == "proposed"
    assert "checkout" in task[2] or "ACME" in task[2]


def test_research_further_idempotent_double_post(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)

    first = act_on_card(1, "research_further", db_path=db_path)
    second = act_on_card(1, "research_further", db_path=db_path)

    assert first.startswith("research_task_created:")
    assert second.startswith("already_proposed:")
    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute("SELECT COUNT(*) FROM research_tasks").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


# --------------------------------------------------------------------------- #
# Reconciliation safety — pass/watch/promote are never presumed buy/sell
# --------------------------------------------------------------------------- #


def test_kind_direction_has_no_entry_for_card_dispositions() -> None:
    for kind in ("pass", "watch", "promote"):
        assert _KIND_DIRECTION.get(kind) is None


def test_reconciliation_treats_promote_as_non_directional(tmp_path: Path) -> None:
    """A 'promote' decision with NO fill in its window reconciles through the
    HOLD/AVOID (fill-vs-no-fill) branch — 'followed' once the window elapses
    with nothing traded — never through the buy/sell direction branch (which
    would require a _KIND_DIRECTION entry that doesn't exist)."""
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)
    act_on_card(1, "promote", db_path=db_path)
    # made_at recorded as "now" by act_on_card — backdate it so the 30d
    # window has already elapsed relative to `now` below.
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE decisions SET made_at = '2026-01-01T00:00:00'")
    conn.commit()
    conn.close()

    portfolio = LivePortfolio(available=True, api_url="http://tracker.test", transactions=[])
    tally = reconcile_decision_actions(
        db_path=db_path, portfolio=portfolio, now=datetime(2026, 3, 1, tzinfo=UTC)
    )

    assert tally["followed"] == 1  # held/passed cleanly through the window
    conn = sqlite3.connect(str(db_path))
    try:
        action = conn.execute("SELECT user_action_kind FROM decisions").fetchone()[0]
    finally:
        conn.close()
    assert action == "followed"


def test_reconciliation_treats_promote_with_a_fill_as_partial(tmp_path: Path) -> None:
    """A 'promote' decision followed by ANY fill (buy or sell) in-window is
    'partial' — the HOLD/AVOID branch's "traded anyway" read — never
    'followed'/'reversed' (those require a _KIND_DIRECTION entry)."""
    db_path = _make_db(tmp_path)
    _insert_artifact(db_path, artifact_id=1)
    act_on_card(1, "promote", db_path=db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE decisions SET made_at = '2026-01-10T00:00:00'")
    conn.commit()
    conn.close()

    portfolio = LivePortfolio(
        available=True,
        api_url="http://tracker.test",
        transactions=[
            LiveTransaction(
                date="2026-01-15",
                ticker=TICKER,
                name=None,
                type="buy",
                subtype=None,
                quantity=1.0,
                amount=-1.0,
                account_name="x",
            )
        ],
    )
    tally = reconcile_decision_actions(
        db_path=db_path, portfolio=portfolio, now=datetime(2026, 3, 1, tzinfo=UTC)
    )

    assert tally["partial"] == 1
    conn = sqlite3.connect(str(db_path))
    try:
        action = conn.execute("SELECT user_action_kind FROM decisions").fetchone()[0]
    finally:
        conn.close()
    assert action == "partial"
