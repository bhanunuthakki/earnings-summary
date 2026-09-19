from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from pipeline import evaluation_dialogues


def _make_context_json(
    ticker: str, candidate_id: int | None = None, instrument: str = "stock"
) -> tuple[str, str]:
    payload = {
        "schema_version": "session_context.v1",
        "company_ticker": ticker,
        "evaluation_candidate_id": candidate_id,
        "evaluation_instrument_type": instrument,
    }
    raw = json.dumps(payload)
    return raw, sha256(raw.encode("utf-8")).hexdigest()


def _init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
          user_id TEXT, ticker TEXT, name TEXT, list_type TEXT,
          instrument_type TEXT, archived_at TEXT
        );
        CREATE TABLE discovery_candidates (
          id INTEGER PRIMARY KEY, user_id TEXT, ticker TEXT, status TEXT
        );
        CREATE TABLE ask_sessions (
          id TEXT PRIMARY KEY, scope TEXT, title TEXT DEFAULT '',
          created_at TEXT DEFAULT '2026-08-20T00:00:00Z', updated_at TEXT, distilled_at TEXT
        );
        CREATE INDEX ix_ask_sessions_scope_updated ON ask_sessions (scope, updated_at DESC);
        CREATE TABLE ask_session_contexts (
          session_id TEXT PRIMARY KEY, schema_version TEXT DEFAULT 'session_context.v1',
          context_json TEXT NOT NULL, context_sha256 TEXT NOT NULL, revision INTEGER DEFAULT 0,
          created_at TEXT DEFAULT '2026-08-20T00:00:00Z', updated_at TEXT DEFAULT '2026-08-20T00:00:00Z'
        );
        CREATE TABLE analyst_notes (
          id INTEGER PRIMARY KEY, user_id TEXT, ticker TEXT, kind TEXT DEFAULT 'observation',
          status TEXT, body TEXT DEFAULT '', anchor_type TEXT, anchor_key TEXT,
          source TEXT DEFAULT 'user', source_ref TEXT, supersedes_id INTEGER,
          resolution_note TEXT, context_json TEXT, created_at TEXT,
          updated_at TEXT DEFAULT '2026-08-20T00:00:00Z', resolved_at TEXT,
          decision_id INTEGER, position_entry_id INTEGER, link_auto_resolve INTEGER DEFAULT 0,
          fact_ref TEXT
        );
        """
    )
    return conn


def _default_db(tmp_path: Path) -> Path:
    path = tmp_path / "dialogues.db"
    conn = _init_db(path)
    conn.executescript(
        """
        INSERT INTO tracked_companies VALUES
          ('bhanu', 'ZZZ', 'Zeta Stock', 'evaluation', 'equity', NULL),
          ('bhanu', 'AAA', 'Alpha Fund', 'evaluation', 'etf', NULL),
          ('bhanu', 'OLD', 'Archived', 'evaluation', 'equity', '2026-01-01');
        INSERT INTO discovery_candidates (id, user_id, ticker, status) VALUES
          (7, 'bhanu', 'ZZZ', 'built');
        INSERT INTO ask_sessions (id, scope, updated_at) VALUES
          ('session-z', 'portfolio', '2026-08-21T12:00:00Z');
        """
    )
    c_json, c_sha = _make_context_json("ZZZ", 7, "stock")
    conn.execute(
        "INSERT INTO ask_session_contexts (session_id, context_json, context_sha256) VALUES (?, ?, ?)",
        ("session-z", c_json, c_sha),
    )
    conn.execute(
        "INSERT INTO analyst_notes (user_id, ticker, created_at, status) VALUES ('bhanu', 'ZZZ', '2026-08-20T10:00:00Z', 'open')"
    )
    conn.commit()
    conn.close()
    return path


def test_dialogues_are_bounded_sorted_and_join_explicit_candidate_session(tmp_path: Path) -> None:
    path = _default_db(tmp_path)
    result = evaluation_dialogues.load_evaluation_dialogues(path)

    assert result.total_active == 2
    assert result.total_matching == 2
    assert result.matching_state == "complete"
    assert [item.ticker for item in result.items] == ["ZZZ", "AAA"]
    stock, fund = result.items
    assert fund.instrument_type == "etf"
    assert fund.workup_readiness == "available"
    assert fund.ask_session_link_state == "unlinked"
    assert stock.discovery_candidate_id == 7
    assert stock.ask_session_id == "session-z"
    assert stock.ask_session_link_state == "linked"
    assert stock.open_note_count == 1

    result_alpha = evaluation_dialogues.load_evaluation_dialogues(path, sort="ticker_asc")
    assert [item.ticker for item in result_alpha.items] == ["AAA", "ZZZ"]


def test_materialized_cte_deterministic_cutoff_and_context_join(tmp_path: Path) -> None:
    path = tmp_path / "tied_cutoff.db"
    conn = _init_db(path)

    conn.execute(
        "INSERT INTO tracked_companies VALUES ('bhanu', 'TIED', 'Tied Corp', 'evaluation', 'equity', NULL)"
    )

    # Insert 5,002 sessions with identical timestamp
    ts = "2026-08-20T12:00:00Z"
    c_json, c_sha = _make_context_json("TIED", None, "stock")
    session_data = [(f"sess-{i:05d}", "portfolio", ts) for i in range(5002)]
    conn.executemany(
        "INSERT INTO ask_sessions (id, scope, updated_at) VALUES (?, ?, ?)", session_data
    )

    context_data = [(f"sess-{i:05d}", c_json, c_sha) for i in range(5002)]
    conn.executemany(
        "INSERT INTO ask_session_contexts (session_id, context_json, context_sha256) VALUES (?, ?, ?)",
        context_data,
    )
    conn.commit()
    conn.close()

    result = evaluation_dialogues.load_evaluation_dialogues(path)
    assert result.total_active == 1
    assert len(result.items) == 1
    # Bounded fetch 5,001 entries ordered by id DESC -> highest id is sess-05001
    assert result.items[0].ask_session_id == "sess-05001"
    assert "sessions_source_unavailable" in result.reason_codes
    assert "relevance_partial" in result.reason_codes


def test_session_truncation_boundary_5000_vs_5001(tmp_path: Path) -> None:
    # 5,000 sessions: complete
    path_5k = tmp_path / "sess_5k.db"
    conn = _init_db(path_5k)
    conn.execute(
        "INSERT INTO tracked_companies VALUES ('bhanu', 'S5K', '5K Corp', 'evaluation', 'equity', NULL)"
    )
    c_json, c_sha = _make_context_json("S5K", None, "stock")
    session_data = [
        (f"s-{i:05d}", "portfolio", f"2026-08-20T12:00:{i % 60:02d}Z") for i in range(5000)
    ]
    conn.executemany(
        "INSERT INTO ask_sessions (id, scope, updated_at) VALUES (?, ?, ?)", session_data
    )
    context_data = [(f"s-{i:05d}", c_json, c_sha) for i in range(5000)]
    conn.executemany(
        "INSERT INTO ask_session_contexts (session_id, context_json, context_sha256) VALUES (?, ?, ?)",
        context_data,
    )
    conn.commit()
    conn.close()

    res_5k = evaluation_dialogues.load_evaluation_dialogues(path_5k, filter_state="has_dialogue")
    assert res_5k.matching_state == "complete"
    assert res_5k.total_matching == 1
    assert "sessions_source_unavailable" not in res_5k.reason_codes

    # 5,001 sessions: truncated but rows processed
    path_5001 = tmp_path / "sess_5001.db"
    conn = _init_db(path_5001)
    conn.execute(
        "INSERT INTO tracked_companies VALUES ('bhanu', 'S5K', '5K Corp', 'evaluation', 'equity', NULL)"
    )
    session_data = [
        (f"s-{i:05d}", "portfolio", f"2026-08-20T12:00:{i % 60:02d}Z") for i in range(5001)
    ]
    conn.executemany(
        "INSERT INTO ask_sessions (id, scope, updated_at) VALUES (?, ?, ?)", session_data
    )
    context_data = [(f"s-{i:05d}", c_json, c_sha) for i in range(5001)]
    conn.executemany(
        "INSERT INTO ask_session_contexts (session_id, context_json, context_sha256) VALUES (?, ?, ?)",
        context_data,
    )
    conn.commit()
    conn.close()

    res_5001 = evaluation_dialogues.load_evaluation_dialogues(
        path_5001, filter_state="has_dialogue"
    )
    assert res_5001.matching_state == "indeterminate"
    assert res_5001.total_matching is None
    assert "sessions_source_unavailable" in res_5001.reason_codes
    assert "relevance_partial" in res_5001.reason_codes


def test_notes_truncation_boundary_50000_vs_50001(tmp_path: Path) -> None:
    # 50,000 notes: complete
    path_50k = tmp_path / "notes_50k.db"
    conn = _init_db(path_50k)
    conn.execute(
        "INSERT INTO tracked_companies VALUES ('bhanu', 'N50K', '50K Corp', 'evaluation', 'equity', NULL)"
    )
    notes_data = [("bhanu", "N50K", "open", "2026-08-20T12:00:00Z") for _ in range(50000)]
    conn.executemany(
        "INSERT INTO analyst_notes (user_id, ticker, status, created_at) VALUES (?, ?, ?, ?)",
        notes_data,
    )
    conn.commit()
    conn.close()

    res_50k = evaluation_dialogues.load_evaluation_dialogues(path_50k, filter_state="has_notes")
    assert res_50k.matching_state == "complete"
    assert res_50k.total_matching == 1
    assert "notes_source_unavailable" not in res_50k.reason_codes

    # 50,001 notes: truncated, processes 50,000 notes, marks indeterminate
    path_50001 = tmp_path / "notes_50001.db"
    conn = _init_db(path_50001)
    conn.execute(
        "INSERT INTO tracked_companies VALUES ('bhanu', 'N50K', '50K Corp', 'evaluation', 'equity', NULL)"
    )
    notes_data = [("bhanu", "N50K", "open", "2026-08-20T12:00:00Z") for _ in range(50001)]
    conn.executemany(
        "INSERT INTO analyst_notes (user_id, ticker, status, created_at) VALUES (?, ?, ?, ?)",
        notes_data,
    )
    conn.commit()
    conn.close()

    res_50001 = evaluation_dialogues.load_evaluation_dialogues(path_50001, filter_state="has_notes")
    assert res_50001.matching_state == "indeterminate"
    assert res_50001.total_matching is None
    assert "notes_source_unavailable" in res_50001.reason_codes
    assert "relevance_partial" in res_50001.reason_codes


def test_discovery_failure_sets_link_state_unknown(tmp_path: Path) -> None:
    path = tmp_path / "disc_fail.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
          user_id TEXT, ticker TEXT, name TEXT, list_type TEXT,
          instrument_type TEXT, archived_at TEXT
        );
        CREATE TABLE ask_sessions (
          id TEXT PRIMARY KEY, scope TEXT, updated_at TEXT
        );
        CREATE INDEX ix_ask_sessions_scope_updated ON ask_sessions (scope, updated_at DESC);
        CREATE TABLE ask_session_contexts (
          session_id TEXT PRIMARY KEY, schema_version TEXT, context_json TEXT, context_sha256 TEXT
        );
        INSERT INTO tracked_companies VALUES
          ('bhanu', 'LINKED', 'Linked Corp', 'evaluation', 'equity', NULL),
          ('bhanu', 'UNLINK', 'Unlinked Corp', 'evaluation', 'equity', NULL);
        INSERT INTO ask_sessions VALUES ('s-linked', 'portfolio', '2026-08-20T12:00:00Z');
        """
    )
    c_json, c_sha = _make_context_json("LINKED", None, "stock")
    conn.execute(
        "INSERT INTO ask_session_contexts VALUES ('s-linked', 'session_context.v1', ?, ?)",
        (c_json, c_sha),
    )
    conn.commit()
    conn.close()

    result = evaluation_dialogues.load_evaluation_dialogues(path)
    by_ticker = {item.ticker: item for item in result.items}

    assert by_ticker["LINKED"].ask_session_link_state == "linked"
    assert by_ticker["UNLINK"].ask_session_link_state == "unknown"
    assert "discovery_source_unavailable" in result.reason_codes

    # has_dialogue filter becomes indeterminate
    res_filt = evaluation_dialogues.load_evaluation_dialogues(path, filter_state="has_dialogue")
    assert res_filt.matching_state == "indeterminate"
    assert res_filt.total_matching is None


