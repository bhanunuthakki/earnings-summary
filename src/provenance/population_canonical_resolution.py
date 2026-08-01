# pyright: reportPrivateUsage=false
"""Populate exact, issuer-scoped canonical resolutions and projections.

The operator treats ``cutoff_at`` as the knowledge clock and ``recorded_at`` as
the system clock.  Its full input graph is committed before the first write,
and every sealing boundary is followed by an exact set-equality audit.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Literal, Self, cast

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionPolicy,
    ResolutionSnapshotScope,
)
from provenance.population_completeness import (
    PopulationArtifactSetCommitment,
    PopulationPlaneVerification,
    PopulationTemporalScope,
    canonical_json,
    digest_text,
    stream_population_artifact_set,
)
from provenance.source_fact_stream import bind_resolution_snapshot_watermark
from search.canonical_fact_projection import (
    ProjectionConfig,
    ProjectionGenerationRequest,
    build_canonical_projection_generation,
    verify_canonical_projection_generation,
)

_POLICY = ResolutionPolicy(
    name="complete_sealed_assertion_resolution",
    version="1",
    config={
        "candidate_admission": "sealed_source_publication_and_active_exact_binding",
        "conflict_policy": "resolve_only_when_all_eligible_values_agree",
        "source_tier_preference": False,
    },
)
_RESOLUTION_SELECTION_POLICY = "canonical-resolution-terminal-at-k-observed-through-o.v1"
_PROJECTION_SELECTION_POLICY = "canonical-projection-terminal-at-k-observed-through-o.v1"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CanonicalResolutionPopulationRequest(_FrozenModel):
    cutoff_at: datetime
    operation_recorded_at: datetime = Field(
        validation_alias=AliasChoices("operation_recorded_at", "recorded_at")
    )
    apply: bool = False
    phase: Literal["resolutions", "snapshots", "projections", "all"] = "all"
    after_canonical_metric_cell_id: str | None = None
    max_cells: int | None = Field(default=None, ge=1)
    input_commitment_sha256: str | None = None
    plan_commitment_sha256: str | None = None

    @field_validator("input_commitment_sha256", "plan_commitment_sha256")
    @classmethod
    def _commitment_sha(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("population commitment must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _commitment_contract(self) -> Self:
        if (
            self.cutoff_at.tzinfo is None
            or self.cutoff_at.utcoffset() is None
            or self.operation_recorded_at.tzinfo is None
            or self.operation_recorded_at.utcoffset() is None
        ):
            raise ValueError("canonical temporal scope must include a timezone")
        if (self.input_commitment_sha256 is None) != (self.plan_commitment_sha256 is None):
            raise ValueError("population commitments must be supplied together")
        bounded = self.after_canonical_metric_cell_id is not None or self.max_cells is not None
        if self.apply and bounded and self.input_commitment_sha256 is None:
            raise ValueError("a bounded apply requires population commitments")
        return self


class CanonicalResolutionCheckpoint(_FrozenModel):
    bounded: bool
    safe_to_seal: bool
    after_canonical_metric_cell_id: str | None
    last_canonical_metric_cell_id: str | None
    processed_cell_count: int = Field(ge=0)
    remaining_cell_count: int = Field(ge=0)
    can_resume: bool


class _CandidateInput(_FrozenModel):
    canonical_metric_cell_id: str
    reporting_entity_id: str
    observation_id: str
    binding_revision_id: str
    subject_binding_revision_id: str
    issuer_id: str
    subject_reporting_entity_id: str
    subject_binding_outcome: str
    subject_binding_knowledge_at: datetime
    subject_binding_recorded_at: datetime
    resolver_candidate_json: str
    resolver_candidate_sha256: str


class CanonicalResolutionScopeManifest(_FrozenModel):
    issuer_id: str
    reporting_entity_ids: tuple[str, ...]
    canonical_cell_count: int = Field(gt=0)
    canonical_cell_set_sha256: str = Field(min_length=64, max_length=64)


class CanonicalResolutionPrewriteManifest(_FrozenModel):
    cutoff_at: datetime
    recorded_at: datetime
    policy_name: str
    policy_version: str
    policy_config_sha256: str
    canonical_cell_count: int = Field(gt=0)
    canonical_cell_set_sha256: str = Field(min_length=64, max_length=64)
    candidate_input_count: int = Field(ge=0)
    candidate_input_set_sha256: str = Field(min_length=64, max_length=64)
    ontology_snapshot_id: str
    ontology_snapshot_member_set_sha256: str
    ontology_member_count: int = Field(gt=0)
    ontology_member_set_sha256: str = Field(min_length=64, max_length=64)
    issuer_scopes: tuple[CanonicalResolutionScopeManifest, ...]

    @property
    def commitment_sha256(self) -> str:
        return _sha(self.model_dump(mode="json"))


class CanonicalResolutionPopulationResult(_FrozenModel):
    mode: Literal["dry_run", "apply"]
    phase: str
    state: Literal["planned", "partial", "complete"]
    expected_cell_count: int
    resolved_cell_count: int
    unresolved_cell_count: int
    retired_cell_count: int
    planned_resolved_cell_count: int
    planned_unresolved_cell_count: int
    planned_retired_cell_count: int
    resolution_reason_counts: dict[str, int]
    resolution_plan_commitment_sha256: str
    processed_cell_count: int
    last_canonical_metric_cell_id: str | None
    expected_issuer_count: int
    resolution_snapshot_count: int
    projection_count: int
    projection_entry_count: int
    input_commitment_sha256: str
    plan_commitment_sha256: str
    post_state_commitment_sha256: str
    output_commitment_sha256: str
    checkpoint: CanonicalResolutionCheckpoint


CanonicalResolutionReceiptOutcome = Literal[
    "planned",
    "applied",
    "checkpoint",
    "blocked",
    "complete",
]


class CanonicalResolutionOperationReceipt(_FrozenModel):
    schema_version: Literal["canonical-resolution-operation-receipt/v1"] = (
        "canonical-resolution-operation-receipt/v1"
    )
    database_path: str = Field(min_length=1, max_length=1_024)
    database_instance_id: str = Field(
        min_length=50,
        max_length=64,
        pattern=r"^database-instance:[0-9a-f]{32}$",
    )
    operation_id: str = Field(
        min_length=95,
        max_length=95,
        pattern=r"^canonical-resolution-operation:[0-9a-f]{64}$",
    )
    alembic_revision: str = Field(min_length=1, max_length=128)
    request: CanonicalResolutionPopulationRequest
    result: CanonicalResolutionPopulationResult
    outcome: CanonicalResolutionReceiptOutcome
    blocker_counts: dict[str, int]
    document_prerequisite_receipt_sha256: str
    prior_checkpoint_receipt_sha256: str | None
    admission_receipt_sha256: str | None
    request_sha256: str
    result_sha256: str
    receipt_sha256: str

    @field_validator(
        "document_prerequisite_receipt_sha256",
        "prior_checkpoint_receipt_sha256",
        "admission_receipt_sha256",
        "request_sha256",
        "result_sha256",
        "receipt_sha256",
    )
    @classmethod
    def _receipt_sha(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("receipt commitment must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _receipt_contract(self) -> Self:
        if self.request.apply != (self.result.mode == "apply"):
            raise ValueError("canonical operation receipt mode does not match its result")
        if self.request.apply != (self.admission_receipt_sha256 is not None):
            raise ValueError("a canonical apply receipt requires one dry-run admission")
        if self.request.after_canonical_metric_cell_id is not None and (
            self.prior_checkpoint_receipt_sha256 is None
        ):
            raise ValueError("a canonical cursor resume must bind one prior checkpoint")
        if self.request_sha256 != _model_sha(self.request):
            raise ValueError("canonical operation request commitment does not match")
        if self.result_sha256 != _model_sha(self.result):
            raise ValueError("canonical operation result commitment does not match")
        if self.operation_id != canonical_resolution_operation_id(
            database_instance_id=self.database_instance_id,
            request=self.request,
            document_prerequisite_receipt_sha256=self.document_prerequisite_receipt_sha256,
            admission_receipt_sha256=self.admission_receipt_sha256,
            prior_checkpoint_receipt_sha256=self.prior_checkpoint_receipt_sha256,
        ):
            raise ValueError("canonical operation identity does not match")
        if self.blocker_counts != _canonical_blocker_counts(self.result):
            raise ValueError("canonical blocker census does not match")
        if self.outcome != _canonical_receipt_outcome(self.request, self.result):
            raise ValueError("canonical operation outcome does not match")
        if self.receipt_sha256 != _canonical_receipt_sha(self):
            raise ValueError("canonical operation receipt commitment does not match")
        return self


def build_canonical_resolution_receipt(
    *,
    database_path: str,
    database_instance_id: str,
    alembic_revision: str,
    request: CanonicalResolutionPopulationRequest,
    result: CanonicalResolutionPopulationResult,
    document_prerequisite_receipt_sha256: str,
    prior_checkpoint_receipt_sha256: str | None,
    admission_receipt_sha256: str | None,
) -> CanonicalResolutionOperationReceipt:
    """Bind one exact canonical population attempt to immutable evidence."""

    payload: dict[str, object] = {
        "schema_version": "canonical-resolution-operation-receipt/v1",
        "database_path": database_path,
        "database_instance_id": database_instance_id,
        "alembic_revision": alembic_revision,
        "request": request,
        "result": result,
        "outcome": _canonical_receipt_outcome(request, result),
        "blocker_counts": _canonical_blocker_counts(result),
        "document_prerequisite_receipt_sha256": document_prerequisite_receipt_sha256,
        "prior_checkpoint_receipt_sha256": prior_checkpoint_receipt_sha256,
        "admission_receipt_sha256": admission_receipt_sha256,
        "request_sha256": _model_sha(request),
        "result_sha256": _model_sha(result),
    }
    payload["operation_id"] = canonical_resolution_operation_id(
        database_instance_id=database_instance_id,
        request=request,
        document_prerequisite_receipt_sha256=document_prerequisite_receipt_sha256,
        admission_receipt_sha256=admission_receipt_sha256,
        prior_checkpoint_receipt_sha256=prior_checkpoint_receipt_sha256,
    )
    payload["receipt_sha256"] = digest_text(
        canonical_json(
            {
                key: value.model_dump(mode="json") if isinstance(value, BaseModel) else value
                for key, value in payload.items()
            }
        )
    )
    return CanonicalResolutionOperationReceipt.model_validate(payload)


def verify_canonical_resolution_receipt(
    receipt: CanonicalResolutionOperationReceipt,
) -> bool:
    """Return whether every nested and top-level canonical commitment agrees."""

    try:
        CanonicalResolutionOperationReceipt.model_validate(receipt.model_dump(mode="json"))
    except ValueError:
        return False
    return True


def database_instance_id(conn: sqlite3.Connection) -> str:
    """Return the immutable identity installed for this database lineage."""

    rows = conn.execute(
        "SELECT database_instance_id FROM database_runtime_identity WHERE singleton=1"
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("canonical database identity is missing or ambiguous")
    value = str(rows[0][0])
    suffix = value.removeprefix("database-instance:")
    if (
        len(value) != 50
        or len(suffix) != 32
        or any(character not in "0123456789abcdef" for character in suffix)
    ):
        raise ValueError("canonical database identity is invalid")
    return value


def persist_canonical_resolution_receipt(
    conn: sqlite3.Connection,
    receipt: CanonicalResolutionOperationReceipt,
) -> bool:
    """Persist one immutable apply receipt inside the caller transaction."""

    if not receipt.request.apply or not verify_canonical_resolution_receipt(receipt):
        raise ValueError("only a valid canonical apply receipt can enter the ledger")
    payload = receipt.model_dump_json()
    values = (
        receipt.operation_id,
        receipt.operation_id,
        receipt.database_instance_id,
        receipt.document_prerequisite_receipt_sha256,
        receipt.request_sha256,
        receipt.result_sha256,
        receipt.receipt_sha256,
        payload,
    )
    cursor = conn.execute(
        "INSERT OR IGNORE INTO canonical_resolution_operation_ledger "
        "(operation_id,idempotency_key,database_instance_id,"
        "document_prerequisite_receipt_sha256,request_sha256,result_sha256,"
        "receipt_sha256,receipt_json) VALUES (?,?,?,?,?,?,?,?)",
        values,
    )
    if cursor.rowcount == 1:
        return True
    existing = conn.execute(
        "SELECT operation_id,idempotency_key,database_instance_id,"
        "document_prerequisite_receipt_sha256,request_sha256,result_sha256,"
        "receipt_sha256,receipt_json FROM canonical_resolution_operation_ledger "
        "WHERE operation_id=?",
        (receipt.operation_id,),
    ).fetchone()
    if existing is None or tuple(existing) != values:
        raise ValueError("canonical operation replay changed immutable evidence")
    return False


def load_canonical_resolution_receipt(
    conn: sqlite3.Connection,
    operation_id: str,
) -> CanonicalResolutionOperationReceipt | None:
    """Load and verify the canonical receipt for one exact operation."""

    row = conn.execute(
        "SELECT receipt_json FROM canonical_resolution_operation_ledger WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    receipt = CanonicalResolutionOperationReceipt.model_validate_json(str(row[0]))
    if not verify_canonical_resolution_receipt(receipt) or receipt.operation_id != operation_id:
        raise ValueError("stored canonical operation receipt is invalid")
    return receipt


def canonical_resolution_operation_id(
    *,
    database_instance_id: str,
    request: CanonicalResolutionPopulationRequest,
    document_prerequisite_receipt_sha256: str,
    admission_receipt_sha256: str | None,
    prior_checkpoint_receipt_sha256: str | None,
) -> str:
    material = canonical_json(
        {
            "admission_receipt_sha256": admission_receipt_sha256,
            "database_instance_id": database_instance_id,
            "document_prerequisite_receipt_sha256": document_prerequisite_receipt_sha256,
            "prior_checkpoint_receipt_sha256": prior_checkpoint_receipt_sha256,
            "request_sha256": _model_sha(request),
        }
    )
    return "canonical-resolution-operation:" + digest_text(material)


def _canonical_receipt_outcome(
    request: CanonicalResolutionPopulationRequest,
    result: CanonicalResolutionPopulationResult,
) -> CanonicalResolutionReceiptOutcome:
    if result.planned_unresolved_cell_count or result.unresolved_cell_count:
        return "blocked"
    if not request.apply:
        return "planned"
    if result.checkpoint.bounded:
        return "checkpoint"
    if result.state != "complete":
        return "blocked"
    if request.phase in {"snapshots", "projections", "all"}:
        return "complete"
    return "applied"


def _canonical_blocker_counts(result: CanonicalResolutionPopulationResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    if result.planned_unresolved_cell_count:
        counts["unresolved_cell"] = result.planned_unresolved_cell_count
    for reason in ("no_admitted_observation", "materially_conflicting_assertions"):
        value = result.resolution_reason_counts.get(reason, 0)
        if value:
            counts[reason] = value
    return dict(sorted(counts.items()))


def _model_sha(model: BaseModel) -> str:
    return digest_text(canonical_json(model.model_dump(mode="json")))


def _canonical_receipt_sha(receipt: CanonicalResolutionOperationReceipt) -> str:
    payload = receipt.model_dump(mode="json")
    payload.pop("receipt_sha256")
    return digest_text(canonical_json(payload))


def verify_canonical_resolution(
    conn: sqlite3.Connection,
    scope: PopulationTemporalScope,
) -> PopulationPlaneVerification:
    """Verify persisted issuer resolution seals at K as actually visible by O."""

    knowledge, observed = _utc(scope.knowledge_cutoff), _utc(scope.observed_through)
    expected = _expected_canonical_issuer_count(conn, knowledge, observed)
    artifacts = stream_population_artifact_set(
        conn,
        table="canonical_fact_resolution_snapshot_seals",
        query=(
            "SELECT seal.resolution_snapshot_id AS artifact_id,"
            "scope.scope_sha256 AS payload_sha256,"
            "seal.member_set_sha256 AS seal_sha256,"
            "scope.cutoff_at AS knowledge_at,"
            "seal.recorded_at AS recorded_at "
            "FROM canonical_fact_resolution_snapshot_scope_headers scope "
            "JOIN canonical_fact_resolution_snapshot_seals seal "
            "ON seal.resolution_snapshot_id=scope.resolution_snapshot_id "
            "WHERE datetime(scope.cutoff_at)=datetime(?) "
            "AND datetime(scope.recorded_at)=datetime(?) "
            "AND datetime(seal.recorded_at)=datetime(?) "
            "ORDER BY scope.issuer_id,seal.resolution_snapshot_id"
        ),
        params=(_db_time(knowledge), _db_time(observed), _db_time(observed)),
        selection_policy_id=_RESOLUTION_SELECTION_POLICY,
    )
    _require_unambiguous_artifact_scope(
        conn,
        table="canonical_fact_resolution_snapshot_scope_headers",
        cutoff_column="cutoff_at",
        recorded_column="recorded_at",
        subject_column="issuer_id",
        knowledge=knowledge,
        observed=observed,
    )
    terminal_ids = _terminal_resolution_ids(conn, knowledge, observed)
    expected_ids = tuple(
        _snapshot_id(item.issuer_id, knowledge, observed)
        for item in _scope_manifest(conn, knowledge, observed)
    )
    _require_terminal_artifact_ids(
        actual=terminal_ids,
        expected=expected_ids,
        plane_name="canonical resolution",
    )
    if len(terminal_ids) == expected:
        original_row_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            manifest = _prewrite_manifest(conn, knowledge, observed)
            _verify_snapshot_sets(conn, knowledge, observed, manifest)
        finally:
            conn.row_factory = original_row_factory
    return _plane_verification(
        plane_name="canonical_resolution",
        scope=scope,
        expected=expected,
        artifacts=(artifacts,),
        selection_policy_id=_RESOLUTION_SELECTION_POLICY,
    )


def verify_canonical_projection(
    conn: sqlite3.Connection,
    scope: PopulationTemporalScope,
) -> PopulationPlaneVerification:
    """Verify persisted issuer projection seals at K as actually visible by O."""

    knowledge, observed = _utc(scope.knowledge_cutoff), _utc(scope.observed_through)
    expected = _expected_canonical_issuer_count(conn, knowledge, observed)
    artifacts = stream_population_artifact_set(
        conn,
        table="canonical_fact_projection_seals",
        query=(
            "SELECT seal.generation_id AS artifact_id,"
            "generation.generation_sha256 AS payload_sha256,"
            "seal.projection_seal_sha256 AS seal_sha256,"
            "generation.cutoff_at AS knowledge_at,"
            "seal.sealed_at AS recorded_at "
            "FROM canonical_fact_projection_generations generation "
            "JOIN canonical_fact_projection_seals seal "
            "ON seal.generation_id=generation.generation_id "
            "JOIN canonical_fact_resolution_snapshot_scope_headers scope "
            "ON scope.resolution_snapshot_id=generation.resolution_snapshot_id "
            "WHERE datetime(generation.cutoff_at)=datetime(?) "
            "AND datetime(generation.recorded_at)=datetime(?) "
            "AND datetime(seal.sealed_at)=datetime(?) "
            "ORDER BY scope.issuer_id,generation.generation_id"
        ),
        params=(_db_time(knowledge), _db_time(observed), _db_time(observed)),
        selection_policy_id=_PROJECTION_SELECTION_POLICY,
    )
    duplicate = conn.execute(
        "SELECT 1 FROM canonical_fact_projection_generations generation "
        "JOIN canonical_fact_projection_seals seal "
        "ON seal.generation_id=generation.generation_id "
        "JOIN canonical_fact_resolution_snapshot_scope_headers scope "
        "ON scope.resolution_snapshot_id=generation.resolution_snapshot_id "
        "WHERE datetime(generation.cutoff_at)=datetime(?) "
        "AND datetime(generation.recorded_at)=datetime(?) "
        "AND datetime(seal.sealed_at)=datetime(?) "
        "GROUP BY scope.issuer_id HAVING COUNT(*)<>1 LIMIT 1",
        (_db_time(knowledge), _db_time(observed), _db_time(observed)),
    ).fetchone()
    if duplicate is not None:
        raise ValueError("canonical projection artifact scope is ambiguous at K,O")
    terminal_ids = _terminal_projection_ids(conn, knowledge, observed)
    expected_ids = tuple(
        _projection_id(item.issuer_id, knowledge, observed)
        for item in _scope_manifest(conn, knowledge, observed)
    )
    _require_terminal_artifact_ids(
        actual=terminal_ids,
        expected=expected_ids,
        plane_name="canonical projection",
    )
    if len(terminal_ids) == expected:
        original_row_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            manifest = _prewrite_manifest(conn, knowledge, observed)
            _verify_projection_sets(conn, knowledge, observed, manifest)
        finally:
            conn.row_factory = original_row_factory
    return _plane_verification(
        plane_name="canonical_projection",
        scope=scope,
        expected=expected,
        artifacts=(artifacts,),
        selection_policy_id=_PROJECTION_SELECTION_POLICY,
    )


def populate_canonical_resolution(
    conn: sqlite3.Connection,
    request: CanonicalResolutionPopulationRequest,
) -> CanonicalResolutionPopulationResult:
    """Populate one phase only after freezing and verifying its full input graph."""

    cutoff, recorded = _utc(request.cutoff_at), _utc(request.operation_recorded_at)
    if recorded < cutoff:
        raise ValueError("population recorded_at must not precede cutoff_at")
    bounded = request.after_canonical_metric_cell_id is not None or request.max_cells is not None
    if bounded and request.phase in {"snapshots", "projections"}:
        raise ValueError("bounded population cannot enter a sealing phase")

    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        manifest = _prewrite_manifest(conn, cutoff, recorded)
        input_sha = manifest.commitment_sha256
        plan_sha = _population_plan_commitment(request, input_sha)
        _verify_commitments(request, input_sha=input_sha, plan_sha=plan_sha)
        planned_counts, reason_counts, resolution_plan_sha = _resolution_plan_summary(
            conn,
            cutoff,
            recorded,
            manifest,
        )
        processed = 0
        last_cell: str | None = None
        effective_phase = "resolutions" if bounded and request.phase == "all" else request.phase
        if request.apply and effective_phase in {"resolutions", "all"}:
            engine = CanonicalFactResolutionEngine(conn)
            batch = _bounded_cells(
                conn,
                cutoff,
                recorded,
                after=request.after_canonical_metric_cell_id,
                limit=request.max_cells,
            )
            for cell_id in batch:
                engine.resolve(cell_id, cutoff, _POLICY, recorded_at=recorded)
                processed += 1
                last_cell = cell_id
        complete_resolutions = _resolution_set_is_exact(conn, cutoff, recorded)
        counts = _status_counts(conn, cutoff, recorded)
        projection_ready = complete_resolutions and counts["unresolved"] == 0

        if request.apply and projection_ready and effective_phase in {"snapshots", "all"}:
            _seal_snapshots(conn, cutoff, recorded, manifest)
            _verify_snapshot_sets(conn, cutoff, recorded, manifest)
        if request.apply and projection_ready and effective_phase in {"projections", "all"}:
            _verify_snapshot_sets(conn, cutoff, recorded, manifest)
            _build_projections(conn, cutoff, recorded, manifest)
            _verify_projection_sets(conn, cutoff, recorded, manifest)

        remaining = _remaining_resolution_count(conn, cutoff, recorded)
        safe_to_seal = not bounded and projection_ready
        state: Literal["planned", "partial", "complete"]
        if not request.apply:
            state = "planned"
        elif bounded or not projection_ready:
            state = "partial"
        else:
            state = "complete"
        checkpoint = CanonicalResolutionCheckpoint(
            bounded=bounded,
            safe_to_seal=safe_to_seal,
            after_canonical_metric_cell_id=request.after_canonical_metric_cell_id,
            last_canonical_metric_cell_id=last_cell,
            processed_cell_count=processed,
            remaining_cell_count=remaining,
            can_resume=bounded and remaining > 0,
        )
        post_state_sha = _output_commitment(conn, cutoff, recorded, manifest)
        return CanonicalResolutionPopulationResult(
            mode="apply" if request.apply else "dry_run",
            phase=request.phase,
            state=state,
            expected_cell_count=manifest.canonical_cell_count,
            resolved_cell_count=counts["resolved"],
            unresolved_cell_count=counts["unresolved"],
            retired_cell_count=counts["retired"],
            planned_resolved_cell_count=planned_counts["resolved"],
            planned_unresolved_cell_count=planned_counts["unresolved"],
            planned_retired_cell_count=planned_counts["retired"],
            resolution_reason_counts=reason_counts,
            resolution_plan_commitment_sha256=resolution_plan_sha,
            processed_cell_count=processed,
            last_canonical_metric_cell_id=last_cell,
            expected_issuer_count=len(manifest.issuer_scopes),
            resolution_snapshot_count=_owned_snapshot_count(conn, cutoff, recorded, manifest),
            projection_count=_owned_projection_count(conn, cutoff, recorded, manifest),
            projection_entry_count=_owned_projection_entry_count(conn, cutoff, recorded, manifest),
            input_commitment_sha256=input_sha,
            plan_commitment_sha256=plan_sha,
            post_state_commitment_sha256=post_state_sha,
            output_commitment_sha256=post_state_sha,
            checkpoint=checkpoint,
        )
    finally:
        conn.row_factory = original_row_factory


def _prewrite_manifest(
    conn: sqlite3.Connection,
    cutoff: datetime,
    recorded: datetime,
) -> CanonicalResolutionPrewriteManifest:
    cell_count, cell_sha = _canonical_cell_commitment(conn, cutoff, recorded)
    if cell_count == 0:
        raise ValueError("canonical resolution manifest has no cells")
    snapshot_id, snapshot_sha, ontology_count = _verified_ontology_snapshot(conn, cutoff, recorded)
    candidate_count, candidate_sha = _candidate_input_commitment(
        conn,
        cutoff,
        recorded,
        ontology_snapshot_id=snapshot_id,
    )
    scopes = _scope_manifest(conn, cutoff, recorded)
    return CanonicalResolutionPrewriteManifest(
        cutoff_at=cutoff,
        recorded_at=recorded,
        policy_name=_POLICY.name,
        policy_version=_POLICY.version,
        policy_config_sha256=_POLICY.config_sha256,
        canonical_cell_count=cell_count,
        canonical_cell_set_sha256=cell_sha,
        candidate_input_count=candidate_count,
        candidate_input_set_sha256=candidate_sha,
        ontology_snapshot_id=snapshot_id,
        ontology_snapshot_member_set_sha256=snapshot_sha,
        ontology_member_count=ontology_count,
        ontology_member_set_sha256=snapshot_sha,
        issuer_scopes=scopes,
    )


class _CanonicalArrayFold:
    def __init__(self) -> None:
        self._digest = hashlib.sha256(b"[")
        self._count = 0
        self._length = 1

    def add(self, value: object) -> None:
        encoded = _canonical_json(value).encode()
        if self._count:
            self._digest.update(b",")
            self._length += 1
        self._digest.update(encoded)
        self._length += len(encoded)
        self._count += 1

    @property
    def count(self) -> int:
        return self._count

    @property
    def final_length(self) -> int:
        return self._length + 1

    def hexdigest(self) -> str:
        digest = self._digest.copy()
        digest.update(b"]")
        return digest.hexdigest()


def _canonical_cell_commitment(
    conn: sqlite3.Connection,
    cutoff: datetime,
    observed_through: datetime,
) -> tuple[int, str]:
    raw_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM canonical_metric_cells "
            "WHERE datetime(knowledge_at)<=datetime(?) "
            "AND datetime(recorded_at)<=datetime(?)",
            (_db_time(cutoff), _db_time(observed_through)),
        ).fetchone()[0]
    )
    fold = _CanonicalArrayFold()
    cursor = conn.execute(
        "SELECT cell.canonical_metric_cell_id,cell.reporting_entity_id,entity.issuer_id "
        "FROM canonical_metric_cells cell "
        "JOIN reporting_entities entity "
        "ON entity.reporting_entity_id=cell.reporting_entity_id "
        "WHERE datetime(cell.knowledge_at)<=datetime(?) "
        "AND datetime(cell.recorded_at)<=datetime(?) "
        "ORDER BY cell.canonical_metric_cell_id",
        (_db_time(cutoff), _db_time(observed_through)),
    )
    for row in cursor:
        fold.add(
            {
                "canonical_metric_cell_id": str(row["canonical_metric_cell_id"]),
                "issuer_id": str(row["issuer_id"]),
                "reporting_entity_id": str(row["reporting_entity_id"]),
            }
        )
    if fold.count != raw_count:
        raise ValueError("canonical cell reporting entity is not registered")
    return fold.count, fold.hexdigest()


def _candidate_input_commitment(
    conn: sqlite3.Connection,
    cutoff: datetime,
    observed_through: datetime,
    *,
    ontology_snapshot_id: str,
) -> tuple[int, str]:
    fold = _CanonicalArrayFold()
    engine = CanonicalFactResolutionEngine(conn)
    rows = conn.execute(
        "SELECT cell.canonical_metric_cell_id,cell.reporting_entity_id,entity.issuer_id "
        "FROM canonical_metric_cells cell "
        "JOIN reporting_entities entity "
        "ON entity.reporting_entity_id=cell.reporting_entity_id "
        "WHERE datetime(cell.knowledge_at)<=datetime(?) "
        "AND datetime(cell.recorded_at)<=datetime(?) "
        "ORDER BY cell.canonical_metric_cell_id",
        (_db_time(cutoff), _db_time(observed_through)),
    )
    for row in rows:
        cell_id = str(row["canonical_metric_cell_id"])
        issuer_id = str(row["issuer_id"])
        reporting_entity_id = str(row["reporting_entity_id"])
        for candidate in engine.candidate_manifest(
            cell_id,
            cutoff,
            observed_through=observed_through,
        ):
            subject = conn.execute(
                "SELECT anchor.subject_binding_revision_id,subject.issuer_id,"
                "subject.reporting_entity_id,subject.outcome,"
                "subject.knowledge_at,subject.recorded_at "
                "FROM fact_reported_observation_anchors_v2 anchor "
                "JOIN recorded_subject_binding_revisions subject "
                "ON subject.binding_revision_id=anchor.subject_binding_revision_id "
                "WHERE anchor.observation_id=? "
                "AND datetime(subject.knowledge_at)<=datetime(?) "
                "AND datetime(subject.recorded_at)<=datetime(?)",
                (
                    candidate.observation_id,
                    _db_time(cutoff),
                    _db_time(observed_through),
                ),
            ).fetchone()
            if subject is None:
                raise ValueError("resolver candidate lacks frozen subject lineage")
            subject_issuer = str(subject["issuer_id"] or "")
            subject_entity = str(subject["reporting_entity_id"] or "")
            if (
                str(subject["outcome"]) != "selected"
                or subject_issuer != issuer_id
                or subject_entity != reporting_entity_id
            ):
                raise ValueError("resolver candidate subject lineage crosses current scope")
            if (
                conn.execute(
                    "SELECT 1 FROM ontology_snapshot_members "
                    "WHERE ontology_snapshot_id=? AND member_kind='binding' AND member_id=?",
                    (ontology_snapshot_id, candidate.binding_revision_id),
                ).fetchone()
                is None
            ):
                raise ValueError("ontology snapshot omits a required active binding")
            item = _CandidateInput(
                canonical_metric_cell_id=cell_id,
                reporting_entity_id=reporting_entity_id,
                observation_id=candidate.observation_id,
                binding_revision_id=candidate.binding_revision_id,
                subject_binding_revision_id=str(subject[0]),
                issuer_id=issuer_id,
                subject_reporting_entity_id=subject_entity,
                subject_binding_outcome=str(subject["outcome"]),
                subject_binding_knowledge_at=_parse_time(subject["knowledge_at"]),
                subject_binding_recorded_at=_parse_time(subject["recorded_at"]),
                resolver_candidate_json=candidate.candidate_json,
                resolver_candidate_sha256=candidate.candidate_sha256,
            )
            fold.add(item.model_dump(mode="json"))
    return fold.count, fold.hexdigest()


def _scope_manifest(
    conn: sqlite3.Connection,
    cutoff: datetime,
    observed_through: datetime,
) -> tuple[CanonicalResolutionScopeManifest, ...]:
    scopes: list[CanonicalResolutionScopeManifest] = []
    issuer_rows = conn.execute(
        "SELECT DISTINCT entity.issuer_id "
        "FROM canonical_metric_cells cell "
        "JOIN reporting_entities entity "
        "ON entity.reporting_entity_id=cell.reporting_entity_id "
        "WHERE datetime(cell.knowledge_at)<=datetime(?) "
        "AND datetime(cell.recorded_at)<=datetime(?) "
        "ORDER BY entity.issuer_id",
        (_db_time(cutoff), _db_time(observed_through)),
    )
    for issuer_row in issuer_rows:
        issuer_id = str(issuer_row[0])
        entities = tuple(
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT cell.reporting_entity_id "
                "FROM canonical_metric_cells cell "
                "JOIN reporting_entities entity "
                "ON entity.reporting_entity_id=cell.reporting_entity_id "
                "WHERE entity.issuer_id=? "
                "AND datetime(cell.knowledge_at)<=datetime(?) "
                "AND datetime(cell.recorded_at)<=datetime(?) "
                "ORDER BY cell.reporting_entity_id",
                (issuer_id, _db_time(cutoff), _db_time(observed_through)),
            )
        )
        fold = _CanonicalArrayFold()
        for cell_row in conn.execute(
            "SELECT cell.canonical_metric_cell_id "
            "FROM canonical_metric_cells cell "
            "JOIN reporting_entities entity "
            "ON entity.reporting_entity_id=cell.reporting_entity_id "
            "WHERE entity.issuer_id=? "
            "AND datetime(cell.knowledge_at)<=datetime(?) "
            "AND datetime(cell.recorded_at)<=datetime(?) "
            "ORDER BY cell.canonical_metric_cell_id",
            (issuer_id, _db_time(cutoff), _db_time(observed_through)),
        ):
            fold.add(str(cell_row[0]))
        scopes.append(
            CanonicalResolutionScopeManifest(
                issuer_id=issuer_id,
                reporting_entity_ids=entities,
                canonical_cell_count=fold.count,
                canonical_cell_set_sha256=fold.hexdigest(),
            )
        )
    return tuple(scopes)


def _verified_ontology_snapshot(
    conn: sqlite3.Connection,
    cutoff: datetime,
    recorded: datetime,
) -> tuple[str, str, int]:
    headers = conn.execute(
        "WITH eligible AS ("
        "SELECT header.ontology_snapshot_id,header.recorded_at,seal.member_count,"
        "length(seal.canonical_member_set_json) AS canonical_length,"
        "seal.member_set_sha256 "
        "FROM ontology_snapshot_headers header "
        "JOIN ontology_snapshot_seals seal "
        "ON seal.ontology_snapshot_id=header.ontology_snapshot_id "
        "WHERE datetime(header.cutoff_at)=datetime(?) "
        "AND datetime(header.recorded_at)<=datetime(?) "
        "AND datetime(seal.sealed_at)<=datetime(?)"
        "), terminal AS ("
        "SELECT MAX(datetime(recorded_at)) AS recorded_at FROM eligible"
        ") "
        "SELECT eligible.ontology_snapshot_id,eligible.member_count,"
        "eligible.canonical_length,eligible.member_set_sha256 "
        "FROM eligible JOIN terminal "
        "ON datetime(eligible.recorded_at)=terminal.recorded_at "
        "ORDER BY eligible.ontology_snapshot_id",
        (_db_time(cutoff), _db_time(recorded), _db_time(recorded)),
    ).fetchall()
    if len(headers) != 1:
        raise ValueError("exactly one terminal sealed ontology snapshot is required at the cutoff")
    snapshot_id = str(headers[0]["ontology_snapshot_id"])
    actual_rows = conn.execute(
        "SELECT member_kind,member_id,member_sha256 "
        "FROM ontology_snapshot_members WHERE ontology_snapshot_id=? "
        "ORDER BY member_kind,member_id",
        (snapshot_id,),
    )
    expected_rows = conn.execute(
        "SELECT member_kind,member_id,member_sha256 "
        "FROM v_ontology_snapshot_expected_members WHERE ontology_snapshot_id=? "
        "ORDER BY member_kind,member_id",
        (snapshot_id,),
    )
    fold = _CanonicalArrayFold()
    while True:
        actual = actual_rows.fetchone()
        expected = expected_rows.fetchone()
        if actual is None or expected is None:
            if actual is not None or expected is not None:
                raise ValueError("ontology snapshot is stale, incomplete, or tampered")
            break
        actual_key = tuple(
            str(actual[key]) for key in ("member_kind", "member_id", "member_sha256")
        )
        expected_key = tuple(
            str(expected[key]) for key in ("member_kind", "member_id", "member_sha256")
        )
        if actual_key != expected_key:
            raise ValueError("ontology snapshot is stale, incomplete, or tampered")
        fold.add({"id": actual_key[1], "kind": actual_key[0], "sha256": actual_key[2]})
    if (
        int(headers[0]["member_count"]) != fold.count
        or int(headers[0]["canonical_length"]) != fold.final_length
        or str(headers[0]["member_set_sha256"]) != fold.hexdigest()
    ):
        raise ValueError("ontology snapshot is stale, incomplete, or tampered")
    mismatch = conn.execute(
        "SELECT 1 FROM ("
        "SELECT member_id FROM ontology_snapshot_members "
        "WHERE ontology_snapshot_id=? AND member_kind='canonical_cell' "
        "EXCEPT SELECT canonical_metric_cell_id FROM canonical_metric_cells "
        "WHERE datetime(knowledge_at)<=datetime(?) AND datetime(recorded_at)<=datetime(?)"
        ") UNION ALL SELECT 1 FROM ("
        "SELECT canonical_metric_cell_id FROM canonical_metric_cells "
        "WHERE datetime(knowledge_at)<=datetime(?) AND datetime(recorded_at)<=datetime(?) "
        "EXCEPT SELECT member_id FROM ontology_snapshot_members "
        "WHERE ontology_snapshot_id=? AND member_kind='canonical_cell'"
        ") LIMIT 1",
        (
            snapshot_id,
            _db_time(cutoff),
            _db_time(recorded),
            _db_time(cutoff),
            _db_time(recorded),
            snapshot_id,
        ),
    ).fetchone()
    if mismatch is not None:
        raise ValueError("ontology snapshot canonical-cell set is not exact")
    return snapshot_id, str(headers[0]["member_set_sha256"]), fold.count


def _seal_snapshots(
    conn: sqlite3.Connection,
    cutoff: datetime,
    recorded: datetime,
    manifest: CanonicalResolutionPrewriteManifest,
) -> None:
    engine = CanonicalFactResolutionEngine(conn)
    for scope in manifest.issuer_scopes:
        snapshot_id = _snapshot_id(scope.issuer_id, cutoff, recorded)
        engine.seal_snapshot(
            snapshot_id,
            cutoff,
            recorded,
            ResolutionSnapshotScope(
                issuer_id=scope.issuer_id,
                reporting_entity_ids=scope.reporting_entity_ids,
            ),
        )
        bind_resolution_snapshot_watermark(
            conn,
            resolution_snapshot_id=snapshot_id,
            cutoff_at=cutoff,
            recorded_at=recorded,
        )


def _verify_snapshot_sets(
    conn: sqlite3.Connection,
    cutoff: datetime,
    recorded: datetime,
    manifest: CanonicalResolutionPrewriteManifest,
) -> None:
    engine = CanonicalFactResolutionEngine(conn)
    for scope in manifest.issuer_scopes:
        snapshot_id = _snapshot_id(scope.issuer_id, cutoff, recorded)
        receipt = engine.verify_snapshot(
            snapshot_id,
            cutoff,
            observed_through=recorded,
        )
        if _utc(receipt.recorded_at) > recorded:
            raise ValueError("resolution snapshot is outside the system cutoff")
        if (
            receipt.scope.issuer_id != scope.issuer_id
            or receipt.scope.reporting_entity_ids != scope.reporting_entity_ids
        ):
            raise ValueError("resolution snapshot scope differs from frozen subject scope")
        if not _snapshot_scope_is_exact(
            conn,
            snapshot_id=snapshot_id,
            issuer_id=scope.issuer_id,
            cutoff=cutoff,
            recorded=recorded,
        ):
            raise ValueError("resolution snapshot cell scope is not exact")


def _build_projections(
    conn: sqlite3.Connection,
    cutoff: datetime,
    recorded: datetime,
    manifest: CanonicalResolutionPrewriteManifest,
) -> None:
    for scope in manifest.issuer_scopes:
        generation_id = _projection_id(scope.issuer_id, cutoff, recorded)
        build_canonical_projection_generation(
            conn,
            ProjectionGenerationRequest(
                generation_id=generation_id,
                idempotency_key=generation_id,
                generation_kind="checkpoint",
                resolution_snapshot_id=_snapshot_id(scope.issuer_id, cutoff, recorded),
                ontology_snapshot_id=manifest.ontology_snapshot_id,
                cutoff_at=cutoff,
                recorded_at=recorded,
                config=ProjectionConfig(),
            ),
        )


def _verify_projection_sets(
    conn: sqlite3.Connection,
    cutoff: datetime,
    recorded: datetime,
    manifest: CanonicalResolutionPrewriteManifest,
) -> None:
    for scope in manifest.issuer_scopes:
        generation_id = _projection_id(scope.issuer_id, cutoff, recorded)
        verify_canonical_projection_generation(
            conn,
            generation_id,
            resolution_snapshot_id=_snapshot_id(scope.issuer_id, cutoff, recorded),
            ontology_snapshot_id=manifest.ontology_snapshot_id,
            cutoff_at=cutoff,
            observed_through=recorded,
        )
        if not _projection_scope_is_exact(
            conn,
            generation_id=generation_id,
            issuer_id=scope.issuer_id,
            cutoff=cutoff,
            recorded=recorded,
        ):
            raise ValueError("projection canonical-cell set is not exact")


def _latest_resolution_sql() -> str:
    return (
        "SELECT canonical_metric_cell_id,status,canonical_resolution_revision_id,"
        "resolution_sha256 FROM canonical_fact_resolution_revisions resolution "
        "WHERE datetime(knowledge_at)<=datetime(?) "
        "AND datetime(recorded_at)<=datetime(?) "
        "AND NOT EXISTS (SELECT 1 "
        "FROM canonical_fact_resolution_revisions newer "
        "WHERE newer.canonical_metric_cell_id=resolution.canonical_metric_cell_id "
        "AND newer.revision>resolution.revision "
        "AND datetime(newer.knowledge_at)<=datetime(?) "
        "AND datetime(newer.recorded_at)<=datetime(?)) "
    )


def _latest_resolution_params(cutoff: datetime, recorded: datetime) -> tuple[str, ...]:
    return (
        _db_time(cutoff),
        _db_time(recorded),
        _db_time(cutoff),
        _db_time(recorded),
    )


def _resolution_set_is_exact(
    conn: sqlite3.Connection,
    cutoff: datetime,
    recorded: datetime,
) -> bool:
    latest = _latest_resolution_sql()
    params = _latest_resolution_params(cutoff, recorded)
    mismatch = conn.execute(
        "SELECT 1 FROM ("  # nosec B608 -- fixed helper SQL; all values are bound
        "SELECT canonical_metric_cell_id FROM canonical_metric_cells "
        "WHERE datetime(knowledge_at)<=datetime(?) AND datetime(recorded_at)<=datetime(?) "
        f"EXCEPT SELECT canonical_metric_cell_id FROM ({latest})"
        ") UNION ALL SELECT 1 FROM ("
        f"SELECT canonical_metric_cell_id FROM ({latest}) "
        "EXCEPT SELECT canonical_metric_cell_id FROM canonical_metric_cells "
        "WHERE datetime(knowledge_at)<=datetime(?) AND datetime(recorded_at)<=datetime(?)"
        ") LIMIT 1",
        (
            _db_time(cutoff),
            _db_time(recorded),
            *params,
            *params,
            _db_time(cutoff),
            _db_time(recorded),
        ),
    ).fetchone()
    return mismatch is None


def _resolution_plan_summary(
    conn: sqlite3.Connection,
    cutoff: datetime,
    recorded: datetime,
    manifest: CanonicalResolutionPrewriteManifest,
) -> tuple[dict[str, int], dict[str, int], str]:
    """Plan every cell without writes and commit the exact deterministic outcomes."""

    engine = CanonicalFactResolutionEngine(conn)
    status_counts = {"resolved": 0, "unresolved": 0, "retired": 0}
    reason_counts: dict[str, int] = {}
    fold = _CanonicalArrayFold()
    for cell_id in _bounded_cells(
        conn,
        cutoff,
        recorded,
        after=None,
        limit=None,
    ):
        plan = engine.plan(
            cell_id,
            cutoff,
            _POLICY,
            observed_through=recorded,
        )
        status_counts[plan.status] += 1
        reason_counts[plan.reason_code] = reason_counts.get(plan.reason_code, 0) + 1
        fold.add(plan.model_dump(mode="json"))
    if fold.count != manifest.canonical_cell_count:
        raise ValueError("canonical resolution plan does not cover the frozen cell set")
    return status_counts, dict(sorted(reason_counts.items())), fold.hexdigest()


def _status_counts(
    conn: sqlite3.Connection,
    cutoff: datetime,
    recorded: datetime,
) -> dict[str, int]:
    counts = {"resolved": 0, "unresolved": 0, "retired": 0}
    rows = conn.execute(
        f"SELECT status,COUNT(*) FROM ({_latest_resolution_sql()}) GROUP BY status",  # nosec B608 -- fixed helper SQL; values are bound
        _latest_resolution_params(cutoff, recorded),
    )
    for status, count in rows:
        counts[str(status)] = int(count)
    return counts


def _bounded_cells(
    conn: sqlite3.Connection,
    cutoff: datetime,
    observed_through: datetime,
    *,
    after: str | None,
    limit: int | None,
) -> Iterator[str]:
    sql = (
        "SELECT canonical_metric_cell_id FROM canonical_metric_cells "
        "WHERE datetime(knowledge_at)<=datetime(?) "
        "AND datetime(recorded_at)<=datetime(?) "
        "AND canonical_metric_cell_id>? ORDER BY canonical_metric_cell_id"
    )
    params: tuple[object, ...] = (
        _db_time(cutoff),
        _db_time(observed_through),
        after or "",
    )
    if limit is not None:
        sql += " LIMIT ?"
        params = (*params, limit)
    return (str(row[0]) for row in conn.execute(sql, params))


def _remaining_resolution_count(
    conn: sqlite3.Connection,
    cutoff: datetime,
    recorded: datetime,
) -> int:
    latest = _latest_resolution_sql()
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM canonical_metric_cells cell "  # nosec B608 -- fixed helper SQL; all values are bound
            "WHERE datetime(cell.knowledge_at)<=datetime(?) "
            "AND datetime(cell.recorded_at)<=datetime(?) "
            f"AND NOT EXISTS (SELECT 1 FROM ({latest}) current "
            "WHERE current.canonical_metric_cell_id=cell.canonical_metric_cell_id)",
            (
                _db_time(cutoff),
                _db_time(recorded),
                *_latest_resolution_params(cutoff, recorded),
            ),
        ).fetchone()[0]
    )


def _snapshot_scope_is_exact(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    issuer_id: str,
    cutoff: datetime,
    recorded: datetime,
) -> bool:
    mismatch = conn.execute(
        "SELECT 1 FROM ("
        "SELECT cell.canonical_metric_cell_id FROM canonical_metric_cells cell "
        "JOIN reporting_entities entity "
        "ON entity.reporting_entity_id=cell.reporting_entity_id "
        "WHERE entity.issuer_id=? "
        "AND datetime(cell.knowledge_at)<=datetime(?) "
        "AND datetime(cell.recorded_at)<=datetime(?) "
        "EXCEPT SELECT canonical_metric_cell_id "
        "FROM canonical_fact_resolution_snapshot_members WHERE resolution_snapshot_id=?"
        ") UNION ALL SELECT 1 FROM ("
        "SELECT canonical_metric_cell_id "
        "FROM canonical_fact_resolution_snapshot_members WHERE resolution_snapshot_id=? "
        "EXCEPT SELECT cell.canonical_metric_cell_id FROM canonical_metric_cells cell "
        "JOIN reporting_entities entity "
        "ON entity.reporting_entity_id=cell.reporting_entity_id "
        "WHERE entity.issuer_id=? "
        "AND datetime(cell.knowledge_at)<=datetime(?) "
        "AND datetime(cell.recorded_at)<=datetime(?)"
        ") LIMIT 1",
        (
            issuer_id,
            _db_time(cutoff),
            _db_time(recorded),
            snapshot_id,
            snapshot_id,
            issuer_id,
            _db_time(cutoff),
            _db_time(recorded),
        ),
    ).fetchone()
    return mismatch is None


def _projection_scope_is_exact(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    issuer_id: str,
    cutoff: datetime,
    recorded: datetime,
) -> bool:
    latest = _latest_resolution_sql()
    params = _latest_resolution_params(cutoff, recorded)
    mismatch = conn.execute(
        "SELECT 1 FROM canonical_fact_projection_entries "  # nosec B608 -- fixed helper SQL; all values are bound
        "WHERE generation_id=? AND change_kind<>'upsert' "
        "UNION ALL SELECT 1 FROM ("
        f"SELECT current.canonical_metric_cell_id FROM ({latest}) current "
        "JOIN canonical_metric_cells cell "
        "ON cell.canonical_metric_cell_id=current.canonical_metric_cell_id "
        "JOIN reporting_entities entity "
        "ON entity.reporting_entity_id=cell.reporting_entity_id "
        "WHERE current.status='resolved' AND entity.issuer_id=? "
        "AND datetime(cell.knowledge_at)<=datetime(?) "
        "AND datetime(cell.recorded_at)<=datetime(?) "
        "EXCEPT SELECT canonical_metric_cell_id FROM canonical_fact_projection_entries "
        "WHERE generation_id=?"
        ") UNION ALL SELECT 1 FROM ("
        "SELECT canonical_metric_cell_id FROM canonical_fact_projection_entries "
        "WHERE generation_id=? "
        f"EXCEPT SELECT current.canonical_metric_cell_id FROM ({latest}) current "
        "JOIN canonical_metric_cells cell "
        "ON cell.canonical_metric_cell_id=current.canonical_metric_cell_id "
        "JOIN reporting_entities entity "
        "ON entity.reporting_entity_id=cell.reporting_entity_id "
        "WHERE current.status='resolved' AND entity.issuer_id=? "
        "AND datetime(cell.knowledge_at)<=datetime(?) "
        "AND datetime(cell.recorded_at)<=datetime(?)"
        ") LIMIT 1",
        (
            generation_id,
            *params,
            issuer_id,
            _db_time(cutoff),
            _db_time(recorded),
            generation_id,
            generation_id,
            *params,
            issuer_id,
            _db_time(cutoff),
            _db_time(recorded),
        ),
    ).fetchone()
    return mismatch is None


def _owned_snapshot_count(
    conn: sqlite3.Connection,
    cutoff: datetime,
    recorded: datetime,
    manifest: CanonicalResolutionPrewriteManifest,
) -> int:
    return sum(
        conn.execute(
            "SELECT COUNT(*) FROM canonical_fact_resolution_snapshot_seals "
            "WHERE resolution_snapshot_id=?",
            (_snapshot_id(scope.issuer_id, cutoff, recorded),),
        ).fetchone()[0]
        for scope in manifest.issuer_scopes
    )


def _owned_projection_count(
    conn: sqlite3.Connection,
    cutoff: datetime,
    recorded: datetime,
    manifest: CanonicalResolutionPrewriteManifest,
) -> int:
    return sum(
        conn.execute(
            "SELECT COUNT(*) FROM canonical_fact_projection_seals WHERE generation_id=?",
            (_projection_id(scope.issuer_id, cutoff, recorded),),
        ).fetchone()[0]
        for scope in manifest.issuer_scopes
    )


def _owned_projection_entry_count(
    conn: sqlite3.Connection,
    cutoff: datetime,
    recorded: datetime,
    manifest: CanonicalResolutionPrewriteManifest,
) -> int:
    return sum(
        conn.execute(
            "SELECT effective_entry_count FROM canonical_fact_projection_seals "
            "WHERE generation_id=?",
            (_projection_id(scope.issuer_id, cutoff, recorded),),
        ).fetchone()[0]
        for scope in manifest.issuer_scopes
        if conn.execute(
            "SELECT 1 FROM canonical_fact_projection_seals WHERE generation_id=?",
            (_projection_id(scope.issuer_id, cutoff, recorded),),
        ).fetchone()
        is not None
    )


def _snapshot_id(issuer_id: str, cutoff: datetime, recorded: datetime) -> str:
    return "population-resolution-snapshot:" + _digest(
        issuer_id,
        _db_time(cutoff),
        _db_time(recorded),
        _POLICY.config_sha256,
    )


def _projection_id(issuer_id: str, cutoff: datetime, recorded: datetime) -> str:
    return "population-projection:" + _digest(
        issuer_id,
        _db_time(cutoff),
        _db_time(recorded),
        _POLICY.config_sha256,
    )


def _output_commitment(
    conn: sqlite3.Connection,
    cutoff: datetime,
    recorded: datetime,
    manifest: CanonicalResolutionPrewriteManifest,
) -> str:
    fold = _CanonicalArrayFold()
    for row in conn.execute(
        _latest_resolution_sql() + "ORDER BY canonical_metric_cell_id",
        _latest_resolution_params(cutoff, recorded),
    ):
        fold.add(
            (
                "resolution",
                str(row["canonical_resolution_revision_id"]),
                str(row["resolution_sha256"]),
            )
        )
    for scope in manifest.issuer_scopes:
        snapshot_id = _snapshot_id(scope.issuer_id, cutoff, recorded)
        snapshot = conn.execute(
            "SELECT member_set_sha256 FROM canonical_fact_resolution_snapshot_seals "
            "WHERE resolution_snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if snapshot is not None:
            fold.add(("snapshot", snapshot_id, str(snapshot[0])))
        generation_id = _projection_id(scope.issuer_id, cutoff, recorded)
        projection = conn.execute(
            "SELECT generation_sha256 FROM canonical_fact_projection_generations "
            "WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        if projection is not None:
            fold.add(("projection", generation_id, str(projection[0])))
    return fold.hexdigest()


def _population_plan_commitment(
    request: CanonicalResolutionPopulationRequest,
    input_sha: str,
) -> str:
    return _sha(
        {
            "after_canonical_metric_cell_id": request.after_canonical_metric_cell_id,
            "cutoff_at": _db_time(request.cutoff_at),
            "input_commitment_sha256": input_sha,
            "max_cells": request.max_cells,
            "phase": request.phase,
            "operation_recorded_at": _db_time(request.operation_recorded_at),
        }
    )


def _verify_commitments(
    request: CanonicalResolutionPopulationRequest,
    *,
    input_sha: str,
    plan_sha: str,
) -> None:
    if request.input_commitment_sha256 is not None and request.input_commitment_sha256 != input_sha:
        raise ValueError("canonical resolution input commitment changed")
    if request.plan_commitment_sha256 is not None and request.plan_commitment_sha256 != plan_sha:
        raise ValueError("canonical resolution plan commitment changed")


def _expected_canonical_issuer_count(
    conn: sqlite3.Connection,
    knowledge: datetime,
    observed: datetime,
) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(DISTINCT entity.issuer_id) "
            "FROM canonical_metric_cells cell "
            "JOIN reporting_entities entity "
            "ON entity.reporting_entity_id=cell.reporting_entity_id "
            "WHERE datetime(cell.knowledge_at)<=datetime(?) "
            "AND datetime(cell.recorded_at)<=datetime(?)",
            (_db_time(knowledge), _db_time(observed)),
        ).fetchone()[0]
    )


def _terminal_resolution_ids(
    conn: sqlite3.Connection,
    knowledge: datetime,
    observed: datetime,
) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT scope.resolution_snapshot_id "
            "FROM canonical_fact_resolution_snapshot_scope_headers scope "
            "JOIN canonical_fact_resolution_snapshot_seals seal "
            "ON seal.resolution_snapshot_id=scope.resolution_snapshot_id "
            "WHERE datetime(scope.cutoff_at)=datetime(?) "
            "AND datetime(scope.recorded_at)=datetime(?) "
            "AND datetime(seal.recorded_at)=datetime(?) "
            "ORDER BY scope.issuer_id,scope.resolution_snapshot_id",
            (_db_time(knowledge), _db_time(observed), _db_time(observed)),
        )
    )


def _terminal_projection_ids(
    conn: sqlite3.Connection,
    knowledge: datetime,
    observed: datetime,
) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT generation.generation_id "
            "FROM canonical_fact_projection_generations generation "
            "JOIN canonical_fact_projection_seals seal "
            "ON seal.generation_id=generation.generation_id "
            "JOIN canonical_fact_resolution_snapshot_scope_headers scope "
            "ON scope.resolution_snapshot_id=generation.resolution_snapshot_id "
            "WHERE datetime(generation.cutoff_at)=datetime(?) "
            "AND datetime(generation.recorded_at)=datetime(?) "
            "AND datetime(seal.sealed_at)=datetime(?) "
            "ORDER BY scope.issuer_id,generation.generation_id",
            (_db_time(knowledge), _db_time(observed), _db_time(observed)),
        )
    )


def _require_terminal_artifact_ids(
    *,
    actual: tuple[str, ...],
    expected: tuple[str, ...],
    plane_name: str,
) -> None:
    if len(actual) != len(set(actual)):
        raise ValueError(f"{plane_name} terminal artifact ids are duplicated at K,O")
    unexpected = tuple(sorted(set(actual) - set(expected)))
    if unexpected:
        raise ValueError(f"{plane_name} terminal artifact ids differ from expected K,O scope")


def _require_unambiguous_artifact_scope(
    conn: sqlite3.Connection,
    *,
    table: str,
    cutoff_column: str,
    recorded_column: str,
    subject_column: str,
    knowledge: datetime,
    observed: datetime,
) -> None:
    allowed = {
        (
            "canonical_fact_resolution_snapshot_scope_headers",
            "cutoff_at",
            "recorded_at",
            "issuer_id",
        )
    }
    if (table, cutoff_column, recorded_column, subject_column) not in allowed:
        raise ValueError("unsupported canonical artifact ambiguity check")
    duplicate = conn.execute(
        f"SELECT 1 FROM {table} "  # nosec B608 -- identifiers are fixed allowlisted constants
        f"WHERE datetime({cutoff_column})=datetime(?) "
        f"AND datetime({recorded_column})=datetime(?) "
        f"GROUP BY {subject_column} HAVING COUNT(*)<>1 LIMIT 1",  # nosec B608
        (_db_time(knowledge), _db_time(observed)),
    ).fetchone()
    if duplicate is not None:
        raise ValueError("canonical resolution artifact scope is ambiguous at K,O")


def _plane_verification(
    *,
    plane_name: Literal["canonical_resolution", "canonical_projection"],
    scope: PopulationTemporalScope,
    expected: int,
    artifacts: tuple[PopulationArtifactSetCommitment, ...],
    selection_policy_id: str,
) -> PopulationPlaneVerification:
    if expected <= 0:
        raise ValueError(f"{plane_name} expected universe is empty at K,O")
    materialized = artifacts[0].row_count
    if materialized > expected:
        raise ValueError(f"{plane_name} persisted artifact set exceeds expected universe")
    failed = expected - materialized
    details = cast(
        dict[str, JsonValue],
        {
            "knowledge_cutoff": _db_time(scope.knowledge_cutoff),
            "observed_through": _db_time(scope.observed_through),
            "selection_policy_id": selection_policy_id,
        },
    )
    output_material = {
        "artifact_sets": [item.model_dump(mode="json") for item in artifacts],
        "details": details,
        "exclusion_counts": {},
        "expected_count": expected,
        "failed_count": failed,
        "materialized_count": materialized,
        "plane_name": plane_name,
    }
    return PopulationPlaneVerification(
        plane_name=plane_name,
        expected_count=expected,
        materialized_count=materialized,
        excluded_count=0,
        failed_count=failed,
        exclusion_counts={},
        input_commitment_sha256=digest_text(
            canonical_json(
                {
                    "expected_count": expected,
                    "knowledge_cutoff": scope.knowledge_cutoff,
                    "observed_through": scope.observed_through,
                    "selection_policy_id": selection_policy_id,
                }
            )
        ),
        output_commitment_sha256=digest_text(canonical_json(output_material)),
        artifact_sets=artifacts,
        details=details,
    )


def _canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: object) -> str:
    material = value if isinstance(value, str) else _canonical_json(value)
    return hashlib.sha256(material.encode()).hexdigest()


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse_time(value: object) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    if not isinstance(value, str):
        raise ValueError("expected an ISO-8601 timestamp")
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
