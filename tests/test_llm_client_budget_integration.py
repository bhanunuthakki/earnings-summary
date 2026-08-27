"""Integration tests: src/llm_client _call_claude × src/llm_budget hook.

Mocks the claude CLI subprocess + budget DB so we can exercise the
pre-call gate without hitting the real LLM. Verifies:

  * Hard-block: raises LLMBudgetExceeded BEFORE the subprocess fires.
  * Soft cap: logs a warning + proceeds (subprocess still called).
  * Warn threshold: logs warning + records alert + proceeds.
  * force_budget_bypass=True: skips the check even when over a hard cap.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

import llm_budget
import llm_call_ledger
import llm_client
from llm_client import LLMBudgetExceeded, call_llm

# ---------------------------------------------------------------------------
# Helpers — DB seed + subprocess stub
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

_CREATE_LLM_BUDGETS = """
CREATE TABLE llm_budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose VARCHAR(64) NOT NULL,
    monthly_cap_usd NUMERIC(10,2) NOT NULL,
    warn_threshold_pct FLOAT NOT NULL DEFAULT 0.80,
    hard_block BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    notes TEXT,
    CONSTRAINT uq_llm_budgets_purpose UNIQUE (purpose)
);
"""

_CREATE_LLM_BUDGET_ALERTS = """
CREATE TABLE llm_budget_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose VARCHAR(64) NOT NULL,
    month VARCHAR(7) NOT NULL,
    threshold_pct FLOAT NOT NULL,
    alerted_at DATETIME NOT NULL,
    spend_at_alert_usd NUMERIC(10,4) NOT NULL,
    CONSTRAINT uq_alerts UNIQUE (purpose, month, threshold_pct)
);
"""


def _seed_db(
    path: Path,
    *,
    purpose: str,
    cap_usd: float,
    spent_usd: float,
    hard_block: bool,
) -> None:
    """Initialize a fresh DB with one budget row + one historical call so
    that current_month_spend(purpose) returns `spent_usd`."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_CREATE_LLM_CALLS + _CREATE_LLM_BUDGETS + _CREATE_LLM_BUDGET_ALERTS)
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 created_at, updated_at)
            VALUES (?, ?, 0.80, ?, ?, ?)
            """,
            (purpose, cap_usd, 1 if hard_block else 0, now, now),
        )
        if spent_usd > 0:
            conn.execute(
                """
                INSERT INTO llm_calls
                    (called_at, purpose, model, prompt_sha256, prompt_chars,
                     elapsed_ms, cost_estimate_usd)
                VALUES (?, ?, 'claude-sonnet-4-6', 'x', 100, 1000, ?)
                """,
                (now, purpose, spent_usd),
            )
        conn.commit()
    finally:
        conn.close()


def _good_cli_response(text: str = "ok") -> str:
    """Mimic `claude -p --output-format json` envelope."""
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
def stub_claude_cli(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Bypass `_verify_setup_once` (which calls shutil.which) and replace
    subprocess.run with a stub that records every invocation. The dict
    yielded back to the test exposes `calls` (a counter)."""
    monkeypatch.setattr(llm_client, "_setup_verified", True)
    monkeypatch.setattr(llm_client, "_claude_cli_path", r"C:\fake\claude.cmd")

    state: dict[str, Any] = {"calls": 0, "last_input": None}

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        state["calls"] += 1
        state["last_input"] = kwargs.get("input")
        cmd_args = cast("list[str]", args[0]) if args else []
        return subprocess.CompletedProcess(
            args=cmd_args,
            returncode=0,
            stdout=_good_cli_response("stubbed-response"),
            stderr="",
        )

    monkeypatch.setattr(llm_client.subprocess, "run", _fake_run)
    yield state


@pytest.fixture
def db_patch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point `llm_budget.resolve_db_path` (and the ledger writer's
    resolver) at a tmp-path DB. Tests configure the row contents via
    `_seed_db`. Returns the path so the test can re-read or alter it.

    Both modules import the shared resolver via
    ``from db_paths import resolve_db_path``, so the name is bound as a
    module-level attribute on each — patch it in each module's namespace."""
    db = tmp_path / "test.db"

    def _fake_resolve(override: Path | str | None) -> Path | None:
        if override is not None:
            return Path(override)
        return db

    monkeypatch.setattr(llm_budget, "resolve_db_path", _fake_resolve)
    # Ledger writer also resolves DB path — point it at the same DB so the
    # post-call telemetry write doesn't fail audibly. (record_call is
    # best-effort and would log+swallow anyway, but cleaner this way.)
    monkeypatch.setattr(llm_call_ledger, "resolve_db_path", _fake_resolve)
    return db


# ---------------------------------------------------------------------------
# Hard block — raises LLMBudgetExceeded before subprocess fires
# ---------------------------------------------------------------------------


