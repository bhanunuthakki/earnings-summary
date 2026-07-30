"""Deterministic corpus and semantic-runtime population gates."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

import provenance.population_retrieval_runtime as population
from provenance.population_completeness import PopulationTemporalScope
from provenance.population_retrieval_runtime import (
    RetrievalRuntimePopulationRequest,
    populate_retrieval_runtime,
    verify_retrieval_runtime,
)
from search.corpus_builder import CorpusBuildResult

STAMP = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
SHA = hashlib.sha256(b"retrieval-runtime-test").hexdigest()


def _database(*, complete_inventory: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE issuer_entities (
            issuer_id TEXT PRIMARY KEY
        );
        CREATE TABLE source_obligation_revisions (
            obligation_revision_id TEXT PRIMARY KEY,
            obligation_key TEXT NOT NULL,
            revision INTEGER NOT NULL,
            issuer_id TEXT NOT NULL,
            reporting_entity_id TEXT,
            document_family TEXT NOT NULL,
            obligation_state TEXT NOT NULL,
            active_from TEXT NOT NULL,
            active_to TEXT,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE source_inventory_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            inventory_key TEXT NOT NULL,
            revision INTEGER NOT NULL,
            issuer_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE source_inventory_snapshot_seals (
            snapshot_id TEXT PRIMARY KEY,
            expected_component_count INTEGER NOT NULL,
            component_digest_sha256 TEXT NOT NULL,
            completion_status TEXT NOT NULL,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE expected_documents (
            expected_document_id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            expected_document_key TEXT NOT NULL,
            issuer_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE expected_document_obligation_bindings (
            expected_document_id TEXT PRIMARY KEY,
            source_obligation_revision_id TEXT NOT NULL,
            issuer_id TEXT NOT NULL,
            reporting_entity_id TEXT,
            document_family TEXT NOT NULL,
            effective_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE source_coverage_assessments (
            assessment_id TEXT PRIMARY KEY,
            expected_document_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            coverage_status TEXT NOT NULL,
            document_version_id TEXT,
            reason_code TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE search_corpus_manifests (
            manifest_id TEXT PRIMARY KEY,
            corpus_key TEXT NOT NULL,
            revision INTEGER NOT NULL,
            knowledge_cutoff TEXT,
            recorded_at TEXT NOT NULL,
            selector_code_version TEXT NOT NULL
        );
        CREATE TABLE search_corpus_manifest_seals (
            manifest_id TEXT PRIMARY KEY,
            completion_status TEXT NOT NULL,
            expected_document_count INTEGER NOT NULL,
            membership_digest_sha256 TEXT NOT NULL,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE search_corpus_document_memberships (
            manifest_id TEXT NOT NULL,
            expected_document_key TEXT NOT NULL,
            document_version_id TEXT,
            membership_status TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        CREATE TABLE search_manifest_source_inventories (
            manifest_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL
        );
        CREATE TABLE search_index_runs (
            index_run_id TEXT PRIMARY KEY,
            manifest_id TEXT NOT NULL,
            index_kind TEXT NOT NULL,
            outcome TEXT NOT NULL,
            config_sha256 TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE TABLE search_projection_seals (
            index_run_id TEXT PRIMARY KEY,
            manifest_id TEXT NOT NULL,
            index_kind TEXT NOT NULL,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE search_embedding_model_promotions (
            promotion_id TEXT PRIMARY KEY,
            purpose TEXT NOT NULL,
            revision INTEGER NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            runtime_artifact_json TEXT,
            runtime_artifact_sha256 TEXT,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        """
    )
    stamp = STAMP.isoformat()
    conn.execute("INSERT INTO issuer_entities VALUES ('issuer')")
    conn.execute(
        "INSERT INTO source_obligation_revisions VALUES "
        "('obligation','issuer:periodic',1,'issuer','entity',"
        "'operating_company_periodic','required',?,NULL,?,?)",
        (stamp, stamp, stamp),
    )
    conn.execute(
        "INSERT INTO source_inventory_snapshots VALUES ('inventory','issuer:sec',1,'issuer',?)",
        (stamp,),
    )
    conn.execute(
        "INSERT INTO source_inventory_snapshot_seals VALUES ('inventory',1,?,?,?)",
        (SHA, "complete" if complete_inventory else "incomplete", stamp),
    )
    conn.execute(
        "INSERT INTO expected_documents VALUES "
        "('expected','inventory','issuer:2026Q2:10-Q','issuer',?)",
        (stamp,),
    )
    conn.execute(
        "INSERT INTO expected_document_obligation_bindings VALUES "
        "('expected','obligation','issuer','entity','operating_company_periodic',?,?,?)",
        (stamp, stamp, stamp),
    )
    conn.execute(
        "INSERT INTO source_coverage_assessments VALUES "
        "('assessment','expected',1,'extracted','document','covered',?,?)",
        (stamp, stamp),
    )
    conn.commit()
    return conn


