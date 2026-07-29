from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config

import search.canonical_fact_projection as projection
from alembic import command
from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionSnapshotScope,
)
from provenance.metric_ontology import MetricOntology, OntologySnapshot
from provenance.source_fact_stream import bind_resolution_snapshot_watermark
from search.canonical_fact_projection import (
    CanonicalFactProjectionError,
    ProjectionConfig,
    ProjectionGenerationRequest,
    build_canonical_projection_generation,
    canonical_decimal,
    canonical_json,
    digest_text,
    plan_canonical_fact_query,
    verify_canonical_projection_generation,
)

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "0213_decision_draft_provider_id"
HEAD = "0255_scoped_canonical_resolution_snapshots"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
EMPTY_SCOPE = ResolutionSnapshotScope(
    issuer_id="issuer-empty",
    reporting_entity_ids=("reporting-empty",),
)


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _empty_database(path: Path) -> sqlite3.Connection:
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE llm_budgets (
            purpose TEXT PRIMARY KEY,
            monthly_cap_usd REAL NOT NULL,
            warn_threshold_pct REAL NOT NULL,
            hard_block INTEGER NOT NULL,
            on_exceed TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            notes TEXT
        );
        """
    )
    legacy.commit()
    legacy.close()
    config = _config(path)
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO issuer_entities VALUES (?,?,?,?)",
        ("issuer-empty", "issuer-empty", "operating_company", T0.isoformat()),
    )
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
        (
            "reporting-empty",
            "reporting-empty",
            "issuer-empty",
            "legal_registrant",
            "Empty issuer",
            T0.isoformat(),
        ),
    )
    MetricOntology(conn).seal_snapshot(
        OntologySnapshot(
            ontology_snapshot_id="ontology-empty",
            idempotency_key="ontology-empty",
            cutoff_at=T0,
            recorded_at=T0,
        )
    )
    resolver = CanonicalFactResolutionEngine(conn)
    resolver.seal_snapshot("resolution-empty", T0, T0, EMPTY_SCOPE)
    bind_resolution_snapshot_watermark(
        conn,
        resolution_snapshot_id="resolution-empty",
        cutoff_at=T0,
        recorded_at=T0,
    )
    return conn


def _request(generation_id: str = "generation-empty") -> ProjectionGenerationRequest:
    return ProjectionGenerationRequest(
        generation_id=generation_id,
        idempotency_key=generation_id,
        generation_kind="checkpoint",
        resolution_snapshot_id="resolution-empty",
        ontology_snapshot_id="ontology-empty",
        cutoff_at=T0,
        recorded_at=T0,
        config=ProjectionConfig(max_batch_facts=2),
    )


class _FetchTrackingCursor:
    def __init__(
        self,
        cursor: sqlite3.Cursor,
        connection: _FetchTrackingConnection,
    ) -> None:
        self._cursor = cursor
        self._connection = connection

    @property
    def description(self) -> tuple[tuple[object, ...], ...] | None:
        return cast(tuple[tuple[object, ...], ...] | None, self._cursor.description)

    def fetchone(self) -> tuple[object, ...] | None:
        return cast(tuple[object, ...] | None, self._cursor.fetchone())

    def fetchmany(self, size: int = 1) -> list[tuple[object, ...]]:
        rows = cast(list[tuple[object, ...]], self._cursor.fetchmany(size))
        self._connection.requested_fetch_sizes.append(size)
        self._connection.returned_fetch_sizes.append(len(rows))
        return rows

    def fetchall(self) -> list[tuple[object, ...]]:
        rows = cast(list[tuple[object, ...]], self._cursor.fetchall())
        self._connection.returned_fetch_sizes.append(len(rows))
        return rows

    def __iter__(self) -> Iterator[tuple[object, ...]]:
        return cast(Iterator[tuple[object, ...]], iter(self._cursor))


class _FetchTrackingConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.requested_fetch_sizes: list[int] = []
        self.returned_fetch_sizes: list[int] = []

    @property
    def row_factory(self) -> object:
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value: object) -> None:
        self._connection.row_factory = cast(
            Callable[[sqlite3.Cursor, tuple[object, ...]], object] | None,
            value,
        )

    def create_function(
        self,
        name: str,
        narg: int,
        func: Callable[..., bytes | float | int | str | None] | None,
        *,
        deterministic: bool = False,
    ) -> None:
        self._connection.create_function(
            name,
            narg,
            func,
            deterministic=deterministic,
        )

    def execute(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> _FetchTrackingCursor:
        return _FetchTrackingCursor(
            self._connection.execute(sql, parameters),
            self,
        )


def _reset_empty_generation(
    conn: sqlite3.Connection,
) -> tuple[
    ProjectionGenerationRequest,
    Callable[[sqlite3.Connection, ProjectionGenerationRequest], None],
    tuple[str, ...],
]:
    request = _request()
    write_buckets_and_seal = cast(
        Callable[[sqlite3.Connection, ProjectionGenerationRequest], None],
        getattr(projection, "_write_buckets_and_seal"),
    )
    entry_columns = cast(tuple[str, ...], getattr(projection, "_ENTRY_COLUMNS"))
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    for table in (
        "canonical_fact_projection_audit_receipts",
        "canonical_fact_projection_seals",
        "canonical_fact_projection_buckets",
        "canonical_fact_projection_batches",
        "canonical_fact_projection_entries",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    conn.execute(
        "DELETE FROM canonical_fact_projection_audit_receipts WHERE generation_id=?",
        (request.generation_id,),
    )
    conn.execute(
        "DELETE FROM canonical_fact_projection_seals WHERE generation_id=?",
        (request.generation_id,),
    )
    conn.execute(
        "DELETE FROM canonical_fact_projection_buckets WHERE generation_id=?",
        (request.generation_id,),
    )
    conn.execute(
        "DELETE FROM canonical_fact_projection_batches WHERE generation_id=?",
        (request.generation_id,),
    )
    conn.execute(
        "DELETE FROM canonical_fact_projection_entries WHERE generation_id=?",
        (request.generation_id,),
    )
    return request, write_buckets_and_seal, entry_columns


def _insert_synthetic_entries(
    conn: sqlite3.Connection,
    request: ProjectionGenerationRequest,
    entry_columns: tuple[str, ...],
    entries: list[dict[str, object]],
) -> None:
    conn.executemany(
        "INSERT INTO canonical_fact_projection_entries "
        f"({','.join(entry_columns)}) VALUES "
        f"({','.join('?' for _ in entry_columns)})",
        [tuple(entry[column] for column in entry_columns) for entry in entries],
    )
    for batch_ordinal, first in enumerate(range(0, len(entries), 2)):
        members = entries[first : first + 2]
        hashes = [str(entry["entry_sha256"]) for entry in members]
        payload = canonical_json(hashes)
        conn.execute(
            "INSERT INTO canonical_fact_projection_batches VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                request.generation_id,
                batch_ordinal,
                first,
                first + len(members) - 1,
                members[0]["canonical_metric_cell_id"],
                members[-1]["canonical_metric_cell_id"],
                len(members),
                sum(len(str(member["entry_json"]).encode("utf-8")) for member in members),
                0,
                payload,
                digest_text(payload),
            ),
        )


def _replace_empty_generation_with_deletes(
    conn: sqlite3.Connection,
    *,
    count: int,
) -> None:
    request, write_buckets_and_seal, entry_columns = _reset_empty_generation(conn)
    delete_entry = cast(
        Callable[[str, int, str], dict[str, object]],
        getattr(projection, "_delete_entry"),
    )
    entries = [
        delete_entry(request.generation_id, ordinal, f"deleted-{ordinal:08d}")
        for ordinal in range(count)
    ]
    _insert_synthetic_entries(conn, request, entry_columns, entries)
    write_buckets_and_seal(conn, request)
    conn.commit()


def _synthetic_upsert(
    generation_id: str,
    ordinal: int,
    *,
    bucket: int,
) -> dict[str, object]:
    payload_columns = cast(tuple[str, ...], getattr(projection, "_ENTRY_PAYLOAD_COLUMNS"))
    coordinate = f"skewed-{ordinal:08d}"
    exact_hash = "a" * 64
    payload: dict[str, object] = {column: None for column in payload_columns}
    payload.update(
        {
            "binding_commitment_sha256": exact_hash,
            "binding_revision_id": f"binding-{ordinal}",
            "canonical_metric_cell_id": coordinate,
            "canonical_metric_name": "Revenue",
            "canonical_resolution_revision_id": f"resolution-{ordinal}",
            "canonical_search_text": f"Revenue {ordinal}",
            "canonical_value": str(ordinal),
            "currency": "USD",
            "dimensions_json": "[]",
            "evidence_document_version_id": f"document-{ordinal}",
            "evidence_locator_json": "{}",
            "evidence_locator_sha256": digest_text("{}"),
            "evidence_node_id": f"node-{ordinal}",
            "mapping_commitment_sha256": exact_hash,
            "mapping_revision_id": f"mapping-{ordinal}",
            "metric_definition_commitment_sha256": exact_hash,
            "metric_definition_revision_id": f"definition-{ordinal}",
            "period_end": "2026-01-01T00:00:00.000000Z",
            "period_kind": "instant",
            "reporting_entity_id": "entity",
            "selected_observation_id": f"observation-{ordinal}",
            "source_publication_id": f"publication-{ordinal}",
            "source_publication_member_id": f"member-{ordinal}",
            "source_publication_member_sha256": exact_hash,
            "source_publication_seal_id": f"publication-seal-{ordinal}",
            "source_record_commitment_sha256": exact_hash,
            "unit_key": "USD",
            "value_kind": "numeric",
        }
    )
    committed_payload = {
        **payload,
        "change_kind": "upsert",
        "entry_version": "canonical_fact_projection_entry.v1",
    }
    entry_json = canonical_json(committed_payload)
    return {
        "generation_id": generation_id,
        "entry_ordinal": ordinal,
        "change_kind": "upsert",
        "digest_bucket": bucket,
        **payload,
        "entry_json": entry_json,
        "entry_sha256": digest_text(entry_json),
    }


def _synthetic_selected_source(ordinal: int) -> dict[str, object]:
    exact_hash = "a" * 64
    coordinate = f"production-canonical:{ordinal:012d}"
    return {
        "aliases_json": "[]",
        "binding_commitment_sha256": exact_hash,
        "binding_revision_id": f"binding-{ordinal}",
        "canonical_metric_cell_id": coordinate,
        "canonical_name": "Revenue",
        "canonical_resolution_revision_id": f"resolution-{ordinal}",
        "currency": "USD",
        "definition_text": "Revenue recognized from customer contracts.",
        "dimension_set_json": "[]",
        "document_version_id": f"document-{ordinal}",
        "evidence_node_id": f"node-{ordinal}",
        "mapping_commitment_sha256": exact_hash,
        "mapping_revision_id": f"mapping-{ordinal}",
        "metric_definition_commitment_sha256": exact_hash,
        "metric_definition_revision_id": f"definition-{ordinal}",
        "numeric_value": str(ordinal),
        "period_end": T0,
        "period_kind": "instant",
        "period_start": None,
        "reporting_entity_id": "entity",
        "scope_security_id": None,
        "selected_observation_id": f"observation-{ordinal}",
        "source_fact_cell_id": f"fact-cell-{ordinal}",
        "source_locator_json": "{}",
        "source_locator_sha256": digest_text("{}"),
        "source_publication_id": f"publication-{ordinal}",
        "source_publication_member_id": f"member-{ordinal}",
        "source_publication_member_sha256": exact_hash,
        "source_publication_seal_id": f"publication-seal-{ordinal}",
        "source_record_commitment_sha256": exact_hash,
        "text_value": None,
        "unit_key": "USD",
        "value_kind": "numeric",
    }


def test_empty_generation_is_exact_replayable_and_row_factory_neutral(
    tmp_path: Path,
) -> None:
    conn = _empty_database(tmp_path / "empty-projection.db")
    try:
        first = build_canonical_projection_generation(conn, _request())
        replay = build_canonical_projection_generation(conn, _request())
        assert first == replay
        assert first.change_count == 0
        assert first.effective_entry_count == 0
        assert first.batch_count == 0
        assert first.bucket_count == 4096
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM canonical_fact_projection_buckets "
                "WHERE generation_id='generation-empty'"
            ).fetchone()[0]
            == 4096
        )

        conn.row_factory = None
        tuple_verified = verify_canonical_projection_generation(
            conn,
            "generation-empty",
            resolution_snapshot_id="resolution-empty",
            ontology_snapshot_id="ontology-empty",
            cutoff_at=T0,
        )
        conn.row_factory = sqlite3.Row
        row_verified = verify_canonical_projection_generation(
            conn,
            "generation-empty",
            resolution_snapshot_id="resolution-empty",
            ontology_snapshot_id="ontology-empty",
            cutoff_at=T0,
        )
        assert tuple_verified.projection_seal_sha256 == (row_verified.projection_seal_sha256)
    finally:
        conn.close()


def test_strict_verification_streams_large_generation_and_detects_tamper(
    tmp_path: Path,
) -> None:
    database = tmp_path / "streamed-projection-audit.db"
    conn = _empty_database(database)
    try:
        build_canonical_projection_generation(conn, _request())
        _replace_empty_generation_with_deletes(conn, count=2_501)
    finally:
        conn.close()

    raw = sqlite3.connect(database)
    tracked = _FetchTrackingConnection(raw)
    try:
        verified = verify_canonical_projection_generation(
            cast(sqlite3.Connection, tracked),
            "generation-empty",
            resolution_snapshot_id="resolution-empty",
            ontology_snapshot_id="ontology-empty",
            cutoff_at=T0,
        )
        assert verified.change_count == 2_501
        assert verified.tombstone_count == 2_501
        assert max(tracked.requested_fetch_sizes) == 1_000
        assert max(tracked.returned_fetch_sizes) <= 1_000

        raw.execute(
            "UPDATE canonical_fact_projection_entries SET entry_json='{}' "
            "WHERE generation_id='generation-empty' AND entry_ordinal=1200"
        )
        with pytest.raises(
            CanonicalFactProjectionError,
            match="projection_entry_commitment_tampered",
        ):
            verify_canonical_projection_generation(
                cast(sqlite3.Connection, tracked),
                "generation-empty",
                resolution_snapshot_id="resolution-empty",
                ontology_snapshot_id="ontology-empty",
                cutoff_at=T0,
            )
        assert max(tracked.returned_fetch_sizes) <= 1_000
    finally:
        raw.close()


def test_checkpoint_build_streams_skewed_bucket_and_enforces_storage_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "skewed-bucket-build.db"
    conn = _empty_database(database)
    try:
        build_canonical_projection_generation(conn, _request())
        request, write_buckets_and_seal, entry_columns = _reset_empty_generation(conn)
        entries = [
            _synthetic_upsert(request.generation_id, ordinal, bucket=7) for ordinal in range(2_501)
        ]
        _insert_synthetic_entries(conn, request, entry_columns, entries)
        tracked = _FetchTrackingConnection(conn)
        write_buckets_and_seal(cast(sqlite3.Connection, tracked), request)
        conn.commit()

        stored = conn.execute(
            "SELECT entry_count,canonical_entry_set_json,entry_set_sha256 "
            "FROM canonical_fact_projection_buckets "
            "WHERE generation_id=? AND digest_bucket=7",
            (request.generation_id,),
        ).fetchone()
        expected_payload = canonical_json([str(entry["entry_sha256"]) for entry in entries])
        assert stored is not None
        assert tuple(stored) == (
            2_501,
            expected_payload,
            digest_text(expected_payload),
        )
        assert max(tracked.requested_fetch_sizes) == 1_000
        assert max(tracked.returned_fetch_sizes) <= 1_000

        request, write_buckets_and_seal, entry_columns = _reset_empty_generation(conn)
        _insert_synthetic_entries(conn, request, entry_columns, entries)
        monkeypatch.setattr(projection, "MAX_BUCKET_ENTRY_COUNT", 2_500)
        with pytest.raises(
            CanonicalFactProjectionError,
            match="projection_bucket_row_cap_exceeded",
        ):
            write_buckets_and_seal(conn, request)
    finally:
        conn.close()


def test_unchanged_delta_prefetches_parent_state_once_and_resets_scan_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _empty_database(tmp_path / "delta-prefetch-regression.db")
    try:
        build_canonical_projection_generation(conn, _request())
        request, _, entry_columns = _reset_empty_generation(conn)
        sources = [_synthetic_selected_source(ordinal) for ordinal in range(2_001)]
        upsert_entry = cast(
            Callable[[str, int, dict[str, object]], dict[str, object]],
            getattr(projection, "_upsert_entry"),
        )
        parent_entries = [
            upsert_entry(request.generation_id, ordinal, source)
            for ordinal, source in enumerate(sources)
        ]
        conn.executemany(
            "INSERT INTO canonical_fact_projection_entries "
            f"({','.join(entry_columns)}) VALUES "
            f"({','.join('?' for _ in entry_columns)})",
            [tuple(entry[column] for column in entry_columns) for entry in parent_entries],
        )
        delta = ProjectionGenerationRequest(
            generation_id="generation-delta",
            idempotency_key="generation-delta",
            generation_kind="delta",
            parent_generation_id=request.generation_id,
            resolution_snapshot_id=request.resolution_snapshot_id,
            ontology_snapshot_id=request.ontology_snapshot_id,
            cutoff_at=request.cutoff_at,
            recorded_at=request.recorded_at,
            config=ProjectionConfig(
                max_batch_facts=1_000,
                max_batch_milliseconds=10,
            ),
        )

        def selected_rows(
            _conn: sqlite3.Connection,
            _request: ProjectionGenerationRequest,
        ) -> Iterator[dict[str, object]]:
            return iter(sources)

        def deleted_coordinates(
            _conn: sqlite3.Connection,
            _parent: str,
            _snapshot: str,
        ) -> Iterator[str]:
            return iter(())

        monkeypatch.setattr(projection, "_selected_rows_keyset", selected_rows)
        monkeypatch.setattr(
            projection,
            "_deleted_coordinates_batched",
            deleted_coordinates,
        )
        clock = 0.0

        def deterministic_monotonic() -> float:
            nonlocal clock
            clock += 0.000006
            return clock

        monkeypatch.setattr(projection, "_monotonic", deterministic_monotonic)
        effective_state_queries = 0

        def trace_sql(statement: str) -> None:
            nonlocal effective_state_queries
            if "WITH RECURSIVE lineage" in statement:
                effective_state_queries += 1

        conn.set_trace_callback(trace_sql)
        write_entries = cast(
            Callable[[sqlite3.Connection, ProjectionGenerationRequest], None],
            getattr(projection, "_write_entries_and_batches"),
        )
        write_entries(conn, delta)
        conn.set_trace_callback(None)

        assert effective_state_queries == 1
        assert clock > delta.config.max_batch_milliseconds / 1_000
        assert (
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM canonical_fact_projection_entries WHERE generation_id=?",
                    (delta.generation_id,),
                ).fetchone()[0]
            )
            == 0
        )
    finally:
        conn.close()


def test_delta_rejects_parent_from_another_issuer_scope(tmp_path: Path) -> None:
    conn = _empty_database(tmp_path / "scope-mismatch.db")
    try:
        parent = build_canonical_projection_generation(conn, _request("generation-parent"))
        assert parent.resolution_scope_sha256 == EMPTY_SCOPE.scope_sha256
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-other", "issuer-other", "operating_company", T0.isoformat()),
        )
        conn.execute(
            "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
            (
                "reporting-other",
                "reporting-other",
                "issuer-other",
                "legal_registrant",
                "Other issuer",
                T0.isoformat(),
            ),
        )
        other_scope = ResolutionSnapshotScope(
            issuer_id="issuer-other",
            reporting_entity_ids=("reporting-other",),
        )
        CanonicalFactResolutionEngine(conn).seal_snapshot("resolution-other", T0, T0, other_scope)
        bind_resolution_snapshot_watermark(
            conn,
            resolution_snapshot_id="resolution-other",
            cutoff_at=T0,
            recorded_at=T0,
        )
        with pytest.raises(
            CanonicalFactProjectionError,
            match="projection_parent_scope_mismatch",
        ):
            build_canonical_projection_generation(
                conn,
                ProjectionGenerationRequest(
                    generation_id="generation-cross-issuer-delta",
                    idempotency_key="generation-cross-issuer-delta",
                    generation_kind="delta",
                    parent_generation_id=parent.generation_id,
                    resolution_snapshot_id="resolution-other",
                    ontology_snapshot_id="ontology-empty",
                    cutoff_at=T0,
                    recorded_at=T0,
                ),
            )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM canonical_fact_projection_scope_bindings "
                "WHERE generation_id='generation-cross-issuer-delta'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_0255_refuses_inferred_upgrade_and_nonempty_downgrade(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy-populated.db"
    legacy = sqlite3.connect(legacy_path)
    legacy.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE llm_budgets (
            purpose TEXT PRIMARY KEY,
            monthly_cap_usd REAL NOT NULL,
            warn_threshold_pct REAL NOT NULL,
            hard_block INTEGER NOT NULL,
            on_exceed TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            notes TEXT
        );
        """
    )
    legacy.close()
    legacy_config = _config(legacy_path)
    command.stamp(legacy_config, BASE_REVISION)
    command.upgrade(legacy_config, "0254_filing_xbrl_processor_closure")
    legacy = sqlite3.connect(legacy_path)
    legacy.execute("DROP TRIGGER trg_canonical_fact_snapshot_exact")
    empty_json = canonical_json([])
    legacy.execute(
        "INSERT INTO canonical_fact_resolution_snapshot_seals VALUES (?,?,?,?,?,?,?)",
        (
            "legacy-global-snapshot",
            "legacy-global-snapshot",
            T0.isoformat(),
            0,
            empty_json,
            digest_text(empty_json),
            T0.isoformat(),
        ),
    )
    legacy.commit()
    legacy.close()
    with pytest.raises(RuntimeError, match="refuses to infer issuer scope"):
        command.upgrade(legacy_config, HEAD)

    scoped_path = tmp_path / "scoped-populated.db"
    scoped = _empty_database(scoped_path)
    scoped.commit()
    scoped.close()
    with pytest.raises(RuntimeError, match="refuses to discard committed scoped"):
        command.downgrade(_config(scoped_path), "0254_filing_xbrl_processor_closure")


