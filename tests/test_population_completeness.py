# pyright: reportPrivateUsage=false
"""Full-universe population receipt meta-gate."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

from provenance.population_completeness import (
    _CUTOVER_WRITE_AUTHORITY,
    REQUIRED_CUTOVER_AUDIT_GATES,
    REQUIRED_POPULATION_PLANES,
    PopulationAuditReceipt,
    PopulationCompletenessLedger,
    PopulationParityReceipt,
    PopulationPlaneName,
    PopulationPlaneReceipt,
    PopulationRun,
    PopulationTemporalScope,
    canonical_json,
    digest_text,
    population_run_identity,
    stream_population_artifact_set,
)

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
SHA = hashlib.sha256(b"population-test").hexdigest()
SCOPE = PopulationTemporalScope(knowledge_cutoff=STAMP, observed_through=STAMP)
RUN_ID = population_run_identity(SHA, SHA, SCOPE)


_MIGRATED_DB_BUILDER: Callable[..., Path] | None = None


@pytest.fixture(autouse=True)
def _bind_migrated_db(migrated_db: Callable[..., Path]) -> None:
    global _MIGRATED_DB_BUILDER
    _MIGRATED_DB_BUILDER = migrated_db


def _seed_embedding_governance(conn: sqlite3.Connection) -> tuple[str, str, str]:
    from provenance.evidence_ledger import (
        ContentBlob,
        DocumentVersion,
        EvidenceLedger,
        EvidenceNode,
        ExtractionRun,
        SourceObservation,
    )
    from search.embedding_eval import (
        DEFAULT_CANDIDATES,
        CandidateEvaluationCoordinate,
        CandidateMetrics,
        EmbeddingRecommendationArtifact,
        EvalThresholds,
    )
    from search.embedding_promotion import (
        EmbeddingApprovalReceipt,
        persist_evaluation_receipt,
        persist_promotion,
        promotion_from_evaluation,
        receipt_from_evaluation,
    )
    from search.embedding_runtime_artifact import (
        EmbeddingRuntimeArtifact,
        RuntimeArtifactFile,
        RuntimeComponentVersion,
    )
    from search.embedding_runtime_registration import (
        persist_runtime_registration,
        registration_from_artifact,
    )
    from search.grounded import (
        CorpusDocumentMembership,
        CorpusManifest,
        CorpusManifestSeal,
        EmbeddingArtifact,
        GroundedSearchStore,
        IndexMembership,
        IndexRun,
        SearchChunk,
        membership_digest,
    )

    ts = STAMP.isoformat()
    models = tuple(sorted(DEFAULT_CANDIDATES))
    runtimes = {
        model: registration_from_artifact(
            EmbeddingRuntimeArtifact(
                provider="fastembed",
                model=model,
                dimensions=768 if "base" in model else 384,
                execution_provider="CPUExecutionProvider",
                execution_settings=(),
                component_versions=(
                    RuntimeComponentVersion(component="fastembed", version="1.2.3"),
                ),
                files=(
                    RuntimeArtifactFile(
                        logical_name="model.onnx",
                        role="model",
                        size_bytes=3,
                        sha256=("b" if "base" in model else "c") * 64,
                    ),
                ),
            ),
            registered_at=STAMP,
            purpose="evidence_vector_retrieval",
        )
        for model in models
    }
    winner = models[1]
    winner_runtime = runtimes[winner]
    seal_ids = {models[0]: "seal-0", models[1]: "projection-1"}
    eval_artifact = EmbeddingRecommendationArtifact(
        purpose="evidence_vector_retrieval",
        golden_sha256=SHA,
        k=10,
        thresholds=EvalThresholds(minimum_cases=30),
        results=tuple(
            CandidateMetrics(
                model=model,
                case_count=30,
                recall_at_k=0.9 if model == winner else 0.8,
                mrr=0.8 if model == winner else 0.7,
                ndcg=0.82 if model == winner else 0.75,
                mean_latency_ms=30,
                coverage=1,
                runtime_artifact_sha256=runtimes[model].runtime_artifact_sha256,
            )
            for model in models
        ),
        candidate_coordinates=tuple(
            CandidateEvaluationCoordinate(
                model=model,
                index_run_id=f"run-{idx}",
                manifest_id="manifest-1",
                projection_seal_id=seal_ids[model],
                projection_records_sha256=SHA,
                artifact_set_sha256=SHA,
                config_sha256=SHA,
                chunk_count=1,
                chunk_set_sha256=SHA,
                runtime_registration_id=runtimes[model].runtime_registration_id,
                runtime_artifact_sha256=runtimes[model].runtime_artifact_sha256,
                sealed_at=STAMP,
            )
            for idx, model in enumerate(models)
        ),
        evaluated_at=STAMP,
        recommended_model=winner,
        reason="test promotion",
    )
    eval_receipt = receipt_from_evaluation(eval_artifact, evaluated_at=STAMP)
    approval = EmbeddingApprovalReceipt(
        purpose="evidence_vector_retrieval",
        decision="approved",
        evaluation_artifact_sha256=eval_receipt.evaluation_artifact_sha256,
        approved_model=winner,
        approved_by="test",
        approved_at=STAMP,
        rationale="test promotion",
    )
    promotion = promotion_from_evaluation(
        eval_artifact,
        runtime_registration=winner_runtime,
        approval=approval,
        revision=1,
    )
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=SHA,
            byte_size=4,
            media_type="text/plain",
            storage_uri="file:///pop",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="obs-pop",
            idempotency_key="obs-pop",
            source_kind="sec",
            source_url="https://sec.test/pop",
            blob_sha256=SHA,
            source_published_at=STAMP,
            filing_at=STAMP,
            accepted_at=STAMP,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=SHA,
            collector_code_version="collector@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="doc-pop",
            document_key="ACME:10-Q",
            version_sequence=1,
            observation_id="obs-pop",
            blob_sha256=SHA,
            issuer_id="issuer",
            ticker="ACME",
            document_type="filing",
            form_type="10-Q",
            language="en",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id="extract-pop",
            idempotency_key="extract-pop",
            document_version_id="doc-pop",
            input_sha256=SHA,
            extractor_name="parser",
            extractor_config_sha256=SHA,
            extractor_code_version="parser@1",
            output_sha256=SHA,
            started_at=STAMP,
            completed_at=STAMP,
            outcome="succeeded",
        )
    )
    ledger.persist(
        EvidenceNode(
            node_id="node-pop",
            evidence_key="ACME:node",
            revision=1,
            extraction_run_id="extract-pop",
            node_kind="passage",
            text="Revenue",
            recorded_at=STAMP,
        )
    )
    store = GroundedSearchStore(conn)
    store.persist(
        CorpusManifest(
            manifest_id="manifest-1",
            idempotency_key="manifest-1",
            corpus_key="corpus-1",
            revision=1,
            selection_config_sha256=SHA,
            selector_code_version="selector@1",
            recorded_at=STAMP,
        )
    )
    member = CorpusDocumentMembership(
        membership_id="manifest-1:doc-pop",
        manifest_id="manifest-1",
        expected_document_key="ACME:10-Q",
        document_version_id="doc-pop",
        membership_status="included",
        reason="eligible",
        recorded_at=STAMP,
    )
    store.persist(member)
    chunk = SearchChunk(
        chunk_id="chunk-pop",
        idempotency_key="chunk-pop",
        manifest_id="manifest-1",
        evidence_node_id="node-pop",
        chunk_key="ACME:chunk:0",
        chunk_revision=1,
        text="Revenue",
        char_start=0,
        char_end=7,
        chunker_config_sha256=SHA,
        chunker_code_version="chunker@1",
        available_at=STAMP,
        recorded_at=STAMP,
    )
    store.persist(chunk)
    store.persist(
        CorpusManifestSeal(
            manifest_id="manifest-1",
            expected_document_count=1,
            membership_digest_sha256=membership_digest([member]),
            completion_status="complete",
            sealed_at=STAMP,
        )
    )
    for runtime in runtimes.values():
        persist_runtime_registration(conn, runtime)
    for idx, model in enumerate(models):
        run_id = f"run-{idx}"
        runtime = runtimes[model]
        dims = 768 if "base" in model else 384
        store.persist(
            IndexRun(
                index_run_id=run_id,
                idempotency_key=run_id,
                index_key=f"index-{idx}",
                revision=1,
                manifest_id="manifest-1",
                index_kind="vector",
                config_sha256=SHA,
                code_version="indexer@1",
                outcome="succeeded",
                started_at=STAMP,
                completed_at=STAMP,
            )
        )
        store.persist(
            IndexMembership(
                index_run_id=run_id,
                chunk_id="chunk-pop",
                membership_status="included",
                recorded_at=STAMP,
            )
        )
        store.persist(
            EmbeddingArtifact(
                embedding_artifact_id=f"embed-pop-{idx}",
                idempotency_key=f"embed-pop-{idx}",
                index_run_id=run_id,
                chunk_id="chunk-pop",
                purpose="grounded_search",
                provider="fastembed",
                model=model,
                dimensions=dims,
                vector_sha256=SHA,
                storage_uri=f"vector://embed-pop-{idx}",
                input_sha256=chunk.content_sha256 or SHA,
                request_config_sha256=SHA,
                runtime_artifact_sha256=runtime.runtime_artifact_sha256,
                outcome="succeeded",
                started_at=STAMP,
                completed_at=STAMP,
            )
        )
        conn.execute(
            "INSERT INTO search_projection_seals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                seal_ids[model],
                seal_ids[model],
                run_id,
                "manifest-1",
                "vector",
                1,
                SHA,
                SHA,
                SHA,
                "fastembed",
                model,
                dims,
                SHA,
                f"file:///{seal_ids[model]}",
                ts,
                runtime.runtime_artifact_sha256,
            ),
        )
    persist_evaluation_receipt(conn, eval_receipt)
    persist_promotion(conn, promotion)
    conn.commit()
    return (
        winner_runtime.runtime_registration_id,
        eval_receipt.evaluation_receipt_id,
        promotion.promotion_id,
    )


_EMBEDDING_IDS: tuple[str, str, str] | None = None


def _database(tmp_path: Path) -> sqlite3.Connection:
    global _EMBEDDING_IDS
    from sqlite_runtime import register_sqlite_integrity_functions

    path = tmp_path / "population-receipt.db"
    assert _MIGRATED_DB_BUILDER is not None
    _MIGRATED_DB_BUILDER(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    register_sqlite_integrity_functions(conn)
    _EMBEDDING_IDS = _seed_embedding_governance(conn)
    return conn


def _run() -> PopulationRun:
    return PopulationRun(
        population_run_id=RUN_ID,
        idempotency_key=RUN_ID,
        policy_name="full_universe_population",
        policy_version="1",
        policy_config_sha256=SHA,
        source_snapshot_sha256=SHA,
        temporal_scope=SCOPE,
        verified_at=STAMP,
    )


def _plane(
    name: str,
    *,
    failed: int = 0,
    output_sha256: str = SHA,
) -> PopulationPlaneReceipt:
    result: dict[str, JsonValue] = {"failed_count": failed, "plane_name": name}
    if name == "retrieval_runtime":
        reg_id, eval_id, promo_id = _EMBEDDING_IDS or ("runtime-1", "evaluation-1", "promotion-1")
        result["governance"] = {
            "evaluation_receipt_id": eval_id,
            "evaluation_evaluated_at": STAMP.isoformat(),
            "promotion_id": promo_id,
            "promotion_recorded_at": STAMP.isoformat(),
            "projection_seal_ids": ["projection-1"],
            "projection_sealed_at": {"projection-1": STAMP.isoformat()},
            "runtime_registered_at": STAMP.isoformat(),
            "runtime_registration_id": reg_id,
        }
    return PopulationPlaneReceipt(
        population_run_id=RUN_ID,
        plane_name=cast(PopulationPlaneName, name),
        expected_count=1,
        materialized_count=1 - failed,
        excluded_count=0,
        failed_count=failed,
        input_commitment_sha256=SHA,
        output_commitment_sha256=output_sha256,
        status="blocked" if failed else "complete",
        details=cast(
            dict[str, JsonValue],
            {
                "artifact_sets": [
                    {
                        "row_count": 1,
                        "rows_sha256": SHA,
                        "selection_policy_id": f"{name}.v1",
                        "table": f"{name}_artifacts",
                    }
                ],
                "exclusion_counts": {},
                "result": result,
                "temporal_scope": SCOPE.model_dump(mode="json"),
                "verifier": {
                    "code_sha256": SHA,
                    "name": f"{name}-verifier",
                    "result_sha256": digest_text(canonical_json(result)),
                    "version": "1",
                },
            },
        ),
        temporal_scope=SCOPE,
        verified_at=STAMP,
    )


def _parity(*, absent: int = 0) -> PopulationParityReceipt:
    return PopulationParityReceipt(
        population_run_id=RUN_ID,
        eligible_legacy_count=1,
        canonical_count=1 - absent,
        matched_count=1 - absent,
        mismatched_count=0,
        absent_count=absent,
        extra_count=0,
        status="blocked" if absent else "complete",
        report={"absent_count": absent},
        temporal_scope=SCOPE,
        verified_at=STAMP,
    )


def _audit() -> PopulationAuditReceipt:
    coverage: list[dict[str, JsonValue]] = [
        {
            "eligible_count": 1,
            "failed_count": 0,
            "gate": gate,
            "verified_count": 1,
        }
        for gate in REQUIRED_CUTOVER_AUDIT_GATES
    ]
    gate_evidence: list[dict[str, JsonValue]] = []
    for gate in sorted(REQUIRED_CUTOVER_AUDIT_GATES):
        tables: list[dict[str, JsonValue]] = [
            {"row_count": 1, "rows_sha256": SHA, "table": f"{gate}_table"}
        ]
        gate_evidence.append(
            {
                "gate": gate,
                "gate_evidence_sha256": digest_text(
                    canonical_json({"gate": gate, "tables": tables})
                ),
                "tables": cast(JsonValue, tables),
            }
        )
    watermark_material: dict[str, JsonValue] = {
        "knowledge_cutoff": STAMP.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "observed_through": STAMP.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "gates": cast(
            JsonValue,
            [
                {
                    "gate": item["gate"],
                    "gate_evidence_sha256": item["gate_evidence_sha256"],
                }
                for item in gate_evidence
            ],
        ),
    }
    return PopulationAuditReceipt(
        population_run_id=RUN_ID,
        verifier_name="population-cutover-readiness-auditor",
        verifier_version="2",
        verifier_code_sha256=SHA,
        verifier_config_sha256=SHA,
        temporal_scope=SCOPE,
        verified_at=STAMP,
        required_gate_count=13,
        eligible_count=13,
        verified_count=13,
        failed_count=0,
        evidence={
            "coverage": cast(JsonValue, coverage),
            "findings": cast(JsonValue, []),
            "gate_evidence": cast(JsonValue, gate_evidence),
            "has_blockers": False,
            "schema_version": "data-cutover-readiness-audit/v1",
            "tables_present": cast(JsonValue, []),
            "watermark_material": watermark_material,
            "watermark_sha256": digest_text(canonical_json(watermark_material)),
        },
    )


def test_seal_requires_all_seven_nonempty_planes_and_exact_parity(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    try:
        ledger = PopulationCompletenessLedger(conn)
        assert not hasattr(ledger, "record_verified_cutover")
        receipt = ledger._record_verified_cutover(
            run=_run(),
            planes=tuple(_plane(name) for name in REQUIRED_POPULATION_PLANES),
            parity=_parity(),
            audit=_audit(),
            sealed_at=STAMP,
            authority=_CUTOVER_WRITE_AUTHORITY,
        )
        conn.commit()

        assert receipt.required_plane_count == 7
        assert receipt.complete_plane_count == 7
        assert receipt.audit_receipt_sha256 == _audit().receipt_sha256
        assert ledger.verify(RUN_ID) == receipt
        assert ledger._record_run(_run()) is False
        assert ledger._record_plane(_plane(REQUIRED_POPULATION_PLANES[0])) is False
    finally:
        conn.close()


def test_seal_fails_closed_for_missing_or_blocked_work(tmp_path: Path) -> None:
    conn = _database(tmp_path)
    try:
        ledger = PopulationCompletenessLedger(conn)
        with pytest.raises(RuntimeError, match="direct population sealing is disabled"):
            ledger.seal(RUN_ID, sealed_at=STAMP)
        with pytest.raises(ValueError, match="exactly the seven"), conn:
            ledger._record_verified_cutover(
                run=_run(),
                planes=tuple(_plane(name) for name in REQUIRED_POPULATION_PLANES[:-1]),
                parity=_parity(),
                audit=_audit(),
                sealed_at=STAMP,
                authority=_CUTOVER_WRITE_AUTHORITY,
            )
    finally:
        conn.close()


def test_empty_green_receipt_and_append_only_mutation_are_rejected(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    try:
        ledger = PopulationCompletenessLedger(conn)
        ledger._record_run(_run())
        with pytest.raises(ValueError, match="greater than 0"):
            PopulationPlaneReceipt(
                population_run_id=RUN_ID,
                plane_name="identity_scope",
                expected_count=0,
                materialized_count=0,
                excluded_count=0,
                failed_count=0,
                input_commitment_sha256=SHA,
                output_commitment_sha256=SHA,
                status="complete",
                details={
                    "artifact_sets": [
                        {
                            "row_count": 0,
                            "rows_sha256": SHA,
                            "selection_policy_id": "identity.v1",
                            "table": "identity_artifacts",
                        }
                    ],
                    "exclusion_counts": {},
                    "result": {},
                    "temporal_scope": SCOPE.model_dump(mode="json"),
                    "verifier": {
                        "code_sha256": SHA,
                        "name": "identity-verifier",
                        "result_sha256": digest_text(canonical_json({})),
                        "version": "1",
                    },
                },
                temporal_scope=SCOPE,
                verified_at=STAMP,
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE population_run_headers SET policy_version='2' WHERE population_run_id=?",
                (RUN_ID,),
            )
    finally:
        conn.close()


def test_zero_materialized_or_unapproved_exclusions_cannot_be_green() -> None:
    with pytest.raises(ValueError, match="status"):
        PopulationPlaneReceipt(
            population_run_id=RUN_ID,
            plane_name="identity_scope",
            expected_count=1,
            materialized_count=0,
            excluded_count=1,
            failed_count=0,
            input_commitment_sha256=SHA,
            output_commitment_sha256=SHA,
            status="complete",
            details={
                "artifact_sets": [
                    {
                        "row_count": 0,
                        "rows_sha256": SHA,
                        "selection_policy_id": "identity.v1",
                        "table": "identity_artifacts",
                    }
                ],
                "exclusion_counts": {},
                "result": {},
                "temporal_scope": SCOPE.model_dump(mode="json"),
                "verifier": {
                    "code_sha256": SHA,
                    "name": "identity-verifier",
                    "result_sha256": digest_text(canonical_json({})),
                    "version": "1",
                },
            },
            temporal_scope=SCOPE,
            verified_at=STAMP,
        )


def test_plane_result_commitment_and_internal_write_authority_are_enforced(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="result commitment"):
        PopulationPlaneReceipt.model_validate(
            {
                **_plane("identity_scope").model_dump(),
                "details": {
                    "artifact_sets": _plane("identity_scope").details["artifact_sets"],
                    "exclusion_counts": {},
                    "result": {"changed": True},
                    "temporal_scope": SCOPE.model_dump(mode="json"),
                    "verifier": {
                        "code_sha256": SHA,
                        "name": "identity-verifier",
                        "result_sha256": SHA,
                        "version": "1",
                    },
                },
            }
        )

    conn = _database(tmp_path)
    try:
        ledger = PopulationCompletenessLedger(conn)
        with pytest.raises(RuntimeError, match="internal verifier authority"):
            ledger._record_verified_cutover(
                run=_run(),
                planes=tuple(_plane(name) for name in REQUIRED_POPULATION_PLANES),
                parity=_parity(),
                audit=_audit(),
                sealed_at=STAMP,
                authority=object(),
            )
        assert conn.execute("SELECT COUNT(*) FROM population_run_headers").fetchone() == (0,)
    finally:
        conn.close()


def test_existing_cutover_replay_recomputes_every_semantic_commitment(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    try:
        ledger = PopulationCompletenessLedger(conn)
        planes = tuple(_plane(name) for name in REQUIRED_POPULATION_PLANES)
        ledger._record_verified_cutover(
            run=_run(),
            planes=planes,
            parity=_parity(),
            audit=_audit(),
            sealed_at=STAMP,
            authority=_CUTOVER_WRITE_AUTHORITY,
        )
        changed = tuple(
            _plane(name, output_sha256=("b" * 64 if name == "research_snapshot" else SHA))
            for name in REQUIRED_POPULATION_PLANES
        )

        with pytest.raises(ValueError, match="research_snapshot plane commitment"):
            ledger._verify_fresh_cutover(
                run=_run(),
                planes=changed,
                parity=_parity(),
                audit=_audit(),
                authority=_CUTOVER_WRITE_AUTHORITY,
            )
    finally:
        conn.close()


def test_audit_requires_exact_13_gate_evidence_and_full_watermark() -> None:
    valid = _audit()
    missing_gate_evidence = dict(valid.evidence)
    coverage = list(cast(list[dict[str, object]], missing_gate_evidence["coverage"]))
    missing_gate_evidence["coverage"] = cast(JsonValue, coverage[:-1])
    with pytest.raises(ValueError, match="exactly 13 gates"):
        PopulationAuditReceipt.model_validate(
            {
                **valid.model_dump(exclude={"evidence"}),
                "evidence": missing_gate_evidence,
            }
        )

    tampered = dict(valid.evidence)
    gate_evidence = list(cast(list[dict[str, object]], tampered["gate_evidence"]))
    first = dict(gate_evidence[0])
    tables = list(cast(list[dict[str, object]], first["tables"]))
    tables[0] = {**tables[0], "rows_sha256": "b" * 64}
    first["tables"] = tables
    gate_evidence[0] = first
    tampered["gate_evidence"] = cast(JsonValue, gate_evidence)
    with pytest.raises(ValueError, match="gate evidence commitment"):
        PopulationAuditReceipt.model_validate(
            {
                **valid.model_dump(exclude={"evidence"}),
                "evidence": tampered,
            }
        )


def test_database_triggers_reject_missing_verifier_result_and_audit_gates(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    try:
        ledger = PopulationCompletenessLedger(conn)
        ledger._record_run(_run())
        invalid_details = canonical_json(
            {
                "exclusion_counts": {},
                "verifier": {
                    "code_sha256": SHA,
                    "name": "identity-verifier",
                    "result_sha256": SHA,
                    "version": "1",
                },
            }
        )
        with pytest.raises(sqlite3.IntegrityError, match="plane receipt mismatch"):
            conn.execute(
                "INSERT INTO population_plane_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    RUN_ID,
                    "identity_scope",
                    1,
                    1,
                    0,
                    0,
                    SHA,
                    SHA,
                    "complete",
                    invalid_details,
                    digest_text(invalid_details),
                    STAMP,
                    STAMP,
                    STAMP,
                ),
            )

        audit = _audit()
        invalid_evidence = dict(audit.evidence)
        invalid_evidence["coverage"] = cast(
            JsonValue,
            cast(list[JsonValue], invalid_evidence["coverage"])[:-1],
        )
        evidence_json = canonical_json(invalid_evidence)
        with pytest.raises(sqlite3.IntegrityError, match="audit receipt mismatch"):
            conn.execute(
                "INSERT INTO population_cutover_audit_receipts VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    RUN_ID,
                    audit.verifier_name,
                    audit.verifier_version,
                    audit.verifier_code_sha256,
                    audit.verifier_config_sha256,
                    audit.cutoff_at,
                    audit.temporal_scope.observed_through,
                    audit.audited_at,
                    audit.required_gate_count,
                    audit.eligible_count,
                    audit.verified_count,
                    audit.failed_count,
                    evidence_json,
                    digest_text(evidence_json),
                    audit.canonical_receipt_json,
                    audit.receipt_sha256,
                ),
            )

        negative_evidence = dict(audit.evidence)
        negative_coverage = [
            dict(row) for row in cast(list[dict[str, JsonValue]], negative_evidence["coverage"])
        ]
        negative_coverage[0]["eligible_count"] = -1
        negative_coverage[0]["verified_count"] = -1
        negative_coverage[1]["eligible_count"] = 3
        negative_coverage[1]["verified_count"] = 3
        negative_evidence["coverage"] = cast(JsonValue, negative_coverage)
        negative_evidence_json = canonical_json(negative_evidence)
        negative_evidence_sha = digest_text(negative_evidence_json)
        negative_receipt = cast(
            dict[str, JsonValue],
            json.loads(audit.canonical_receipt_json),
        )
        negative_receipt["evidence_sha256"] = negative_evidence_sha
        negative_receipt_json = canonical_json(negative_receipt)
        with pytest.raises(sqlite3.IntegrityError, match="audit receipt mismatch"):
            conn.execute(
                "INSERT INTO population_cutover_audit_receipts VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    RUN_ID,
                    audit.verifier_name,
                    audit.verifier_version,
                    audit.verifier_code_sha256,
                    audit.verifier_config_sha256,
                    audit.cutoff_at,
                    audit.temporal_scope.observed_through,
                    audit.audited_at,
                    audit.required_gate_count,
                    audit.eligible_count,
                    audit.verified_count,
                    audit.failed_count,
                    negative_evidence_json,
                    negative_evidence_sha,
                    negative_receipt_json,
                    digest_text(negative_receipt_json),
                ),
            )
    finally:
        conn.close()


def test_streamed_audit_watermark_binds_full_row_state_not_only_counts() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE evidence_rows ("
        "evidence_id TEXT PRIMARY KEY,payload_sha256 TEXT NOT NULL,"
        "seal_sha256 TEXT NOT NULL,knowledge_at TEXT NOT NULL,recorded_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO evidence_rows VALUES (?,?,?,?,?)",
        ("evidence-1", SHA, SHA, STAMP.isoformat(), STAMP.isoformat()),
    )
    query = (
        "SELECT evidence_id artifact_id,fact_sha256(payload_sha256) payload_sha256,"
        "seal_sha256,"
        "knowledge_at,recorded_at FROM evidence_rows "
        "WHERE datetime(knowledge_at)<=datetime(?) AND datetime(recorded_at)<=datetime(?) "
        "ORDER BY evidence_id"
    )
    first = stream_population_artifact_set(
        conn,
        table="evidence_rows",
        query=query,
        params=(STAMP.isoformat(), STAMP.isoformat()),
        selection_policy_id="evidence-rows.K-knowledge.O-recorded.v1",
        fetch_size=1,
    )
    conn.execute(
        "UPDATE evidence_rows SET payload_sha256=? WHERE evidence_id=?",
        ("b" * 64, "evidence-1"),
    )
    second = stream_population_artifact_set(
        conn,
        table="evidence_rows",
        query=query,
        params=(STAMP.isoformat(), STAMP.isoformat()),
        selection_policy_id="evidence-rows.K-knowledge.O-recorded.v1",
        fetch_size=1,
    )

    assert first.row_count == second.row_count == 1
    assert first.rows_sha256 != second.rows_sha256


def test_clockless_child_watermark_uses_exact_governed_parent_cutoff() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE canonical_fact_resolution_snapshot_seals (
            resolution_snapshot_id TEXT PRIMARY KEY,
            cutoff_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE canonical_fact_resolution_snapshot_members (
            resolution_snapshot_id TEXT NOT NULL,
            member_ordinal INTEGER NOT NULL,
            member_sha256 TEXT NOT NULL,
            seal_sha256 TEXT NOT NULL
        );
        """
    )
    future = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    conn.executemany(
        "INSERT INTO canonical_fact_resolution_snapshot_seals VALUES (?,?,?)",
        (
            ("snapshot-cutoff", STAMP.isoformat(), STAMP.isoformat()),
            ("snapshot-future", future.isoformat(), future.isoformat()),
        ),
    )
    conn.executemany(
        "INSERT INTO canonical_fact_resolution_snapshot_members VALUES (?,?,?,?)",
        (
            ("snapshot-cutoff", 0, "a" * 64, SHA),
            ("snapshot-future", 0, "b" * 64, SHA),
        ),
    )
    query = (
        "SELECT member.resolution_snapshot_id||':'||member.member_ordinal artifact_id,"
        "member.member_sha256 payload_sha256,member.seal_sha256 seal_sha256,"
        "parent.cutoff_at knowledge_at,parent.recorded_at recorded_at "
        "FROM canonical_fact_resolution_snapshot_members member "
        "JOIN canonical_fact_resolution_snapshot_seals parent USING (resolution_snapshot_id) "
        "WHERE datetime(parent.cutoff_at)<=datetime(?) "
        "AND datetime(parent.recorded_at)<=datetime(?) "
        "ORDER BY member.resolution_snapshot_id,member.member_ordinal"
    )
    baseline = stream_population_artifact_set(
        conn,
        table="canonical_fact_resolution_snapshot_members",
        query=query,
        params=(STAMP.isoformat(), STAMP.isoformat()),
        selection_policy_id="resolution-members.K-parent.O-parent.v1",
        fetch_size=1,
    )
    conn.execute(
        "UPDATE canonical_fact_resolution_snapshot_members "
        "SET member_sha256=? WHERE resolution_snapshot_id=?",
        ("c" * 64, "snapshot-future"),
    )
    future_changed = stream_population_artifact_set(
        conn,
        table="canonical_fact_resolution_snapshot_members",
        query=query,
        params=(STAMP.isoformat(), STAMP.isoformat()),
        selection_policy_id="resolution-members.K-parent.O-parent.v1",
        fetch_size=1,
    )
    conn.execute(
        "UPDATE canonical_fact_resolution_snapshot_members "
        "SET member_sha256=? WHERE resolution_snapshot_id=?",
        ("d" * 64, "snapshot-cutoff"),
    )
    cutoff_changed = stream_population_artifact_set(
        conn,
        table="canonical_fact_resolution_snapshot_members",
        query=query,
        params=(STAMP.isoformat(), STAMP.isoformat()),
        selection_policy_id="resolution-members.K-parent.O-parent.v1",
        fetch_size=1,
    )

    assert baseline.row_count == future_changed.row_count == 1
    assert baseline.rows_sha256 == future_changed.rows_sha256
    assert cutoff_changed.rows_sha256 != baseline.rows_sha256


