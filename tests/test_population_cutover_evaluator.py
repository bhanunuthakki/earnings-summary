# pyright: reportPrivateUsage=false
"""Authority-owned population cutover evaluator and seal writer."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import JsonValue

from provenance.integrity_audit import (
    CutoverAuditOptions,
    CutoverGateCandidateCommitment,
    CutoverGateCoverage,
    CutoverReadinessSummary,
)
from provenance.population_completeness import (
    REQUIRED_CUTOVER_AUDIT_GATES,
    REQUIRED_POPULATION_PLANES,
    PopulationArtifactSetCommitment,
    PopulationCompletenessLedger,
    PopulationParityReceipt,
    PopulationPlaneName,
    PopulationPlaneVerification,
    PopulationTemporalScope,
    canonical_json,
    digest_text,
)
from provenance.population_cutover import (
    PopulationCutoverBlockedError,
    PopulationCutoverEvaluationRequest,
    build_population_audit_receipt,
    evaluate_population_cutover,
)

STAMP = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
SHA = hashlib.sha256(b"population-evaluator-test").hexdigest()
SCOPE = PopulationTemporalScope(knowledge_cutoff=STAMP, observed_through=STAMP)


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


def _database(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> sqlite3.Connection:
    global _EMBEDDING_IDS
    from sqlite_runtime import register_sqlite_integrity_functions

    path = migrated_db(tmp_path / "population-evaluator.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    register_sqlite_integrity_functions(conn)
    _EMBEDDING_IDS = _seed_embedding_governance(conn)
    return conn


def _verification(name: PopulationPlaneName, *, failed: int = 0) -> PopulationPlaneVerification:
    artifact = PopulationArtifactSetCommitment(
        table=f"{name}_artifacts",
        row_count=1 - failed,
        rows_sha256=SHA,
        selection_policy_id=f"{name}.v1",
    )
    details: dict[str, JsonValue] = {"plane_name": name}
    if name == "retrieval_runtime":
        reg_id, eval_id, promo_id = _EMBEDDING_IDS or ("runtime-1", "evaluation-1", "promotion-1")
        details["governance"] = {
            "evaluation_receipt_id": eval_id,
            "evaluation_evaluated_at": STAMP.isoformat(),
            "promotion_id": promo_id,
            "promotion_recorded_at": STAMP.isoformat(),
            "projection_seal_ids": ["projection-1"],
            "projection_sealed_at": {"projection-1": STAMP.isoformat()},
            "runtime_registered_at": STAMP.isoformat(),
            "runtime_registration_id": reg_id,
        }
    material = {
        "artifact_sets": [artifact.model_dump(mode="json")],
        "details": details,
        "exclusion_counts": {},
        "expected_count": 1,
        "failed_count": failed,
        "materialized_count": 1 - failed,
        "plane_name": name,
    }
    return PopulationPlaneVerification(
        plane_name=name,
        expected_count=1,
        materialized_count=1 - failed,
        excluded_count=0,
        failed_count=failed,
        exclusion_counts={},
        input_commitment_sha256=SHA,
        output_commitment_sha256=digest_text(canonical_json(material)),
        artifact_sets=(artifact,),
        details=details,
    )


def _summary(*, changed_gate_sha: str | None = None) -> CutoverReadinessSummary:
    return CutoverReadinessSummary(
        knowledge_cutoff=STAMP,
        observed_through=STAMP,
        generated_at=STAMP,
        coverage=tuple(
            CutoverGateCoverage(gate=gate, eligible_count=1, verified_count=1, failed_count=0)
            for gate in REQUIRED_CUTOVER_AUDIT_GATES
        ),
        candidate_commitments=tuple(
            CutoverGateCandidateCommitment(
                gate=gate,
                selection_policy_id=f"{gate}.K.O.v1",
                row_count=1,
                rows_sha256=(changed_gate_sha if index == 0 and changed_gate_sha else SHA),
            )
            for index, gate in enumerate(REQUIRED_CUTOVER_AUDIT_GATES)
        ),
        findings=(),
        has_blockers=False,
        tables_present=("a", "b"),
    )


def _parity(population_run_id: str) -> PopulationParityReceipt:
    return PopulationParityReceipt(
        population_run_id=population_run_id,
        eligible_legacy_count=1,
        canonical_count=1,
        matched_count=1,
        mismatched_count=0,
        absent_count=0,
        extra_count=0,
        status="complete",
        report={"selection_policy_id": "fixture"},
        temporal_scope=SCOPE,
        verified_at=STAMP,
    )


def _request(
    *,
    apply: bool,
    source_snapshot_sha256: str | None = SHA,
    evaluated_at: datetime = STAMP,
    sealed_at: datetime = STAMP,
) -> PopulationCutoverEvaluationRequest:
    return PopulationCutoverEvaluationRequest(
        temporal_scope=SCOPE,
        policy_config_sha256=SHA,
        source_snapshot_sha256=source_snapshot_sha256,
        evaluated_at=evaluated_at,
        sealed_at=sealed_at,
        apply=apply,
    )


def _fake_verifier(
    plane: PopulationPlaneName,
) -> Callable[[sqlite3.Connection, PopulationTemporalScope], PopulationPlaneVerification]:
    def verify(
        _conn: sqlite3.Connection,
        _scope: PopulationTemporalScope,
    ) -> PopulationPlaneVerification:
        return _verification(plane)

    return verify


def _failed_projection(
    _conn: sqlite3.Connection,
    _scope: PopulationTemporalScope,
) -> PopulationPlaneVerification:
    return _verification("canonical_projection", failed=1)


def _marker_verifier(
    plane: PopulationPlaneName,
    seen: list[str],
    *,
    on_first_read: Callable[[], None] | None = None,
) -> Callable[[sqlite3.Connection, PopulationTemporalScope], PopulationPlaneVerification]:
    def verify(
        conn: sqlite3.Connection,
        _scope: PopulationTemporalScope,
    ) -> PopulationPlaneVerification:
        row = conn.execute("SELECT value FROM candidate_marker WHERE id=1").fetchone()
        assert row is not None
        marker = str(row[0])
        seen.append(marker)
        if len(seen) == 1 and on_first_read is not None:
            on_first_read()
        artifact = PopulationArtifactSetCommitment(
            table=f"{plane}_artifacts",
            row_count=1,
            rows_sha256=digest_text(marker),
            selection_policy_id=f"{plane}.v1",
        )
        details: dict[str, JsonValue] = {"marker": marker, "plane_name": plane}
        if plane == "retrieval_runtime":
            details["governance"] = _verification(plane).details["governance"]
        material = {
            "artifact_sets": [artifact.model_dump(mode="json")],
            "details": details,
            "exclusion_counts": {},
            "expected_count": 1,
            "failed_count": 0,
            "materialized_count": 1,
            "plane_name": plane,
        }
        return PopulationPlaneVerification(
            plane_name=plane,
            expected_count=1,
            materialized_count=1,
            excluded_count=0,
            failed_count=0,
            exclusion_counts={},
            input_commitment_sha256=digest_text(f"input:{marker}"),
            output_commitment_sha256=digest_text(canonical_json(material)),
            artifact_sets=(artifact,),
            details=details,
        )

    return verify


def _patch_marker_verifiers(
    monkeypatch: pytest.MonkeyPatch,
    seen: list[str],
    *,
    on_first_read: Callable[[], None] | None = None,
) -> None:
    import provenance.population_cutover as cutover

    monkeypatch.setattr(
        cutover,
        "_PLANE_VERIFIERS",
        tuple(
            (
                plane,
                _marker_verifier(plane, seen, on_first_read=on_first_read),
                "provenance/population_identity.py",
            )
            for plane in REQUIRED_POPULATION_PLANES
        ),
    )
    monkeypatch.setattr(cutover, "audit_cutover_readiness", _fake_audit)
    monkeypatch.setattr(cutover, "verify_full_universe_legacy_parity", _fake_parity)


def _fake_audit(
    _conn: sqlite3.Connection,
    _options: CutoverAuditOptions,
) -> CutoverReadinessSummary:
    return _summary()


def _fake_parity(
    _conn: sqlite3.Connection,
    *,
    population_run_id: str,
    scope: PopulationTemporalScope,
    verified_at: datetime,
    page_size: int,
    max_pages: int,
    max_rows_per_issuer: int,
) -> PopulationParityReceipt:
    del scope, verified_at, page_size, max_pages, max_rows_per_issuer
    return _parity(population_run_id)


def _patch_green(monkeypatch: pytest.MonkeyPatch) -> None:
    import provenance.population_cutover as cutover

    verifiers: tuple[
        tuple[
            PopulationPlaneName,
            Callable[[sqlite3.Connection, PopulationTemporalScope], PopulationPlaneVerification],
            str,
        ],
        ...,
    ] = tuple(
        (
            plane,
            _fake_verifier(plane),
            "provenance/population_identity.py",
        )
        for name in REQUIRED_POPULATION_PLANES
        for plane in (name,)
    )
    monkeypatch.setattr(cutover, "_PLANE_VERIFIERS", verifiers)
    monkeypatch.setattr(cutover, "audit_cutover_readiness", _fake_audit)
    monkeypatch.setattr(cutover, "verify_full_universe_legacy_parity", _fake_parity)


def test_audit_receipt_binds_exact_candidate_row_state() -> None:
    first = build_population_audit_receipt(_summary(), population_run_id="run", verified_at=STAMP)
    changed = build_population_audit_receipt(
        _summary(changed_gate_sha="b" * 64), population_run_id="run", verified_at=STAMP
    )
    assert first.required_gate_count == 13
    assert first.eligible_count == first.verified_count == 13
    assert first.evidence_sha256 != changed.evidence_sha256


def test_evaluator_dry_run_is_read_only_and_apply_atomically_seals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _database(tmp_path, migrated_db)
    _patch_green(monkeypatch)
    try:
        dry_run = evaluate_population_cutover(
            conn,
            _request(apply=False, source_snapshot_sha256=None),
            trusted_now=STAMP,
        )
        assert dry_run.status == "ready"
        assert dry_run.cutover is None
        assert conn.execute("SELECT COUNT(*) FROM population_run_headers").fetchone() == (0,)

        request = _request(
            apply=True,
            source_snapshot_sha256=dry_run.run.source_snapshot_sha256,
        )
        applied = evaluate_population_cutover(conn, request, trusted_now=STAMP)
        replay = evaluate_population_cutover(conn, request, trusted_now=STAMP)
        assert applied.status == "sealed"
        assert replay == applied
        assert applied.cutover is not None
        assert (
            PopulationCompletenessLedger(conn).verify(applied.run.population_run_id)
            == applied.cutover
        )
    finally:
        conn.close()


def test_apply_requires_a_verified_source_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _database(tmp_path, migrated_db)
    _patch_green(monkeypatch)
    try:
        with pytest.raises(PopulationCutoverBlockedError, match="source snapshot"):
            evaluate_population_cutover(
                conn,
                _request(apply=True, source_snapshot_sha256=None),
                trusted_now=STAMP,
            )
        assert conn.execute("SELECT COUNT(*) FROM population_run_headers").fetchone() == (0,)
    finally:
        conn.close()


def test_future_clocks_are_rejected_before_any_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    import provenance.population_cutover as cutover

    conn = _database(tmp_path, migrated_db)
    called = False

    def unexpected(
        _conn: sqlite3.Connection,
        _scope: PopulationTemporalScope,
    ) -> PopulationPlaneVerification:
        nonlocal called
        called = True
        return _verification("identity_scope")

    monkeypatch.setattr(
        cutover,
        "_PLANE_VERIFIERS",
        (("identity_scope", unexpected, "provenance/population_identity.py"),),
    )
    future = STAMP + timedelta(minutes=6)
    try:
        with pytest.raises(PopulationCutoverBlockedError, match="trusted clock"):
            evaluate_population_cutover(
                conn,
                _request(
                    apply=False,
                    source_snapshot_sha256=None,
                    evaluated_at=future,
                    sealed_at=future,
                ),
                trusted_now=STAMP,
            )
        assert called is False
    finally:
        conn.close()


def test_dry_run_keeps_one_snapshot_across_a_concurrent_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _database(tmp_path, migrated_db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE candidate_marker(id INTEGER PRIMARY KEY,value TEXT NOT NULL)")
    conn.execute("INSERT INTO candidate_marker VALUES (1,'before')")
    conn.commit()
    seen: list[str] = []

    def mutate_live_database() -> None:
        contender = sqlite3.connect(tmp_path / "population-evaluator.db", timeout=2)
        try:
            contender.execute("UPDATE candidate_marker SET value='after' WHERE id=1")
            contender.commit()
        finally:
            contender.close()

    _patch_marker_verifiers(monkeypatch, seen, on_first_read=mutate_live_database)
    try:
        result = evaluate_population_cutover(
            conn,
            _request(apply=False, source_snapshot_sha256=None),
            trusted_now=STAMP,
        )
        assert result.status == "ready"
        assert seen == ["before"] * len(REQUIRED_POPULATION_PLANES)
        assert conn.execute("SELECT value FROM candidate_marker WHERE id=1").fetchone() == (
            "after",
        )
    finally:
        conn.close()


def test_apply_rejects_a_changed_candidate_snapshot_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _database(tmp_path, migrated_db)
    conn.execute("CREATE TABLE candidate_marker(id INTEGER PRIMARY KEY,value TEXT NOT NULL)")
    conn.execute("INSERT INTO candidate_marker VALUES (1,'before')")
    conn.commit()
    seen: list[str] = []
    _patch_marker_verifiers(monkeypatch, seen)
    try:
        dry_run = evaluate_population_cutover(
            conn,
            _request(apply=False, source_snapshot_sha256=None),
            trusted_now=STAMP,
        )
        conn.execute("UPDATE candidate_marker SET value='after' WHERE id=1")
        conn.commit()
        with pytest.raises(PopulationCutoverBlockedError, match="source snapshot"):
            evaluate_population_cutover(
                conn,
                _request(
                    apply=True,
                    source_snapshot_sha256=dry_run.run.source_snapshot_sha256,
                ),
                trusted_now=STAMP,
            )
        assert conn.execute("SELECT COUNT(*) FROM population_run_headers").fetchone() == (0,)
    finally:
        conn.close()


def test_apply_holds_the_writer_boundary_through_verification_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _database(tmp_path, migrated_db)
    conn.execute("CREATE TABLE candidate_marker(id INTEGER PRIMARY KEY,value TEXT NOT NULL)")
    conn.execute("INSERT INTO candidate_marker VALUES (1,'before')")
    conn.commit()
    seen: list[str] = []
    attempt_write = False
    outcomes: list[str] = []

    def mutate_live_database() -> None:
        if not attempt_write:
            return
        contender = sqlite3.connect(tmp_path / "population-evaluator.db", timeout=0)
        try:
            contender.execute("UPDATE candidate_marker SET value='after' WHERE id=1")
            contender.commit()
            outcomes.append("committed")
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc).lower()
            outcomes.append("locked")
        finally:
            contender.close()

    _patch_marker_verifiers(monkeypatch, seen, on_first_read=mutate_live_database)
    try:
        dry_run = evaluate_population_cutover(
            conn,
            _request(apply=False, source_snapshot_sha256=None),
            trusted_now=STAMP,
        )
        seen.clear()
        attempt_write = True
        applied = evaluate_population_cutover(
            conn,
            _request(
                apply=True,
                source_snapshot_sha256=dry_run.run.source_snapshot_sha256,
            ),
            trusted_now=STAMP,
        )
        assert applied.status == "sealed"
        assert outcomes == ["locked"]
        assert seen == ["before"] * len(REQUIRED_POPULATION_PLANES)
        assert conn.execute("SELECT value FROM candidate_marker WHERE id=1").fetchone() == (
            "before",
        )
    finally:
        conn.close()


def test_verifier_closure_manifest_is_explicit_bounded_and_complete() -> None:
    import provenance.population_cutover as cutover

    closure = cutover._VERIFIER_CLOSURE_FILES
    assert closure == tuple(sorted(set(closure)))
    assert len(closure) <= 64
    assert {
        "provenance/integrity_audit.py",
        "provenance/legacy_canonical_parity.py",
        "provenance/population_canonical_resolution.py",
        "provenance/population_completeness.py",
        "provenance/population_cutover.py",
        "provenance/population_document_processing.py",
        "provenance/population_identity.py",
        "provenance/population_research_snapshots.py",
        "provenance/population_retrieval_runtime.py",
        "provenance/population_source_facts.py",
        "provenance/verifier_identity.py",
    } <= set(closure)


def test_blocked_plane_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    import provenance.population_cutover as cutover

    conn = _database(tmp_path, migrated_db)
    _patch_green(monkeypatch)
    verifiers = list(cutover._PLANE_VERIFIERS)
    verifiers[3] = (
        "canonical_projection",
        _failed_projection,
        "provenance/population_identity.py",
    )
    monkeypatch.setattr(cutover, "_PLANE_VERIFIERS", tuple(verifiers))
    try:
        with pytest.raises(PopulationCutoverBlockedError, match="canonical_projection"):
            evaluate_population_cutover(conn, _request(apply=True))
        assert conn.execute("SELECT COUNT(*) FROM population_run_headers").fetchone() == (0,)
    finally:
        conn.close()
