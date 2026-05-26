"""Tests for src/llm_call_ledger.py and the migration 0034 schema.

Covers the ledger writer in isolation (no LLM calls) plus the CLI JSON parser.
End-to-end ledger writes from a real LLM call are exercised by the smoke run
in `execution/show_llm_spend.py` and the Phase 0 validation script.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from llm_call_ledger import (
    LlmCallRecord,
    parse_claude_json_output,
    record_call,
    sha256_text,
    usage_from_json_meta,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_llm_calls_table(conn: sqlite3.Connection) -> None:
    """Mirror migration 0034 in-test. Keeps these unit tests independent from
    the alembic run (so failures here flag bugs in the schema, not the
    migration runner)."""
    conn.executescript(
        """
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
    )
    conn.commit()


def _make_record(**overrides: object) -> LlmCallRecord:
    base = LlmCallRecord(
        called_at=datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC),
        model="claude-sonnet-4-6",
        prompt_sha256="0" * 64,
        prompt_chars=100,
        elapsed_ms=1234,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# ---------------------------------------------------------------------------
# sha256_text
# ---------------------------------------------------------------------------


def test_sha256_text_deterministic() -> None:
    assert sha256_text("hello") == sha256_text("hello")
    assert sha256_text("hello") != sha256_text("HELLO")


def test_sha256_text_handles_unicode() -> None:
    # Common in financial docs: U+2212 minus, U+2014 em-dash, etc.
    assert len(sha256_text("Q1 − 12.3%")) == 64


# ---------------------------------------------------------------------------
# parse_claude_json_output
# ---------------------------------------------------------------------------


def test_parse_claude_json_output_happy_path() -> None:
    raw = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "the response text",
            "total_cost_usd": 0.05,
            "usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 2000,
                "cache_read_input_tokens": 5000,
                "output_tokens": 200,
            },
        }
    )
    text, meta = parse_claude_json_output(raw)
    assert text == "the response text"
    assert meta["total_cost_usd"] == 0.05
    assert meta["usage"]["input_tokens"] == 100


def test_parse_claude_json_output_raises_on_invalid_json() -> None:
    with pytest.raises(ValueError, match="did not return JSON"):
        parse_claude_json_output("not json at all")


def test_parse_claude_json_output_raises_on_non_object() -> None:
    with pytest.raises(ValueError, match="non-object JSON"):
        parse_claude_json_output("[1, 2, 3]")


def test_parse_claude_json_output_raises_on_is_error_true() -> None:
    raw = json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "api_error_status": 529,
            "result": "Overloaded",
        }
    )
    with pytest.raises(ValueError, match="reported error"):
        parse_claude_json_output(raw)


def test_parse_claude_json_output_raises_on_missing_result() -> None:
    raw = json.dumps({"type": "result", "is_error": False, "usage": {}})
    with pytest.raises(ValueError, match="missing string `result`"):
        parse_claude_json_output(raw)


# ---------------------------------------------------------------------------
# usage_from_json_meta
# ---------------------------------------------------------------------------


def test_usage_from_json_meta_extracts_all_fields() -> None:
    meta = {
        "total_cost_usd": 0.07,
        "usage": {
            "input_tokens": 9,
            "cache_creation_input_tokens": 60000,
            "cache_read_input_tokens": 1234,
            "output_tokens": 67,
        },
    }
    usage = usage_from_json_meta(meta)
    assert usage["input_tokens"] == 9
    assert usage["cache_creation_input_tokens"] == 60000
    assert usage["cache_read_input_tokens"] == 1234
    assert usage["output_tokens"] == 67
    assert usage["cost_estimate_usd"] == 0.07


def test_usage_from_json_meta_handles_missing_usage_block() -> None:
    usage = usage_from_json_meta({"total_cost_usd": 0.05})
    assert usage["input_tokens"] is None
    assert usage["output_tokens"] is None
    assert usage["cost_estimate_usd"] == 0.05


def test_usage_from_json_meta_handles_no_cost() -> None:
    usage = usage_from_json_meta({"usage": {"input_tokens": 10}})
    assert usage["input_tokens"] == 10
    assert usage["cost_estimate_usd"] is None


