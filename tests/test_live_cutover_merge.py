from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import cast

import pytest

from provenance import live_cutover_merge as cutover
from provenance.latest_state_activation import CandidateFileIdentity
from provenance.live_cutover_merge import (
    LiveCutoverMergeError,
    TableColumn,
    apply_live_cutover_merge,
    plan_live_cutover_merge,
)
from schema_compat import expected_head

_AUTHORITY_TABLES = cutover.GOVERNED_TABLES_0259 | cutover.OPERATIONAL_TABLES_0259

_ORIGINAL_SOURCE_WRITE_DENIAL_FENCE = cast(
    "Callable[[tuple[Path, ...]], AbstractContextManager[None]]",
    getattr(cutover, "_source_write_denial_fence"),
)

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


@pytest.fixture(autouse=True)
def _allow_functional_cutover_tests_on_non_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "nt":
        monkeypatch.setattr(
            cutover,
            "_source_write_denial_fence",
            lambda _paths: nullcontext(),
        )


@pytest.mark.skipif(os.name == "nt", reason="unsupported-platform contract")
def test_source_write_denial_fence_fails_closed_outside_windows(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    source.touch()

    with (
        pytest.raises(LiveCutoverMergeError, match="requires Windows"),
        _ORIGINAL_SOURCE_WRITE_DENIAL_FENCE((source,)),
    ):
        pass


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

    assert plan.policy_version == "5"
    assert [table.table for table in plan.tables] == ["alerts"]
    assert plan.tables[0].added_row_count == 1
    assert plan.tables[0].changed_row_count == 1
    assert len(plan.live_source_sha256) == 64
    assert len(plan.governed_source_sha256) == 64
    assert len(plan.tables[0].live_rows_sha256) == 64
    assert len(plan.tables[0].governed_rows_sha256) == 64
    assert len(plan.tables[0].selected_delta_sha256) == 64
    assert len(plan.plan_sha256) == 64


def test_integer_primary_key_content_order_is_indexable() -> None:
    content_order_sql = cast(
        "Callable[..., str]",
        getattr(cutover, "_content_order_sql"),
    )
    schema = (
        TableColumn(name="id", type="INTEGER", notnull=0, default=None, pk=1),
        TableColumn(name="body", type="TEXT", notnull=1, default=None, pk=0),
    )

    assert (
        content_order_sql(
            None,
            schema=schema,
            primary_key=("id",),
            integer_primary_key_is_total=True,
        )
        == '"id"'
    )
    assert (
        content_order_sql(
            "src",
            schema=schema,
            primary_key=("id",),
            integer_primary_key_is_total=True,
        )
        == 'src."id"'
    )

    keyless_order = content_order_sql(
        None,
        schema=schema,
        primary_key=(),
        integer_primary_key_is_total=False,
    )
    assert keyless_order == '_cutover_value_key("id"), _cutover_value_key("body")'

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES (1, 'old')")
        connection.execute("ATTACH DATABASE ':memory:' AS live_delta")
        connection.execute(
            "CREATE TABLE live_delta.sample (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO live_delta.sample VALUES (1, 'new'), (2, 'added')")
        order_sql = content_order_sql(
            "src",
            schema=schema,
            primary_key=("id",),
            integer_primary_key_is_total=True,
        )
        query_plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT src.id, src.body, "
            "CASE WHEN EXISTS (SELECT 1 FROM main.sample AS dst "
            "WHERE dst.id IS src.id) THEN 1 ELSE 0 END "
            "FROM live_delta.sample AS src WHERE NOT EXISTS ("
            "SELECT 1 FROM main.sample AS dst "
            "WHERE dst.id IS src.id AND dst.body IS src.body) "
            f"ORDER BY {order_sql}"  # nosec B608
        ).fetchall()
    finally:
        connection.close()
    assert all("USE TEMP B-TREE" not in str(row[3]) for row in query_plan)


