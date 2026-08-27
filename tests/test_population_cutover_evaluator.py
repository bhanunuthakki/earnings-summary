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


def _database(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> sqlite3.Connection:
    path = tmp_path / "population-evaluator.db"
    migrated_db(
        path,
        stamp="0213_decision_draft_provider_id",
        target="0256_population_cutover_receipts",
        archived=True,
    )
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
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
        details["governance"] = {
            "evaluation_receipt_id": "evaluation-1",
            "evaluation_evaluated_at": STAMP.isoformat(),
            "promotion_id": "promotion-1",
            "promotion_recorded_at": STAMP.isoformat(),
            "projection_seal_ids": ["projection-1"],
            "projection_sealed_at": {"projection-1": STAMP.isoformat()},
            "runtime_registered_at": STAMP.isoformat(),
            "runtime_registration_id": "runtime-1",
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
