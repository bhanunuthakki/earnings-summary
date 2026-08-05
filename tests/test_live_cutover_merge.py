from __future__ import annotations

import hashlib
import os
import sqlite3
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import cast

import pytest

from provenance import live_cutover_merge as cutover
from provenance.latest_state_activation import CandidateFileIdentity
from provenance.live_cutover_merge import (
    CutoverSourceHealthReceipt,
    LiveCutoverMergeError,
    SourceComponentState,
    TableColumn,
    audit_cutover_source_health,
)
from provenance.live_cutover_merge import (
    apply_live_cutover_merge as _apply_live_cutover_merge,
)
from provenance.live_cutover_merge import (
    plan_live_cutover_merge as _plan_live_cutover_merge,
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
    assert expected_head() == "0273_post_earnings_readout_budget"
    assert _POST_0260_GOVERNED_TABLES <= cutover.GOVERNED_TABLES_0259
    assert "news_events" in cutover.OPERATIONAL_TABLES_0259
    assert {
        "archive_generations",
        "archive_generation_table_commitments",
        "archive_generation_registration_receipts",
    } <= cutover.OPERATIONAL_TABLES_0259
    assert len(_AUTHORITY_TABLES) == 332


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
            INSERT INTO alembic_version VALUES ('0273_post_earnings_readout_budget');
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


def _health_artifact(database: Path, artifact: Path) -> None:
    source_state = cast(
        "Callable[[Path], tuple[tuple[str, CandidateFileIdentity | None], ...]]",
        getattr(cutover, "_source_physical_state"),
    )(database.resolve())
    source_sha256 = cast(
        "Callable[[Path], str]",
        getattr(cutover, "_source_snapshot_sha256"),
    )(database.resolve())
    draft = CutoverSourceHealthReceipt(
        schema_name="live-cutover-source-health/v1",
        database=str(database.resolve()),
        source_sha256=source_sha256,
        source_state=tuple(
            SourceComponentState(label=label, identity=identity) for label, identity in source_state
        ),
        alembic_revision="0273_post_earnings_readout_budget",
        integrity_check="ok",
        foreign_key_violations=0,
        receipt_sha256="0" * 64,
    )
    receipt_sha256 = cast(
        "Callable[[CutoverSourceHealthReceipt], str]",
        getattr(cutover, "_health_receipt_sha256"),
    )(draft)
    receipt = draft.model_copy(update={"receipt_sha256": receipt_sha256})
    artifact.write_bytes((receipt.model_dump_json() + "\n").encode("utf-8"))


def plan_live_cutover_merge(
    live: Path,
    governed: Path,
    *,
    live_health_receipt: Path | None = None,
    governed_health_receipt: Path | None = None,
):
    if live_health_receipt is None or governed_health_receipt is None:
        assert live_health_receipt is None and governed_health_receipt is None
        live_health_receipt = Path(f"{live}.health.json")
        governed_health_receipt = Path(f"{governed}.health.json")
        _health_artifact(live, live_health_receipt)
        _health_artifact(governed, governed_health_receipt)
    return _plan_live_cutover_merge(
        live,
        governed,
        live_health_receipt=live_health_receipt,
        governed_health_receipt=governed_health_receipt,
    )


def apply_live_cutover_merge(
    live: Path,
    governed: Path,
    destination: Path,
    *,
    expected_plan_sha256: str,
    live_health_receipt: Path | None = None,
    governed_health_receipt: Path | None = None,
    receipt_path: Path | None = None,
):
    if live_health_receipt is None or governed_health_receipt is None:
        assert live_health_receipt is None and governed_health_receipt is None
        live_health_receipt = Path(f"{live}.health.json")
        governed_health_receipt = Path(f"{governed}.health.json")
        _health_artifact(live, live_health_receipt)
        _health_artifact(governed, governed_health_receipt)
    if receipt_path is None:
        receipt_path = Path(f"{destination}.receipt.json")
    return _apply_live_cutover_merge(
        live,
        governed,
        destination,
        expected_plan_sha256=expected_plan_sha256,
        live_health_receipt=live_health_receipt,
        governed_health_receipt=governed_health_receipt,
        receipt_path=receipt_path,
    )


def test_audit_cutover_source_health_binds_exhaustive_checks(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _database(database, operational_rows=(), evidence_rows=())

    receipt = audit_cutover_source_health(database)

    assert receipt.integrity_check == "ok"
    assert receipt.foreign_key_violations == 0
    assert receipt.alembic_revision == "0273_post_earnings_readout_budget"
    assert receipt.receipt_sha256 != "0" * 64


def test_health_receipts_replace_repeated_source_integrity_scans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
    live_health = tmp_path / "live-health.json"
    governed_health = tmp_path / "governed-health.json"
    destination = tmp_path / "candidate.db"
    _database(live, operational_rows=(), evidence_rows=())
    _database(governed, operational_rows=(), evidence_rows=())
    _health_artifact(live, live_health)
    _health_artifact(governed, governed_health)

    def unexpected_health_scan(_label: str, _connection: sqlite3.Connection) -> None:
        raise AssertionError("source health must be admitted from the bound receipt")

    monkeypatch.setattr(cutover, "_require_healthy", unexpected_health_scan)

    plan = plan_live_cutover_merge(
        live,
        governed,
        live_health_receipt=live_health,
        governed_health_receipt=governed_health,
    )

    assert plan.policy_version == "7"
    assert plan.live_health_receipt is not None
    assert plan.governed_health_receipt is not None
    assert plan.live_health_receipt.artifact_path == str(live_health.resolve())
    assert len(plan.live_health_receipt.artifact_sha256) == 64

    receipt = apply_live_cutover_merge(
        live,
        governed,
        destination,
        expected_plan_sha256=plan.plan_sha256,
        live_health_receipt=live_health,
        governed_health_receipt=governed_health,
    )
    assert receipt.plan.plan_sha256 == plan.plan_sha256


def test_health_receipt_rejects_source_or_artifact_drift(tmp_path: Path) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
    live_health = tmp_path / "live-health.json"
    governed_health = tmp_path / "governed-health.json"
    _database(live, operational_rows=(), evidence_rows=())
    _database(governed, operational_rows=(), evidence_rows=())
    _health_artifact(live, live_health)
    _health_artifact(governed, governed_health)

    connection = sqlite3.connect(live)
    try:
        connection.execute("INSERT INTO alerts VALUES (1, 'drift', '2026')")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(LiveCutoverMergeError, match="health receipt does not match"):
        plan_live_cutover_merge(
            live,
            governed,
            live_health_receipt=live_health,
            governed_health_receipt=governed_health,
        )

    _health_artifact(live, live_health)
    payload = live_health.read_text(encoding="utf-8").replace(
        '"integrity_check":"ok"',
        '"integrity_check":"bad"',
    )
    live_health.write_bytes(payload.encode("utf-8"))
    with pytest.raises(LiveCutoverMergeError, match="health receipt commitment"):
        plan_live_cutover_merge(
            live,
            governed,
            live_health_receipt=live_health,
            governed_health_receipt=governed_health,
        )


def test_plan_rejects_hardlink_alias_between_sources(tmp_path: Path) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed-hardlink.db"
    live_health = tmp_path / "live-health.json"
    governed_health = tmp_path / "governed-health.json"
    _database(live, operational_rows=(), evidence_rows=())
    os.link(live, governed)
    _health_artifact(live, live_health)
    _health_artifact(governed, governed_health)

    with pytest.raises(LiveCutoverMergeError, match="must be distinct files"):
        _plan_live_cutover_merge(
            live,
            governed,
            live_health_receipt=live_health,
            governed_health_receipt=governed_health,
        )


def test_apply_rejects_protected_source_and_evidence_namespaces(tmp_path: Path) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
    live_health = tmp_path / "live-health.json"
    governed_health = tmp_path / "governed-health.json"
    _database(live, operational_rows=(), evidence_rows=())
    _database(governed, operational_rows=(), evidence_rows=())
    _health_artifact(live, live_health)
    _health_artifact(governed, governed_health)
    plan = _plan_live_cutover_merge(
        live,
        governed,
        live_health_receipt=live_health,
        governed_health_receipt=governed_health,
    )

    for destination in (
        Path(f"{live}-wal"),
        Path(f"{live}-shm"),
        Path(f"{live}-journal"),
        live_health,
    ):
        with pytest.raises(LiveCutoverMergeError, match="destination namespace aliases"):
            _apply_live_cutover_merge(
                live,
                governed,
                destination,
                expected_plan_sha256=plan.plan_sha256,
                live_health_receipt=live_health,
                governed_health_receipt=governed_health,
                receipt_path=tmp_path / f"{destination.name}.receipt.json",
            )

    destination = tmp_path / "candidate.db"
    with pytest.raises(LiveCutoverMergeError, match="receipt aliases"):
        _apply_live_cutover_merge(
            live,
            governed,
            destination,
            expected_plan_sha256=plan.plan_sha256,
            live_health_receipt=live_health,
            governed_health_receipt=governed_health,
            receipt_path=Path(f"{live}-wal"),
        )
    assert not destination.exists()


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

    assert plan.policy_version == "7"
    assert [table.table for table in plan.tables] == ["alerts"]
    assert plan.tables[0].added_row_count == 1
    assert plan.tables[0].changed_row_count == 1
    assert len(plan.live_source_sha256) == 64
    assert len(plan.governed_source_sha256) == 64
    assert len(plan.tables[0].live_rows_sha256) == 64
    assert len(plan.tables[0].governed_rows_sha256) == 64
    assert len(plan.tables[0].selected_delta_sha256) == 64
    assert len(plan.plan_sha256) == 64


def test_plan_hashes_each_fenced_source_once(
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
    live_health = Path(f"{live}.health.json")
    governed_health = Path(f"{governed}.health.json")
    _health_artifact(live, live_health)
    _health_artifact(governed, governed_health)
    original_source_sha = cast(
        "Callable[[Path], str]",
        getattr(cutover, "_source_snapshot_sha256"),
    )
    calls: Counter[Path] = Counter()

    def counted_source_sha(path: Path) -> str:
        calls[path.resolve()] += 1
        return original_source_sha(path)

    monkeypatch.setattr(cutover, "_source_snapshot_sha256", counted_source_sha)

    _plan_live_cutover_merge(
        live,
        governed,
        live_health_receipt=live_health,
        governed_health_receipt=governed_health,
    )

    assert calls == Counter({live.resolve(): 1, governed.resolve(): 1})


def test_plan_rejects_physical_source_state_drift_after_scan(
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
    live_health = Path(f"{live}.health.json")
    governed_health = Path(f"{governed}.health.json")
    _health_artifact(live, live_health)
    _health_artifact(governed, governed_health)
    original_source_state = cast(
        "Callable[[Path], object]",
        getattr(cutover, "_source_physical_state"),
    )
    calls: Counter[Path] = Counter()

    def drifting_source_state(path: Path) -> object:
        resolved = path.resolve()
        calls[resolved] += 1
        if resolved == live.resolve() and calls[resolved] == 2:
            return (("main", None),)
        return original_source_state(path)

    monkeypatch.setattr(cutover, "_source_physical_state", drifting_source_state)

    with pytest.raises(LiveCutoverMergeError, match="live source changed while planning"):
        _plan_live_cutover_merge(
            live,
            governed,
            live_health_receipt=live_health,
            governed_health_receipt=governed_health,
        )


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


def test_apply_hashes_each_continuously_fenced_source_once(
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
    live_health = Path(f"{live}.health.json")
    governed_health = Path(f"{governed}.health.json")
    original_source_sha = cast(
        "Callable[[Path], str]",
        getattr(cutover, "_source_snapshot_sha256"),
    )
    calls: Counter[Path] = Counter()

    def counted_source_sha(path: Path) -> str:
        calls[path.resolve()] += 1
        return original_source_sha(path)

    monkeypatch.setattr(cutover, "_source_snapshot_sha256", counted_source_sha)

    _apply_live_cutover_merge(
        live,
        governed,
        destination,
        expected_plan_sha256=reviewed.plan_sha256,
        live_health_receipt=live_health,
        governed_health_receipt=governed_health,
        receipt_path=Path(f"{destination}.receipt.json"),
    )

    assert calls == Counter({live.resolve(): 1, governed.resolve(): 1})


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


def test_apply_cleans_owned_destination_when_receipt_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
    destination = tmp_path / "candidate.db"
    receipt_path = tmp_path / "candidate-receipt.json"
    _database(live, operational_rows=(), evidence_rows=())
    _database(governed, operational_rows=(), evidence_rows=())
    plan = plan_live_cutover_merge(live, governed)

    def fail_publication(_path: Path, _payload: str) -> bool:
        raise OSError("injected receipt publication failure")

    monkeypatch.setattr(cutover, "publish_text_no_clobber", fail_publication)

    with pytest.raises(OSError, match="injected receipt publication failure"):
        apply_live_cutover_merge(
            live,
            governed,
            destination,
            expected_plan_sha256=plan.plan_sha256,
            receipt_path=receipt_path,
        )

    assert not destination.exists()
    assert not receipt_path.exists()
    assert all(
        not Path(f"{destination}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows destination write-denial fence")
def test_apply_denies_destination_mutation_between_hash_and_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "live.db"
    governed = tmp_path / "governed.db"
    destination = tmp_path / "candidate.db"
    receipt_path = tmp_path / "candidate-receipt.json"
    _database(live, operational_rows=(), evidence_rows=())
    _database(governed, operational_rows=(), evidence_rows=())
    plan = plan_live_cutover_merge(live, governed)
    original_publish = cast(
        "Callable[[Path, str], bool]",
        getattr(cutover, "publish_text_no_clobber"),
    )
    mutation_refused = False

    def attempt_mutation_then_publish(path: Path, payload: str) -> bool:
        nonlocal mutation_refused
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(destination, timeout=0.1)
            connection.execute("INSERT INTO alerts VALUES (1, 'race', '2026-08-03T00:00:00Z')")
            connection.commit()
        except sqlite3.Error:
            mutation_refused = True
        finally:
            if connection is not None:
                connection.close()
        return original_publish(path, payload)

    monkeypatch.setattr(cutover, "publish_text_no_clobber", attempt_mutation_then_publish)

    receipt = apply_live_cutover_merge(
        live,
        governed,
        destination,
        expected_plan_sha256=plan.plan_sha256,
        receipt_path=receipt_path,
    )

    assert mutation_refused
    assert receipt_path.exists()
    connection = sqlite3.connect(destination)
    try:
        assert connection.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
    finally:
        connection.close()
    assert receipt.destination_sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()


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


@pytest.mark.skipif(os.name != "nt", reason="Windows destination write-denial fence")
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
    substitution_refused = False

    def substitute_on_identity_reacquisition(path: Path) -> CandidateFileIdentity:
        nonlocal identity_calls, substitution_refused
        if path.resolve() == destination.resolve():
            identity_calls += 1
            if identity_calls == 5:
                try:
                    path.replace(displaced)
                    replacement.replace(path)
                except PermissionError:
                    substitution_refused = True
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

    assert identity_calls == 5
    assert substitution_refused
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
    original_source_state = cast(
        "Callable[[Path], object]",
        getattr(cutover, "_source_physical_state"),
    )
    admitted_live_state = original_source_state(live)
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

    def admitted_source_state(path: Path) -> object:
        if live_mutated and path.resolve() == live.resolve():
            return admitted_live_state
        return original_source_state(path)

    def disabled_source_fence(_paths: tuple[Path, ...]) -> nullcontext[None]:
        return nullcontext()

    monkeypatch.setattr(cutover, "_copy_database", copy_then_mutate_live)
    monkeypatch.setattr(cutover, "_source_physical_state", admitted_source_state)
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

    with pytest.raises(LiveCutoverMergeError, match="destination namespace aliases"):
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
