from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from provenance.immutable_artifact import ImmutableArtifactConflictError
from provenance.latest_state_rehearsal import (
    ArtifactCommitment,
    SemanticQualificationEvidence,
)
from provenance.latest_state_semantic_qualification import (
    SemanticQualificationRequest,
    generate_semantic_qualification_evidence,
    verify_semantic_qualification_current,
)
from provenance.scope_identity import RetrievalScope, derive_retrieval_scope_id

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64


class _FactHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_metric_cell_id: str
    entry_sha256: str


def _scope(
    issuer: str,
    entity: str,
    ticker: str,
    *,
    source_scope_key: str = "investor-research",
) -> RetrievalScope:
    return RetrievalScope(
        scope_id=derive_retrieval_scope_id(
            source_scope_key=source_scope_key,
            issuer_id=issuer,
        ),
        source_scope_key=source_scope_key,
        source_scope_revision_id=f"revision-{issuer}-{source_scope_key}",
        ticker=ticker,
        issuer_id=issuer,
        reporting_entity_id=entity,
    )


def _database(path: Path, scopes: tuple[RetrievalScope, ...], *, included: int = 1) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE latest_governed_fact_entries(
                scope_key TEXT,canonical_metric_cell_id TEXT,canonical_metric_name TEXT,
                period_end TEXT,fact_generation_id TEXT
            );
            CREATE TABLE search_corpus_manifest_seals(
                manifest_id TEXT PRIMARY KEY,expected_document_count INTEGER,
                membership_digest_sha256 TEXT,completion_status TEXT
            );
            CREATE TABLE v_search_corpus_coverage(
                manifest_id TEXT PRIMARY KEY,expected_document_count INTEGER,
                included_document_count INTEGER,missing_document_count INTEGER,
                quarantined_document_count INTEGER
            );
            CREATE TABLE search_chunks(chunk_id TEXT,manifest_id TEXT);
            """
        )
        issuer_ids: set[str] = set()
        for scope in scopes:
            suffix = scope.issuer_id[-1]
            conn.execute(
                "INSERT INTO latest_governed_fact_entries VALUES (?,?,?,?,?)",
                (scope.scope_id, f"cell-{suffix}", "Revenue", "2025-12-31", f"gen-{suffix}"),
            )
            if scope.issuer_id not in issuer_ids:
                issuer_ids.add(scope.issuer_id)
                conn.execute(
                    "INSERT INTO search_corpus_manifest_seals VALUES (?,?,?,?)",
                    (f"manifest-{suffix}", 1, SHA_A, "complete"),
                )
                conn.execute(
                    "INSERT INTO v_search_corpus_coverage VALUES (?,?,?,?,?)",
                    (f"manifest-{suffix}", 1, included, 1 - included, 0),
                )
                conn.execute(
                    "INSERT INTO search_chunks VALUES (?,?)",
                    (f"chunk-{suffix}", f"manifest-{suffix}"),
                )
        conn.commit()
    finally:
        conn.close()


def _request(tmp_path: Path) -> tuple[SemanticQualificationRequest, ArtifactCommitment]:
    index_root = tmp_path / "indexes"
    runtime_root = tmp_path / "runtime"
    index_root.mkdir()
    runtime_root.mkdir()
    request = SemanticQualificationRequest(
        index_root=index_root.resolve(),
        runtime_root=runtime_root.resolve(),
        exact_row_cap=100,
        fact_canary_limit=10,
        max_fact_canary_milliseconds=1_000,
        max_issuer_qualification_milliseconds=1_000,
    )
    path = tmp_path / "semantic-request.json"
    path.write_text(request.model_dump_json(), encoding="utf-8")
    return request, ArtifactCommitment.from_path(path)


def _install_ready_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scopes: tuple[RetrievalScope, ...],
    registry_path: Path,
    mutate_registry: bool = False,
) -> None:
    ready_scopes: list[SimpleNamespace] = []
    for ordinal, scope in enumerate(scopes, start=1):
        suffix = scope.issuer_id[-1]
        bundle = SimpleNamespace(
            corpus_manifest_id=f"manifest-{suffix}",
            lexical_index_run_id=f"lexical-{suffix}",
            vector_index_run_id=f"vector-{suffix}",
            embedding_promotion_id="embedding-1",
        )
        promotion = SimpleNamespace(
            promotion_id=f"ask-promotion-{ordinal}",
            scope_id=scope.scope_id,
            source_scope_key=scope.source_scope_key,
            source_scope_revision_id=scope.source_scope_revision_id,
            issuer_id=scope.issuer_id,
            reporting_entity_id=scope.reporting_entity_id,
            research_snapshot_id=f"research-{ordinal}",
            fact_generation_id=f"gen-{suffix}",
            fact_projection_seal_sha256=SHA_B,
            source_inventory_ids=(f"inventory-{ordinal}",),
            narrative_bundles=(bundle,),
        )
        ready_scopes.append(SimpleNamespace(scope=scope, promotion=promotion))

    def load_scopes(
        _conn: sqlite3.Connection,
        _path: Path,
        *,
        requested_tickers: tuple[str, ...] | None = None,
        registry_payload: bytes | None = None,
    ) -> tuple[RetrievalScope, ...]:
        del requested_tickers, registry_payload
        return scopes

    def assess(
        _conn: sqlite3.Connection,
        _scopes: tuple[RetrievalScope, ...],
        *,
        runtime: object = None,
    ) -> SimpleNamespace:
        del runtime
        return SimpleNamespace(outcome="ready", scopes=tuple(ready_scopes))

    monkeypatch.setattr(
        "provenance.latest_state_semantic_qualification.load_production_scopes",
        load_scopes,
    )
    monkeypatch.setattr(
        "provenance.latest_state_semantic_qualification.assess_retrieval_readiness",
        assess,
    )
    ordered_issuer_scopes = tuple(
        sorted(
            {scope.issuer_id: scope for scope in scopes}.values(),
            key=lambda item: item.issuer_id,
        )
    )
    calls = 0

    def qualify(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        scope = ordered_issuer_scopes[calls]
        suffix = scope.issuer_id[-1]
        calls += 1
        if mutate_registry and calls == 1:
            registry_path.write_text('{"changed":true}', encoding="utf-8")
        lexical = SimpleNamespace(
            document_family="annual_securities_report",
            query_sha256=SHA_A,
            hit_set_sha256=SHA_B,
            hit_count=1,
        )
        semantic = SimpleNamespace(
            query_sha256=SHA_A,
            candidate_set_sha256=SHA_B,
            backend_receipt_sha256=SHA_A,
            candidate_count=1,
            seed_chunk_id=f"chunk-{suffix}",
            seed_candidate_rank=1,
        )
        issuer = SimpleNamespace(
            issuer_id=scope.issuer_id,
            expected_obligation_count=1,
            expected_document_count=1,
            sealed_inventory_count=1,
            manifest_id=f"manifest-{suffix}",
            lexical_index_run_id=f"lexical-{suffix}",
            vector_index_run_id=f"vector-{suffix}",
            embedding_promotion_id="embedding-1",
            lexical_canaries=(lexical,),
            semantic_canary=semantic,
            outcome="ready",
            reason_codes=(),
        )
        return SimpleNamespace(
            mode="dry_run",
            phase="qualify",
            expected_issuer_count=len(ordered_issuer_scopes),
            processed_issuer_count=1,
            ready_issuer_count=1,
            failed_issuer_count=0,
            issuer_results=(issuer,),
        )

    monkeypatch.setattr(
        "provenance.latest_state_semantic_qualification.populate_retrieval_runtime",
        qualify,
    )

    def embedding_promotion(_conn: sqlite3.Connection) -> SimpleNamespace:
        return SimpleNamespace(
            promotion_id="embedding-1",
            runtime_registration_id="runtime-registration-1",
            runtime_artifact_sha256=SHA_A,
        )

    monkeypatch.setattr(
        "provenance.latest_state_semantic_qualification.current_promotion",
        embedding_promotion,
    )

    def projection_seal(
        _conn: sqlite3.Connection,
        *,
        index_run_id: str,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            index_run_id=index_run_id,
            projection_seal_id=f"seal-{index_run_id}",
            projection_records_sha256=SHA_A,
            artifact_set_sha256=SHA_B,
            runtime_artifact_sha256=SHA_A,
        )

    monkeypatch.setattr(
        "provenance.latest_state_semantic_qualification.load_projection_seal",
        projection_seal,
    )

    def facts(*_args: object, **kwargs: object) -> tuple[_FactHit, ...]:
        generation_id = str(kwargs["generation_id"])
        suffix = generation_id[-1]
        return (
            _FactHit(
                canonical_metric_cell_id=f"cell-{suffix}",
                entry_sha256=SHA_A,
            ),
        )

    monkeypatch.setattr(
        "provenance.latest_state_semantic_qualification.search_canonical_facts",
        facts,
    )


def test_generator_binds_composite_scopes_full_corpus_runtime_and_grounded_canaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes = tuple(
        sorted(
            (_scope("issuer-1", "entity-1", "AAA"), _scope("issuer-2", "entity-2", "BBB")),
            key=lambda item: item.scope_id,
        )
    )
    database = tmp_path / "candidate.db"
    _database(database, scopes)
    registry = tmp_path / "registry.json"
    registry.write_text('{"registry":"composite"}', encoding="utf-8")
    request, request_artifact = _request(tmp_path)
    _install_ready_dependencies(monkeypatch, scopes=scopes, registry_path=registry)

    evidence = generate_semantic_qualification_evidence(
        database_path=database,
        registry_path=registry,
        cutoff_at=NOW,
        operation_recorded_at=NOW,
        request=request,
        request_artifact=request_artifact,
    )

    assert evidence.production_scope_ids == tuple(scope.scope_id for scope in scopes)
    assert [item.source_scope_key for item in evidence.scope_qualifications] == [
        "investor-research",
        "investor-research",
    ]
    assert {item.issuer_id for item in evidence.scope_qualifications} == {
        "issuer-1",
        "issuer-2",
    }
    assert evidence.corpus_document_count == 2
    assert evidence.grounded_fact_canary_count == 2
    assert evidence.grounded_narrative_canary_count == 4
    assert evidence.failure_count == 0
    assert evidence.request_artifact == request_artifact
    assert evidence.registry_artifact.verify()
    assert database.read_bytes()


def test_generator_shares_issuer_runtime_without_collapsing_distinct_source_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes = tuple(
        sorted(
            (
                _scope("issuer-1", "entity-1", "AAA"),
                _scope(
                    "issuer-1",
                    "entity-1",
                    "AAA",
                    source_scope_key="issuer-filings",
                ),
            ),
            key=lambda item: item.scope_id,
        )
    )
    database = tmp_path / "candidate.db"
    _database(database, scopes)
    registry = tmp_path / "registry.json"
    registry.write_text('{"registry":"composite"}', encoding="utf-8")
    request, request_artifact = _request(tmp_path)
    _install_ready_dependencies(monkeypatch, scopes=scopes, registry_path=registry)

    evidence = generate_semantic_qualification_evidence(
        database_path=database,
        registry_path=registry,
        cutoff_at=NOW,
        operation_recorded_at=NOW,
        request=request,
        request_artifact=request_artifact,
    )

    assert len(evidence.issuer_qualifications) == 1
    assert len(evidence.scope_qualifications) == 2
    assert len(set(evidence.production_scope_ids)) == 2
    assert {item.source_scope_key for item in evidence.scope_qualifications} == {
        "investor-research",
        "issuer-filings",
    }
    assert {item.corpus_manifest_id for item in evidence.scope_qualifications} == {
        evidence.issuer_qualifications[0].corpus_manifest_id
    }
    assert evidence.corpus_document_count == 1
    assert evidence.grounded_narrative_canary_count == 2


def test_generator_rejects_incomplete_corpus_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes = (_scope("issuer-1", "entity-1", "AAA"),)
    database = tmp_path / "candidate.db"
    _database(database, scopes, included=0)
    registry = tmp_path / "registry.json"
    registry.write_text("{}", encoding="utf-8")
    request, request_artifact = _request(tmp_path)
    _install_ready_dependencies(monkeypatch, scopes=scopes, registry_path=registry)

    with pytest.raises(ValueError, match="full nonempty corpus"):
        generate_semantic_qualification_evidence(
            database_path=database,
            registry_path=registry,
            cutoff_at=NOW,
            operation_recorded_at=NOW,
            request=request,
            request_artifact=request_artifact,
        )


def test_generator_rejects_registry_replacement_during_qualification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes = (_scope("issuer-1", "entity-1", "AAA"),)
    database = tmp_path / "candidate.db"
    _database(database, scopes)
    registry = tmp_path / "registry.json"
    registry.write_text("{}", encoding="utf-8")
    request, request_artifact = _request(tmp_path)
    _install_ready_dependencies(
        monkeypatch,
        scopes=scopes,
        registry_path=registry,
        mutate_registry=True,
    )

    with pytest.raises(ImmutableArtifactConflictError, match="immutable artifact changed"):
        generate_semantic_qualification_evidence(
            database_path=database,
            registry_path=registry,
            cutoff_at=NOW,
            operation_recorded_at=NOW,
            request=request,
            request_artifact=request_artifact,
        )


def test_request_rejects_noncanonical_or_missing_runtime_roots(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(ValueError, match="existing canonical directories"):
        SemanticQualificationRequest(
            index_root=missing,
            runtime_root=missing,
            exact_row_cap=100,
            fact_canary_limit=10,
            max_fact_canary_milliseconds=1_000,
            max_issuer_qualification_milliseconds=1_000,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("max_fact_canary_milliseconds", float("inf")),
        ("max_issuer_qualification_milliseconds", 60_001),
    ),
)
def test_request_rejects_nonfinite_or_policy_bypassing_latency_ceiling(
    tmp_path: Path,
    field: str,
    value: float,
) -> None:
    index_root = tmp_path / "indexes"
    runtime_root = tmp_path / "runtime"
    index_root.mkdir()
    runtime_root.mkdir()
    fields: dict[str, object] = {
        "index_root": index_root.resolve(),
        "runtime_root": runtime_root.resolve(),
        "exact_row_cap": 100,
        "fact_canary_limit": 10,
        "max_fact_canary_milliseconds": 1_000,
        "max_issuer_qualification_milliseconds": 1_000,
    }
    fields[field] = value

    with pytest.raises(ValueError):
        SemanticQualificationRequest.model_validate(fields)


def test_current_verifier_rejects_any_non_timing_semantic_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes = (_scope("issuer-1", "entity-1", "AAA"),)
    database = tmp_path / "candidate.db"
    _database(database, scopes)
    registry = tmp_path / "registry.json"
    registry.write_text("{}", encoding="utf-8")
    request, request_artifact = _request(tmp_path)
    _install_ready_dependencies(monkeypatch, scopes=scopes, registry_path=registry)
    evidence = generate_semantic_qualification_evidence(
        database_path=database,
        registry_path=registry,
        cutoff_at=NOW,
        operation_recorded_at=NOW,
        request=request,
        request_artifact=request_artifact,
    )

    def unchanged_generator(**_kwargs: object) -> SemanticQualificationEvidence:
        return evidence

    monkeypatch.setattr(
        "provenance.latest_state_semantic_qualification.generate_semantic_qualification_evidence",
        unchanged_generator,
    )
    drifted = evidence.model_copy(update={"database_sha256": SHA_B})

    with pytest.raises(ValueError, match="inputs or grounded results changed"):
        verify_semantic_qualification_current(
            database_path=database,
            registry_path=registry,
            cutoff_at=NOW,
            operation_recorded_at=NOW,
            request=request,
            evidence=drifted,
        )