@dataclass
class _MockNote:
    ticker: str
    created_at: str
    status: str = "open"


def test_malformed_note_timestamp_sets_relevance_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _default_db(tmp_path)
    mock_notes = [
        _MockNote("AAA", "bad-timestamp", "open"),
        _MockNote("AAA", "2026-08-19T00:00:00Z", "open"),
    ]

    def _mock_list_notes(**_: object) -> list[_MockNote]:
        return mock_notes

    monkeypatch.setattr(evaluation_dialogues, "list_notes", _mock_list_notes)

    result = evaluation_dialogues.load_evaluation_dialogues(path)
    by_ticker = {item.ticker: item for item in result.items}

    assert "relevance_partial" in result.reason_codes
    assert by_ticker["AAA"].latest_note_at == "2026-08-19T00:00:00Z"
    assert by_ticker["AAA"].open_note_count == 2


def test_only_malformed_note_timestamp_withholds_latest_note_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When all notes have unparseable timestamps, withhold latest_note_at (None) and set relevance_partial."""
    path = _default_db(tmp_path)
    mock_notes = [
        _MockNote("AAA", "corrupted-date-1", "open"),
        _MockNote("AAA", "not-a-valid-iso", "open"),
    ]

    def _mock_list_notes(**_: object) -> list[_MockNote]:
        return mock_notes

    monkeypatch.setattr(evaluation_dialogues, "list_notes", _mock_list_notes)

    result = evaluation_dialogues.load_evaluation_dialogues(path)
    by_ticker = {item.ticker: item for item in result.items}

    assert "relevance_partial" in result.reason_codes
    # Explicit regression: temporal metadata is withheld (None) rather than leaking raw malformed string
    assert by_ticker["AAA"].latest_note_at is None
    assert by_ticker["AAA"].open_note_count == 2


def test_corrupted_json_with_filter_has_dialogue(tmp_path: Path) -> None:
    path = _default_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE ask_session_contexts SET context_json = '{bad json' WHERE session_id = 'session-z'"
    )
    conn.commit()
    conn.close()

    result = evaluation_dialogues.load_evaluation_dialogues(path, filter_state="has_dialogue")
    assert result.matching_state == "indeterminate"
    assert result.total_matching is None
    assert "relevance_partial" in result.reason_codes


def test_missing_context_rows_handling(tmp_path: Path) -> None:
    path = _default_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM ask_session_contexts WHERE session_id = 'session-z'")
    conn.commit()
    conn.close()

    result = evaluation_dialogues.load_evaluation_dialogues(path)
    assert "relevance_partial" in result.reason_codes
    by_ticker = {item.ticker: item for item in result.items}
    assert by_ticker["ZZZ"].ask_session_link_state == "unknown"


def test_table_exists_handles_sqlite_error() -> None:
    class FailingConnection:
        def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise sqlite3.OperationalError("disk I/O error")

    failing_connection: Any = FailingConnection()
    table_exists = getattr(evaluation_dialogues, "_table_exists")
    assert table_exists(failing_connection, "tracked_companies") is False


def test_normalized_ticker_collision_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "collision.db"
    conn = _init_db(path)
    conn.executescript(
        """
        INSERT INTO tracked_companies VALUES
          ('bhanu', 'AAPL', 'Apple Inc', 'evaluation', 'equity', NULL),
          ('bhanu', 'aapl', 'Apple Inc Lowercase', 'evaluation', 'equity', NULL);
        """
    )
    conn.commit()
    conn.close()

    result = evaluation_dialogues.load_evaluation_dialogues(path)
    assert result.state == "unavailable"
    assert result.items == ()
    assert result.total_active is None
    assert result.total_matching is None
    assert result.matching_state == "indeterminate"
    assert result.reason_codes == ("evaluation_source_unavailable",)


def test_source_failure_evaluation_source_unavailable(tmp_path: Path) -> None:
    result = evaluation_dialogues.load_evaluation_dialogues(tmp_path / "non_existent.db")
    assert result.state == "unavailable"
    assert result.reason_codes == ("evaluation_source_unavailable",)


def test_source_failure_discovery_source_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "no_disc.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
          user_id TEXT, ticker TEXT, name TEXT, list_type TEXT,
          instrument_type TEXT, archived_at TEXT
        );
        INSERT INTO tracked_companies VALUES
          ('bhanu', 'CO1', 'Company 1', 'evaluation', 'equity', NULL);
        """
    )
    conn.commit()
    conn.close()

    result = evaluation_dialogues.load_evaluation_dialogues(path)
    assert "discovery_source_unavailable" in result.reason_codes
    assert "relevance_partial" in result.reason_codes


