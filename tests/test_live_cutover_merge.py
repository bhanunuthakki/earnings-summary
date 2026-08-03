from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from provenance import live_cutover_merge as cutover
from provenance.live_cutover_merge import (
    LiveCutoverMergeError,
    TableColumn,
    apply_live_cutover_merge,
    plan_live_cutover_merge,
)
from schema_compat import expected_head

_AUTHORITY_TABLES = cutover.GOVERNED_TABLES_0259 | cutover.OPERATIONAL_TABLES_0259

_POST_0260_GOVERNED_TABLES = frozenset(
    {
        "canonical_resolution_operation_ledger",
        "database_runtime_identity",
        "document_processing_operation_ledger",
        "latest_governed_document_entries",
        "latest_governed_fact_entries",
        "latest_governed_narrative_entries",
        "latest_governed_narrative_fts",
        "latest_governed_narrative_fts_config",
        "latest_governed_narrative_fts_data",
        "latest_governed_narrative_fts_docsize",
        "latest_governed_narrative_fts_idx",
        "latest_governed_population_operation_ledger",
        "latest_governed_population_operation_ledger_v2",
        "latest_governed_refresh_changes",
        "latest_governed_refresh_receipts",
        "latest_governed_refresh_runs",
        "latest_governed_refresh_stage",
        "latest_governed_scope_heads",
        "metric_ontology_operation_ledger",
    }
)


