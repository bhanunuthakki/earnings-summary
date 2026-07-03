"""An explicit ``db_path`` on a governed LLM call must reach the ``llm_calls``
cost ledger WITHOUT a ``db.set_db_path`` bootstrap.

Regression for the /review productionize smoke test: ``review_position('RBRK',
db_path=<prod>)`` ran the LLM fine and persisted the memo, but the cost row
died with "no such table: llm_calls" — ``record_llm_call`` takes no db_path
and resolved ``db.DB_PATH`` (the invocation context's default, a different
sqlite file lacking the table), so the row silently landed nowhere.

The fix under test: ``db_paths.db_path_context`` (a scoped ContextVar override
consulted by ``resolve_db_path`` between the explicit param and the global),
threaded from ``call_llm(db_path=...)`` / ``call_llm_structured(db_path=...)``.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

import db
import llm_client
from db_paths import db_path_context, resolve_db_path
from llm.ledger import record_llm_call
from llm.structured import call_llm_structured

# ---------------------------------------------------------------------------
# Helpers — schema (mirror migration 0034), fixtures, stub CLI
# ---------------------------------------------------------------------------


_CREATE_LLM_CALLS = """
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at DATETIME NOT NULL,
    purpose VARCHAR(64),
    ticker VARCHAR(16),
    scope VARCHAR(64),
    model VARCHAR(64) NOT NULL,
    prompt_sha256 VARCHAR(64) NOT NULL,
    response_sha256 VARCHAR(64),
    prompt_chars INTEGER NOT NULL,
    response_chars INTEGER,
    input_tokens INTEGER,
    cache_creation_input_tokens INTEGER,
    cache_read_input_tokens INTEGER,
    output_tokens INTEGER,
    elapsed_ms INTEGER NOT NULL,
    cost_estimate_usd FLOAT,
    cache_hit BOOLEAN NOT NULL DEFAULT 0,
    fallback_used VARCHAR(16),
    artifact_id INTEGER,
    error TEXT,
    run_id VARCHAR(64)
);
"""


@pytest.fixture
def explicit_db(tmp_path: Path) -> Path:
    """The caller's DB — migrated, i.e. it HAS the llm_calls table."""
    path = tmp_path / "explicit" / "portfolio.db"
    path.parent.mkdir()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_CREATE_LLM_CALLS)
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def decoy_global_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Reproduce the smoke-test invocation context: ``db.DB_PATH`` points at a
    sqlite file that EXISTS but was never migrated (no llm_calls table) — the
    exact prod symptom's wrong-DB target."""
    path = tmp_path / "global" / "portfolio.db"
    path.parent.mkdir()
    sqlite3.connect(str(path)).close()
    monkeypatch.setattr(db, "DB_PATH", str(path))
    return path


def _ledger_rows(path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM llm_calls").fetchall()
    finally:
        conn.close()


def _has_llm_calls_table(path: Path) -> bool:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'llm_calls'"
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _record_minimal(purpose: str = "position_review", ticker: str = "RBRK") -> None:
    """One record_llm_call invocation with the transport's usual arguments —
    note: no db_path parameter exists on this function; it resolves internally."""
    record_llm_call(
        started_at=datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC),
        elapsed_ms=1234,
        model="claude-sonnet-4-6",
        prompt_sha="0" * 64,
        prompt_chars=100,
        purpose=purpose,
        ticker=ticker,
        scope=None,
        run_id=None,
    )


def _good_cli_response(text: str) -> str:
    """Mimic the ``claude -p --output-format json`` envelope."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "total_cost_usd": 0.001,
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 5,
            },
        }
    )


@pytest.fixture
def stub_claude_cli(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Bypass ``_verify_setup_once`` and replace subprocess.run with a stub
    whose `result` payload is valid JSON (call_llm_structured parses it)."""
    monkeypatch.setattr(llm_client, "_setup_verified", True)
    monkeypatch.setattr(llm_client, "_claude_cli_path", r"C:\fake\claude.cmd")
    monkeypatch.delenv("LLM_CAPTURE_DIR", raising=False)

    state: dict[str, Any] = {"calls": 0}

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        state["calls"] += 1
        cmd_args = cast("list[str]", args[0]) if args else []
        return subprocess.CompletedProcess(
            args=cmd_args,
            returncode=0,
            stdout=_good_cli_response(json.dumps({"verdict": "hold"})),
            stderr="",
        )

    monkeypatch.setattr(llm_client.subprocess, "run", _fake_run)
    return state