def test_nullable_integer_primary_key_uses_layout_stable_fallback() -> None:
    table_rows_sha256 = cast(
        "Callable[..., str]",
        getattr(cutover, "_table_rows_sha256"),
    )
    schema = (
        TableColumn(name="id", type="INTEGER", notnull=0, default=None, pk=1),
        TableColumn(name="body", type="TEXT", notnull=1, default=None, pk=0),
    )
    forward = sqlite3.connect(":memory:")
    reverse = sqlite3.connect(":memory:")
    try:
        for connection, rows in (
            (forward, ((None, "alpha"), (None, "beta"))),
            (reverse, ((None, "beta"), (None, "alpha"))),
        ):
            connection.row_factory = sqlite3.Row
            connection.execute(
                "CREATE TABLE sample (id INTEGER PRIMARY KEY DESC, body TEXT NOT NULL)"
            )
            connection.executemany("INSERT INTO sample VALUES (?, ?)", rows)
        forward_sha = table_rows_sha256(
            forward,
            table="sample",
            schema=schema,
            primary_key=("id",),
        )
        reverse_sha = table_rows_sha256(
            reverse,
            table="sample",
            schema=schema,
            primary_key=("id",),
        )
    finally:
        reverse.close()
        forward.close()
    assert forward_sha == reverse_sha


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


@pytest.mark.skipif(os.name != "nt", reason="Windows write-denial fence")
def test_apply_fence_denies_live_mutation_after_plan_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    original_copy = cast(
        "Callable[[Path, Path], CandidateFileIdentity]",
        getattr(cutover, "_copy_database"),
    )

    def copy_then_mutate_live(
        source: Path,
        destination_path: Path,
    ) -> CandidateFileIdentity:
        identity = original_copy(source, destination_path)
        connection = sqlite3.connect(live)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("UPDATE alerts SET state = 'unreviewed' WHERE event_id = 1")
        finally:
            connection.close()
        return identity

    monkeypatch.setattr(cutover, "_copy_database", copy_then_mutate_live)

    apply_live_cutover_merge(
        live,
        governed,
        destination,
        expected_plan_sha256=reviewed.plan_sha256,
    )

    connection = sqlite3.connect(live)
    try:
        assert connection.execute("SELECT state FROM alerts").fetchone() == ("new",)
    finally:
        connection.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows write-denial fence")
def test_apply_fence_denies_governed_mutation_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    original_copy = cast(
        "Callable[[Path, Path], CandidateFileIdentity]",
        getattr(cutover, "_copy_database"),
    )

    def copy_then_mutate_governed(
        source: Path,
        destination_path: Path,
    ) -> CandidateFileIdentity:
        identity = original_copy(source, destination_path)
        connection = sqlite3.connect(governed)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("UPDATE alerts SET state = 'unreviewed' WHERE event_id = 1")
        finally:
            connection.close()
        return identity

    monkeypatch.setattr(cutover, "_copy_database", copy_then_mutate_governed)

    apply_live_cutover_merge(
        live,
        governed,
        destination,
        expected_plan_sha256=reviewed.plan_sha256,
    )

    connection = sqlite3.connect(governed)
    try:
        assert connection.execute("SELECT state FROM alerts").fetchone() == ("old",)
    finally:
        connection.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows write-denial fence")
def test_apply_fence_denies_governed_path_substitution_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
    destination = tmp_path / "candidate.db"
    displaced = tmp_path / "governed-displaced.db"
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

    def attempt_source_replacement(
        _source: Path,
        _destination_path: Path,
    ) -> CandidateFileIdentity:
        governed.replace(displaced)
        raise AssertionError("the governed source fence allowed replacement")

    monkeypatch.setattr(cutover, "_copy_database", attempt_source_replacement)

    with pytest.raises(OSError):
        apply_live_cutover_merge(
            live,
            governed,
            destination,
            expected_plan_sha256=reviewed.plan_sha256,
        )

    assert governed.exists()
    assert not displaced.exists()
    assert not destination.exists()


def test_apply_cleanup_preserves_replacement_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
    destination = tmp_path / "candidate.db"
    displaced = tmp_path / "candidate-owned.db"
    replacement = tmp_path / "candidate-replacement.db"
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
    _database(
        replacement,
        operational_rows=((99, "replacement", "2026-07-30T10:00:00Z"),),
        evidence_rows=(),
    )
    reviewed = plan_live_cutover_merge(live, governed)
    original_copy = cast(
        "Callable[[Path, Path], CandidateFileIdentity]",
        getattr(cutover, "_copy_database"),
    )

    def copy_then_replace_destination(
        source: Path,
        destination_path: Path,
    ) -> CandidateFileIdentity:
        identity = original_copy(source, destination_path)
        destination_path.replace(displaced)
        replacement.replace(destination_path)
        return identity

    monkeypatch.setattr(cutover, "_copy_database", copy_then_replace_destination)

    with pytest.raises(LiveCutoverMergeError, match="destination identity changed"):
        apply_live_cutover_merge(
            live,
            governed,
            destination,
            expected_plan_sha256=reviewed.plan_sha256,
        )

    assert destination.exists()
    connection = sqlite3.connect(destination)
    try:
        assert connection.execute("SELECT event_id FROM alerts").fetchall() == [(99,)]
    finally:
        connection.close()


