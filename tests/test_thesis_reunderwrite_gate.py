"""Scored-miss gate on re-underwrites (monthly_red_team.md Phase 3, PR7).

Covers the predicate (``is_material_change`` / ``breach_onset`` /
``has_scored_miss_since`` / ``evaluate_gate``) in isolation, then the wiring
into ``compute.thesis_evaluator.persist_verdict`` (detect / block / unblock /
override) end to end against a hand-rolled schema mirroring the real
``decisions`` (0130 head) + ``thesis_state`` + ``thesis_evaluations`` tables.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from compute.thesis_evaluator import BreachStatus, ThesisVerdict, persist_verdict
from thesis_reunderwrite_gate import (
    ReUnderwriteBlockedError,
    breach_onset,
    evaluate_gate,
    has_scored_miss_since,
    is_material_change,
)

# ---------------------------------------------------------------------------
# is_material_change
# ---------------------------------------------------------------------------


def test_material_change_detects_rewrite() -> None:
    assert is_material_change("old belief about growth", "brand new different narrative") is True


def test_material_change_ignores_whitespace_reflow() -> None:
    old = "line one\nline two   with  extra space"
    new = "line one line two with extra space"
    assert is_material_change(old, new) is False


def test_material_change_none_new_is_not_a_change() -> None:
    assert is_material_change("old", None) is False
    assert is_material_change("old", "") is False


def test_material_change_none_old_treated_as_change() -> None:
    assert is_material_change(None, "a fresh thesis") is True


# ---------------------------------------------------------------------------
# fixture schema — decisions (0130 head) + thesis_state + thesis_evaluations
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
    source_memo_id INTEGER,
    rationale_excerpt TEXT,
    made_at DATETIME NOT NULL,
    outcome_at DATETIME,
    outcome_label VARCHAR(16),
    outcome_pct FLOAT,
    outcome_notes TEXT,
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    scope VARCHAR(16) NOT NULL DEFAULT 'ticker',
    created_at DATETIME NOT NULL,
    CONSTRAINT ck_decisions_source_present CHECK (
        source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL
        OR recommendation_kind = 'avoid' OR decided_by = 'owner'
    ),
    CONSTRAINT ck_decisions_ticker_scope CHECK (scope = 'portfolio' OR ticker IS NOT NULL)
);
CREATE TABLE thesis_state (
    ticker TEXT PRIMARY KEY,
    thesis TEXT,
    breach_status TEXT,
    last_updated TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    ingested_at TEXT
);
CREATE TABLE thesis_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    evaluated_at TEXT,
    overall_status TEXT,
    rule_evaluations_json TEXT,
    run_id TEXT
);
"""


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.commit()
    return c


def _seed_breach(conn: sqlite3.Connection, ticker: str = "NVO") -> None:
    conn.execute(
        "INSERT INTO thesis_state (ticker, thesis, breach_status, last_updated, raw_json, ingested_at) "
        "VALUES (?, 'old GLP-1 volume-growth thesis', 'breach', '2026-01-01T00:00:00', '{}', '2026-01-01T00:00:00')",
        (ticker,),
    )
    conn.execute(
        "INSERT INTO thesis_evaluations (ticker, evaluated_at, overall_status, rule_evaluations_json) "
        "VALUES (?, '2026-01-01T00:00:00', 'breach', '[]')",
        (ticker,),
    )
    conn.commit()


def _log_scored_miss(
    conn: sqlite3.Connection, ticker: str = "NVO", created_at: str = "2026-02-01T00:00:00"
) -> None:
    conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, conviction, decided_by, scope, "
        "rationale_excerpt, made_at, outcome_at, outcome_label, outcome_notes, created_at) "
        "VALUES (?, 'scored_miss', 'high', 'owner', 'ticker', 'b', '2026-01-01T00:00:00', "
        "'2026-02-01T00:00:00', 'wrong', 'US pricing reform', ?)",
        (ticker, created_at),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# breach_onset / has_scored_miss_since
# ---------------------------------------------------------------------------


def test_breach_onset_walks_unbroken_streak(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO thesis_evaluations (ticker, evaluated_at, overall_status, rule_evaluations_json) VALUES (?, ?, ?, '[]')",
        [
            ("NVO", "2025-11-01T00:00:00", "ok"),
            ("NVO", "2025-12-01T00:00:00", "warn"),
            ("NVO", "2026-01-01T00:00:00", "breach"),
            ("NVO", "2026-02-01T00:00:00", "breach"),
        ],
    )
    conn.commit()
    assert breach_onset(conn, "NVO") == "2025-12-01T00:00:00"


def test_breach_onset_none_when_currently_ok(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO thesis_evaluations (ticker, evaluated_at, overall_status, rule_evaluations_json) VALUES ('NVO', '2026-01-01T00:00:00', 'ok', '[]')"
    )
    conn.commit()
    assert breach_onset(conn, "NVO") is None


def test_has_scored_miss_since_respects_cutoff(conn: sqlite3.Connection) -> None:
    _log_scored_miss(conn, created_at="2025-06-01T00:00:00")  # before onset
    assert has_scored_miss_since(conn, "NVO", "2026-01-01T00:00:00") is False
    _log_scored_miss(conn, created_at="2026-03-01T00:00:00")  # after onset
    assert has_scored_miss_since(conn, "NVO", "2026-01-01T00:00:00") is True


def test_has_scored_miss_since_none_onset_checks_any_time(conn: sqlite3.Connection) -> None:
    assert has_scored_miss_since(conn, "NVO", None) is False
    _log_scored_miss(conn)
    assert has_scored_miss_since(conn, "NVO", None) is True


# ---------------------------------------------------------------------------
# evaluate_gate
# ---------------------------------------------------------------------------