def test_source_failure_instrument_type_unavailable(tmp_path: Path) -> None:
    path = _default_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO tracked_companies VALUES ('bhanu', 'UNKI', 'Unknown Instr', 'evaluation', 'bad_type', NULL)"
    )
    conn.commit()
    conn.close()

    result = evaluation_dialogues.load_evaluation_dialogues(path, filter_state="ready")
    assert "instrument_type_unavailable" in result.reason_codes
    assert result.matching_state == "indeterminate"
    assert result.total_matching is None


def test_session_timestamp_tie_break(tmp_path: Path) -> None:
    path = _default_db(tmp_path)
    conn = sqlite3.connect(path)
    ts = "2026-08-22T10:00:00Z"
    c_json, c_sha = _make_context_json("ZZZ", 7, "stock")
    conn.execute(
        "INSERT INTO ask_sessions (id, scope, updated_at) VALUES ('session-1', 'portfolio', ?)",
        (ts,),
    )
    conn.execute(
        "INSERT INTO ask_sessions (id, scope, updated_at) VALUES ('session-2', 'portfolio', ?)",
        (ts,),
    )
    conn.execute(
        "INSERT INTO ask_session_contexts VALUES ('session-1', 'session_context.v1', ?, ?, 0, '2026-08-20', '2026-08-20')",
        (c_json, c_sha),
    )
    conn.execute(
        "INSERT INTO ask_session_contexts VALUES ('session-2', 'session_context.v1', ?, ?, 0, '2026-08-20', '2026-08-20')",
        (c_json, c_sha),
    )
    conn.commit()
    conn.close()

    result = evaluation_dialogues.load_evaluation_dialogues(path)
    zzz = next(item for item in result.items if item.ticker == "ZZZ")
    assert zzz.ask_session_id == "session-2"