def test_authority_registry_tracks_current_schema() -> None:
    assert expected_head() == "0271_disclosure_thesis_materiality"
    assert _POST_0260_GOVERNED_TABLES <= cutover.GOVERNED_TABLES_0259
    assert "news_events" in cutover.OPERATIONAL_TABLES_0259
    assert len(_AUTHORITY_TABLES) == 329


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
            INSERT INTO alembic_version VALUES ('0271_disclosure_thesis_materiality');
            CREATE TABLE alerts (
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
        for table in sorted(_AUTHORITY_TABLES - {"alembic_version", "alerts", "evidence_nodes"}):
            connection.execute(
                f'CREATE TABLE "{table}" (_registry_marker INTEGER)'  # nosec B608
            )
        connection.executemany(
            "INSERT INTO alerts VALUES (?, ?, ?)",
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

    assert plan.policy_version == "4"
    assert [table.table for table in plan.tables] == ["alerts"]
    assert plan.tables[0].added_row_count == 1
    assert plan.tables[0].changed_row_count == 1
    assert len(plan.live_source_sha256) == 64
    assert len(plan.governed_source_sha256) == 64
    assert len(plan.tables[0].live_rows_sha256) == 64
    assert len(plan.tables[0].governed_rows_sha256) == 64
    assert len(plan.tables[0].selected_delta_sha256) == 64
    assert len(plan.plan_sha256) == 64


def test_apply_preserves_live_operations_and_governed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    original_copy = cast(
        "Callable[[Path, Path], None]",
        getattr(cutover, "_copy_database"),
    )

    def copy_and_stamp_current_head(source: Path, destination_path: Path) -> None:
        original_copy(source, destination_path)
        connection = sqlite3.connect(destination_path)
        try:
            # Full-schema migration coverage lives in the 0261 round-trip tests.
            # This minimal merge fixture models the operational migration seam:
            # immutable authority inputs remain at 0260, then the copied
            # candidate is stamped to the real checkout head before preflight.
            connection.execute(
                "UPDATE alembic_version SET version_num = ?",
                (expected_head(),),
            )
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(cutover, "_copy_database", copy_and_stamp_current_head)

    receipt = apply_live_cutover_merge(
        live,
        governed,
        destination,
        expected_plan_sha256=plan.plan_sha256,
    )

    connection = sqlite3.connect(destination)
    try:
        assert connection.execute(
            "SELECT event_id, state FROM alerts ORDER BY event_id"
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


def test_same_count_value_drift_changes_content_bound_plan(tmp_path: Path) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
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
    before = plan_live_cutover_merge(live, governed)

    connection = sqlite3.connect(live)
    try:
        connection.execute(
            "UPDATE alerts SET state = ? WHERE event_id = ?",
            ("newer", 1),
        )
        connection.commit()
    finally:
        connection.close()

    after = plan_live_cutover_merge(live, governed)

    assert before.tables[0].live_row_count == after.tables[0].live_row_count == 1
    assert before.tables[0].changed_row_count == after.tables[0].changed_row_count == 1
    assert before.tables[0].live_rows_sha256 != after.tables[0].live_rows_sha256
    assert before.tables[0].selected_delta_sha256 != after.tables[0].selected_delta_sha256
    assert before.plan_sha256 != after.plan_sha256


def test_same_count_drift_rejects_stale_plan_before_destination_creation(
    tmp_path: Path,
) -> None:
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
    reviewed = plan_live_cutover_merge(live, governed)

    connection = sqlite3.connect(governed)
    try:
        connection.execute(
            "UPDATE alerts SET recorded_at = ? WHERE event_id = ?",
            ("2026-07-29T11:00:00Z", 1),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(LiveCutoverMergeError, match="plan commitment mismatch"):
        apply_live_cutover_merge(
            live,
            governed,
            destination,
            expected_plan_sha256=reviewed.plan_sha256,
        )

    assert not destination.exists()


@pytest.mark.parametrize("unknown_table", ["future_table", "evidence_future_table"])
def test_plan_rejects_every_unclassified_table(
    tmp_path: Path,
    unknown_table: str,
) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
    _database(live, operational_rows=(), evidence_rows=())
    _database(governed, operational_rows=(), evidence_rows=())
    for path in (live, governed):
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                f'CREATE TABLE "{unknown_table}" (value TEXT)'  # nosec B608
            )
            connection.commit()
        finally:
            connection.close()

    with pytest.raises(LiveCutoverMergeError, match="unclassified table"):
        plan_live_cutover_merge(live, governed)


def test_plan_rejects_source_table_set_and_schema_mismatch(tmp_path: Path) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
    _database(live, operational_rows=(), evidence_rows=())
    _database(governed, operational_rows=(), evidence_rows=())
    connection = sqlite3.connect(governed)
    try:
        connection.execute("DROP TABLE weekly_packet_runs")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(LiveCutoverMergeError, match="source table-set mismatch"):
        plan_live_cutover_merge(live, governed)

    _database(
        tmp_path / "governed-schema.db",
        operational_rows=(),
        evidence_rows=(),
    )
    governed_schema = tmp_path / "governed-schema.db"
    connection = sqlite3.connect(governed_schema)
    try:
        connection.execute("ALTER TABLE alerts ADD COLUMN extra_value TEXT")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(LiveCutoverMergeError, match="source schema mismatch"):
        plan_live_cutover_merge(live, governed_schema)


def test_plan_rejects_source_change_during_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
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
    original = cast(
        "Callable[..., str]",
        getattr(cutover, "_table_rows_sha256"),
    )
    mutated = False

    def mutate_after_live_scan(
        connection: sqlite3.Connection,
        *,
        table: str,
        schema: tuple[TableColumn, ...],
        primary_key: tuple[str, ...],
    ) -> str:
        nonlocal mutated
        result = original(
            connection,
            table=table,
            schema=schema,
            primary_key=primary_key,
        )
        source = Path(
            str(
                next(
                    row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"
                )
            )
        ).resolve()
        if source == live.resolve() and not mutated:
            writer = sqlite3.connect(live)
            try:
                writer.execute(
                    "UPDATE alerts SET recorded_at = ? WHERE event_id = ?",
                    ("2026-07-30T10:01:00Z", 1),
                )
                writer.commit()
            finally:
                writer.close()
            mutated = True
        return result

    monkeypatch.setattr(cutover, "_table_rows_sha256", mutate_after_live_scan)

    with pytest.raises(LiveCutoverMergeError, match="live source changed while planning"):
        plan_live_cutover_merge(live, governed)


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
