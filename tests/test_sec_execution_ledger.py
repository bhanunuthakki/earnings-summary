"""Execution lifecycle cannot overwrite terminal truth or fabricate completion."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from provenance.sec_execution import (
    SecExecutionResult,
    SecExecutionScope,
    begin_sec_execution,
    finish_sec_execution,
    read_sec_executions,
)

NOW = datetime(2026, 9, 19, tzinfo=UTC)


def test_terminal_replay_preserves_time_and_retry_keeps_prior_attempt(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "execution.db")
    with sqlite3.connect(db) as conn:
        scope = SecExecutionScope(kind="inventory_sync", tickers=("ACME",))
        first = begin_sec_execution(conn, request_key="synthetic-request", scope=scope, now=NOW)
        assert first.state == "running"
        assert first.result is None
        outcome = SecExecutionResult(state="failed", reason_code="inventory_failed", failed=1)
        completed = finish_sec_execution(conn, first, outcome, now=NOW + timedelta(seconds=1))
        replay = finish_sec_execution(conn, first, outcome, now=NOW + timedelta(days=1))
        assert replay == completed
        with pytest.raises(ValueError, match="changed its result"):
            finish_sec_execution(
                conn,
                first,
                SecExecutionResult(state="succeeded", reason_code="inventory_complete"),
                now=NOW + timedelta(days=1),
            )
        second = begin_sec_execution(
            conn, request_key="synthetic-request", scope=scope, now=NOW + timedelta(seconds=2)
        )
        assert first.request_id == second.request_id and first.attempt_id != second.attempt_id
        assert read_sec_executions(conn, ticker="ACME") == (second,)
        assert (
            conn.execute("SELECT COUNT(*) FROM sec_execution_receipts WHERE sequence=2").fetchone()[
                0
            ]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM sec_execution_receipts").fetchone()[0] == 5
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE sec_execution_receipts SET state='succeeded'")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM sec_execution_receipts")


def test_transaction_owner_is_preserved_and_incomplete_result_rejected(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "owner.db")
    with sqlite3.connect(db) as conn:
        conn.execute("BEGIN")
        with pytest.raises(RuntimeError, match="active caller transaction"):
            begin_sec_execution(
                conn,
                request_key="test",
                scope=SecExecutionScope(kind="inventory_sync", tickers=("ACME",)),
                now=NOW,
            )
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM sec_execution_receipts").fetchone()[0] == 0
    with pytest.raises(ValueError, match="unresolved work"):
        SecExecutionResult(state="succeeded", reason_code="inventory_complete", deferred=1)