def test_malformed_only_session_selection(tmp_path: Path) -> None:
    path = tmp_path / "malf_only.db"
    conn = _init_db(path)
    conn.execute(
        "INSERT INTO tracked_companies VALUES ('bhanu', 'MALF', 'Malformed Corp', 'evaluation', 'equity', NULL)"
    )
    c_json, c_sha = _make_context_json("MALF", None, "stock")
    conn.execute(
        "INSERT INTO ask_sessions (id, scope, updated_at) VALUES ('s-bad-1', 'portfolio', 'invalid-date-1')"
    )
    conn.execute(
        "INSERT INTO ask_sessions (id, scope, updated_at) VALUES ('s-bad-2', 'portfolio', 'invalid-date-2')"
    )
    conn.execute(
        "INSERT INTO ask_session_contexts VALUES ('s-bad-1', 'session_context.v1', ?, ?, 0, '2026-08-20', '2026-08-20')",
        (c_json, c_sha),
    )
    conn.execute(
        "INSERT INTO ask_session_contexts VALUES ('s-bad-2', 'session_context.v1', ?, ?, 0, '2026-08-20', '2026-08-20')",
        (c_json, c_sha),
    )
    conn.commit()
    conn.close()

    result = evaluation_dialogues.load_evaluation_dialogues(path)
    malf = result.items[0]
    assert malf.ask_session_id == "s-bad-2"
    assert malf.ask_session_link_state == "linked"
    assert "relevance_partial" in result.reason_codes


