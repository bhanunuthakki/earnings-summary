"""Tests for pipeline lifecycle, SafeRowPruner, and scheduler wrapper verification."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from execution.verify_scheduler_wrappers import verify_scheduler_fleet
from pipeline.lifecycle import (
    RETENTION_MATRIX,
    RetentionClass,
    SafeRowPruner,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CRON_DIR = REPO_ROOT / "cron"
MANIFEST_PATH = CRON_DIR / "task_manifest.json"


@pytest.fixture
def mock_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE test_items (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL
        )
        """
    )
    for i in range(10):
        conn.execute(
            "INSERT INTO test_items (ticker, created_at, status) VALUES (?, ?, ?)",
            (f"TKR{i}", "2026-01-01T00:00:00", "stale" if i < 5 else "active"),
        )
    conn.commit()
    return conn


def test_retention_matrix_invariants() -> None:
    assert RetentionClass.EPHEMERAL_STAGING in RETENTION_MATRIX
    staging = RETENTION_MATRIX[RetentionClass.EPHEMERAL_STAGING]
    assert staging.default_ttl_hours == 18
    assert not staging.is_immutable

    ir_corpus = RETENTION_MATRIX[RetentionClass.ADMITTED_IR_CORPUS]
    assert ir_corpus.is_immutable
    assert ir_corpus.default_ttl_hours is None


def test_safe_row_pruner_dry_run_and_execution(mock_db: sqlite3.Connection) -> None:
    pruner = SafeRowPruner(mock_db, run_id="test_prune", max_rows_threshold=100)

    # 1. Dry run
    receipt = pruner.prune_rows(
        table_name="test_items",
        where_clause="status = ?",
        params=("stale",),
        dry_run=True,
    )
    assert receipt.dry_run is True
    assert receipt.rows_matched == 5
    assert receipt.rows_deleted == 0
    assert receipt.restorable_snapshot_json is not None

    # Assert no rows actually deleted
    cursor = mock_db.cursor()
    cursor.execute("SELECT COUNT(*) FROM test_items")
    assert cursor.fetchone()[0] == 10

    # 2. Execution run
    receipt_exec = pruner.prune_rows(
        table_name="test_items",
        where_clause="status = ?",
        params=("stale",),
        dry_run=False,
    )
    assert receipt_exec.dry_run is False
    assert receipt_exec.rows_matched == 5
    assert receipt_exec.rows_deleted == 5

    cursor.execute("SELECT COUNT(*) FROM test_items")
    assert cursor.fetchone()[0] == 5

    # 3. Restore from receipt
    restored = pruner.restore_from_receipt(receipt_exec)
    assert restored == 5

    cursor.execute("SELECT COUNT(*) FROM test_items")
    assert cursor.fetchone()[0] == 10


def test_safe_row_pruner_tripwire_threshold(mock_db: sqlite3.Connection) -> None:
    # Set threshold lower than matched rows
    pruner = SafeRowPruner(mock_db, max_rows_threshold=3)

    with pytest.raises(ValueError, match="Pruning tripwire exceeded"):
        pruner.prune_rows(
            table_name="test_items",
            where_clause="status = ?",
            params=("stale",),
            dry_run=True,
        )


def test_scheduler_fleet_verification() -> None:
    if not MANIFEST_PATH.exists() or not CRON_DIR.exists():
        pytest.skip("Manifest or cron directory missing")

    passed, errors, task_count = verify_scheduler_fleet(CRON_DIR, MANIFEST_PATH)
    assert passed, f"Scheduler fleet verification failed: {errors}"
    assert task_count > 0
