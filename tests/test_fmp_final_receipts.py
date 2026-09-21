"""Terminal run receipts are durable, idempotent and distinct from attempts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import NoReturn

import pytest

from execution.refresh_cache import QueueItem, run_recovery_batch
from pipeline.fmp_operations_view import read_fmp_operational_details
from pipeline.fmp_recovery import CredentialAvailability, finalize_refresh_receipt


def test_runtime_persists_final_receipt_and_replay_is_immutable(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "receipt.db")
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        item = QueueItem(
            "ACME",
            "portfolio",
            "income-statement",
            "quarter",
            "income_statement_quarterly",
            "statement",
            "missing",
            None,
            None,
            99,
            0,
        )

        def no_network(*_args: object) -> NoReturn:
            raise AssertionError("offline test attempted dispatch")

        result = run_recovery_batch(
            conn,
            items=(item,),
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=tmp_path / "missing-corpus",
            now=datetime(2026, 9, 19),
            run_id="synthetic-final",
            project_root=tmp_path,
            dispatch=no_network,
        )
        row = conn.execute(
            "SELECT * FROM fmp_refresh_receipts WHERE run_id=?", (result.run_id,)
        ).fetchone()
        assert row is not None
        assert result.status.value == "FAILED"
        work_ids = tuple(str(r[0]) for r in conn.execute("SELECT work_id FROM fmp_work_backlog"))
        first = finalize_refresh_receipt(
            conn, run_id=result.run_id, expected_work_ids=work_ids, now=datetime(2026, 9, 20)
        )
        current = read_fmp_operational_details(
            conn, as_of=datetime(2026, 9, 19), receipt_max_age=timedelta(hours=24)
        )
        assert current.receipt_state == "disabled"
        assert current.latest_receipt == first
        assert current.backlog_record_count == 1
        assert current.backlog_record_ids == work_ids
        assert [
            (bucket.role, bucket.priority, bucket.count) for bucket in current.role_priority_counts
        ] == [("portfolio", 300, 1)]
        assert current.opened_at is not None and current.last_transition_at is not None
        assert (
            read_fmp_operational_details(
                conn, as_of=datetime(2026, 9, 21), receipt_max_age=timedelta(hours=24)
            ).receipt_state
            == "stale"
        )
        assert first.failed_count == 0 and first.unattempted_count == 1
        assert first.attempt_ids == ()
        assert first.recorded_at == datetime(2026, 9, 19)
        assert conn.execute("SELECT COUNT(*) FROM fmp_refresh_receipts").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM fmp_refresh_receipts")
        conn.rollback()
        with pytest.raises(ValueError, match="changed plan"):
            finalize_refresh_receipt(
                conn, run_id=result.run_id, expected_work_ids=(), now=datetime(2026, 9, 20)
            )