def test_valid_never_displaced_by_malformed_session(tmp_path: Path) -> None:
    path = tmp_path / "valid_displace.db"
    conn = _init_db(path)
    conn.execute(
        "INSERT INTO tracked_companies VALUES ('bhanu', 'DISP', 'Displace Corp', 'evaluation', 'equity', NULL)"
    )
    c_json, c_sha = _make_context_json("DISP", None, "stock")
    conn.execute(
        "INSERT INTO ask_sessions (id, scope, updated_at) VALUES ('s-valid', 'portfolio', '2026-08-15T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ask_sessions (id, scope, updated_at) VALUES ('s-bad', 'portfolio', 'invalid-ts')"
    )
    conn.execute(
        "INSERT INTO ask_session_contexts VALUES ('s-valid', 'session_context.v1', ?, ?, 0, '2026-08-20', '2026-08-20')",
        (c_json, c_sha),
    )
    conn.execute(
        "INSERT INTO ask_session_contexts VALUES ('s-bad', 'session_context.v1', ?, ?, 0, '2026-08-20', '2026-08-20')",
        (c_json, c_sha),
    )
    conn.commit()
    conn.close()

    result = evaluation_dialogues.load_evaluation_dialogues(path)
    disp = result.items[0]
    assert disp.ask_session_id == "s-valid"
    assert disp.ask_session_updated_at == "2026-08-15T00:00:00Z"
    assert "relevance_partial" in result.reason_codes


