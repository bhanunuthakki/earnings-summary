# pyright: reportPrivateUsage=false
"""Verifier-derived, audit-bound full-universe population cutover."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from provenance.integrity_audit import (
    CutoverAuditOptions,
    CutoverReadinessSummary,
    audit_cutover_readiness,
)
from provenance.legacy_canonical_parity import (
    ParityReport,
    ParityRequest,
    ProjectionCoordinate,
    ProjectionCoordinateReader,
    scan_legacy_canonical_parity,
)
from provenance.population_completeness import (
    _CUTOVER_WRITE_AUTHORITY,
    PLANE_EXCLUSION_REASON_CODES,
    REQUIRED_CUTOVER_AUDIT_GATES,
    REQUIRED_POPULATION_PLANES,
    PopulationAuditReceipt,
    PopulationCompletenessLedger,
    PopulationCutoverReceipt,
    PopulationParityReceipt,
    PopulationPlaneName,
    PopulationPlaneReceipt,
    PopulationPlaneVerification,
    PopulationRun,
    PopulationTemporalScope,
    canonical_json,
    digest_text,
    population_run_identity,
)
from provenance.verifier_identity import verifier_source_artifact_sha256
from search.canonical_fact_projection import admit_canonical_projection_for_read
from search.embedding_promotion import LocalVectorRuntimeConfig

_POLICY_NAME = "investor_grade_full_universe_cutover"
_POLICY_VERSION = "2"
_AUDIT_VERIFIER_NAME = "population-cutover-readiness-auditor"
_AUDIT_VERIFIER_VERSION = "2"
_SOURCE_ROOT = Path(__file__).resolve().parents[1]


def _code_sha(*paths: str) -> str:
    return verifier_source_artifact_sha256({path: _SOURCE_ROOT / path for path in sorted(paths)})


_PLANE_CODE_SHA = {
    "identity_scope": _code_sha("provenance/population_identity.py"),
    "source_fact_ontology": _code_sha(
        "provenance/population_source_facts.py",
        "provenance/population_metric_ontology.py",
    ),
    "canonical_resolution": _code_sha("provenance/population_canonical_resolution.py"),
    "canonical_projection": _code_sha(
        "provenance/population_canonical_resolution.py",
        "search/canonical_fact_projection.py",
    ),
    "document_processing": _code_sha("provenance/population_document_processing.py"),
    "research_snapshot": _code_sha("provenance/population_research_snapshots.py"),
    "retrieval_runtime": _code_sha("provenance/population_retrieval_runtime.py"),
}
_AUDIT_CODE_SHA = _code_sha(
    "provenance/integrity_audit.py",
    "provenance/legacy_canonical_parity.py",
    "provenance/population_completeness.py",
    "provenance/population_cutover.py",
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_EFFECTIVE_PROJECTION_CTE = """
WITH RECURSIVE lineage(generation_id,parent_generation_id,depth) AS (
 SELECT generation_id,parent_generation_id,0
 FROM canonical_fact_projection_generations WHERE generation_id=?
 UNION ALL
 SELECT parent.generation_id,parent.parent_generation_id,lineage.depth+1
 FROM canonical_fact_projection_generations parent
 JOIN lineage ON parent.generation_id=lineage.parent_generation_id
 WHERE lineage.depth<32
),
ranked AS (
 SELECT entry.*,lineage.depth,
 row_number() OVER (
   PARTITION BY entry.canonical_metric_cell_id
   ORDER BY lineage.depth ASC
 ) AS state_rank
 FROM lineage
 JOIN canonical_fact_projection_entries entry
 ON entry.generation_id=lineage.generation_id
),
current_state AS (
 SELECT * FROM ranked WHERE state_rank=1
)
"""


class CutoverBlockerCode(StrEnum):
    PLANE_VERIFIER_FAILED = "PLANE_VERIFIER_FAILED"
    PLANE_BLOCKED = "PLANE_BLOCKED"
    PROJECTION_SCOPE_EMPTY = "PROJECTION_SCOPE_EMPTY"
    PROJECTION_SCOPE_INCOMPLETE = "PROJECTION_SCOPE_INCOMPLETE"
    PROJECTION_SCOPE_AMBIGUOUS = "PROJECTION_SCOPE_AMBIGUOUS"
    LEGACY_FACT_UNBOUND = "LEGACY_FACT_UNBOUND"
    LEGACY_TICKER_REUSED = "LEGACY_TICKER_REUSED"
    PARITY_EMPTY = "PARITY_EMPTY"
    PARITY_INCOMPLETE = "PARITY_INCOMPLETE"
    PARITY_DRIFT = "PARITY_DRIFT"
    AUDIT_CUTOFF_MISMATCH = "AUDIT_CUTOFF_MISMATCH"
    AUDIT_GATE_SET_MISMATCH = "AUDIT_GATE_SET_MISMATCH"
    AUDIT_GATE_EMPTY = "AUDIT_GATE_EMPTY"
    AUDIT_GATE_FAILED = "AUDIT_GATE_FAILED"
    AUDIT_FINDING_BLOCKER = "AUDIT_FINDING_BLOCKER"
    CUTOVER_SEAL_FAILED = "CUTOVER_SEAL_FAILED"


class CutoverBlocker(_FrozenModel):
    code: CutoverBlockerCode
    subject: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=1_000)


class PopulationCutoverRequest(_FrozenModel):
    knowledge_cutoff: datetime
    observed_through: datetime
    apply: bool = False
    audit_sample_limit: int = Field(default=20, ge=1, le=500)
    audit_fetch_size: int = Field(default=250, ge=1, le=1_000)
    parity_page_size: int = Field(default=1_000, ge=1, le=1_000)
    parity_max_pages: int = Field(default=10_000, ge=1)
    parity_max_rows: int = Field(default=2_000_000, ge=1)
    retrieval_runtime: LocalVectorRuntimeConfig | None = None

    @property
    def temporal_scope(self) -> PopulationTemporalScope:
        return PopulationTemporalScope(
            knowledge_cutoff=self.knowledge_cutoff,
            observed_through=self.observed_through,
        )

    @property
    def cutoff_at(self) -> datetime:
        return self.knowledge_cutoff


class PlaneVerifierEvidence(_FrozenModel):
    plane_name: PopulationPlaneName
    expected_count: int = Field(gt=0)
    materialized_count: int = Field(ge=0)
    exclusion_counts: dict[str, int]
    failed_count: int = Field(ge=0)
    input_commitment_sha256: str
    output_commitment_sha256: str
    verifier_name: str
    verifier_version: str
    verifier_code_sha256: str
    artifact_sets: tuple[dict[str, JsonValue], ...] = ()
    result: dict[str, JsonValue]


class IssuerProjectionScope(_FrozenModel):
    issuer_id: str
    projection_generation_id: str
    legacy_fact_count: int = Field(gt=0)


class IssuerParitySummary(_FrozenModel):
    issuer_id: str
    projection_generation_id: str
    legacy_fact_count: int = Field(gt=0)
    complete: bool
    cutover_ready: bool
    legacy_rows_scanned: int = Field(ge=0)
    canonical_coordinates_scanned: int = Field(ge=0)
    equal_rows: int = Field(ge=0)
    mismatch_rows: int = Field(ge=0)
    blocking_legacy_rows: int = Field(ge=0)
    canonical_only_rows: int = Field(ge=0)
    disposition_counts: dict[str, int]
    report_sha256: str


class PopulationCutoverEvaluation(_FrozenModel):
    schema_version: str = "population-cutover-evaluation.v2"
    run: PopulationRun | None
    outcome: Literal["blocked", "eligible", "sealed"]
    cutover_ready: bool
    plane_receipts: tuple[PopulationPlaneReceipt, ...]
    parity_receipt: PopulationParityReceipt | None
    audit_receipt: PopulationAuditReceipt | None
    issuer_parity: tuple[IssuerParitySummary, ...]
    audit: CutoverReadinessSummary
    blockers: tuple[CutoverBlocker, ...]
    cutover_receipt: PopulationCutoverReceipt | None


class SQLiteProjectionCoordinateReader(ProjectionCoordinateReader):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._admitted: dict[str, datetime] = {}

    def read_coordinates(
        self,
        *,
        generation_id: str,
        canonical_metric_cell_ids: Sequence[str],
        cutoff_at: datetime,
    ) -> Mapping[str, ProjectionCoordinate]:
        self._admit(generation_id, cutoff_at)
        coordinates = tuple(sorted(set(canonical_metric_cell_ids)))
        if not coordinates:
            return {}
        placeholders = ",".join("?" for _ in coordinates)
        rows = _rows(
            self._conn,
            _EFFECTIVE_PROJECTION_CTE
            + f" SELECT * FROM current_state WHERE canonical_metric_cell_id IN ({placeholders})",  # nosec B608 -- placeholders only
            (generation_id, *coordinates),
        )
        return {
            item.canonical_metric_cell_id: item
            for item in (_projection_coordinate(generation_id, row) for row in rows)
        }

    def read_coordinate_page(
        self,
        *,
        generation_id: str,
        after_coordinate: str | None,
        limit: int,
        cutoff_at: datetime,
    ) -> Sequence[ProjectionCoordinate]:
        self._admit(generation_id, cutoff_at)
        query = (
            _EFFECTIVE_PROJECTION_CTE
            + " SELECT * FROM current_state WHERE canonical_metric_cell_id>? "  # nosec B608 -- internal constant CTE and page query
            "ORDER BY canonical_metric_cell_id LIMIT ?"
        )
        rows = _rows(
            self._conn,
            query,
            (generation_id, after_coordinate or "", limit),
        )
        return tuple(_projection_coordinate(generation_id, row) for row in rows)

    def _admit(self, generation_id: str, cutoff_at: datetime) -> None:
        verified_cutoff = self._admitted.get(generation_id)
        if verified_cutoff is None:
            verified = admit_canonical_projection_for_read(self._conn, generation_id)
            verified_cutoff = _utc(verified.cutoff_at)
            self._admitted[generation_id] = verified_cutoff
        if verified_cutoff != _utc(cutoff_at):
            raise ValueError("projection generation cutoff does not match parity cutoff")


def evaluate_population_cutover(
    conn: sqlite3.Connection,
    request: PopulationCutoverRequest,
    *,
    projection_reader: ProjectionCoordinateReader | None = None,
) -> PopulationCutoverEvaluation:
    preview = _evaluate_state(
        conn,
        request,
        projection_reader=projection_reader or SQLiteProjectionCoordinateReader(conn),
    )
    if not request.apply or preview.blockers:
        return preview
    try:
        conn.execute("BEGIN IMMEDIATE")
        fresh = _evaluate_state(
            conn,
            request,
            projection_reader=SQLiteProjectionCoordinateReader(conn),
        )
        if fresh.blockers or fresh.run is None or fresh.parity_receipt is None:
            raise ValueError("cutover state changed or became blocked under seal lock")
        if fresh.audit_receipt is None:
            raise ValueError("cutover audit receipt is missing")
        existing = conn.execute(
            "SELECT 1 FROM population_cutover_receipts WHERE population_run_id=?",
            (fresh.run.population_run_id,),
        ).fetchone()
        ledger = PopulationCompletenessLedger(conn)
        if existing is None:
            sealed = ledger._record_verified_cutover(
                run=fresh.run,
                planes=fresh.plane_receipts,
                parity=fresh.parity_receipt,
                audit=fresh.audit_receipt,
                sealed_at=fresh.audit_receipt.verified_at,
                authority=_CUTOVER_WRITE_AUTHORITY,
            )
        else:
            sealed = ledger._verify_fresh_cutover(
                run=fresh.run,
                planes=fresh.plane_receipts,
                parity=fresh.parity_receipt,
                audit=fresh.audit_receipt,
                authority=_CUTOVER_WRITE_AUTHORITY,
            )
        conn.commit()
        return fresh.model_copy(
            update={
                "outcome": "sealed",
                "cutover_ready": True,
                "cutover_receipt": sealed,
            }
        )
    except Exception as exc:
        conn.rollback()
        blocker = CutoverBlocker(
            code=CutoverBlockerCode.CUTOVER_SEAL_FAILED,
            subject=preview.run.population_run_id if preview.run else "population_cutover",
            message=f"transactional cutover revalidation failed: {type(exc).__name__}",
        )
        return preview.model_copy(
            update={
                "outcome": "blocked",
                "cutover_ready": False,
                "blockers": _dedupe_blockers((*preview.blockers, blocker)),
            }
        )


def _evaluate_state(
    conn: sqlite3.Connection,
    request: PopulationCutoverRequest,
    *,
    projection_reader: ProjectionCoordinateReader,
) -> PopulationCutoverEvaluation:
    temporal_scope = request.temporal_scope
    audit = audit_cutover_readiness(
        conn,
        CutoverAuditOptions(
            knowledge_cutoff=temporal_scope.knowledge_cutoff,
            observed_through=temporal_scope.observed_through,
            sample_limit=request.audit_sample_limit,
            fetch_size=request.audit_fetch_size,
        ),
    )
    audit_blockers = _audit_blockers(audit, temporal_scope)
    blockers = list(audit_blockers)
    evidence: tuple[PlaneVerifierEvidence, ...] = ()
    try:
        evidence = _derive_plane_evidence(
            conn,
            temporal_scope,
            runtime=request.retrieval_runtime,
        )
    except Exception as exc:
        blockers.append(
            CutoverBlocker(
                code=CutoverBlockerCode.PLANE_VERIFIER_FAILED,
                subject="population_planes",
                message=f"population verifier derivation failed: {type(exc).__name__}",
            )
        )
    run: PopulationRun | None = None
    receipts: tuple[PopulationPlaneReceipt, ...] = ()
    audit_receipt: PopulationAuditReceipt | None = None
    if evidence:
        run, receipts = _build_run_and_receipts(
            evidence,
            temporal_scope,
            audit.generated_at,
        )
        blockers.extend(_plane_blockers(receipts))
        if not audit_blockers:
            audit_receipt = _build_audit_receipt(
                conn,
                run.population_run_id,
                audit,
                request,
            )
    scopes, scope_blockers = discover_issuer_projection_scopes(conn, temporal_scope)
    blockers.extend(scope_blockers)
    summaries: tuple[IssuerParitySummary, ...] = ()
    parity: PopulationParityReceipt | None = None
    if scopes and run is not None:
        reports = tuple(
            scan_legacy_canonical_parity(
                conn,
                ParityRequest(
                    temporal_scope=temporal_scope,
                    projection_generation_id=scope.projection_generation_id,
                    issuer_id=scope.issuer_id,
                    page_size=request.parity_page_size,
                    max_pages=request.parity_max_pages,
                    max_rows=request.parity_max_rows,
                ),
                projection_reader,
            )
            for scope in scopes
        )
        summaries = tuple(
            _issuer_parity_summary(scope, report)
            for scope, report in zip(scopes, reports, strict=True)
        )
        parity, parity_blockers = _aggregate_parity(
            run.population_run_id,
            temporal_scope,
            audit.generated_at,
            reports,
            summaries,
        )
        blockers.extend(parity_blockers)
    blocker_tuple = _dedupe_blockers(blockers)
    return PopulationCutoverEvaluation(
        run=run,
        outcome="blocked" if blocker_tuple else "eligible",
        cutover_ready=False,
        plane_receipts=receipts,
        parity_receipt=parity,
        audit_receipt=audit_receipt,
        issuer_parity=summaries,
        audit=audit,
        blockers=blocker_tuple,
        cutover_receipt=None,
    )


def _derive_plane_evidence(
    conn: sqlite3.Connection,
    temporal_scope: PopulationTemporalScope,
    *,
    runtime: LocalVectorRuntimeConfig | None,
) -> tuple[PlaneVerifierEvidence, ...]:
    from provenance.population_canonical_resolution import (
        verify_canonical_projection,
        verify_canonical_resolution,
    )
    from provenance.population_document_processing import verify_document_processing
    from provenance.population_identity import verify_identity_scope
    from provenance.population_research_snapshots import verify_research_snapshots
    from provenance.population_retrieval_runtime import verify_retrieval_runtime
    from provenance.population_source_facts import verify_source_fact_ontology

    verifications = (
        verify_identity_scope(conn, temporal_scope),
        verify_source_fact_ontology(conn, temporal_scope),
        verify_canonical_resolution(conn, temporal_scope),
        verify_canonical_projection(conn, temporal_scope),
        verify_document_processing(conn, temporal_scope),
        verify_research_snapshots(conn, temporal_scope),
        verify_retrieval_runtime(conn, temporal_scope, runtime=runtime),
    )
    return tuple(_plane_evidence_from_verification(item) for item in verifications)


def _plane_evidence_from_verification(
    verification: PopulationPlaneVerification,
) -> PlaneVerifierEvidence:
    return PlaneVerifierEvidence(
        plane_name=verification.plane_name,
        expected_count=verification.expected_count,
        materialized_count=verification.materialized_count,
        exclusion_counts=dict(sorted(verification.exclusion_counts.items())),
        failed_count=verification.failed_count,
        input_commitment_sha256=verification.input_commitment_sha256,
        output_commitment_sha256=verification.output_commitment_sha256,
        verifier_name=f"{verification.plane_name}-persisted-artifact-verifier",
        verifier_version="2",
        verifier_code_sha256=_PLANE_CODE_SHA[verification.plane_name],
        artifact_sets=tuple(
            cast(dict[str, JsonValue], item.model_dump(mode="json"))
            for item in verification.artifact_sets
        ),
        result=verification.details,
    )


def _build_run_and_receipts(
    evidence: tuple[PlaneVerifierEvidence, ...],
    temporal_scope: PopulationTemporalScope,
    verified_at: datetime,
) -> tuple[PopulationRun, tuple[PopulationPlaneReceipt, ...]]:
    if tuple(sorted(item.plane_name for item in evidence)) != tuple(
        sorted(REQUIRED_POPULATION_PLANES)
    ):
        raise ValueError("verifiers did not produce exactly seven plane results")
    source_sha = digest_text(
        canonical_json(
            [
                {
                    "input_commitment_sha256": item.input_commitment_sha256,
                    "plane_name": item.plane_name,
                }
                for item in sorted(evidence, key=lambda item: item.plane_name)
            ]
        )
    )
    policy_sha = digest_text(
        canonical_json(
            {
                "audit_gates": REQUIRED_CUTOVER_AUDIT_GATES,
                "exclusion_contracts": {
                    key: sorted(value) for key, value in PLANE_EXCLUSION_REASON_CODES.items()
                },
                "plane_verifier_code_sha256": _PLANE_CODE_SHA,
                "policy_name": _POLICY_NAME,
                "policy_version": _POLICY_VERSION,
            }
        )
    )
    run_id = population_run_identity(policy_sha, source_sha, temporal_scope)
    run = PopulationRun(
        population_run_id=run_id,
        idempotency_key=run_id,
        policy_name=_POLICY_NAME,
        policy_version=_POLICY_VERSION,
        policy_config_sha256=policy_sha,
        source_snapshot_sha256=source_sha,
        temporal_scope=temporal_scope,
        verified_at=verified_at,
    )
    receipts = tuple(
        PopulationPlaneReceipt(
            population_run_id=run_id,
            plane_name=item.plane_name,
            expected_count=item.expected_count,
            materialized_count=item.materialized_count,
            excluded_count=sum(item.exclusion_counts.values()),
            failed_count=item.failed_count,
            input_commitment_sha256=item.input_commitment_sha256,
            output_commitment_sha256=item.output_commitment_sha256,
            status=(
                "complete" if item.failed_count == 0 and item.materialized_count > 0 else "blocked"
            ),
            details=cast(
                dict[str, JsonValue],
                {
                    "artifact_sets": list(item.artifact_sets),
                    "exclusion_counts": item.exclusion_counts,
                    "result": item.result,
                    "temporal_scope": temporal_scope.model_dump(mode="json"),
                    "verifier": {
                        "code_sha256": item.verifier_code_sha256,
                        "name": item.verifier_name,
                        "result_sha256": digest_text(canonical_json(item.result)),
                        "version": item.verifier_version,
                    },
                },
            ),
            temporal_scope=temporal_scope,
            verified_at=verified_at,
        )
        for item in sorted(evidence, key=lambda item: item.plane_name)
    )
    return run, receipts


def _build_audit_receipt(
    conn: sqlite3.Connection,
    population_run_id: str,
    audit: CutoverReadinessSummary,
    request: PopulationCutoverRequest,
) -> PopulationAuditReceipt:
    del conn
    if len(audit.candidate_commitments) != len(REQUIRED_CUTOVER_AUDIT_GATES):
        raise ValueError("cutover audit did not commit exactly 13 candidate sets")
    gate_evidence = [
        {
            "gate": item.gate,
            "gate_evidence_sha256": digest_text(
                canonical_json(
                    {
                        "gate": item.gate,
                        "tables": [
                            {
                                "row_count": item.row_count,
                                "rows_sha256": item.rows_sha256,
                                "table": item.selection_policy_id,
                            }
                        ],
                    }
                )
            ),
            "tables": [
                {
                    "row_count": item.row_count,
                    "rows_sha256": item.rows_sha256,
                    "table": item.selection_policy_id,
                }
            ],
        }
        for item in sorted(audit.candidate_commitments, key=lambda value: value.gate)
    ]
    watermark_material: dict[str, JsonValue] = {
        "knowledge_cutoff": _db_time(audit.knowledge_cutoff),
        "observed_through": _db_time(audit.observed_through),
        "gates": cast(
            JsonValue,
            [
                {
                    "gate": str(item["gate"]),
                    "gate_evidence_sha256": str(item["gate_evidence_sha256"]),
                }
                for item in gate_evidence
            ],
        ),
    }
    evidence: dict[str, JsonValue] = {
        "coverage": cast(
            JsonValue,
            [item.model_dump(mode="json") for item in audit.coverage],
        ),
        "findings": cast(
            JsonValue,
            [item.model_dump(mode="json") for item in audit.findings],
        ),
        "gate_evidence": cast(JsonValue, gate_evidence),
        "has_blockers": audit.has_blockers,
        "schema_version": audit.schema_version,
        "tables_present": list(audit.tables_present),
        "watermark_material": watermark_material,
        "watermark_sha256": digest_text(canonical_json(watermark_material)),
    }
    config_sha = digest_text(
        canonical_json(
            {
                "fetch_size": request.audit_fetch_size,
                "required_gates": REQUIRED_CUTOVER_AUDIT_GATES,
                "sample_limit": request.audit_sample_limit,
            }
        )
    )
    return PopulationAuditReceipt(
        population_run_id=population_run_id,
        verifier_name=_AUDIT_VERIFIER_NAME,
        verifier_version=_AUDIT_VERIFIER_VERSION,
        verifier_code_sha256=_AUDIT_CODE_SHA,
        verifier_config_sha256=config_sha,
        temporal_scope=audit.temporal_scope,
        verified_at=audit.generated_at,
        required_gate_count=len(REQUIRED_CUTOVER_AUDIT_GATES),
        eligible_count=sum(item.eligible_count for item in audit.coverage),
        verified_count=sum(item.verified_count for item in audit.coverage),
        failed_count=sum(item.failed_count for item in audit.coverage),
        evidence=evidence,
    )


def discover_issuer_projection_scopes(
    conn: sqlite3.Connection,
    temporal_scope: PopulationTemporalScope,
) -> tuple[tuple[IssuerProjectionScope, ...], list[CutoverBlocker]]:
    knowledge_cutoff = _db_time(temporal_scope.knowledge_cutoff)
    observed_through = _db_time(temporal_scope.observed_through)
    projection_rows = _rows(
        conn,
        """
        SELECT scope.issuer_id,scope.resolution_snapshot_id,generation.generation_id,
               scope_seal.resolution_snapshot_id AS sealed_scope_id,
               projection_seal.generation_id AS sealed_generation_id,
               audit.generation_id AS audited_generation_id
        FROM canonical_fact_resolution_snapshot_scope_headers scope
        LEFT JOIN canonical_fact_resolution_snapshot_scope_seals scope_seal
          ON scope_seal.resolution_snapshot_id=scope.resolution_snapshot_id
        LEFT JOIN canonical_fact_projection_scope_bindings binding
          ON binding.resolution_snapshot_id=scope.resolution_snapshot_id
        LEFT JOIN canonical_fact_projection_generations generation
          ON generation.generation_id=binding.generation_id
         AND datetime(generation.cutoff_at)=datetime(scope.cutoff_at)
        LEFT JOIN canonical_fact_projection_seals projection_seal
          ON projection_seal.generation_id=generation.generation_id
        LEFT JOIN canonical_fact_projection_audit_receipts audit
          ON audit.generation_id=generation.generation_id
        WHERE datetime(scope.cutoff_at)=datetime(?)
        ORDER BY scope.issuer_id,scope.resolution_snapshot_id,generation.generation_id
        """,
        (knowledge_cutoff,),
    )
    blockers: list[CutoverBlocker] = []
    if not projection_rows:
        return (), [
            CutoverBlocker(
                code=CutoverBlockerCode.PROJECTION_SCOPE_EMPTY,
                subject="issuer_projection_scope",
                message="cutoff has no issuer projection scope",
            )
        ]
    fact_counts, fact_blockers = _legacy_fact_counts_by_issuer(
        conn,
        knowledge_cutoff,
        observed_through,
    )
    blockers.extend(fact_blockers)
    by_issuer: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in projection_rows:
        by_issuer[str(row["issuer_id"])].append(row)
    if set(by_issuer) != set(fact_counts):
        blockers.append(
            CutoverBlocker(
                code=CutoverBlockerCode.PROJECTION_SCOPE_INCOMPLETE,
                subject="issuer_projection_scope",
                message="issuer projection set differs from issuer-bound legacy fact set",
            )
        )
    scopes: list[IssuerProjectionScope] = []
    for issuer_id in sorted(set(by_issuer) | set(fact_counts)):
        candidates = by_issuer.get(issuer_id, [])
        complete = [
            row
            for row in candidates
            if row["generation_id"] is not None
            and row["sealed_scope_id"] is not None
            and row["sealed_generation_id"] is not None
            and row["audited_generation_id"] is not None
        ]
        if len(candidates) != 1 or len(complete) != 1 or not fact_counts.get(issuer_id):
            blockers.append(
                CutoverBlocker(
                    code=(
                        CutoverBlockerCode.PROJECTION_SCOPE_AMBIGUOUS
                        if len(candidates) > 1
                        else CutoverBlockerCode.PROJECTION_SCOPE_INCOMPLETE
                    ),
                    subject=issuer_id,
                    message="issuer requires one generation and nonempty bound legacy facts",
                )
            )
            continue
        scopes.append(
            IssuerProjectionScope(
                issuer_id=issuer_id,
                projection_generation_id=str(complete[0]["generation_id"]),
                legacy_fact_count=fact_counts[issuer_id],
            )
        )
    return tuple(scopes), blockers


_LEGACY_FACT_SCOPE_CTE = """
    WITH facts AS (
      SELECT 'financial_facts' fact_table,fact.id fact_row_id,
             upper(trim(fact.ticker)) ticker
      FROM financial_facts fact
      JOIN documents document ON document.id=fact.source_doc_id
      WHERE datetime(document.fetched_at)<=datetime(?)
      UNION ALL
      SELECT 'kpi_facts',fact.id,upper(trim(fact.ticker))
      FROM kpi_facts fact
      JOIN documents document ON document.id=fact.source_doc_id
      WHERE datetime(document.fetched_at)<=datetime(?)
    ),
    ranked AS (
      SELECT match.*,
             row_number() OVER (
               PARTITION BY match.fact_table,match.fact_row_id
               ORDER BY match.revision DESC,match.match_revision_id DESC
             ) row_rank
      FROM legacy_fact_evidence_match_revisions match
      WHERE datetime(match.knowledge_at)<=datetime(?)
        AND datetime(match.recorded_at)<=datetime(?)
    ),
    current_fact_scope AS (
      SELECT facts.fact_table,facts.fact_row_id,facts.ticker,ranked.issuer_id
      FROM facts
      LEFT JOIN ranked ON ranked.fact_table=facts.fact_table
       AND ranked.fact_row_id=facts.fact_row_id AND ranked.row_rank=1
    )