def test_evaluate_gate_not_reunderwrite_when_ok(conn: sqlite3.Connection) -> None:
    v = evaluate_gate(
        conn, ticker="NVO", prior_thesis="old", prior_breach_status="ok", new_thesis="totally new"
    )
    assert v.is_reunderwrite is False
    assert v.blocked is False


def test_evaluate_gate_not_reunderwrite_when_unchanged(conn: sqlite3.Connection) -> None:
    v = evaluate_gate(
        conn,
        ticker="NVO",
        prior_thesis="same text",
        prior_breach_status="breach",
        new_thesis="same text",
    )
    assert v.is_reunderwrite is False


def test_evaluate_gate_blocks_reunderwrite_without_scored_miss(conn: sqlite3.Connection) -> None:
    _seed_breach(conn)
    v = evaluate_gate(
        conn,
        ticker="NVO",
        prior_thesis="old GLP-1 volume-growth thesis",
        prior_breach_status="breach",
        new_thesis="brand new different narrative",
    )
    assert v.is_reunderwrite is True
    assert v.blocked is True
    assert v.onset == "2026-01-01T00:00:00"


def test_evaluate_gate_unblocks_with_scored_miss(conn: sqlite3.Connection) -> None:
    _seed_breach(conn)
    _log_scored_miss(conn, created_at="2026-02-01T00:00:00")
    v = evaluate_gate(
        conn,
        ticker="NVO",
        prior_thesis="old GLP-1 volume-growth thesis",
        prior_breach_status="breach",
        new_thesis="brand new different narrative",
    )
    assert v.is_reunderwrite is True
    assert v.blocked is False
    assert v.scored_miss_found is True


# ---------------------------------------------------------------------------
# persist_verdict wiring — detect / block / unblock / override
# ---------------------------------------------------------------------------


def _verdict(ticker: str, thesis: str, status: BreachStatus, when: datetime) -> ThesisVerdict:
    return ThesisVerdict(
        ticker=ticker,
        thesis=thesis,
        overall_status=status,
        rule_evaluations=(),
        evaluated_at=when,
        soft_rule_results=(),
    )


def test_persist_verdict_blocks_reunderwrite(conn: sqlite3.Connection) -> None:
    _seed_breach(conn)
    v = _verdict("NVO", "brand new different narrative", BreachStatus.OK, datetime(2026, 3, 1))
    with pytest.raises(ReUnderwriteBlockedError) as exc_info:
        persist_verdict(conn, v)
    assert "NVO" in str(exc_info.value)
    assert "log_scored_miss.py" in str(exc_info.value)
    # Blocked — thesis_state must be untouched.
    row = conn.execute("SELECT breach_status FROM thesis_state WHERE ticker='NVO'").fetchone()
    assert row["breach_status"] == "breach"


def test_persist_verdict_unblocks_after_scored_miss(conn: sqlite3.Connection) -> None:
    _seed_breach(conn)
    _log_scored_miss(conn, created_at="2026-02-01T00:00:00")
    v = _verdict("NVO", "brand new different narrative", BreachStatus.OK, datetime(2026, 3, 1))
    persist_verdict(conn, v)  # must not raise
    row = conn.execute("SELECT breach_status FROM thesis_state WHERE ticker='NVO'").fetchone()
    assert row["breach_status"] == "ok"


def test_persist_verdict_override_logs_and_proceeds(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_breach(conn)
    v = _verdict("NVO", "brand new different narrative", BreachStatus.OK, datetime(2026, 3, 1))
    persist_verdict(conn, v, override=True)  # must not raise
    row = conn.execute("SELECT breach_status FROM thesis_state WHERE ticker='NVO'").fetchone()
    assert row["breach_status"] == "ok"


def test_persist_verdict_never_gates_a_fresh_ticker(conn: sqlite3.Connection) -> None:
    """No prior thesis_state row at all -> nothing to re-underwrite from."""
    v = _verdict("NEWTICKER", "first thesis ever written", BreachStatus.OK, datetime(2026, 3, 1))
    persist_verdict(conn, v)  # must not raise
    row = conn.execute("SELECT breach_status FROM thesis_state WHERE ticker='NEWTICKER'").fetchone()
    assert row["breach_status"] == "ok"


def test_persist_verdict_never_gates_ok_thesis(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO thesis_state (ticker, thesis, breach_status, last_updated, raw_json, ingested_at) "
        "VALUES ('WIX', 'an ok thesis', 'ok', '2026-01-01T00:00:00', '{}', '2026-01-01T00:00:00')"
    )
    conn.commit()
    v = _verdict("WIX", "a completely rewritten thesis text", BreachStatus.OK, datetime(2026, 3, 1))
    persist_verdict(conn, v)  # must not raise — status was 'ok', never gated


def test_persist_verdict_never_gates_a_corruption_stub(conn: sqlite3.Connection) -> None:
    """A raw_json._status=stub_regenerated_from_corruption row was never a real
    underwritten belief — sync_thesis_state.py's repair path must not be forced
    through the scored-miss gate (this is data hygiene, not a re-underwrite)."""
    conn.execute(
        "INSERT INTO thesis_state (ticker, thesis, breach_status, last_updated, raw_json, ingested_at) "
        "VALUES ('MELI', 'old', 'warn', '2026-01-01T00:00:00', "
        "'{\"_status\": \"stub_regenerated_from_corruption\"}', '2026-01-01T00:00:00')"
    )
    conn.commit()
    v = _verdict(
        "MELI", "MercadoLibre flywheel, freshly re-ingested", BreachStatus.OK, datetime(2026, 3, 1)
    )
    persist_verdict(conn, v)  # must not raise
    row = conn.execute("SELECT breach_status FROM thesis_state WHERE ticker='MELI'").fetchone()
    assert row["breach_status"] == "ok"