def test_readiness_sorting_rank(tmp_path: Path) -> None:
    path = tmp_path / "readiness.db"
    conn = _init_db(path)
    conn.executescript(
        """
        INSERT INTO tracked_companies VALUES
          ('bhanu', 'STK', 'Stock No Act', 'evaluation', 'equity', NULL),
          ('bhanu', 'ETF', 'ETF No Act', 'evaluation', 'etf', NULL);
        """
    )
    conn.commit()
    conn.close()

    result = evaluation_dialogues.load_evaluation_dialogues(path, sort="relevance")
    tickers = [i.ticker for i in result.items]
    assert tickers == ["ETF", "STK"]


def test_separated_note_semantics(tmp_path: Path) -> None:
    path = _default_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM analyst_notes")
    conn.execute(
        "INSERT INTO analyst_notes (user_id, ticker, created_at, status) VALUES ('bhanu', 'ZZZ', '2026-08-25T00:00:00Z', 'resolved')"
    )
    conn.execute(
        "INSERT INTO analyst_notes (user_id, ticker, created_at, status) VALUES ('bhanu', 'ZZZ', '2026-08-20T00:00:00Z', 'open')"
    )
    conn.commit()
    conn.close()

    result = evaluation_dialogues.load_evaluation_dialogues(path)
    zzz = next(item for item in result.items if item.ticker == "ZZZ")
    assert zzz.open_note_count == 1
    assert zzz.latest_note_at == "2026-08-25T00:00:00Z"


def test_zero_active_short_circuit(tmp_path: Path) -> None:
    path = tmp_path / "zero.db"
    conn = _init_db(path)
    conn.commit()
    conn.close()

    result = evaluation_dialogues.load_evaluation_dialogues(path)
    assert result.state == "available"
    assert result.items == ()
    assert result.total_active == 0
    assert result.total_matching == 0
    assert result.matching_state == "complete"
    assert result.reason_codes == ()


def test_database_immutability_and_write_rejection(tmp_path: Path) -> None:
    path = _default_db(tmp_path)
    initial_sha = hashlib.sha256(path.read_bytes()).hexdigest()

    result = evaluation_dialogues.load_evaluation_dialogues(path)
    assert result.state in {"available", "partial"}

    post_read_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    assert initial_sha == post_read_sha

    # Test chmod 444
    os.chmod(path, 0o444)
    try:
        res_ro = evaluation_dialogues.load_evaluation_dialogues(path)
        assert res_ro.state in {"available", "partial"}
    finally:
        os.chmod(path, 0o644)


def test_strict_limit_and_parameter_validation(tmp_path: Path) -> None:
    path = _default_db(tmp_path)

    assert len(evaluation_dialogues.load_evaluation_dialogues(path, limit=3).items) == 2
    assert len(evaluation_dialogues.load_evaluation_dialogues(path, limit=5).items) == 2
    assert len(evaluation_dialogues.load_evaluation_dialogues(path, limit=10).items) == 2

    bad_limits: tuple[Any, ...] = (4, 99, 0, 3.0, True, False, -1)
    for bad_limit in bad_limits:
        with pytest.raises(ValueError, match="limit must be an integer in"):
            evaluation_dialogues.load_evaluation_dialogues(path, limit=bad_limit)

    bad_sort: Any = "random"
    with pytest.raises(ValueError, match="sort must be one of"):
        evaluation_dialogues.load_evaluation_dialogues(path, sort=bad_sort)

    bad_filter: Any = "random"
    with pytest.raises(ValueError, match="filter_state must be one of"):
        evaluation_dialogues.load_evaluation_dialogues(path, filter_state=bad_filter)


def test_evaluation_dialogue_model_invariants() -> None:
    diag = evaluation_dialogues.EvaluationDialogue(
        state="available",
        items=(),
        total_active=2,
        total_matching=2,
        matching_state="complete",
        reason_codes=(),
    )
    assert diag.total_active == 2

    with pytest.raises(ValidationError):
        invalid_reason_codes: Any = ("invalid_code",)
        evaluation_dialogues.EvaluationDialogue(
            state="available",
            items=(),
            total_active=1,
            total_matching=1,
            matching_state="complete",
            reason_codes=invalid_reason_codes,
        )

    with pytest.raises(
        ValidationError, match="state is 'unavailable' if and only if total_active is None"
    ):
        evaluation_dialogues.EvaluationDialogue(
            state="unavailable",
            items=(),
            total_active=1,
            total_matching=None,
            matching_state="indeterminate",
        )

    with pytest.raises(
        ValidationError,
        match="matching_state is 'indeterminate' if and only if total_matching is None",
    ):
        evaluation_dialogues.EvaluationDialogue(
            state="available",
            items=(),
            total_active=2,
            total_matching=None,
            matching_state="complete",
        )