# ---------------------------------------------------------------------------
# db_path_context — resolution precedence + scoping semantics
# ---------------------------------------------------------------------------


def test_db_path_context_scopes_resolution(explicit_db: Path, decoy_global_db: Path) -> None:
    assert resolve_db_path(None) == Path(decoy_global_db)
    with db_path_context(explicit_db):
        assert resolve_db_path(None) == explicit_db
        # An explicit override param still beats the ambient context.
        assert resolve_db_path(decoy_global_db) == Path(decoy_global_db)
        with db_path_context(decoy_global_db):  # innermost context wins
            assert resolve_db_path(None) == Path(decoy_global_db)
        assert resolve_db_path(None) == explicit_db  # inner exit restores outer
    assert resolve_db_path(None) == Path(decoy_global_db)  # nothing leaks


def test_db_path_context_none_is_noop(decoy_global_db: Path) -> None:
    with db_path_context(None):
        assert resolve_db_path(None) == Path(decoy_global_db)


def test_db_path_context_resets_on_exception(explicit_db: Path, decoy_global_db: Path) -> None:
    with pytest.raises(RuntimeError, match="boom"), db_path_context(explicit_db):
        raise RuntimeError("boom")
    assert resolve_db_path(None) == Path(decoy_global_db)


# ---------------------------------------------------------------------------
# record_llm_call — the ledger writer picks up the ambient context
# ---------------------------------------------------------------------------


def test_record_llm_call_without_context_loses_the_row(
    explicit_db: Path, decoy_global_db: Path
) -> None:
    """The ORIGINAL bug, reproduced: no context and no bootstrap → the writer
    targets db.DB_PATH (table-less), swallows the OperationalError, and the
    row lands NOWHERE. This is the baseline the fix is measured against."""
    _record_minimal()
    assert _ledger_rows(explicit_db) == []
    assert not _has_llm_calls_table(decoy_global_db)


def test_record_llm_call_inside_context_writes_to_explicit_db(
    explicit_db: Path, decoy_global_db: Path
) -> None:
    with db_path_context(explicit_db):
        _record_minimal()
    rows = _ledger_rows(explicit_db)
    assert len(rows) == 1
    assert rows[0]["purpose"] == "position_review"
    assert rows[0]["ticker"] == "RBRK"
    assert not _has_llm_calls_table(decoy_global_db)  # decoy untouched


# ---------------------------------------------------------------------------
# call_llm_structured(db_path=...) — full governed path, stubbed transport
# ---------------------------------------------------------------------------


def test_call_llm_structured_explicit_db_path_logs_cost_row(
    stub_claude_cli: dict[str, Any], explicit_db: Path, decoy_global_db: Path
) -> None:
    """A governed call with an explicit db_path different from db.DB_PATH must
    land its cost/latency row in THAT db's llm_calls — the smoke-test gap."""
    payload = call_llm_structured(
        "Should I trim RBRK?",
        purpose="position_review",
        ticker="RBRK",
        required_keys=("verdict",),
        db_path=explicit_db,
    )
    assert payload == {"verdict": "hold"}
    assert stub_claude_cli["calls"] == 1

    rows = _ledger_rows(explicit_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["purpose"] == "position_review"
    assert row["ticker"] == "RBRK"
    assert row["error"] is None
    assert row["cost_estimate_usd"] == pytest.approx(0.001)
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 5

    # The decoy global stayed untouched AND the scope didn't leak: after the
    # call the process still resolves to db.DB_PATH.
    assert not _has_llm_calls_table(decoy_global_db)
    assert resolve_db_path(None) == Path(decoy_global_db)


def test_call_llm_structured_without_db_path_keeps_global_resolution(
    stub_claude_cli: dict[str, Any], explicit_db: Path, decoy_global_db: Path
) -> None:
    """No db_path → unchanged behavior: the writer resolves db.DB_PATH. Here
    that DB is table-less, so the row is (best-effort) lost — the pre-fix
    symptom, proving db_path= is what routes the row in the test above."""
    payload = call_llm_structured(
        "Should I trim RBRK?",
        purpose="position_review",
        ticker="RBRK",
        required_keys=("verdict",),
    )
    assert payload == {"verdict": "hold"}
    assert _ledger_rows(explicit_db) == []
    assert not _has_llm_calls_table(decoy_global_db)
