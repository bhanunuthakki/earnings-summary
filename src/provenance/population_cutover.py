# pyright: reportPrivateUsage=false
"""Fresh-verifier authority for the full-universe population cutover seal."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from provenance.integrity_audit import (
    CutoverAuditOptions,
    CutoverReadinessSummary,
    audit_cutover_readiness,
)
from provenance.legacy_canonical_parity import (
    ParityDisposition,
    ParityReport,
    ParityRequest,
    ProjectionCoordinate,
    ProjectionCoordinateReader,
    scan_legacy_canonical_parity,
)
from provenance.population_canonical_resolution import (
    verify_canonical_projection,
    verify_canonical_resolution,
)
from provenance.population_completeness import (
    _CUTOVER_WRITE_AUTHORITY,
    REQUIRED_CUTOVER_AUDIT_GATES,
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
from provenance.population_document_processing import verify_document_processing
from provenance.population_identity import verify_identity_scope
from provenance.population_research_snapshots import verify_research_snapshots
from provenance.population_retrieval_runtime import verify_retrieval_runtime
from provenance.population_source_facts import verify_source_fact_ontology
from provenance.verifier_identity import verifier_source_artifact_sha256
from search.canonical_fact_projection import admit_canonical_projection_for_read

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_POLICY_NAME = "full_universe_population"
_POLICY_VERSION = "1"
_AUDIT_VERIFIER_NAME = "population-cutover-readiness-auditor"
_AUDIT_VERIFIER_VERSION = "2"
_PARITY_POLICY = "full-universe-legacy-canonical-parity.v1"
_CURRENT_STATE_CTE = """
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
   PARTITION BY entry.canonical_metric_cell_id ORDER BY lineage.depth ASC
 ) AS state_rank
 FROM lineage
 JOIN canonical_fact_projection_entries entry
 ON entry.generation_id=lineage.generation_id
),
current_state AS (
 SELECT * FROM ranked WHERE state_rank=1
)
"""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return value


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


class PopulationCutoverEvaluationRequest(_FrozenModel):
    temporal_scope: PopulationTemporalScope
    policy_config_sha256: str
    source_snapshot_sha256: str
    evaluated_at: datetime
    sealed_at: datetime
    apply: bool = False
    parity_page_size: int = Field(default=1_000, ge=1, le=10_000)
    parity_max_pages: int = Field(default=10_000, ge=1)
    parity_max_rows_per_issuer: int = Field(default=2_000_000, ge=1)
    audit_sample_limit: int = Field(default=20, ge=1, le=500)
    audit_fetch_size: int = Field(default=250, ge=1, le=1_000)

    _policy_sha = field_validator("policy_config_sha256")(_sha256)
    _source_sha = field_validator("source_snapshot_sha256")(_sha256)

    @model_validator(mode="after")
    def _ordered_clocks(self) -> Self:
        if _utc(self.evaluated_at) < _utc(self.temporal_scope.observed_through):
            raise ValueError("cutover evaluation predates observed_through")
        if _utc(self.sealed_at) < _utc(self.evaluated_at):
            raise ValueError("cutover seal predates its evaluation")
        return self


class PopulationCutoverEvaluation(_FrozenModel):
    status: Literal["ready", "sealed"]
    run: PopulationRun
    planes: tuple[PopulationPlaneReceipt, ...]
    parity: PopulationParityReceipt
    audit: PopulationAuditReceipt
    cutover: PopulationCutoverReceipt | None
    evaluation_sha256: str


class PopulationCutoverBlockedError(RuntimeError):
    """A fresh verifier could not produce one complete cutover evidence set."""

    def __init__(self, stage: str, detail: str) -> None:
        self.stage = stage
        self.detail = detail
        super().__init__(f"{stage}: {detail}")


PlaneVerifier = Callable[[sqlite3.Connection, PopulationTemporalScope], PopulationPlaneVerification]
_PLANE_VERIFIERS: tuple[tuple[PopulationPlaneName, PlaneVerifier, str], ...] = (
    ("identity_scope", verify_identity_scope, "provenance/population_identity.py"),
    ("source_fact_ontology", verify_source_fact_ontology, "provenance/population_source_facts.py"),
    (
        "canonical_resolution",
        verify_canonical_resolution,
        "provenance/population_canonical_resolution.py",
    ),
    (
        "canonical_projection",
        verify_canonical_projection,
        "provenance/population_canonical_resolution.py",
    ),
    (
        "document_processing",
        verify_document_processing,
        "provenance/population_document_processing.py",
    ),
    (
        "research_snapshot",
        verify_research_snapshots,
        "provenance/population_research_snapshots.py",
    ),
    (
        "retrieval_runtime",
        verify_retrieval_runtime,
        "provenance/population_retrieval_runtime.py",
    ),
)


def evaluate_population_cutover(
    conn: sqlite3.Connection,
    request: PopulationCutoverEvaluationRequest,
) -> PopulationCutoverEvaluation:
    """Recompute every gate and atomically append the exact seal when authorized."""

    run_id = population_run_identity(
        request.policy_config_sha256,
        request.source_snapshot_sha256,
        request.temporal_scope,
    )
    run = PopulationRun(
        population_run_id=run_id,
        idempotency_key=run_id,
        policy_name=_POLICY_NAME,
        policy_version=_POLICY_VERSION,
        policy_config_sha256=request.policy_config_sha256,
        source_snapshot_sha256=request.source_snapshot_sha256,
        temporal_scope=request.temporal_scope,
        verified_at=request.evaluated_at,
    )
    plane_receipts = tuple(
        _plane_receipt(
            conn,
            run_id,
            expected_name,
            verifier(conn, request.temporal_scope),
            source_name=source_name,
            verified_at=request.evaluated_at,
            scope=request.temporal_scope,
        )
        for expected_name, verifier, source_name in _PLANE_VERIFIERS
    )
    blocked_planes = tuple(item.plane_name for item in plane_receipts if item.status == "blocked")
    if blocked_planes:
        raise PopulationCutoverBlockedError(
            "planes", "blocked plane receipts: " + ",".join(blocked_planes)
        )
    parity = verify_full_universe_legacy_parity(
        conn,
        population_run_id=run_id,
        scope=request.temporal_scope,
        verified_at=request.evaluated_at,
        page_size=request.parity_page_size,
        max_pages=request.parity_max_pages,
        max_rows_per_issuer=request.parity_max_rows_per_issuer,
    )
    if parity.status == "blocked":
        raise PopulationCutoverBlockedError("parity", "full-universe legacy parity failed")
    audit_summary = audit_cutover_readiness(
        conn,
        CutoverAuditOptions(
            knowledge_cutoff=request.temporal_scope.knowledge_cutoff,
            observed_through=request.temporal_scope.observed_through,
            sample_limit=request.audit_sample_limit,
            fetch_size=request.audit_fetch_size,
        ),
    )
    audit = build_population_audit_receipt(
        audit_summary,
        population_run_id=run_id,
        verified_at=request.evaluated_at,
    )
    cutover: PopulationCutoverReceipt | None = None
    if request.apply:
        ledger = PopulationCompletenessLedger(conn)
        with _savepoint(conn, "evaluate_population_cutover"):
            cutover = ledger._record_verified_cutover(
                run=run,
                planes=plane_receipts,
                parity=parity,
                audit=audit,
                sealed_at=request.sealed_at,
                authority=_CUTOVER_WRITE_AUTHORITY,
            )
            replay = ledger._verify_fresh_cutover(
                run=run,
                planes=plane_receipts,
                parity=parity,
                audit=audit,
                authority=_CUTOVER_WRITE_AUTHORITY,
            )
            if replay != cutover:
                raise RuntimeError("fresh population replay differs from its new cutover seal")
    material = {
        "audit_receipt_sha256": audit.receipt_sha256,
        "cutover_receipt_sha256": None if cutover is None else cutover.receipt_set_sha256,
        "parity_report_sha256": parity.report_sha256,
        "plane_details_sha256": [item.details_sha256 for item in plane_receipts],
        "population_run_id": run_id,
        "status": "ready" if cutover is None else "sealed",
    }
    return PopulationCutoverEvaluation(
        status="ready" if cutover is None else "sealed",
        run=run,
        planes=plane_receipts,
        parity=parity,
        audit=audit,
        cutover=cutover,
        evaluation_sha256=digest_text(canonical_json(material)),
    )


def _plane_receipt(
    conn: sqlite3.Connection,
    population_run_id: str,
    expected_name: PopulationPlaneName,
    verification: PopulationPlaneVerification,
    *,
    source_name: str,
    verified_at: datetime,
    scope: PopulationTemporalScope,
) -> PopulationPlaneReceipt:
    if verification.plane_name != expected_name:
        raise PopulationCutoverBlockedError(
            "planes", f"{expected_name} verifier returned {verification.plane_name}"
        )
    result = cast(dict[str, JsonValue], verification.model_dump(mode="json"))
    if expected_name == "retrieval_runtime":
        supplied = verification.details.get("governance")
        result["governance"] = (
            cast(JsonValue, supplied)
            if isinstance(supplied, dict)
            else cast(JsonValue, _retrieval_governance(conn, scope))
        )
    code_sha = verifier_source_artifact_sha256({source_name: _SOURCE_ROOT / source_name})
    return PopulationPlaneReceipt(
        population_run_id=population_run_id,
        plane_name=expected_name,
        expected_count=verification.expected_count,
        materialized_count=verification.materialized_count,
        excluded_count=verification.excluded_count,
        failed_count=verification.failed_count,
        input_commitment_sha256=verification.input_commitment_sha256,
        output_commitment_sha256=verification.output_commitment_sha256,
        status=(
            "complete"
            if verification.failed_count == 0 and verification.materialized_count > 0
            else "blocked"
        ),
        details={
            "artifact_sets": cast(
                JsonValue,
                [item.model_dump(mode="json") for item in verification.artifact_sets],
            ),
            "exclusion_counts": cast(JsonValue, verification.exclusion_counts),
            "result": result,
            "temporal_scope": cast(JsonValue, scope.model_dump(mode="json")),
            "verifier": {
                "code_sha256": code_sha,
                "name": f"population.{expected_name}.verify",
                "result_sha256": digest_text(canonical_json(result)),
                "version": "1",
            },
        },
        temporal_scope=scope,
        verified_at=verified_at,
    )


def build_population_audit_receipt(
    summary: CutoverReadinessSummary,
    *,
    population_run_id: str,
    verified_at: datetime,
) -> PopulationAuditReceipt:
    """Convert the exact 13-gate summary into its ledger-enforced receipt."""

    if summary.has_blockers:
        raise PopulationCutoverBlockedError("audit", "13-gate readiness audit has blockers")
    expected = tuple(sorted(REQUIRED_CUTOVER_AUDIT_GATES))
    coverage = tuple(sorted(summary.coverage, key=lambda item: item.gate))
    commitments = tuple(sorted(summary.candidate_commitments, key=lambda item: item.gate))
    if tuple(item.gate for item in coverage) != expected or len(coverage) != len(expected):
        raise PopulationCutoverBlockedError("audit", "coverage is not exactly the 13 gates")
    if tuple(item.gate for item in commitments) != expected or len(commitments) != len(expected):
        raise PopulationCutoverBlockedError(
            "audit", "candidate commitments are not exactly the 13 gates"
        )
    gate_evidence: list[dict[str, JsonValue]] = []
    for item in commitments:
        tables: list[dict[str, JsonValue]] = [
            {
                "row_count": item.row_count,
                "rows_sha256": item.rows_sha256,
                "table": item.selection_policy_id,
            }
        ]
        gate_evidence.append(
            {
                "gate": item.gate,
                "gate_evidence_sha256": digest_text(
                    canonical_json({"gate": item.gate, "tables": tables})
                ),
                "tables": cast(JsonValue, tables),
            }
        )
    watermark_material: dict[str, JsonValue] = {
        "knowledge_cutoff": _timestamp(summary.knowledge_cutoff),
        "observed_through": _timestamp(summary.observed_through),
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
    options_material = {
        "knowledge_cutoff": _timestamp(summary.knowledge_cutoff),
        "observed_through": _timestamp(summary.observed_through),
        "schema_version": summary.schema_version,
    }
    return PopulationAuditReceipt(
        population_run_id=population_run_id,
        verifier_name=_AUDIT_VERIFIER_NAME,
        verifier_version=_AUDIT_VERIFIER_VERSION,
        verifier_code_sha256=verifier_source_artifact_sha256(
            {"provenance/integrity_audit.py": _SOURCE_ROOT / "provenance/integrity_audit.py"}
        ),
        verifier_config_sha256=digest_text(canonical_json(options_material)),
        temporal_scope=summary.temporal_scope,
        verified_at=verified_at,
        required_gate_count=len(expected),
        eligible_count=sum(item.eligible_count for item in coverage),
        verified_count=sum(item.verified_count for item in coverage),
        failed_count=sum(item.failed_count for item in coverage),
        evidence={
            "coverage": cast(JsonValue, [item.model_dump(mode="json") for item in coverage]),
            "findings": cast(
                JsonValue, [item.model_dump(mode="json") for item in summary.findings]
            ),
            "gate_evidence": cast(JsonValue, gate_evidence),
            "has_blockers": False,
            "schema_version": summary.schema_version,
            "tables_present": cast(JsonValue, list(summary.tables_present)),
            "watermark_material": watermark_material,
            "watermark_sha256": digest_text(canonical_json(watermark_material)),
        },
    )


def _retrieval_governance(
    conn: sqlite3.Connection,
    scope: PopulationTemporalScope,
) -> dict[str, JsonValue]:
    observed = _timestamp(scope.observed_through)
    promotion = conn.execute(
        "SELECT promotion.promotion_id,promotion.recorded_at,"
        "evaluation.evaluation_receipt_id,evaluation.evaluated_at,"
        "runtime.runtime_registration_id,runtime.registered_at,"
        "runtime.runtime_artifact_sha256 "
        "FROM search_embedding_model_promotions promotion "
        "JOIN search_embedding_evaluation_receipts evaluation "
        "ON evaluation.evaluation_receipt_id=promotion.evaluation_receipt_id "
        "JOIN search_embedding_runtime_registrations runtime "
        "ON runtime.runtime_registration_id=promotion.runtime_registration_id "
        "WHERE promotion.purpose='evidence_vector_retrieval' "
        "AND datetime(promotion.approved_at)<=datetime(?) "
        "AND datetime(promotion.knowledge_at)<=datetime(?) "
        "AND datetime(promotion.recorded_at)<=datetime(?) "
        "AND datetime(evaluation.evaluated_at)<=datetime(?) "
        "AND datetime(runtime.registered_at)<=datetime(?) "
        "AND NOT EXISTS (SELECT 1 FROM search_embedding_model_promotions newer "
        "WHERE newer.purpose=promotion.purpose AND newer.revision>promotion.revision "
        "AND datetime(newer.approved_at)<=datetime(?) "
        "AND datetime(newer.knowledge_at)<=datetime(?) "
        "AND datetime(newer.recorded_at)<=datetime(?)) "
        "ORDER BY promotion.promotion_id",
        (observed,) * 8,
    ).fetchall()
    if len(promotion) != 1:
        raise PopulationCutoverBlockedError(
            "retrieval_runtime", "governed embedding promotion is missing or ambiguous"
        )
    row = promotion[0]
    vector_seals = conn.execute(
        "SELECT projection.projection_seal_id,projection.sealed_at "
        "FROM search_projection_seals projection "
        "JOIN search_corpus_manifests manifest "
        "ON manifest.manifest_id=projection.manifest_id "
        "JOIN search_corpus_manifest_seals corpus_seal "
        "ON corpus_seal.manifest_id=manifest.manifest_id "
        "WHERE projection.index_kind='vector' "
        "AND projection.runtime_artifact_sha256=? "
        "AND datetime(manifest.knowledge_cutoff)=datetime(?) "
        "AND datetime(manifest.recorded_at)<=datetime(?) "
        "AND datetime(corpus_seal.sealed_at)<=datetime(?) "
        "AND corpus_seal.completion_status='complete' "
        "AND datetime(projection.sealed_at)<=datetime(?) "
        "AND NOT EXISTS (SELECT 1 FROM search_corpus_manifests newer "
        "WHERE newer.corpus_key=manifest.corpus_key AND newer.revision>manifest.revision "
        "AND datetime(newer.knowledge_cutoff)=datetime(?) "
        "AND datetime(newer.recorded_at)<=datetime(?)) "
        "ORDER BY projection.projection_seal_id",
        (
            str(row[6]),
            _timestamp(scope.knowledge_cutoff),
            observed,
            observed,
            observed,
            _timestamp(scope.knowledge_cutoff),
            observed,
        ),
    ).fetchall()
    if not vector_seals:
        raise PopulationCutoverBlockedError(
            "retrieval_runtime", "governed vector projection seals are missing"
        )
    seal_ids = [str(item[0]) for item in vector_seals]
    return {
        "evaluation_evaluated_at": str(row[3]),
        "evaluation_receipt_id": str(row[2]),
        "promotion_id": str(row[0]),
        "promotion_recorded_at": str(row[1]),
        "projection_seal_ids": cast(JsonValue, seal_ids),
        "projection_sealed_at": cast(
            JsonValue, {str(item[0]): str(item[1]) for item in vector_seals}
        ),
        "runtime_registered_at": str(row[5]),
        "runtime_registration_id": str(row[4]),
    }


class _SqliteProjectionReader(ProjectionCoordinateReader):
    def __init__(self, conn: sqlite3.Connection, generation_id: str) -> None:
        self._conn = conn
        self._generation_id = generation_id
        admit_canonical_projection_for_read(conn, generation_id)

    def read_coordinates(
        self,
        *,
        generation_id: str,
        canonical_metric_cell_ids: Sequence[str],
        cutoff_at: datetime,
    ) -> Mapping[str, ProjectionCoordinate]:
        del cutoff_at
        self._require_generation(generation_id)
        result: dict[str, ProjectionCoordinate] = {}
        for start in range(0, len(canonical_metric_cell_ids), 400):
            chunk = canonical_metric_cell_ids[start : start + 400]
            if not chunk:
                continue
            # Values are DB-bound; construction only fixes placeholder arity.
            rows = self._conn.execute(
                _CURRENT_STATE_CTE
                + " SELECT * FROM current_state WHERE canonical_metric_cell_id IN ("  # nosec B608
                + ",".join("?" for _ in chunk)
                + ") ORDER BY canonical_metric_cell_id",
                (generation_id, *chunk),
            ).fetchall()
            for row in rows:
                coordinate = _projection_coordinate(generation_id, row)
                result[coordinate.canonical_metric_cell_id] = coordinate
        return result

    def read_coordinate_page(
        self,
        *,
        generation_id: str,
        after_coordinate: str | None,
        limit: int,
        cutoff_at: datetime,
    ) -> Sequence[ProjectionCoordinate]:
        del cutoff_at
        self._require_generation(generation_id)
        # Query text is fixed and every comparison value remains DB-bound.
        rows = self._conn.execute(
            _CURRENT_STATE_CTE + " SELECT * FROM current_state "  # nosec B608
            "WHERE (? IS NULL OR canonical_metric_cell_id>?) "
            "ORDER BY canonical_metric_cell_id LIMIT ?",
            (generation_id, after_coordinate, after_coordinate, limit),
        ).fetchall()
        return tuple(_projection_coordinate(generation_id, row) for row in rows)

    def _require_generation(self, generation_id: str) -> None:
        if generation_id != self._generation_id:
            raise ValueError("projection reader generation differs from admitted generation")


def _projection_coordinate(
    generation_id: str,
    row: sqlite3.Row,
) -> ProjectionCoordinate:
    change = str(row["change_kind"])
    return ProjectionCoordinate(
        generation_id=generation_id,
        canonical_metric_cell_id=str(row["canonical_metric_cell_id"]),
        change_kind="upsert" if change == "upsert" else "tombstone",
        audit_verified=True,
        canonical_resolution_revision_id=_optional_text(row["canonical_resolution_revision_id"]),
        selected_observation_id=_optional_text(row["selected_observation_id"]),
        value_kind=cast(Literal["numeric", "text", "nil"] | None, row["value_kind"]),
        canonical_value=_optional_text(row["canonical_value"]),
        period_end=_optional_text(row["period_end"]),
        unit_key=_optional_text(row["unit_key"]),
        currency=_optional_text(row["currency"]),
    )


def verify_full_universe_legacy_parity(
    conn: sqlite3.Connection,
    *,
    population_run_id: str,
    scope: PopulationTemporalScope,
    verified_at: datetime,
    page_size: int,
    max_pages: int,
    max_rows_per_issuer: int,
) -> PopulationParityReceipt:
    """Run complete parity for every exact issuer projection at K and O."""

    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT scope.issuer_id,generation.generation_id "
            "FROM canonical_fact_projection_generations generation "
            "JOIN canonical_fact_projection_seals seal "
            "ON seal.generation_id=generation.generation_id "
            "JOIN canonical_fact_projection_scope_bindings binding "
            "ON binding.generation_id=generation.generation_id "
            "JOIN canonical_fact_resolution_snapshot_scope_headers scope "
            "ON scope.resolution_snapshot_id=binding.resolution_snapshot_id "
            "WHERE datetime(generation.cutoff_at)=datetime(?) "
            "AND datetime(generation.recorded_at)=datetime(?) "
            "AND datetime(seal.sealed_at)=datetime(?) "
            "ORDER BY scope.issuer_id,generation.generation_id",
            (_timestamp(scope.knowledge_cutoff),) + (_timestamp(scope.observed_through),) * 2,
        ).fetchall()
        coordinates = tuple((str(row["issuer_id"]), str(row["generation_id"])) for row in rows)
        if not coordinates or len({issuer for issuer, _ in coordinates}) != len(coordinates):
            raise PopulationCutoverBlockedError(
                "parity", "exact issuer projection universe is empty or ambiguous"
            )
        reports: list[ParityReport] = []
        for issuer_id, generation_id in coordinates:
            reports.append(
                scan_legacy_canonical_parity(
                    conn,
                    ParityRequest(
                        temporal_scope=scope,
                        projection_generation_id=generation_id,
                        issuer_id=issuer_id,
                        page_size=page_size,
                        max_pages=max_pages,
                        max_rows=max_rows_per_issuer,
                    ),
                    _SqliteProjectionReader(conn, generation_id),
                )
            )
    finally:
        conn.row_factory = original_row_factory
    if any(not report.complete for report in reports):
        raise PopulationCutoverBlockedError("parity", "one or more issuer scans truncated")
    matched = sum(report.equal_rows for report in reports)
    mismatched = sum(report.mismatch_rows for report in reports)
    blocking = sum(report.blocking_legacy_rows for report in reports)
    absent = blocking - mismatched
    extra = sum(
        report.disposition_counts.get(ParityDisposition.CANONICAL_ONLY_NATIVE.value, 0)
        for report in reports
    )
    eligible = matched + mismatched + absent
    canonical = matched + extra
    if eligible < 1 or canonical < 1:
        raise PopulationCutoverBlockedError("parity", "full-universe parity is empty")
    report_payload: dict[str, JsonValue] = {
        "issuer_reports": cast(
            JsonValue,
            [
                {
                    "canonical_coordinates_scanned": item.canonical_coordinates_scanned,
                    "cutover_ready": item.cutover_ready,
                    "issuer_id": item.issuer_id,
                    "legacy_fact_universe_sha256": item.legacy_fact_universe_sha256,
                    "legacy_rows_scanned": item.legacy_rows_scanned,
                    "parity_rows_sha256": item.parity_rows_sha256,
                    "projection_generation_id": item.projection_generation_id,
                }
                for item in reports
            ],
        ),
        "selection_policy_id": _PARITY_POLICY,
        "temporal_scope": cast(JsonValue, scope.model_dump(mode="json")),
    }
    clean = mismatched == absent == extra == 0 and all(item.cutover_ready for item in reports)
    return PopulationParityReceipt(
        population_run_id=population_run_id,
        eligible_legacy_count=eligible,
        canonical_count=canonical,
        matched_count=matched,
        mismatched_count=mismatched,
        absent_count=absent,
        extra_count=extra,
        status="complete" if clean else "blocked",
        report=report_payload,
        temporal_scope=scope,
        verified_at=verified_at,
    )


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str):
    conn.execute(f"SAVEPOINT {name}")  # nosec B608 -- fixed internal name
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO {name}")  # nosec B608 -- fixed internal name
        conn.execute(f"RELEASE {name}")  # nosec B608 -- fixed internal name
        raise
    else:
        conn.execute(f"RELEASE {name}")  # nosec B608 -- fixed internal name


__all__ = [
    "PopulationCutoverBlockedError",
    "PopulationCutoverEvaluation",
    "PopulationCutoverEvaluationRequest",
    "build_population_audit_receipt",
    "evaluate_population_cutover",
    "verify_full_universe_legacy_parity",
]