def test_usage_from_json_meta_rejects_bool_input_tokens() -> None:
    # bool is a subclass of int in Python — defensive code must reject it
    # because `True` would otherwise coerce to `1`.
    usage = usage_from_json_meta({"usage": {"input_tokens": True}})
    assert usage["input_tokens"] is None


# ---------------------------------------------------------------------------
# record_call — happy path + resilience
# ---------------------------------------------------------------------------


def test_record_call_writes_row(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    try:
        _create_llm_calls_table(conn)
    finally:
        conn.close()

    record = _make_record(
        purpose="bear_case",
        ticker="GOOG",
        scope="ticker:GOOG",
        response_sha256="a" * 64,
        response_chars=2000,
        input_tokens=500,
        cache_creation_input_tokens=10000,
        cache_read_input_tokens=20000,
        output_tokens=1500,
        cost_estimate_usd=0.12,
        run_id="run-abc",
    )
    row_id = record_call(record, db_path=db)
    assert row_id is not None

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM llm_calls WHERE id = ?", (row_id,)).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["purpose"] == "bear_case"
    assert row["ticker"] == "GOOG"
    assert row["model"] == "claude-sonnet-4-6"
    assert row["prompt_chars"] == 100
    assert row["response_chars"] == 2000
    assert row["input_tokens"] == 500
    assert row["cache_creation_input_tokens"] == 10000
    assert row["cache_read_input_tokens"] == 20000
    assert row["output_tokens"] == 1500
    assert row["cost_estimate_usd"] == pytest.approx(0.12)
    assert row["elapsed_ms"] == 1234
    assert row["cache_hit"] == 0
    assert row["fallback_used"] is None
    assert row["error"] is None
    assert row["run_id"] == "run-abc"


def test_record_call_handles_null_optional_fields(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    try:
        _create_llm_calls_table(conn)
    finally:
        conn.close()

    # Minimal record — only required fields set.
    record = _make_record()
    row_id = record_call(record, db_path=db)
    assert row_id is not None

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM llm_calls WHERE id = ?", (row_id,)).fetchone()
    finally:
        conn.close()

    assert row["purpose"] is None
    assert row["ticker"] is None
    assert row["response_chars"] is None
    assert row["cost_estimate_usd"] is None


def test_record_call_writes_error_row(tmp_path: Path) -> None:
    """A CLI failure produces a ledger row with error=<msg> and NULL response."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    try:
        _create_llm_calls_table(conn)
    finally:
        conn.close()

    record = _make_record(
        purpose="bear_case",
        ticker="GOOG",
        error="TimeoutExpired: claude -p exceeded 1200s",
    )
    record_call(record, db_path=db)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM llm_calls").fetchone()
    finally:
        conn.close()

    assert row["error"] == "TimeoutExpired: claude -p exceeded 1200s"
    assert row["response_chars"] is None


def test_record_call_writes_fallback_row(tmp_path: Path) -> None:
    """A Gemini-fallback success records fallback_used='gemini'."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    try:
        _create_llm_calls_table(conn)
    finally:
        conn.close()

    record = _make_record(
        purpose="bear_case",
        model="gemini-2.5-flash",
        fallback_used="gemini",
        response_chars=500,
    )
    record_call(record, db_path=db)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM llm_calls").fetchone()
    finally:
        conn.close()

    assert row["fallback_used"] == "gemini"
    assert row["model"] == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# Resilience: missing DB / missing table must NOT raise
# ---------------------------------------------------------------------------


def test_record_call_returns_none_when_db_missing(tmp_path: Path) -> None:
    """Best-effort: a missing DB file does NOT raise — the LLM call must
    succeed even when telemetry can't be written."""
    missing = tmp_path / "no_such.db"
    assert not missing.exists()
    result = record_call(_make_record(), db_path=missing)
    assert result is None


def test_record_call_returns_none_when_table_missing(tmp_path: Path) -> None:
    """A DB that exists but has no llm_calls table → graceful None return.
    This is the 'pre-migration' state on a fresh repo or after a downgrade."""
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()  # creates an empty DB
    assert db.exists()
    result = record_call(_make_record(), db_path=db)
    assert result is None


def test_record_call_returns_none_when_db_corrupted(tmp_path: Path) -> None:
    """A non-SQLite file at the path → graceful None return."""
    db = tmp_path / "corrupt.db"
    db.write_text("this is not a sqlite database")
    result = record_call(_make_record(), db_path=db)
    assert result is None