def test_failed_generation_is_atomic_and_replay_conflicts_fail(
    tmp_path: Path,
) -> None:
    conn = _empty_database(tmp_path / "atomic-projection.db")
    try:
        build_canonical_projection_generation(conn, _request())
        conflict = _request().model_copy(update={"recorded_at": datetime(2026, 1, 2, tzinfo=UTC)})
        with pytest.raises(CanonicalFactProjectionError):
            build_canonical_projection_generation(conn, conflict)
        assert (
            conn.execute("SELECT COUNT(*) FROM canonical_fact_projection_generations").fetchone()[0]
            == 1
        )

        bad = _request("bad-generation").model_copy(update={"ontology_snapshot_id": "missing"})
        with pytest.raises(Exception):
            build_canonical_projection_generation(conn, bad)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM canonical_fact_projection_generations "
                "WHERE generation_id='bad-generation'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_canonical_decimal_is_driver_neutral() -> None:
    assert canonical_decimal("100.000") == "100"
    assert canonical_decimal(0) == "0"
    assert canonical_decimal("-0.000") == "0"
    assert canonical_decimal("1E+6") == "1000000"


def test_mixed_query_plan_keeps_metric_and_requested_periods() -> None:
    plan = plan_canonical_fact_query(
        "Revenue growth in 2024 versus 2023 and management's explanation"
    )
    assert plan.metric_terms == ("revenue",)
    assert plan.years == (2023, 2024)