def test_request_accepts_later_operation_clock_but_rejects_clock_before_cutoff() -> None:
    request = RetrievalRuntimePopulationRequest(
        cutoff_at=STAMP,
        operation_recorded_at=STAMP.replace(hour=13),
    )
    assert request.operation_recorded_at == STAMP.replace(hour=13)

    with pytest.raises(ValidationError, match="operation_recorded_at"):
        RetrievalRuntimePopulationRequest(
            cutoff_at=STAMP,
            operation_recorded_at=STAMP.replace(hour=11),
        )


def test_retrieval_verifier_ignores_artifacts_recorded_after_observation() -> None:
    conn = sqlite3.connect(":memory:")
    recorded = STAMP.replace(hour=13)
    conn.executescript(
        """
        CREATE TABLE source_obligation_revisions (
            obligation_revision_id TEXT,obligation_key TEXT,revision INTEGER,
            issuer_id TEXT,reporting_entity_id TEXT,document_family TEXT,
            obligation_state TEXT,active_from TEXT,active_to TEXT,
            knowledge_at TEXT,recorded_at TEXT
        );
        CREATE TABLE search_corpus_manifests (
            manifest_id TEXT,corpus_key TEXT,revision INTEGER,
            selection_config_sha256 TEXT,knowledge_cutoff TEXT,recorded_at TEXT
        );
        CREATE TABLE search_corpus_manifest_seals (
            manifest_id TEXT,membership_digest_sha256 TEXT,
            completion_status TEXT,sealed_at TEXT
        );
        CREATE TABLE search_projection_seals (
            projection_seal_id TEXT,manifest_id TEXT,index_kind TEXT,
            config_sha256 TEXT,projection_records_sha256 TEXT,
            artifact_set_sha256 TEXT,runtime_artifact_sha256 TEXT,
            provider TEXT,model TEXT,dimensions INTEGER,sealed_at TEXT
        );
        CREATE TABLE search_embedding_model_promotions (
            promotion_id TEXT,purpose TEXT,revision INTEGER,
            provider TEXT,model TEXT,dimensions INTEGER,
            evaluation_artifact_sha256 TEXT,runtime_artifact_sha256 TEXT,
            approved_at TEXT,knowledge_at TEXT,recorded_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO source_obligation_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "obligation",
            "obligation",
            1,
            "issuer",
            "entity",
            "operating_company_periodic",
            "required",
            STAMP.isoformat(),
            None,
            STAMP.isoformat(),
            STAMP.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO search_corpus_manifests VALUES (?,?,?,?,?,?)",
        ("manifest", "issuer", 1, SHA, STAMP.isoformat(), recorded.isoformat()),
    )
    conn.execute(
        "INSERT INTO search_corpus_manifest_seals VALUES (?,?,?,?)",
        ("manifest", SHA, "complete", recorded.isoformat()),
    )
    conn.executemany(
        "INSERT INTO search_projection_seals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            (
                "lexical",
                "manifest",
                "lexical",
                SHA,
                SHA,
                None,
                None,
                None,
                None,
                None,
                recorded.isoformat(),
            ),
            (
                "vector",
                "manifest",
                "vector",
                SHA,
                SHA,
                SHA,
                SHA,
                "provider",
                "model",
                3,
                recorded.isoformat(),
            ),
        ),
    )
    conn.execute(
        "INSERT INTO search_embedding_model_promotions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "promotion",
            population.PURPOSE,
            1,
            "provider",
            "model",
            3,
            SHA,
            SHA,
            recorded.isoformat(),
            recorded.isoformat(),
            recorded.isoformat(),
        ),
    )

    before = verify_retrieval_runtime(
        conn,
        PopulationTemporalScope(
            knowledge_cutoff=STAMP,
            observed_through=STAMP,
        ),
    )
    after = verify_retrieval_runtime(
        conn,
        PopulationTemporalScope(
            knowledge_cutoff=STAMP,
            observed_through=recorded,
        ),
    )

    assert before.failed_count == 1
    assert after.materialized_count == 1


def test_incomplete_inventory_fails_closed_before_corpus_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _database(complete_inventory=False)
    called = False

    def forbidden(*_args: object, **_kwargs: object) -> CorpusBuildResult:
        nonlocal called
        called = True
        raise AssertionError("corpus builder must not run")

    monkeypatch.setattr(population, "build_grounded_search_corpus", forbidden)
    try:
        result = populate_retrieval_runtime(
            conn,
            RetrievalRuntimePopulationRequest(
                cutoff_at=STAMP,
                operation_recorded_at=STAMP,
                apply=True,
                phase="corpus",
            ),
        )
    finally:
        conn.close()

    assert called is False
    assert result.ready_issuer_count == 0
    assert result.failed_issuer_count == 1
    assert result.failed_reason_counts == {"source_inventory_incomplete": 1}


def test_complete_bound_reporting_scope_builds_lexical_corpus_but_not_fake_semantic_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _database()

    def fake_builder(
        connection: sqlite3.Connection,
        request: object,
    ) -> CorpusBuildResult:
        corpus_request = population.CorpusBuildRequest.model_validate(request)
        connection.execute(
            "INSERT INTO search_corpus_manifests VALUES (?,?,?,?,?,?)",
            (
                "manifest",
                corpus_request.corpus_key,
                corpus_request.revision,
                corpus_request.knowledge_cutoff,
                corpus_request.recorded_at,
                corpus_request.selector_code_version,
            ),
        )
        connection.execute(
            "INSERT INTO search_corpus_manifest_seals VALUES ('manifest','complete',1,?,?)",
            (SHA, corpus_request.recorded_at),
        )
        connection.execute(
            "INSERT INTO search_corpus_document_memberships VALUES "
            "('manifest','issuer:2026Q2:10-Q','document','included','coverage:extracted')"
        )
        connection.execute(
            "INSERT INTO search_manifest_source_inventories VALUES ('manifest','inventory')"
        )
        connection.execute(
            "INSERT INTO search_index_runs VALUES ('lexical','manifest','lexical','succeeded',?,?)",
            (SHA, corpus_request.recorded_at),
        )
        return CorpusBuildResult(
            mode="apply",
            manifest_id="manifest",
            lexical_index_run_id="lexical",
            completion_status="complete",
            expected_document_count=1,
            included_document_count=1,
            chunks_planned=1,
            records_created=5,
            records_replayed=0,
            manifest_config_sha256=SHA,
            chunker_config_sha256=SHA,
        )

    monkeypatch.setattr(population, "build_grounded_search_corpus", fake_builder)

    def fake_canaries(
        _connection: sqlite3.Connection,
        *,
        manifest_id: str,
        issuer_id: str,
        family_document_ids: dict[str, tuple[str, ...]],
        cutoff_at: datetime,
    ) -> tuple[population.RetrievalCanaryReceipt, ...]:
        del manifest_id, issuer_id, family_document_ids, cutoff_at
        return (
            population.RetrievalCanaryReceipt(
                document_family="operating_company_periodic",
                query_sha256=SHA,
                hit_set_sha256=SHA,
                hit_count=1,
            ),
        )

    monkeypatch.setattr(
        population,
        "_verify_lexical_canaries",
        fake_canaries,
    )
    try:
        result = populate_retrieval_runtime(
            conn,
            RetrievalRuntimePopulationRequest(
                cutoff_at=STAMP,
                operation_recorded_at=STAMP,
                apply=True,
            ),
        )
    finally:
        conn.close()

    assert result.lexical_manifest_count == 1
    assert result.lexical_canary_count == 1
    assert result.vector_projection_count == 0
    assert result.ready_issuer_count == 0
    assert result.failed_reason_counts == {"embedding_model_not_promoted": 1}
    assert result.input_commitment_sha256 != result.output_commitment_sha256


def test_active_obligation_without_bound_document_is_not_silently_excluded() -> None:
    conn = _database()
    conn.execute("DELETE FROM expected_document_obligation_bindings")
    conn.commit()
    try:
        result = populate_retrieval_runtime(
            conn,
            RetrievalRuntimePopulationRequest(
                cutoff_at=STAMP,
                operation_recorded_at=STAMP,
            ),
        )
    finally:
        conn.close()

    assert result.expected_issuer_count == 1
    assert result.failed_issuer_count == 1
    assert result.failed_reason_counts == {"expected_document_binding_missing": 1}


def test_result_json_includes_exact_per_issuer_coordinates() -> None:
    conn = _database()
    try:
        result = populate_retrieval_runtime(
            conn,
            RetrievalRuntimePopulationRequest(
                cutoff_at=STAMP,
                operation_recorded_at=STAMP,
            ),
        )
    finally:
        conn.close()

    payload = result.model_dump(mode="json")
    assert payload["issuer_results"][0]["issuer_id"] == "issuer"
    assert "manifest_id" in payload["issuer_results"][0]
    assert "vector_index_run_id" in payload["issuer_results"][0]


def test_resume_apply_requires_dry_run_commitments() -> None:
    with pytest.raises(ValidationError, match="commitments"):
        RetrievalRuntimePopulationRequest(
            cutoff_at=STAMP,
            operation_recorded_at=STAMP,
            apply=True,
            after_issuer_id="issuer-a",
        )


def test_population_stops_at_first_failed_issuer_without_advancing_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _database()
    stamp = STAMP.isoformat()
    conn.execute("INSERT INTO issuer_entities VALUES ('issuer-z')")
    conn.execute(
        "INSERT INTO source_obligation_revisions VALUES "
        "('obligation-z','issuer-z:periodic',1,'issuer-z','entity-z',"
        "'operating_company_periodic','required',?,NULL,?,?)",
        (stamp, stamp, stamp),
    )
    conn.commit()
    calls: list[str] = []

    def blocked_builder(
        _connection: sqlite3.Connection,
        request: object,
    ) -> CorpusBuildResult:
        corpus_request = population.CorpusBuildRequest.model_validate(request)
        calls.append(corpus_request.corpus_key)
        raise RuntimeError("deliberate first issuer failure")

    monkeypatch.setattr(population, "build_grounded_search_corpus", blocked_builder)
    preview = populate_retrieval_runtime(
        conn,
        RetrievalRuntimePopulationRequest(
            cutoff_at=STAMP,
            operation_recorded_at=STAMP,
            phase="corpus",
            max_issuers=2,
        ),
    )
    calls.clear()
    try:
        result = populate_retrieval_runtime(
            conn,
            RetrievalRuntimePopulationRequest(
                cutoff_at=STAMP,
                operation_recorded_at=STAMP,
                apply=True,
                phase="corpus",
                max_issuers=2,
                input_commitment_sha256=preview.input_commitment_sha256,
                plan_commitment_sha256=preview.plan_commitment_sha256,
            ),
        )
    finally:
        conn.close()

    assert calls == ["investor-reporting:issuer"]
    assert result.processed_issuer_count == 1
    assert result.last_issuer_id is None