def test_hard_block_raises_before_subprocess(
    stub_claude_cli: dict[str, Any], db_patch: Path
) -> None:
    _seed_db(
        db_patch,
        purpose="bear_case",
        cap_usd=50.00,
        spent_usd=60.00,  # over cap
        hard_block=True,
    )
    with pytest.raises(LLMBudgetExceeded) as exc_info:
        call_llm("hello", purpose="bear_case")
    # Subprocess MUST NOT have been called when hard-blocked.
    assert stub_claude_cli["calls"] == 0
    # Exception carries the BudgetCheck for structured callers.
    assert exc_info.value.check is not None


def test_force_budget_bypass_overrides_hard_block(
    stub_claude_cli: dict[str, Any], db_patch: Path
) -> None:
    """The --force escape hatch: hard cap is bypassed, subprocess runs."""
    _seed_db(
        db_patch,
        purpose="bear_case",
        cap_usd=50.00,
        spent_usd=60.00,
        hard_block=True,
    )
    result = call_llm("hello", purpose="bear_case", force_budget_bypass=True)
    assert result == "stubbed-response"
    assert stub_claude_cli["calls"] == 1


# ---------------------------------------------------------------------------
# Soft cap — log warning + proceed
# ---------------------------------------------------------------------------


def test_soft_cap_logs_and_proceeds(
    stub_claude_cli: dict[str, Any],
    db_patch: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_db(
        db_patch,
        purpose="bear_case",
        cap_usd=50.00,
        spent_usd=60.00,
        hard_block=False,  # soft cap
    )
    with caplog.at_level(logging.WARNING, logger="llm_client"):
        result = call_llm("hello", purpose="bear_case")
    assert result == "stubbed-response"
    assert stub_claude_cli["calls"] == 1
    # Warning must mention soft cap exceeded (record vs string contents
    # in caplog requires the message to have rendered).
    matching = [
        r
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("event") == "llm_budget_soft_cap_exceeded"
    ]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# Warn threshold — at 80% logs warning + records alert
# ---------------------------------------------------------------------------


def test_warn_threshold_records_alert(
    stub_claude_cli: dict[str, Any],
    db_patch: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_db(
        db_patch,
        purpose="bear_case",
        cap_usd=50.00,
        spent_usd=42.00,  # 84% of cap → past 80% warn threshold
        hard_block=True,
    )
    with caplog.at_level(logging.WARNING, logger="llm_client"):
        result = call_llm("hello", purpose="bear_case")
    assert result == "stubbed-response"
    assert stub_claude_cli["calls"] == 1

    # Warn log fired
    warn_records = [
        r
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("event") == "llm_budget_warn_threshold"
    ]
    assert len(warn_records) == 1

    # Alert row written
    conn = sqlite3.connect(str(db_patch))
    try:
        rows = conn.execute("SELECT * FROM llm_budget_alerts").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1


def test_warn_threshold_alert_dedupes_across_calls(
    stub_claude_cli: dict[str, Any], db_patch: Path
) -> None:
    """Two LLM calls past the 80% threshold in the same month → exactly
    one alert row written. The dedupe is the whole point of the alert table."""
    _seed_db(
        db_patch,
        purpose="bear_case",
        cap_usd=50.00,
        spent_usd=42.00,
        hard_block=True,
    )
    call_llm("first", purpose="bear_case")
    call_llm("second", purpose="bear_case")
    call_llm("third", purpose="bear_case")
    conn = sqlite3.connect(str(db_patch))
    try:
        rows = conn.execute("SELECT * FROM llm_budget_alerts WHERE threshold_pct = 0.8").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1


def test_purpose_none_fails_before_budget_and_transport(
    stub_claude_cli: dict[str, Any], db_patch: Path
) -> None:
    """Unclassified calls cannot bypass the governed purpose registry."""
    _seed_db(
        db_patch,
        purpose="__default__",
        cap_usd=50.00,
        spent_usd=999.00,
        hard_block=True,
    )
    from llm.resolver import InvalidLLMPurposeError

    with pytest.raises(InvalidLLMPurposeError):
        call_llm("hello", purpose=None)
    assert stub_claude_cli["calls"] == 0


def test_unexpected_budget_error_fails_closed_and_redacts(
    stub_claude_cli: dict[str, Any],
    db_patch: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_db(
        db_patch,
        purpose="bear_case",
        cap_usd=50.00,
        spent_usd=0,
        hard_block=True,
    )

    def broken_check(_purpose: str) -> object:
        raise RuntimeError("credential=top-secret")

    monkeypatch.setattr(llm_budget, "check_budget", broken_check)
    with (
        caplog.at_level(logging.ERROR, logger="llm_client"),
        pytest.raises(LLMBudgetExceeded, match="budget enforcement unavailable"),
    ):
        call_llm("hello", purpose="bear_case")

    assert stub_claude_cli["calls"] == 0
    assert "RuntimeError" in caplog.text
    assert "top-secret" not in caplog.text