"""


def _legacy_fact_counts_by_issuer(
    conn: sqlite3.Connection,
    knowledge_cutoff: str,
    observed_through: str,
) -> tuple[dict[str, int], list[CutoverBlocker]]:
    params = (
        knowledge_cutoff,
        knowledge_cutoff,
        knowledge_cutoff,
        observed_through,
    )
    counts: dict[str, int] = {}
    blockers: list[CutoverBlocker] = []
    count_cursor = conn.execute(
        _LEGACY_FACT_SCOPE_CTE
        + """
        SELECT issuer_id,COUNT(*) fact_count
        FROM current_fact_scope
        WHERE issuer_id IS NOT NULL
        GROUP BY issuer_id
        ORDER BY issuer_id
        """,  # nosec B608 -- internal constant CTE plus constant aggregate
        params,
    )
    for row in count_cursor:
        counts[str(row[0])] = int(row[1])

    unbound = conn.execute(
        _LEGACY_FACT_SCOPE_CTE
        + """
        SELECT COUNT(*) fact_count
        FROM current_fact_scope
        WHERE issuer_id IS NULL
        """,  # nosec B608 -- internal constant CTE plus constant aggregate
        params,
    ).fetchone()
    unbound_count = 0 if unbound is None else int(unbound[0])
    if unbound_count:
        blockers.append(
            CutoverBlocker(
                code=CutoverBlockerCode.LEGACY_FACT_UNBOUND,
                subject="legacy_fact_scope",
                message=(f"{unbound_count} legacy facts have no cutoff-exact issuer-bound match"),
            )
        )

    reused_cursor = conn.execute(
        _LEGACY_FACT_SCOPE_CTE
        + """
        SELECT ticker,COUNT(DISTINCT issuer_id) issuer_count
        FROM current_fact_scope
        WHERE issuer_id IS NOT NULL
        GROUP BY ticker
        HAVING COUNT(DISTINCT issuer_id)>1
        ORDER BY ticker
        """,  # nosec B608 -- internal constant CTE plus constant aggregate
        params,
    )
    while reused_rows := reused_cursor.fetchmany(250):
        for row in reused_rows:
            blockers.append(
                CutoverBlocker(
                    code=CutoverBlockerCode.LEGACY_TICKER_REUSED,
                    subject=str(row[0]),
                    message=(f"legacy ticker resolves to {int(row[1])} issuers at cutoff"),
                )
            )
    return counts, blockers


def _plane_blockers(
    receipts: tuple[PopulationPlaneReceipt, ...],
) -> list[CutoverBlocker]:
    return [
        CutoverBlocker(
            code=CutoverBlockerCode.PLANE_BLOCKED,
            subject=item.plane_name,
            message=(
                f"verifier found failed={item.failed_count}, materialized={item.materialized_count}"
            ),
        )
        for item in receipts
        if item.status != "complete"
    ]


def _audit_blockers(
    audit: CutoverReadinessSummary,
    temporal_scope: PopulationTemporalScope,
) -> list[CutoverBlocker]:
    blockers: list[CutoverBlocker] = []
    if audit.temporal_scope != temporal_scope:
        blockers.append(
            CutoverBlocker(
                code=CutoverBlockerCode.AUDIT_CUTOFF_MISMATCH,
                subject="cutover_audit",
                message="audit temporal scope differs from population scope",
            )
        )
    names = tuple(sorted(item.gate for item in audit.coverage))
    if names != tuple(sorted(REQUIRED_CUTOVER_AUDIT_GATES)):
        blockers.append(
            CutoverBlocker(
                code=CutoverBlockerCode.AUDIT_GATE_SET_MISMATCH,
                subject="cutover_audit",
                message="audit must return exactly the 13 governed gates",
            )
        )
    for gate in audit.coverage:
        if gate.eligible_count == 0:
            blockers.append(
                CutoverBlocker(
                    code=CutoverBlockerCode.AUDIT_GATE_EMPTY,
                    subject=gate.gate,
                    message="governed audit gate has zero eligible evidence",
                )
            )
        if gate.failed_count or gate.verified_count != gate.eligible_count:
            blockers.append(
                CutoverBlocker(
                    code=CutoverBlockerCode.AUDIT_GATE_FAILED,
                    subject=gate.gate,
                    message=f"{gate.failed_count} of {gate.eligible_count} failed",
                )
            )
    if audit.has_blockers:
        blockers.append(
            CutoverBlocker(
                code=CutoverBlockerCode.AUDIT_FINDING_BLOCKER,
                subject="cutover_audit",
                message="audit contains blocking findings",
            )
        )
    return blockers


def _issuer_parity_summary(
    scope: IssuerProjectionScope,
    report: ParityReport,
) -> IssuerParitySummary:
    canonical_only = report.disposition_counts.get("canonical_only_native", 0)
    return IssuerParitySummary(
        issuer_id=scope.issuer_id,
        projection_generation_id=scope.projection_generation_id,
        legacy_fact_count=scope.legacy_fact_count,
        complete=report.complete,
        cutover_ready=report.cutover_ready and canonical_only == 0,
        legacy_rows_scanned=report.legacy_rows_scanned,
        canonical_coordinates_scanned=report.canonical_coordinates_scanned,
        equal_rows=report.equal_rows,
        mismatch_rows=report.mismatch_rows,
        blocking_legacy_rows=report.blocking_legacy_rows,
        canonical_only_rows=canonical_only,
        disposition_counts=report.disposition_counts,
        report_sha256=digest_text(canonical_json(report.model_dump(mode="json", exclude={"rows"}))),
    )


def _aggregate_parity(
    run_id: str,
    temporal_scope: PopulationTemporalScope,
    verified_at: datetime,
    reports: tuple[ParityReport, ...],
    summaries: tuple[IssuerParitySummary, ...],
) -> tuple[PopulationParityReceipt | None, list[CutoverBlocker]]:
    blockers: list[CutoverBlocker] = []
    if (
        not reports
        or any(item.legacy_rows_scanned == 0 for item in reports)
        or any(item.canonical_coordinates_scanned == 0 for item in reports)
    ):
        blockers.append(
            CutoverBlocker(
                code=CutoverBlockerCode.PARITY_EMPTY,
                subject="legacy_canonical_parity",
                message="every issuer needs nonempty legacy and canonical coverage",
            )
        )
    if any(not item.complete or item.truncated for item in reports):
        blockers.append(
            CutoverBlocker(
                code=CutoverBlockerCode.PARITY_INCOMPLETE,
                subject="legacy_canonical_parity",
                message="issuer parity did not exhaust both universes",
            )
        )
    if blockers:
        return None, blockers
    matched = sum(item.equal_rows for item in reports)
    mismatched = sum(item.mismatch_rows for item in reports)
    absent = sum(max(item.blocking_legacy_rows - item.mismatch_rows, 0) for item in reports)
    extra = sum(item.canonical_only_rows for item in summaries)
    if mismatched or absent or extra:
        blockers.append(
            CutoverBlocker(
                code=CutoverBlockerCode.PARITY_DRIFT,
                subject="legacy_canonical_parity",
                message=f"mismatched={mismatched}, absent={absent}, extra={extra}",
            )
        )
    report: dict[str, JsonValue] = {
        "aggregate_version": "issuer-bound-legacy-parity.v2",
        "issuer_reports": [item.model_dump(mode="json") for item in summaries],
        "observed_canonical_coordinates": sum(
            item.canonical_coordinates_scanned for item in reports
        ),
    }
    return (
        PopulationParityReceipt(
            population_run_id=run_id,
            eligible_legacy_count=matched + mismatched + absent,
            canonical_count=matched + extra,
            matched_count=matched,
            mismatched_count=mismatched,
            absent_count=absent,
            extra_count=extra,
            status="blocked" if blockers else "complete",
            report=report,
            temporal_scope=temporal_scope,
            verified_at=verified_at,
        ),
        blockers,
    )


def _projection_coordinate(
    generation_id: str,
    row: dict[str, object],
) -> ProjectionCoordinate:
    upsert = str(row["change_kind"]) == "upsert"
    return ProjectionCoordinate(
        generation_id=generation_id,
        canonical_metric_cell_id=str(row["canonical_metric_cell_id"]),
        change_kind="upsert" if upsert else "tombstone",
        audit_verified=True,
        canonical_resolution_revision_id=(
            str(row["canonical_resolution_revision_id"]) if upsert else None
        ),
        selected_observation_id=str(row["selected_observation_id"]) if upsert else None,
        value_kind=(
            cast(Literal["numeric", "text", "nil"], str(row["value_kind"])) if upsert else None
        ),
        canonical_value=None if row["canonical_value"] is None else str(row["canonical_value"]),
        period_end=str(row["period_end"]) if upsert else None,
        unit_key=str(row["unit_key"]) if upsert else None,
        currency=None if row["currency"] is None else str(row["currency"]),
    )


def _rows(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...],
) -> list[dict[str, object]]:
    cursor = conn.execute(sql, params)
    names = tuple(item[0] for item in cursor.description or ())
    rows: list[dict[str, object]] = []
    while page := cursor.fetchmany(250):
        rows.extend(dict(zip(names, tuple(row), strict=True)) for row in page)
    return rows


def _dedupe_blockers(
    blockers: Sequence[CutoverBlocker],
) -> tuple[CutoverBlocker, ...]:
    unique = {(item.code.value, item.subject, item.message): item for item in blockers}
    return tuple(unique[key] for key in sorted(unique))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("population cutover timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "REQUIRED_CUTOVER_AUDIT_GATES",
    "CutoverBlocker",
    "CutoverBlockerCode",
    "IssuerParitySummary",
    "IssuerProjectionScope",
    "PlaneVerifierEvidence",
    "PopulationCutoverEvaluation",
    "PopulationCutoverRequest",
    "SQLiteProjectionCoordinateReader",
    "discover_issuer_projection_scopes",
    "evaluate_population_cutover",
]
