from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Protocol, Self, cast

import pytest

import provenance.population_source_facts as population
from provenance.population_completeness import PopulationTemporalScope
from provenance.population_source_facts import (
    SourceFactPopulationBatchError,
    SourceFactPopulationRequest,
    populate_source_fact_plane,
    verify_source_fact_ontology,
)
from provenance.source_fact_repository import SourceFactPublication

STAMP = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
CUTOFF = STAMP + timedelta(hours=1)
RECORDED = STAMP + timedelta(hours=2)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _sqlite_sha(value: object) -> str:
    return _sha(str(value))


@pytest.fixture
def conn() -> Generator[sqlite3.Connection, None, None]:
    database = sqlite3.connect(":memory:")
    database.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL
        );
        CREATE TABLE evidence_document_versions (
            document_version_id TEXT PRIMARY KEY,
            observation_id TEXT NOT NULL,
            issuer_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE evidence_source_observations (
            observation_id TEXT PRIMARY KEY,
            observed_at TEXT NOT NULL,
            retrieved_at TEXT NOT NULL
        );
        CREATE TABLE evidence_extraction_runs (
            extraction_run_id TEXT PRIMARY KEY,
            document_version_id TEXT NOT NULL,
            input_sha256 TEXT NOT NULL,
            extractor_name TEXT NOT NULL,
            extractor_config_sha256 TEXT NOT NULL,
            extractor_code_version TEXT NOT NULL,
            output_sha256 TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            outcome TEXT NOT NULL
        );
        CREATE TABLE evidence_nodes (
            node_id TEXT PRIMARY KEY,
            extraction_run_id TEXT NOT NULL,
            locator_json TEXT,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE reported_observations (
            observation_id TEXT PRIMARY KEY,
            concept_key TEXT NOT NULL,
            period_start TEXT,
            period_end TEXT NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            dimensions_json TEXT NOT NULL,
            numeric_value TEXT,
            text_value TEXT,
            currency TEXT,
            unit TEXT,
            observation_status TEXT NOT NULL,
            evidence_node_id TEXT NOT NULL,
            available_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            method TEXT NOT NULL,
            method_version TEXT NOT NULL
        );
        CREATE TABLE fact_observation_revisions (
            fact_table TEXT NOT NULL,
            fact_row_id INTEGER NOT NULL,
            fact_revision INTEGER NOT NULL,
            observation_id TEXT NOT NULL,
            source_document_id INTEGER NOT NULL,
            source_tier TEXT NOT NULL,
            locator_json TEXT,
            captured_at TEXT NOT NULL,
            PRIMARY KEY (fact_table, fact_row_id, fact_revision)
        );
        CREATE TABLE recorded_subject_binding_revisions (
            binding_revision_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            recorded_issuer_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            issuer_id TEXT,
            reporting_entity_id TEXT,
            security_id TEXT,
            outcome TEXT NOT NULL,
            decision_kind TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            reason_details_json TEXT NOT NULL,
            material_dissent INTEGER NOT NULL,
            effective_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            supersedes_binding_revision_id TEXT
        );
        CREATE TABLE fact_cells_v2 (
            fact_cell_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            reporting_entity_id TEXT NOT NULL,
            scope_security_id TEXT,
            concept_namespace TEXT NOT NULL,
            concept_name TEXT NOT NULL,
            taxonomy_name TEXT NOT NULL,
            taxonomy_version TEXT,
            accounting_basis TEXT NOT NULL,
            consolidation_scope TEXT NOT NULL,
            period_kind TEXT NOT NULL,
            period_start TEXT,
            period_end TEXT NOT NULL,
            fiscal_year INTEGER,
            fiscal_period TEXT,
            unit_key TEXT NOT NULL,
            currency TEXT,
            effective_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE fact_cell_identity_seals_v2 (
            fact_cell_id TEXT PRIMARY KEY,
            semantic_key_version TEXT NOT NULL,
            semantic_key_sha256 TEXT NOT NULL,
            semantic_identity_json TEXT NOT NULL,
            dimension_set_json TEXT NOT NULL
        );
        CREATE TABLE fact_dimensions_normalized_v2 (
            dimension_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            fact_cell_id TEXT NOT NULL,
            dimension_ordinal INTEGER NOT NULL,
            axis_namespace TEXT NOT NULL,
            axis_name TEXT NOT NULL,
            member_kind TEXT NOT NULL,
            explicit_member_namespace TEXT,
            explicit_member_name TEXT,
            typed_member_value_json TEXT,
            typed_member_value_sha256 TEXT,
            recorded_at TEXT NOT NULL
        );
        """
    )
    _seed_binding(database, revision=1, reporting_entity_id="reporting-1", stamp=STAMP)
    _seed_observation(database)
    database.commit()
    try:
        yield database
    finally:
        database.close()


def _seed_binding(
    conn: sqlite3.Connection,
    *,
    revision: int,
    reporting_entity_id: str | None,
    stamp: datetime,
) -> None:
    conn.execute(
        "INSERT INTO recorded_subject_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"binding-{revision}",
            f"binding-key-{revision}",
            "issuer-recorded",
            revision,
            "issuer-canonical" if reporting_entity_id is not None else None,
            reporting_entity_id,
            None,
            "selected" if reporting_entity_id is not None else "retired",
            "deterministic",
            "test",
            "{}",
            0,
            stamp.isoformat(),
            stamp.isoformat(),
            stamp.isoformat(),
            None if revision == 1 else f"binding-{revision - 1}",
        ),
    )


def _seed_observation(
    conn: sqlite3.Connection,
    *,
    suffix: str = "1",
    fact_row_id: int = 1,
    fact_revision: int = 1,
    status: str = "reported",
    run_outcome: str = "succeeded",
    dimensions_json: str = "[]",
) -> None:
    document_id = int(suffix)
    run_id = f"run-{suffix}"
    node_id = f"node-{suffix}"
    observation_id = f"observation-{suffix}"
    conn.execute(
        "INSERT INTO documents VALUES (?,?,?)",
        (document_id, "fmp", "sec_10k"),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations VALUES (?,?,?)",
        (f"source-{suffix}", STAMP.isoformat(), RECORDED.isoformat()),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES (?,?,?,?)",
        (
            f"document-{suffix}",
            f"source-{suffix}",
            "issuer-recorded",
            RECORDED.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            f"document-{suffix}",
            _sha(f"input-{suffix}"),
            "test-extractor",
            _sha("config"),
            "test-v1",
            _sha(f"output-{suffix}"),
            STAMP.isoformat(),
            STAMP.isoformat(),
            run_outcome,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_nodes VALUES (?,?,?,?)",
        (node_id, run_id, '{"line":1}', STAMP.isoformat()),
    )
    conn.execute(
        "INSERT INTO reported_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            observation_id,
            "revenue",
            (STAMP - timedelta(days=365)).isoformat(),
            STAMP.isoformat(),
            "annual",
            dimensions_json,
            str(100 + int(suffix)),
            None,
            "USD",
            "USD",
            status,
            node_id,
            STAMP.isoformat(),
            STAMP.isoformat(),
            "test",
            "1",
        ),
    )
    conn.execute(
        "INSERT INTO fact_observation_revisions VALUES (?,?,?,?,?,?,?,?)",
        (
            "financial_facts",
            fact_row_id,
            fact_revision,
            observation_id,
            document_id,
            "primary",
            '{"line":1}',
            STAMP.isoformat(),
        ),
    )


def _request(
    *,
    apply: bool = False,
    after: str | None = None,
    input_sha: str | None = None,
    max_runs: int | None = None,
    output_sha: str | None = None,
) -> SourceFactPopulationRequest:
    return SourceFactPopulationRequest(
        apply=apply,
        data_cutoff_at=CUTOFF,
        operation_recorded_at=RECORDED,
        after_extraction_run_id=after,
        max_runs=max_runs,
        input_commitment_sha256=input_sha,
        planned_output_commitment_sha256=output_sha,
    )


class _RecordingRepository:
    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.publications: list[SourceFactPublication] = []

    def publish(self, publication: SourceFactPublication) -> SimpleNamespace:
        extraction_run_id = publication.extraction_seals[0].extraction_run_id
        if self.fail_on == extraction_run_id:
            raise RuntimeError("injected publish failure")
        self.publications.append(publication)
        return SimpleNamespace(created_record_ids=("created",), exact_replay=False)


class _ReplayAwareRepository(_RecordingRepository):
    def __init__(self) -> None:
        super().__init__()
        self._publications_by_key: dict[str, SourceFactPublication] = {}

    def publish(self, publication: SourceFactPublication) -> SimpleNamespace:
        existing = self._publications_by_key.get(publication.idempotency_key)
        if existing is not None:
            if existing != publication:
                raise ValueError("replayed source publication changed")
            return SimpleNamespace(created_record_ids=(), exact_replay=True)
        self._publications_by_key[publication.idempotency_key] = publication
        return super().publish(publication)


class _PersistingRepository(_RecordingRepository):
    """Minimal target-plane persistence to expose between-run reuse."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self.conn = conn

    def publish(self, publication: SourceFactPublication) -> SimpleNamespace:
        for reported in publication.reported_facts:
            cell = reported.cell
            if (
                self.conn.execute(
                    "SELECT 1 FROM fact_cells_v2 WHERE fact_cell_id=?",
                    (cell.fact_cell_id,),
                ).fetchone()
                is not None
            ):
                continue
            self.conn.execute(
                "INSERT INTO fact_cells_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cell.fact_cell_id,
                    cell.idempotency_key,
                    cell.reporting_entity_id,
                    cell.scope_security_id,
                    cell.concept_namespace,
                    cell.concept_name,
                    cell.taxonomy_name,
                    cell.taxonomy_version,
                    cell.accounting_basis,
                    cell.consolidation_scope,
                    cell.period_kind,
                    cell.period_start.isoformat() if cell.period_start else None,
                    cell.period_end.isoformat(),
                    cell.fiscal_year,
                    cell.fiscal_period,
                    cell.unit_key,
                    cell.currency,
                    cell.effective_at.isoformat(),
                    cell.knowledge_at.isoformat(),
                    cell.recorded_at.isoformat(),
                ),
            )
            self.conn.execute(
                "INSERT INTO fact_cell_identity_seals_v2 VALUES (?,?,?,?,?)",
                (
                    cell.fact_cell_id,
                    cell.semantic_key_version,
                    cell.semantic_key_sha256,
                    cell.semantic_identity_json,
                    cell.dimensions_json,
                ),
            )
        return super().publish(publication)


class _SpillProtocol(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(self, *args: object) -> None: ...

    def add_run(self, run_id: str, first_capture: datetime) -> None: ...

    def add_edge(self, parent_run_id: str, child_run_id: str) -> None: ...

    def topological_run_ids(self) -> tuple[str, ...]: ...


def _install_repository(
    monkeypatch: pytest.MonkeyPatch,
    repository: _RecordingRepository,
) -> None:
    def factory(_conn: sqlite3.Connection) -> _RecordingRepository:
        return repository

    monkeypatch.setattr(population, "SourceFactRepository", factory)


def test_existing_semantic_cell_cache_reuses_validated_envelope_across_batches(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn.row_factory = sqlite3.Row
    source_rows = cast(Callable[..., sqlite3.Cursor], getattr(population, "_source_rows"))
    source_fact_from_row = cast(Callable[..., object], getattr(population, "_source_fact_from_row"))
    fact = source_fact_from_row(
        source_rows(conn, _request()).fetchone(),
        policy_sha=_sha("policy"),
        prior_observation_id=None,
        operation_recorded_at=RECORDED,
    )
    cell = getattr(fact, "cell")
    conn.execute(
        "INSERT INTO fact_cells_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cell.fact_cell_id,
            cell.idempotency_key,
            cell.reporting_entity_id,
            cell.scope_security_id,
            cell.concept_namespace,
            cell.concept_name,
            cell.taxonomy_name,
            cell.taxonomy_version,
            cell.accounting_basis,
            cell.consolidation_scope,
            cell.period_kind,
            cell.period_start,
            cell.period_end,
            cell.fiscal_year,
            cell.fiscal_period,
            cell.unit_key,
            cell.currency,
            cell.effective_at,
            cell.knowledge_at,
            cell.recorded_at,
        ),
    )
    conn.execute(
        "INSERT INTO fact_cell_identity_seals_v2 VALUES (?,?,?,?,?)",
        (
            cell.fact_cell_id,
            cell.semantic_key_version,
            cell.semantic_key_sha256,
            cell.semantic_identity_json,
            cell.dimensions_json,
        ),
    )
    cache = getattr(population, "_SemanticCellCache")()
    resolver = cast(
        Callable[..., tuple[object, ...]], getattr(population, "_reuse_existing_semantic_cells")
    )
    original_existing_cell = getattr(population, "_existing_cell")
    reconstruction_count = 0

    def count_reconstruction(*args: object) -> object:
        nonlocal reconstruction_count
        reconstruction_count += 1
        return original_existing_cell(*args)

    monkeypatch.setattr(population, "_existing_cell", count_reconstruction)
    semantic_selects: list[str] = []
    conn.set_trace_callback(
        lambda statement: (
            semantic_selects.append(statement)
            if "FROM fact_cell_identity_seals_v2 seal" in statement
            else None
        )
    )
    try:
        first = resolver(conn, (fact,), cache)
        second = resolver(conn, (fact,), cache)
    finally:
        conn.set_trace_callback(None)

    assert first == second
    assert first[0] == fact
    assert reconstruction_count == 1
    assert len(semantic_selects) == 1


def test_normalized_dimension_identity_is_scoped_to_fact_cell() -> None:
    recorded_at = datetime(2026, 7, 29, tzinfo=UTC)
    raw = '[{"key":"segment","value":"Cloud"}]'
    dimensions = cast(
        Callable[..., tuple[object, ...]],
        getattr(population, "_dimensions"),
    )

    first = dimensions(raw, recorded_at, fact_cell_id="fact-cell:first")
    replay = dimensions(raw, recorded_at, fact_cell_id="fact-cell:first")
    second = dimensions(raw, recorded_at, fact_cell_id="fact-cell:second")

    assert first == replay
    assert getattr(first[0], "dimension_id") != getattr(second[0], "dimension_id")
    assert getattr(first[0], "idempotency_key") != getattr(
        second[0],
        "idempotency_key",
    )


def test_population_plan_retains_run_metadata_not_observation_graphs(
    conn: sqlite3.Connection,
) -> None:
    plan_builder = cast(
        Callable[[sqlite3.Connection, SourceFactPopulationRequest], object],
        getattr(population, "_population_plan"),
    )

    plan = plan_builder(conn, _request())
    run_plans = cast(dict[str, object], getattr(plan, "run_plans"))

    assert not hasattr(plan, "publications")
    assert len(run_plans) == 1
    assert not hasattr(run_plans["run-1"], "facts")
    assert not hasattr(run_plans["run-1"], "observations")
    assert not hasattr(
        cast(object, getattr(population, "_MutableRunPlan")),
        "parents",
    )


def test_spilled_run_dependencies_preserve_topological_waves() -> None:
    spill_type = cast(
        Callable[[], _SpillProtocol],
        getattr(population, "_RunDependencySpill"),
    )
    run_ids = ("run-a", "run-b", "run-c", "run-d")

    with spill_type() as spill:
        for index, run_id in enumerate(run_ids):
            spill.add_run(run_id, STAMP + timedelta(seconds=index))
        spill.add_edge("run-a", "run-c")
        spill.add_edge("run-b", "run-c")
        spill.add_edge("run-c", "run-d")
        ordered = spill.topological_run_ids()

    assert ordered == ("run-a", "run-b", "run-c", "run-d")


def test_commitment_fold_preencoded_path_is_byte_exact() -> None:
    fold_type = cast(type[object], getattr(population, "_CommitmentFold"))
    payload = {"nested": [1, "two", {"three": True}]}
    direct = cast(object, fold_type("test-namespace"))
    preencoded = cast(object, fold_type("test-namespace"))

    getattr(direct, "add")("reported_fact", payload)
    encoded = getattr(fold_type, "encode")("reported_fact", payload)
    getattr(preencoded, "add_encoded")(encoded)

    assert getattr(direct, "hexdigest")() == getattr(preencoded, "hexdigest")()


def test_dependency_spill_add_run_scales_with_unique_runs(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_observation(conn, suffix="2", fact_row_id=2)
    conn.execute("UPDATE evidence_nodes SET extraction_run_id='run-1' WHERE node_id='node-2'")
    spill_type = getattr(population, "_RunDependencySpill")
    original = getattr(spill_type, "add_run")
    calls: list[str] = []

    def counted_add_run(self: object, run_id: str, first_capture: datetime) -> None:
        calls.append(run_id)
        original(self, run_id, first_capture)

    monkeypatch.setattr(spill_type, "add_run", counted_add_run)

    result = populate_source_fact_plane(conn, _request())

    assert result.eligible_count == 2
    assert calls == ["run-1"]


def test_apply_rejects_spoofed_commitments_before_publish(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _RecordingRepository()
    _install_repository(monkeypatch, repository)
    dry_run = populate_source_fact_plane(conn, _request())

    with pytest.raises(ValueError, match="input commitment"):
        populate_source_fact_plane(
            conn,
            _request(apply=True, input_sha="0" * 64, output_sha="1" * 64),
        )
    with pytest.raises(ValueError, match="planned output commitment"):
        populate_source_fact_plane(
            conn,
            _request(
                apply=True,
                input_sha=dry_run.input_commitment_sha256,
                output_sha="1" * 64,
            ),
        )
    conn.execute(
        "UPDATE reported_observations SET numeric_value='999' WHERE observation_id='observation-1'"
    )
    with pytest.raises(ValueError, match="input commitment"):
        populate_source_fact_plane(
            conn,
            _request(
                apply=True,
                input_sha=dry_run.input_commitment_sha256,
                output_sha=dry_run.planned_output_commitment_sha256,
            ),
        )

    assert repository.publications == []


def test_binding_is_frozen_as_of_cutoff_and_operation_clock(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    future = RECORDED + timedelta(days=1)
    _seed_binding(conn, revision=2, reporting_entity_id="reporting-2", stamp=future)
    repository = _RecordingRepository()
    _install_repository(monkeypatch, repository)

    populate_source_fact_plane(conn, _request(apply=True))

    fact = repository.publications[0].reported_facts[0]
    assert fact.cell.reporting_entity_id == "reporting-1"
    assert fact.observation.subject_binding_revision_id == "binding-1"
    assert fact.observation.recorded_at == RECORDED
    assert repository.publications[0].recorded_at == RECORDED


def test_publication_uses_knowledge_clock_and_is_exactly_replayable(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dry_run = populate_source_fact_plane(conn, _request())
    repository = _ReplayAwareRepository()
    _install_repository(monkeypatch, repository)
    request = _request(
        apply=True,
        input_sha=dry_run.input_commitment_sha256,
        output_sha=dry_run.planned_output_commitment_sha256,
    )

    first = populate_source_fact_plane(conn, request)
    replay = populate_source_fact_plane(conn, request)

    publication = repository.publications[0]
    knowledge_at = publication.extraction_seals[0].knowledge_at
    assert publication.created_at == knowledge_at == STAMP
    assert publication.recorded_at == RECORDED
    assert publication.created_at <= CUTOFF
    assert publication.recorded_at <= RECORDED
    assert publication.created_at <= publication.recorded_at
    assert first.exact_replay_run_count == 0
    assert replay.exact_replay_run_count == 1
    assert len(repository.publications) == 1


def test_population_policy_bump_changes_all_manifest_commitments(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corrected = populate_source_fact_plane(conn, _request())

    assert corrected.policy_version == "5"
    assert getattr(population, "_COMMITMENT_NAMESPACE_VERSION") == "v4"
    monkeypatch.setattr(population, "_POLICY_VERSION", "4")
    monkeypatch.setattr(population, "_COMMITMENT_NAMESPACE_VERSION", "v3")
    legacy = populate_source_fact_plane(conn, _request())

    assert legacy.policy_config_sha256 != corrected.policy_config_sha256
    assert legacy.input_commitment_sha256 != corrected.input_commitment_sha256
    assert legacy.planned_output_commitment_sha256 != corrected.planned_output_commitment_sha256


def test_future_knowledge_is_not_published(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn.execute(
        "UPDATE reported_observations SET available_at=? WHERE observation_id=?",
        ((CUTOFF + timedelta(seconds=1)).isoformat(), "observation-1"),
    )
    repository = _RecordingRepository()
    _install_repository(monkeypatch, repository)

    result = populate_source_fact_plane(conn, _request(apply=True))

    assert result.eligible_count == 0
    assert result.expected_count == 0
    assert repository.publications == []


def test_lineage_uses_nearest_prior_eligible_revision_without_dangling_parent(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn.execute("DELETE FROM fact_observation_revisions")
    conn.execute("DELETE FROM reported_observations")
    conn.execute("DELETE FROM evidence_nodes")
    conn.execute("DELETE FROM evidence_extraction_runs")
    conn.execute("DELETE FROM evidence_document_versions")
    conn.execute("DELETE FROM evidence_source_observations")
    conn.execute("DELETE FROM documents")
    _seed_observation(conn, suffix="1", fact_revision=1, status="derived")
    _seed_observation(conn, suffix="2", fact_revision=2, status="reported")
    _seed_observation(conn, suffix="3", fact_revision=3, status="reported")
    repository = _RecordingRepository()
    _install_repository(monkeypatch, repository)

    populate_source_fact_plane(conn, _request(apply=True))

    observations = [
        fact.observation
        for publication in repository.publications
        for fact in publication.reported_facts
    ]
    assert observations[0].revision_kind == "initial"
    assert observations[0].supersedes_observation_id is None
    assert observations[1].revision_kind == "correction"
    assert observations[1].supersedes_observation_id == observations[0].observation_id


def test_semantic_cell_reuse_loads_actual_dimension_identity(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn.execute(
        "UPDATE reported_observations SET dimensions_json=?",
        ('[{"key":"segment","value":"Cloud"}]',),
    )
    initial = _RecordingRepository()
    _install_repository(monkeypatch, initial)
    populate_source_fact_plane(conn, _request(apply=True))
    planned_cell = initial.publications[0].reported_facts[0].cell
    actual_id = "fact-cell:preexisting"
    actual_dimension_id = "fact-dimension:preexisting"
    dimension = planned_cell.dimensions[0]
    conn.execute(
        "INSERT INTO fact_cells_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            actual_id,
            "fact-cell-key:preexisting",
            planned_cell.reporting_entity_id,
            planned_cell.scope_security_id,
            planned_cell.concept_namespace,
            planned_cell.concept_name,
            planned_cell.taxonomy_name,
            planned_cell.taxonomy_version,
            planned_cell.accounting_basis,
            planned_cell.consolidation_scope,
            planned_cell.period_kind,
            planned_cell.period_start.isoformat() if planned_cell.period_start else None,
            planned_cell.period_end.isoformat(),
            planned_cell.fiscal_year,
            planned_cell.fiscal_period,
            planned_cell.unit_key,
            planned_cell.currency,
            planned_cell.effective_at.isoformat(),
            planned_cell.knowledge_at.isoformat(),
            planned_cell.recorded_at.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO fact_cell_identity_seals_v2 VALUES (?,?,?,?,?)",
        (
            actual_id,
            planned_cell.semantic_key_version,
            planned_cell.semantic_key_sha256,
            planned_cell.semantic_identity_json,
            planned_cell.dimensions_json,
        ),
    )
    conn.execute(
        "INSERT INTO fact_dimensions_normalized_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            actual_dimension_id,
            "fact-dimension-key:preexisting",
            actual_id,
            0,
            dimension.axis_namespace,
            dimension.axis_name,
            dimension.member_kind,
            dimension.explicit_member_namespace,
            dimension.explicit_member_name,
            None,
            None,
            planned_cell.recorded_at.isoformat(),
        ),
    )

    replay = _RecordingRepository()
    _install_repository(monkeypatch, replay)
    populate_source_fact_plane(conn, _request(apply=True))
    replay_cell = replay.publications[0].reported_facts[0].cell

    assert replay_cell.fact_cell_id == actual_id
    assert replay_cell.dimensions[0].dimension_id == actual_dimension_id
    assert replay_cell.dimensions[0].idempotency_key == "fact-dimension-key:preexisting"


def test_semantic_cell_resolution_queries_are_bounded_by_batches(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(2, 7):
        suffix = str(index)
        _seed_observation(conn, suffix=suffix, fact_row_id=index)
        conn.execute(
            "UPDATE reported_observations SET concept_key=? WHERE observation_id=?",
            (f"metric-{suffix}", f"observation-{suffix}"),
        )
    repository = _PersistingRepository(conn)
    _install_repository(monkeypatch, repository)
    populate_source_fact_plane(conn, _request(apply=True))
    monkeypatch.setattr(population, "_SEMANTIC_CELL_BATCH_SIZE", 2)

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        populate_source_fact_plane(conn, _request())
    finally:
        conn.set_trace_callback(None)

    semantic_queries = [
        statement
        for statement in statements
        if "FROM fact_cell_identity_seals_v2 seal" in statement
    ]
    dimension_queries = [
        statement for statement in statements if "FROM fact_dimensions_normalized_v2" in statement
    ]
    assert len(semantic_queries) == 3
    assert len(dimension_queries) == 3


def test_plan_commitments_are_identical_empty_partial_and_full(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_observation(conn, suffix="2", fact_row_id=2)
    conn.execute(
        "UPDATE reported_observations SET concept_key='operating_income' "
        "WHERE observation_id='observation-2'"
    )
    empty = populate_source_fact_plane(conn, _request())
    repository = _PersistingRepository(conn)
    _install_repository(monkeypatch, repository)

    first = populate_source_fact_plane(
        conn,
        _request(
            apply=True,
            input_sha=empty.input_commitment_sha256,
            max_runs=1,
            output_sha=empty.planned_output_commitment_sha256,
        ),
    )
    partial = populate_source_fact_plane(conn, _request())
    populate_source_fact_plane(
        conn,
        _request(
            after=first.last_extraction_run_id,
            apply=True,
            input_sha=empty.input_commitment_sha256,
            output_sha=empty.planned_output_commitment_sha256,
        ),
    )
    full = populate_source_fact_plane(conn, _request())

    assert partial.input_commitment_sha256 == empty.input_commitment_sha256
    assert partial.planned_output_commitment_sha256 == empty.planned_output_commitment_sha256
    assert full.input_commitment_sha256 == empty.input_commitment_sha256
    assert full.planned_output_commitment_sha256 == empty.planned_output_commitment_sha256


def test_batched_semantic_cell_resolution_fails_closed_on_corrupt_seal(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _PersistingRepository(conn)
    _install_repository(monkeypatch, repository)
    populate_source_fact_plane(conn, _request(apply=True))
    conn.execute(
        'UPDATE fact_cell_identity_seals_v2 SET dimension_set_json=\'[{"axis_name":"tampered"}]\''
    )

    with pytest.raises(
        ValueError,
        match="existing semantic cell commitment conflicts",
    ):
        populate_source_fact_plane(conn, _request())


def test_nonsemantic_fiscal_label_cannot_change_later_run_output(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_observation(conn, suffix="2", fact_row_id=2)
    conn.execute(
        "UPDATE reported_observations SET fiscal_period_type='quarter' "
        "WHERE observation_id='observation-2'"
    )
    repository = _PersistingRepository(conn)
    _install_repository(monkeypatch, repository)

    result = populate_source_fact_plane(conn, _request(apply=True))

    assert result.processed_run_count == 2
    assert len(repository.publications) == 2
    first = repository.publications[0].reported_facts[0]
    second = repository.publications[1].reported_facts[0]
    assert first.cell.semantic_key_sha256 == second.cell.semantic_key_sha256
    assert first.cell == second.cell
    assert first.cell.fiscal_period is None
    assert first.observation.source_entry_sha256 != second.observation.source_entry_sha256


def test_failed_extraction_run_is_not_eligible(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn.execute("UPDATE evidence_extraction_runs SET outcome='failed'")
    repository = _RecordingRepository()
    _install_repository(monkeypatch, repository)

    result = populate_source_fact_plane(conn, _request(apply=True))

    assert result.eligible_count == 0
    assert result.exclusion_counts["incomplete_extraction_run"] == 1
    assert repository.publications == []


def test_partial_failure_returns_manifest_bound_checkpoint_and_resumes(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_observation(conn, suffix="2", fact_row_id=2)
    dry_run = populate_source_fact_plane(conn, _request())
    failing = _RecordingRepository(fail_on="run-2")
    _install_repository(monkeypatch, failing)

    with pytest.raises(SourceFactPopulationBatchError) as captured:
        populate_source_fact_plane(
            conn,
            _request(
                apply=True,
                input_sha=dry_run.input_commitment_sha256,
                output_sha=dry_run.planned_output_commitment_sha256,
            ),
        )

    assert captured.value.last_committed_extraction_run_id == "run-1"
    assert captured.value.input_commitment_sha256 == dry_run.input_commitment_sha256
    resumed = _RecordingRepository()
    _install_repository(monkeypatch, resumed)
    result = populate_source_fact_plane(
        conn,
        _request(
            apply=True,
            after="run-1",
            input_sha=dry_run.input_commitment_sha256,
            output_sha=dry_run.planned_output_commitment_sha256,
        ),
    )
    assert result.last_extraction_run_id == "run-2"
    assert len(resumed.publications) == 1


def test_persisted_verifier_ignores_post_observation_clock_revision() -> None:
    conn = sqlite3.connect(":memory:")
    conn.create_function(
        "fact_sha256",
        1,
        _sqlite_sha,
        deterministic=True,
    )
    conn.executescript(
        """
        CREATE TABLE fact_cells_v2 (
            fact_cell_id TEXT PRIMARY KEY, knowledge_at TEXT, recorded_at TEXT
        );
        CREATE TABLE fact_observations_v2 (
            observation_id TEXT PRIMARY KEY, fact_cell_id TEXT,
            observation_kind TEXT, knowledge_at TEXT, recorded_at TEXT
        );
        CREATE TABLE fact_cell_identity_seals_v2 (
            fact_cell_id TEXT PRIMARY KEY, semantic_key_sha256 TEXT, sealed_at TEXT
        );
        CREATE TABLE fact_reported_observation_anchors_v2 (
            observation_id TEXT PRIMARY KEY, extraction_run_id TEXT,
            anchor_payload_sha256 TEXT, recorded_at TEXT
        );
        CREATE TABLE fact_observation_payload_commitments_v2 (
            observation_id TEXT PRIMARY KEY, observation_payload_sha256 TEXT,
            committed_at TEXT
        );
        CREATE TABLE fact_extraction_run_completeness_seals_v2 (
            extraction_run_id TEXT PRIMARY KEY, observation_set_sha256 TEXT,
            knowledge_at TEXT, recorded_at TEXT
        );
        CREATE TABLE source_observation_taxonomy_assertions (
            observation_id TEXT PRIMARY KEY, commitment_sha256 TEXT,
            anchor_payload_sha256 TEXT, extraction_output_sha256 TEXT,
            observation_payload_sha256 TEXT, observation_set_sha256 TEXT,
            knowledge_at TEXT, recorded_at TEXT
        );
        CREATE TABLE fact_cell_canonical_binding_revisions (
            binding_revision_id TEXT PRIMARY KEY, source_observation_id TEXT,
            revision INTEGER, binding_status TEXT, commitment_sha256 TEXT,
            knowledge_at TEXT, recorded_at TEXT
        );
        CREATE TABLE ontology_snapshot_headers (
            ontology_snapshot_id TEXT PRIMARY KEY, cutoff_at TEXT, recorded_at TEXT
        );
        CREATE TABLE ontology_snapshot_seals (
            ontology_snapshot_id TEXT PRIMARY KEY, member_count INTEGER,
            member_set_sha256 TEXT, sealed_at TEXT
        );
        """
    )
    digest = _sha("persisted")
    cutoff_text, recorded_text = CUTOFF.isoformat(), RECORDED.isoformat()
    conn.execute(
        "INSERT INTO fact_cells_v2 VALUES (?,?,?)",
        ("cell", cutoff_text, recorded_text),
    )
    conn.execute(
        "INSERT INTO fact_observations_v2 VALUES (?,?,?,?,?)",
        ("observation", "cell", "reported", cutoff_text, recorded_text),
    )
    conn.execute(
        "INSERT INTO fact_cell_identity_seals_v2 VALUES (?,?,?)",
        ("cell", digest, recorded_text),
    )
    conn.execute(
        "INSERT INTO fact_reported_observation_anchors_v2 VALUES (?,?,?,?)",
        ("observation", "run", digest, recorded_text),
    )
    conn.execute(
        "INSERT INTO fact_observation_payload_commitments_v2 VALUES (?,?,?)",
        ("observation", digest, recorded_text),
    )
    conn.execute(
        "INSERT INTO fact_extraction_run_completeness_seals_v2 VALUES (?,?,?,?)",
        ("run", digest, cutoff_text, recorded_text),
    )
    conn.execute(
        "INSERT INTO source_observation_taxonomy_assertions VALUES (?,?,?,?,?,?,?,?)",
        (
            "observation",
            digest,
            digest,
            digest,
            digest,
            digest,
            cutoff_text,
            recorded_text,
        ),
    )
    conn.execute(
        "INSERT INTO fact_cell_canonical_binding_revisions VALUES (?,?,?,?,?,?,?)",
        ("binding-1", "observation", 1, "bound", digest, cutoff_text, recorded_text),
    )
    conn.execute(
        "INSERT INTO fact_cell_canonical_binding_revisions VALUES (?,?,?,?,?,?,?)",
        (
            "binding-2",
            "observation",
            2,
            "retired",
            digest,
            cutoff_text,
            (RECORDED + timedelta(hours=1)).isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO ontology_snapshot_headers VALUES (?,?,?)",
        ("snapshot", cutoff_text, recorded_text),
    )
    conn.execute(
        "INSERT INTO ontology_snapshot_seals VALUES (?,?,?,?)",
        ("snapshot", 1, digest, recorded_text),
    )

    verification = verify_source_fact_ontology(
        conn,
        PopulationTemporalScope(
            knowledge_cutoff=CUTOFF,
            observed_through=RECORDED,
        ),
    )

    assert verification.materialized_count == 1
    assert verification.failed_count == 0
    assert verification.details["ontology_snapshot_id"] == "snapshot"