def test_apply_keeps_original_destination_identity_through_final_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
    destination = tmp_path / "candidate.db"
    displaced = tmp_path / "candidate-owned.db"
    replacement = tmp_path / "candidate-replacement.db"
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
    _database(
        replacement,
        operational_rows=((99, "replacement", "2026-07-30T10:00:00Z"),),
        evidence_rows=(),
    )
    reviewed = plan_live_cutover_merge(live, governed)
    original_identity = cast(
        "Callable[[Path], CandidateFileIdentity]",
        getattr(cutover, "candidate_file_identity"),
    )
    identity_calls = 0

    def substitute_on_identity_reacquisition(path: Path) -> CandidateFileIdentity:
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 5:
            path.replace(displaced)
            replacement.replace(path)
        return original_identity(path)

    monkeypatch.setattr(
        cutover,
        "candidate_file_identity",
        substitute_on_identity_reacquisition,
    )

    receipt = apply_live_cutover_merge(
        live,
        governed,
        destination,
        expected_plan_sha256=reviewed.plan_sha256,
    )

    assert identity_calls == 4
    assert not displaced.exists()
    assert replacement.exists()
    assert receipt.destination_sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()


def test_apply_verifies_staged_rows_against_reviewed_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    original_copy = cast(
        "Callable[[Path, Path], CandidateFileIdentity]",
        getattr(cutover, "_copy_database"),
    )
    original_source_sha = cast(
        "Callable[[Path], str]",
        getattr(cutover, "_source_snapshot_sha256"),
    )
    live_mutated = False

    def copy_then_mutate_live(
        source: Path,
        destination_path: Path,
    ) -> CandidateFileIdentity:
        nonlocal live_mutated
        identity = original_copy(source, destination_path)
        connection = sqlite3.connect(live)
        try:
            connection.execute("UPDATE alerts SET state = 'unreviewed' WHERE event_id = 1")
            connection.commit()
        finally:
            connection.close()
        live_mutated = True
        return identity

    def admitted_source_sha(path: Path) -> str:
        if live_mutated and path.resolve() == live.resolve():
            return reviewed.live_source_sha256
        return original_source_sha(path)

    def disabled_source_fence(_paths: tuple[Path, ...]) -> nullcontext[None]:
        return nullcontext()

    monkeypatch.setattr(cutover, "_copy_database", copy_then_mutate_live)
    monkeypatch.setattr(cutover, "_source_snapshot_sha256", admitted_source_sha)
    monkeypatch.setattr(
        cutover,
        "_source_write_denial_fence",
        disabled_source_fence,
    )

    with pytest.raises(LiveCutoverMergeError, match="staged live rows differ"):
        apply_live_cutover_merge(
            live,
            governed,
            destination,
            expected_plan_sha256=reviewed.plan_sha256,
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


@pytest.mark.skipif(os.name != "nt", reason="Windows write-denial fence")
def test_plan_fence_denies_source_change_during_scan(
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
    mutation_denied = False

    def mutate_after_live_scan(
        connection: sqlite3.Connection,
        *,
        table: str,
        schema: tuple[TableColumn, ...],
        primary_key: tuple[str, ...],
    ) -> str:
        nonlocal mutation_denied
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
        if source == live.resolve() and not mutation_denied:
            writer = sqlite3.connect(live)
            try:
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    writer.execute(
                        "UPDATE alerts SET recorded_at = ? WHERE event_id = ?",
                        ("2026-07-30T10:01:00Z", 1),
                    )
            finally:
                writer.close()
            mutation_denied = True
        return result

    monkeypatch.setattr(cutover, "_table_rows_sha256", mutate_after_live_scan)

    plan = plan_live_cutover_merge(live, governed)

    assert mutation_denied
    assert plan.tables[0].live_row_count == 1


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