def test_audit_metadata_contract_rejects_inconsistent_schema_findings_and_tables() -> None:
    valid = _audit()
    invalid_schema = json.loads(canonical_json(valid.evidence))
    invalid_schema["schema_version"] = "invented/v99"
    with pytest.raises(ValueError, match="schema version"):
        PopulationAuditReceipt.model_validate(
            {**valid.model_dump(exclude={"evidence"}), "evidence": invalid_schema}
        )

    invalid_tables = json.loads(canonical_json(valid.evidence))
    invalid_tables["tables_present"] = ["z_table", "a_table", "a_table"]
    with pytest.raises(ValueError, match="sorted and unique"):
        PopulationAuditReceipt.model_validate(
            {**valid.model_dump(exclude={"evidence"}), "evidence": invalid_tables}
        )

    invalid_findings = json.loads(canonical_json(valid.evidence))
    invalid_findings["findings"] = [
        {
            "code": "BLOCKER",
            "count": 1,
            "query_context": "SELECT 1",
            "remediation": "hard-stop",
            "samples": [],
            "severity": "blocker",
        }
    ]
    with pytest.raises(ValueError, match="blocking findings"):
        PopulationAuditReceipt.model_validate(
            {**valid.model_dump(exclude={"evidence"}), "evidence": invalid_findings}
        )


def test_audit_trigger_rejects_self_hashed_inconsistent_receipt_metadata(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    try:
        ledger = PopulationCompletenessLedger(conn)
        ledger._record_run(_run())
        audit = _audit()
        receipt_payload = json.loads(audit.canonical_receipt_json)
        receipt_payload["verifier_name"] = "fabricated-verifier"
        receipt_json = canonical_json(receipt_payload)
        with pytest.raises(sqlite3.IntegrityError, match="audit receipt mismatch"):
            conn.execute(
                "INSERT INTO population_cutover_audit_receipts VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    RUN_ID,
                    audit.verifier_name,
                    audit.verifier_version,
                    audit.verifier_code_sha256,
                    audit.verifier_config_sha256,
                    audit.cutoff_at,
                    audit.temporal_scope.observed_through,
                    audit.audited_at,
                    audit.required_gate_count,
                    audit.eligible_count,
                    audit.verified_count,
                    audit.failed_count,
                    audit.canonical_evidence_json,
                    audit.evidence_sha256,
                    receipt_json,
                    digest_text(receipt_json),
                ),
            )
    finally:
        conn.close()
