# pyright: reportPrivateUsage=false
"""Authority-owned population cutover evaluator and seal writer."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from pydantic import JsonValue

from alembic import command
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

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
SHA = hashlib.sha256(b"population-evaluator-test").hexdigest()
SCOPE = PopulationTemporalScope(knowledge_cutoff=STAMP, observed_through=STAMP)


def _database(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "population-evaluator.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE financial_facts (id INTEGER PRIMARY KEY,source_doc_id INTEGER NOT NULL);"
        "CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY,source_doc_id INTEGER NOT NULL);"
    )
    conn.commit()
    conn.close()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0256_population_cutover_receipts")
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


def _request(*, apply: bool) -> PopulationCutoverEvaluationRequest:
    return PopulationCutoverEvaluationRequest(
        temporal_scope=SCOPE,
        policy_config_sha256=SHA,
        source_snapshot_sha256=SHA,
        evaluated_at=STAMP,
        sealed_at=STAMP,
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
) -> None:
    conn = _database(tmp_path)
    _patch_green(monkeypatch)
    try:
        dry_run = evaluate_population_cutover(conn, _request(apply=False))
        assert dry_run.status == "ready"
        assert dry_run.cutover is None
        assert conn.execute("SELECT COUNT(*) FROM population_run_headers").fetchone() == (0,)

        applied = evaluate_population_cutover(conn, _request(apply=True))
        assert applied.status == "sealed"
        assert applied.cutover is not None
        assert (
            PopulationCompletenessLedger(conn).verify(applied.run.population_run_id)
            == applied.cutover
        )
    finally:
        conn.close()


def test_blocked_plane_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import provenance.population_cutover as cutover

    conn = _database(tmp_path)
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
