from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from provenance.live_cutover_merge import (
    LiveCutoverMergeError,
    apply_live_cutover_merge,
    plan_live_cutover_merge,
)


def _database(
    path: Path,
    *,
    operational_rows: tuple[tuple[int, str, str], ...],
    evidence_rows: tuple[tuple[int, str], ...],
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE alembic_version (
                version_num TEXT PRIMARY KEY
            );
            INSERT INTO alembic_version VALUES ('0258_fact_anchor_run_lookup_index');
            CREATE TABLE operational_events (
                event_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE evidence_nodes (
                evidence_node_id INTEGER PRIMARY KEY,
                body TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO operational_events VALUES (?, ?, ?)",
            operational_rows,
        )
        connection.executemany(
            "INSERT INTO evidence_nodes VALUES (?, ?)",
            evidence_rows,
        )
        connection.commit()
    finally:
        connection.close()


def test_plan_is_content_bound_and_excludes_governed_substrate(tmp_path: Path) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
    _database(
        live,
        operational_rows=((1, "new", "2026-07-30T10:00:00Z"), (2, "added", "2026")),
        evidence_rows=((99, "must-not-import"),),
    )
    _database(
        governed,
        operational_rows=((1, "old", "2026-07-29T10:00:00Z"),),
        evidence_rows=((1, "sealed"),),
    )

    plan = plan_live_cutover_merge(live, governed)

    assert [table.table for table in plan.tables] == ["operational_events"]
    assert plan.tables[0].added_row_count == 1
    assert plan.tables[0].changed_row_count == 1
    assert len(plan.plan_sha256) == 64


def test_apply_preserves_live_operations_and_governed_evidence(tmp_path: Path) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
    destination = tmp_path / "candidate.db"
    _database(
        live,
        operational_rows=((1, "new", "2026-07-30T10:00:00Z"), (2, "added", "2026")),
        evidence_rows=((99, "must-not-import"),),
    )
    _database(
        governed,
        operational_rows=((1, "old", "2026-07-29T10:00:00Z"),),
        evidence_rows=((1, "sealed"),),
    )
    plan = plan_live_cutover_merge(live, governed)

    receipt = apply_live_cutover_merge(
        live,
        governed,
        destination,
        expected_plan_sha256=plan.plan_sha256,
    )

    connection = sqlite3.connect(destination)
    try:
        assert connection.execute(
            "SELECT event_id, state FROM operational_events ORDER BY event_id"
        ).fetchall() == [(1, "new"), (2, "added")]
        assert connection.execute(
            "SELECT evidence_node_id, body FROM evidence_nodes"
        ).fetchall() == [(1, "sealed")]
    finally:
        connection.close()
    assert receipt.quick_check == "ok"
    assert receipt.foreign_key_violations == 0
    assert receipt.applied_tables[0].live_rows_not_preserved == 0


def test_apply_fails_closed_on_plan_drift(tmp_path: Path) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
    destination = tmp_path / "candidate.db"
    _database(
        live,
        operational_rows=((1, "new", "2026-07-30T10:00:00Z"),),
        evidence_rows=(),
    )
    _database(
        governed,
        operational_rows=((1, "old", "2026-07-29T10:00:00Z"),),
        evidence_rows=(),
    )

    with pytest.raises(LiveCutoverMergeError, match="plan commitment mismatch"):
        apply_live_cutover_merge(
            live,
            governed,
            destination,
            expected_plan_sha256="0" * 64,
        )

    assert not destination.exists()


def test_apply_never_overwrites_a_source_or_existing_destination(tmp_path: Path) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
    _database(live, operational_rows=(), evidence_rows=())
    _database(governed, operational_rows=(), evidence_rows=())
    plan = plan_live_cutover_merge(live, governed)

    with pytest.raises(LiveCutoverMergeError, match="must not replace"):
        apply_live_cutover_merge(
            live,
            governed,
            live,
            expected_plan_sha256=plan.plan_sha256,
        )
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"do not replace")
    with pytest.raises(LiveCutoverMergeError, match="already exists"):
        apply_live_cutover_merge(
            live,
            governed,
            existing,
            expected_plan_sha256=plan.plan_sha256,
        )
