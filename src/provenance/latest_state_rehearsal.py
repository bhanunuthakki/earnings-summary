"""Fail-closed evidence contracts for the sealed latest-state rehearsal.

The population engines keep ownership of their own database transitions and
immutable ledgers.  This module only binds those transitions into one bounded,
resumable rehearsal chain and supplies the two missing candidate boundaries:
post-mutation sealing and terminal composite readiness evidence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.immutable_artifact import (
    ImmutableArtifactConflictError,
    read_stable_artifact,
    require_no_reparse_points,
)
from provenance.latest_governed_state import LatestGovernedCohortAudit
from provenance.latest_state_activation import (
    candidate_file_identity,
    require_checkpointed_sidecars,
)

_HEX = frozenset("0123456789abcdef")
_SHA_PATTERN = r"^[0-9a-f]{64}$"
_DATABASE_INSTANCE_PATTERN = r"^database-instance:[0-9a-f]{32}$"
_LATEST_TABLES = (
    "latest_governed_refresh_runs",
    "latest_governed_refresh_stage",
    "latest_governed_refresh_receipts",
    "latest_governed_refresh_changes",
    "latest_governed_scope_heads",
    "latest_governed_fact_entries",
    "latest_governed_document_entries",
    "latest_governed_narrative_entries",
    "latest_governed_narrative_fts",
)
_TERMINALLY_EMPTY_TABLES = frozenset(
    {"latest_governed_refresh_stage", "latest_governed_refresh_changes"}
)
SEMANTIC_FACT_CANARY_POLICY_MAX_MILLISECONDS = 10_000.0
SEMANTIC_ISSUER_QUALIFICATION_POLICY_MAX_MILLISECONDS = 60_000.0


class RehearsalError(ValueError):
    """A sealed-rehearsal invariant failed closed."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RehearsalStage(StrEnum):
    """One bounded outer transition per invocation, in canonical order."""

    UPGRADE = "upgrade"
    DOCUMENT = "document"
    ONTOLOGY = "ontology"
    CANONICAL = "canonical"
    ADMISSION_SEAL = "admission_seal"
    ADMISSION_AUDIT = "admission_audit"
    ADMISSION_COVERAGE = "admission_coverage"
    ADMISSION_ELIGIBILITY = "admission_eligibility"
    LATEST = "latest"
    TERMINAL_SEAL = "terminal_seal"
    TERMINAL_AUDIT = "terminal_audit"
    TERMINAL_COVERAGE = "terminal_coverage"
    TERMINAL_ELIGIBILITY = "terminal_eligibility"
    COHORT_AUDIT = "cohort_audit"
    REPLAY = "replay"
    RESTORE = "restore"
    PERFORMANCE = "performance"
    SEMANTIC = "semantic"
    COMPOSITE = "composite"


class ArtifactCommitment(_FrozenModel):
    """Stable identity and content commitment for one immutable small artifact."""

    path: str = Field(min_length=1, max_length=2_048)
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    modified_time_ns: int = Field(ge=0)
    changed_time_ns: int = Field(ge=0)
    file_sha256: str = Field(pattern=_SHA_PATTERN)

    @classmethod
    def from_path(cls, path: Path) -> ArtifactCommitment:
        snapshot, _payload = read_stable_artifact(path)
        return cls(
            path=str(snapshot.path),
            device=snapshot.device,
            inode=snapshot.inode,
            size_bytes=snapshot.size_bytes,
            modified_time_ns=snapshot.modified_time_ns,
            changed_time_ns=snapshot.changed_time_ns,
            file_sha256=snapshot.file_sha256,
        )

    def verify(self) -> bool:
        try:
            return self == ArtifactCommitment.from_path(Path(self.path))
        except (ImmutableArtifactConflictError, OSError, ValueError):
            return False


class DatabaseFileState(_FrozenModel):
    """Stable physical identity for one checkpointed rehearsal database."""

    path: str = Field(min_length=1, max_length=2_048)
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    modified_time_ns: int = Field(ge=0)
    changed_time_ns: int = Field(ge=0)
    file_sha256: str = Field(pattern=_SHA_PATTERN)

    @classmethod
    def from_path(cls, path: Path) -> DatabaseFileState:
        database = path.expanduser().resolve()
        require_checkpointed_sidecars(database)
        before = candidate_file_identity(database)
        file_sha256 = _sha256(database)
        after = candidate_file_identity(database)
        if before != after:
            raise RehearsalError("rehearsal database changed while its state was captured")
        return cls(
            path=str(database),
            device=after.device,
            inode=after.inode,
            size_bytes=after.size_bytes,
            modified_time_ns=after.modified_time_ns,
            changed_time_ns=after.changed_time_ns,
            file_sha256=file_sha256,
        )

    def verify(self) -> bool:
        try:
            return self == DatabaseFileState.from_path(Path(self.path))
        except (OSError, RuntimeError, ValueError):
            return False


class PopulationCheckpointEvidence(_FrozenModel):
    """Exactly one underlying population CLI invocation and its crash boundary."""

    operator: Literal["document", "ontology", "canonical", "latest"]
    mode: Literal["dry_run", "apply"]
    exit_code: Literal[0, 3]
    request_cursor: str | None = Field(default=None, max_length=512)
    result_cursor: str | None = Field(default=None, max_length=512)
    operator_receipt: ArtifactCommitment
    operator_receipt_sha256: str = Field(pattern=_SHA_PATTERN)
    prior_operator_receipt: ArtifactCommitment | None = None
    prior_operator_receipt_sha256: str | None = Field(default=None, pattern=_SHA_PATTERN)
    admission_receipt: ArtifactCommitment | None = None
    admission_receipt_sha256: str | None = Field(default=None, pattern=_SHA_PATTERN)
    database_before: DatabaseFileState
    database_after: DatabaseFileState

    @model_validator(mode="after")
    def _single_invocation(self) -> Self:
        if self.mode == "dry_run" and (
            self.exit_code != 0
            or self.database_before != self.database_after
            or self.admission_receipt is not None
            or self.admission_receipt_sha256 is not None
        ):
            raise ValueError("dry-run population checkpoints must be one nonmutating call")
        if self.mode == "apply" and self.exit_code == 3 and not self.result_cursor:
            raise ValueError("resumable population checkpoints require an exact cursor")
        if self.mode == "apply" and self.exit_code == 0 and self.result_cursor is not None:
            raise ValueError("terminal population checkpoints cannot retain a cursor")
        if (self.prior_operator_receipt is None) != (self.prior_operator_receipt_sha256 is None):
            raise ValueError("prior operator receipt and commitment must be paired")
        if self.mode == "apply" and (
            self.admission_receipt is None or self.admission_receipt_sha256 is None
        ):
            raise ValueError("apply checkpoints require the exact dry-run admission receipt")
        return self


class RehearsalPlan(_FrozenModel):
    """Exact immutable inputs and bounds for a future isolated rehearsal."""

    schema_version: Literal["latest-governed-rehearsal-plan/v2"] = (
        "latest-governed-rehearsal-plan/v2"
    )
    repo_root: str = Field(min_length=1, max_length=2_048)
    database_path: str = Field(min_length=1, max_length=2_048)
    evidence_directory: str = Field(min_length=1, max_length=2_048)
    compressed_clone_receipt: str = Field(min_length=1, max_length=2_048)
    production_scope_registry: str = Field(min_length=1, max_length=2_048)
    expected_source_revision: str = Field(min_length=1, max_length=128)
    expected_target_revision: str = Field(min_length=1, max_length=128)
    cutoff_at: datetime
    operation_recorded_at: datetime
    max_document_obligations: int = Field(ge=1, le=100_000)
    max_ontology_observations: int = Field(ge=1, le=100_000)
    max_canonical_cells: int = Field(ge=1, le=100_000)
    max_latest_batch_rows: int = Field(ge=1, le=10_000)
    high_risk_sample_size: int = Field(ge=1, le=1_000)
    stage_order: tuple[RehearsalStage, ...]
    plan_sha256: str = Field(pattern=_SHA_PATTERN)

    @field_validator("cutoff_at", "operation_recorded_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("rehearsal timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def _contract(self) -> Self:
        if self.stage_order != tuple(RehearsalStage):
            raise ValueError("rehearsal stage order differs from the canonical sequence")
        protected_paths = tuple(
            Path(value).expanduser()
            for value in (
                self.repo_root,
                self.database_path,
                self.evidence_directory,
                self.compressed_clone_receipt,
                self.production_scope_registry,
            )
        )
        if any(not path.is_absolute() or path.resolve() != path for path in protected_paths):
            raise ValueError("rehearsal plan paths must be absolute and canonical")
        for path in protected_paths:
            require_no_reparse_points(path)
        inputs = (
            Path(self.database_path),
            Path(self.compressed_clone_receipt),
            Path(self.production_scope_registry),
        )
        if len(set(inputs)) != len(inputs):
            raise ValueError("rehearsal inputs must be distinct artifacts")
        live = Path(self.repo_root) / "data" / "portfolio.db"
        if Path(self.database_path) == live:
            raise ValueError("rehearsal database must not be the canonical live database")
        if not self.verify_commitment():
            raise ValueError("rehearsal plan commitment mismatch")
        return self

    @classmethod
    def create(cls, **raw: object) -> RehearsalPlan:
        normalized = dict(raw)
        for key in (
            "repo_root",
            "database_path",
            "evidence_directory",
            "compressed_clone_receipt",
            "production_scope_registry",
        ):
            if key not in normalized:
                raise ValueError(f"missing rehearsal plan path: {key}")
            path = Path(str(normalized[key])).expanduser().resolve()
            require_no_reparse_points(path)
            normalized[key] = str(path)
        normalized["stage_order"] = tuple(RehearsalStage)
        normalized.pop("plan_sha256", None)
        normalized.setdefault("schema_version", "latest-governed-rehearsal-plan/v2")
        commitment_payload = {
            key: (
                value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else value
            )
            for key, value in normalized.items()
        }
        normalized["plan_sha256"] = _digest(commitment_payload)
        return cls.model_validate(normalized)

    def verify_commitment(self) -> bool:
        payload = self.model_dump(mode="json")
        stored = str(payload.pop("plan_sha256"))
        return stored == _digest(payload)


class RehearsalCheckpoint(_FrozenModel):
    """One immutable outer checkpoint over an existing operator transition."""

    schema_version: str = "latest-governed-rehearsal-checkpoint/v1"
    plan_sha256: str = Field(pattern=_SHA_PATTERN)
    transition_ordinal: int = Field(ge=0)
    stage: RehearsalStage
    next_stage: RehearsalStage | None
    stage_complete: bool
    database_path: str = Field(min_length=1, max_length=2_048)
    database_instance_id: str = Field(pattern=_DATABASE_INSTANCE_PATTERN)
    alembic_revision: str = Field(min_length=1, max_length=128)
    database_before: DatabaseFileState
    database_after: DatabaseFileState
    prior_checkpoint_path: str | None = Field(default=None, max_length=2_048)
    prior_checkpoint_sha256: str | None = Field(default=None, pattern=_SHA_PATTERN)
    prior_receipt_sha256: str | None = Field(default=None, pattern=_SHA_PATTERN)
    output_artifacts: tuple[ArtifactCommitment, ...] = Field(min_length=1)
    population_checkpoint: PopulationCheckpointEvidence | None = None
    receipt_sha256: str = Field(pattern=_SHA_PATTERN)


def verify_rehearsal_checkpoint(receipt: RehearsalCheckpoint) -> bool:
    payload = receipt.model_dump(mode="json")
    stored = str(payload.pop("receipt_sha256"))
    return stored == _digest(payload)


def build_rehearsal_checkpoint(
    *,
    plan: RehearsalPlan,
    stage: RehearsalStage,
    database_instance_id: str,
    alembic_revision: str,
    database_before: DatabaseFileState,
    database_after: DatabaseFileState,
    output_artifacts: tuple[ArtifactCommitment, ...],
    prior_checkpoint: tuple[ArtifactCommitment, RehearsalCheckpoint] | None,
    stage_complete: bool,
    population_checkpoint: PopulationCheckpointEvidence | None = None,
) -> RehearsalCheckpoint:
    """Advance exactly one canonical stage or one checkpoint within that stage."""

    if not plan.verify_commitment():
        raise RehearsalError("rehearsal plan commitment mismatch")
    if prior_checkpoint is not None and prior_checkpoint[1].next_stage is not stage:
        raise RehearsalError("rehearsal stage order cannot be skipped")
    if prior_checkpoint is not None and not prior_checkpoint[0].verify():
        raise RehearsalError("prior checkpoint artifact changed")
    if not output_artifacts or any(not item.verify() for item in output_artifacts):
        raise RehearsalError("stage output artifact is missing or changed")
    population_stages = {
        RehearsalStage.DOCUMENT: "document",
        RehearsalStage.ONTOLOGY: "ontology",
        RehearsalStage.CANONICAL: "canonical",
        RehearsalStage.LATEST: "latest",
    }
    if stage in population_stages:
        if (
            population_checkpoint is None
            or population_checkpoint.operator != population_stages[stage]
            or not population_checkpoint.operator_receipt.verify()
            or population_checkpoint.operator_receipt not in output_artifacts
        ):
            raise RehearsalError("population stage lacks its exact single-call checkpoint")
        expected_complete = (
            population_checkpoint.mode == "apply" and population_checkpoint.exit_code == 0
        )
        if stage_complete != expected_complete:
            raise RehearsalError("population stage completion differs from operator receipt")
        if population_checkpoint.database_before != database_before or (
            population_checkpoint.database_after != database_after
        ):
            raise RehearsalError("population evidence differs from outer database states")
    elif population_checkpoint is not None:
        raise RehearsalError("non-population stages cannot carry a population checkpoint")
    prior_file_sha: str | None = None
    prior_receipt_sha: str | None = None
    if prior_checkpoint is None:
        if stage is not RehearsalStage.UPGRADE:
            raise RehearsalError("rehearsal stage order must begin with upgrade")
    else:
        prior_artifact, prior = prior_checkpoint
        if not prior_artifact.verify():
            raise RehearsalError("prior checkpoint artifact changed")
        try:
            _snapshot, payload = read_stable_artifact(Path(prior_artifact.path))
            observed = RehearsalCheckpoint.model_validate_json(payload)
        except (ImmutableArtifactConflictError, OSError, ValueError) as exc:
            raise RehearsalError("prior checkpoint artifact changed") from exc
        if observed != prior or not verify_rehearsal_checkpoint(prior):
            raise RehearsalError("prior checkpoint artifact changed")
        if prior.next_stage is not stage:
            raise RehearsalError("rehearsal stage order cannot be skipped")
        if prior.plan_sha256 != plan.plan_sha256:
            raise RehearsalError("prior checkpoint belongs to a different plan")
        if prior.database_instance_id != database_instance_id:
            raise RehearsalError("rehearsal database instance changed")
        if prior.database_after != database_before:
            raise RehearsalError("rehearsal database bytes changed between checkpoints")
        if prior.stage is stage and stage in population_stages:
            previous = prior.population_checkpoint
            current = population_checkpoint
            if previous is None or current is None:
                raise RehearsalError("population checkpoint sequence is incomplete")
            if current.mode == "apply":
                if (
                    previous.mode != "dry_run"
                    or current.admission_receipt != previous.operator_receipt
                    or current.admission_receipt_sha256 != previous.operator_receipt_sha256
                    or current.request_cursor != previous.request_cursor
                    or current.prior_operator_receipt != previous.prior_operator_receipt
                    or current.prior_operator_receipt_sha256
                    != previous.prior_operator_receipt_sha256
                ):
                    raise RehearsalError("apply checkpoint differs from its dry-run admission")
            elif (
                previous.mode != "apply"
                or previous.exit_code != 3
                or current.request_cursor != previous.result_cursor
                or current.prior_operator_receipt != previous.operator_receipt
                or current.prior_operator_receipt_sha256 != previous.operator_receipt_sha256
            ):
                raise RehearsalError("resumed dry run differs from the prior apply checkpoint")
        prior_file_sha = prior_artifact.file_sha256
        prior_receipt_sha = prior.receipt_sha256
    transition_ordinal = (
        0 if prior_checkpoint is None else prior_checkpoint[1].transition_ordinal + 1
    )
    stage_index = plan.stage_order.index(stage)
    if stage_complete:
        next_stage = (
            plan.stage_order[stage_index + 1] if stage_index + 1 < len(plan.stage_order) else None
        )
    else:
        next_stage = stage
    core = {
        "alembic_revision": alembic_revision,
        "database_instance_id": database_instance_id,
        "database_path": plan.database_path,
        "database_before": database_before.model_dump(mode="json"),
        "database_after": database_after.model_dump(mode="json"),
        "next_stage": next_stage,
        "output_artifacts": [item.model_dump(mode="json") for item in output_artifacts],
        "plan_sha256": plan.plan_sha256,
        "population_checkpoint": (
            None if population_checkpoint is None else population_checkpoint.model_dump(mode="json")
        ),
        "prior_checkpoint_path": (None if prior_checkpoint is None else prior_checkpoint[0].path),
        "prior_checkpoint_sha256": prior_file_sha,
        "prior_receipt_sha256": prior_receipt_sha,
        "schema_version": "latest-governed-rehearsal-checkpoint/v1",
        "stage": stage,
        "stage_complete": stage_complete,
        "transition_ordinal": transition_ordinal,
    }
    return RehearsalCheckpoint.model_validate(core | {"receipt_sha256": _digest(core)})


class GroundedCanaryCommitment(_FrozenModel):
    """One reproducible fact, lexical, or semantic retrieval canary."""

    canary_kind: Literal["fact", "lexical", "semantic"]
    query_sha256: str = Field(pattern=_SHA_PATTERN)
    result_set_sha256: str = Field(pattern=_SHA_PATTERN)
    result_count: int = Field(gt=0)
    elapsed_milliseconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    document_family: str | None = Field(default=None, min_length=1, max_length=64)
    backend_receipt_sha256: str | None = Field(default=None, pattern=_SHA_PATTERN)
    expected_source_id: str | None = Field(default=None, min_length=1, max_length=128)
    observed_rank: int | None = Field(default=None, ge=1, le=1_000)

    @model_validator(mode="after")
    def _kind_coordinates(self) -> Self:
        if (self.canary_kind == "lexical") != (self.document_family is not None):
            raise ValueError("only lexical canaries carry a document family")
        if (self.canary_kind == "semantic") != (self.backend_receipt_sha256 is not None):
            raise ValueError("only semantic canaries carry a backend receipt")
        if (self.canary_kind in {"fact", "semantic"}) != (
            self.expected_source_id is not None and self.observed_rank is not None
        ):
            raise ValueError("fact and semantic canaries require an exact grounded source rank")
        if (self.canary_kind == "fact") != (self.elapsed_milliseconds is not None):
            raise ValueError("only fact canaries carry an attributable per-canary latency")
        return self


class SemanticIssuerQualification(_FrozenModel):
    """Issuer-owned corpus, runtime, projection, and narrative canary evidence."""

    issuer_id: str = Field(min_length=1, max_length=128)
    corpus_manifest_id: str = Field(min_length=1, max_length=128)
    corpus_membership_sha256: str = Field(pattern=_SHA_PATTERN)
    corpus_expected_document_count: int = Field(gt=0)
    corpus_included_document_count: int = Field(gt=0)
    corpus_chunk_count: int = Field(gt=0)
    lexical_index_run_id: str = Field(min_length=1, max_length=128)
    vector_index_run_id: str = Field(min_length=1, max_length=128)
    projection_seal_id: str = Field(min_length=1, max_length=128)
    projection_records_sha256: str = Field(pattern=_SHA_PATTERN)
    projection_artifact_set_sha256: str = Field(pattern=_SHA_PATTERN)
    embedding_promotion_id: str = Field(min_length=1, max_length=128)
    runtime_registration_id: str = Field(min_length=1, max_length=128)
    runtime_artifact_sha256: str = Field(pattern=_SHA_PATTERN)
    lexical_canaries: tuple[GroundedCanaryCommitment, ...] = Field(min_length=1)
    semantic_canary: GroundedCanaryCommitment
    qualification_elapsed_milliseconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _complete(self) -> Self:
        families = tuple(item.document_family for item in self.lexical_canaries)
        normalized_families = tuple(item for item in families if item is not None)
        if (
            self.corpus_expected_document_count != self.corpus_included_document_count
            or self.semantic_canary.canary_kind != "semantic"
            or any(item.canary_kind != "lexical" for item in self.lexical_canaries)
            or len(normalized_families) != len(families)
            or normalized_families != tuple(sorted(set(normalized_families)))
        ):
            raise ValueError("semantic issuer qualification is incomplete or ambiguous")
        return self


class SemanticScopeQualification(_FrozenModel):
    """Scope-owned Ask/fact evidence bound to one issuer runtime qualification."""

    scope_id: str = Field(min_length=1, max_length=256)
    source_scope_key: str = Field(min_length=1, max_length=128)
    source_scope_revision_id: str = Field(min_length=1, max_length=128)
    issuer_id: str = Field(min_length=1, max_length=128)
    reporting_entity_id: str = Field(min_length=1, max_length=128)
    ask_promotion_id: str = Field(min_length=1, max_length=128)
    research_snapshot_id: str = Field(min_length=1, max_length=128)
    fact_generation_id: str = Field(min_length=1, max_length=128)
    fact_projection_seal_sha256: str = Field(pattern=_SHA_PATTERN)
    source_inventory_count: int = Field(gt=0)
    source_inventory_set_sha256: str = Field(pattern=_SHA_PATTERN)
    corpus_manifest_id: str = Field(min_length=1, max_length=128)
    lexical_index_run_id: str = Field(min_length=1, max_length=128)
    vector_index_run_id: str = Field(min_length=1, max_length=128)
    embedding_promotion_id: str = Field(min_length=1, max_length=128)
    fact_canary: GroundedCanaryCommitment

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.fact_canary.canary_kind != "fact":
            raise ValueError("semantic scope qualification is incomplete or ambiguous")
        return self


class SemanticQualificationEvidence(_FrozenModel):
    """Exact Ask/runtime qualification required before reader promotion."""

    schema_version: Literal["latest-governed-semantic-qualification/v2"] = (
        "latest-governed-semantic-qualification/v2"
    )
    database_sha256: str = Field(pattern=_SHA_PATTERN)
    request_artifact: ArtifactCommitment
    registry_artifact: ArtifactCommitment
    production_scope_ids: tuple[str, ...] = Field(min_length=1)
    issuer_qualifications: tuple[SemanticIssuerQualification, ...] = Field(min_length=1)
    scope_qualifications: tuple[SemanticScopeQualification, ...] = Field(min_length=1)
    corpus_document_count: int = Field(gt=0)
    grounded_fact_canary_count: int = Field(gt=0)
    grounded_narrative_canary_count: int = Field(gt=0)
    failure_count: Literal[0] = 0
    max_fact_canary_milliseconds: float = Field(
        gt=0,
        le=SEMANTIC_FACT_CANARY_POLICY_MAX_MILLISECONDS,
        allow_inf_nan=False,
    )
    max_issuer_qualification_milliseconds: float = Field(
        gt=0,
        le=SEMANTIC_ISSUER_QUALIFICATION_POLICY_MAX_MILLISECONDS,
        allow_inf_nan=False,
    )
    observed_max_fact_canary_milliseconds: float = Field(gt=0, allow_inf_nan=False)
    observed_max_issuer_qualification_milliseconds: float = Field(gt=0, allow_inf_nan=False)
    qualification_sha256: str = Field(pattern=_SHA_PATTERN)

    @model_validator(mode="after")
    def _ready(self) -> Self:
        scope_ids = tuple(item.scope_id for item in self.scope_qualifications)
        optional_fact_times = tuple(
            item.fact_canary.elapsed_milliseconds for item in self.scope_qualifications
        )
        if any(item is None for item in optional_fact_times):
            raise ValueError("semantic fact canary latency is missing")
        fact_times = tuple(cast(float, item) for item in optional_fact_times)
        issuer_times = tuple(
            item.qualification_elapsed_milliseconds for item in self.issuer_qualifications
        )
        issuer_ids = tuple(item.issuer_id for item in self.issuer_qualifications)
        if issuer_ids != tuple(sorted(set(issuer_ids))):
            raise ValueError("semantic issuer qualifications must be unique and sorted")
        by_issuer = {item.issuer_id: item for item in self.issuer_qualifications}
        manifests: dict[str, tuple[str, int, int, int]] = {}
        for item in self.issuer_qualifications:
            coordinates = (
                item.corpus_membership_sha256,
                item.corpus_expected_document_count,
                item.corpus_included_document_count,
                item.corpus_chunk_count,
            )
            existing = manifests.setdefault(item.corpus_manifest_id, coordinates)
            if existing != coordinates:
                raise ValueError("shared semantic corpus coordinates are inconsistent")
        expected_documents = sum(item[1] for item in manifests.values())
        narrative_canaries = sum(
            len(item.lexical_canaries) + 1 for item in self.issuer_qualifications
        )
        scope_runtime_mismatch = any(
            (issuer := by_issuer.get(item.issuer_id)) is None
            or (
                item.corpus_manifest_id,
                item.lexical_index_run_id,
                item.vector_index_run_id,
                item.embedding_promotion_id,
            )
            != (
                issuer.corpus_manifest_id,
                issuer.lexical_index_run_id,
                issuer.vector_index_run_id,
                issuer.embedding_promotion_id,
            )
            for item in self.scope_qualifications
        )
        if (
            tuple(sorted(set(self.production_scope_ids))) != self.production_scope_ids
            or scope_ids != self.production_scope_ids
            or len({item.ask_promotion_id for item in self.scope_qualifications})
            != len(self.scope_qualifications)
            or scope_runtime_mismatch
            or self.corpus_document_count != expected_documents
            or self.grounded_fact_canary_count != len(self.scope_qualifications)
            or self.grounded_narrative_canary_count != narrative_canaries
            or self.observed_max_fact_canary_milliseconds != max(fact_times)
            or self.observed_max_issuer_qualification_milliseconds != max(issuer_times)
            or self.observed_max_fact_canary_milliseconds > self.max_fact_canary_milliseconds
            or self.observed_max_issuer_qualification_milliseconds
            > self.max_issuer_qualification_milliseconds
        ):
            raise ValueError("semantic qualification is incomplete or ambiguous")
        payload = self.model_dump(mode="json", exclude={"qualification_sha256"})
        if self.qualification_sha256 != _digest(payload):
            raise ValueError("semantic qualification commitment mismatch")
        return self


def build_semantic_qualification_evidence(
    **fields: object,
) -> SemanticQualificationEvidence:
    """Seal semantic/runtime evidence after its owning verifier succeeds."""

    core = dict(fields)
    core.pop("qualification_sha256", None)
    normalized = {key: _json_value(value) for key, value in core.items()}
    normalized.setdefault("schema_version", "latest-governed-semantic-qualification/v2")
    return SemanticQualificationEvidence.model_validate(
        normalized | {"qualification_sha256": _digest(normalized)}
    )


class AdmissionBundle(_FrozenModel):
    """Pre-population candidate admission; it remains immutable after writes."""

    candidate_audit: ArtifactCommitment
    candidate_coverage: ArtifactCommitment
    bound_eligibility: ArtifactCommitment
    production_scope_ids: tuple[str, ...] = Field(min_length=1)
    terminal_commitments: dict[str, str]

    @model_validator(mode="after")
    def _cohort(self) -> Self:
        if (
            self.production_scope_ids != tuple(sorted(set(self.production_scope_ids)))
            or set(self.terminal_commitments) != set(self.production_scope_ids)
            or any(
                len(value) != 64 or any(character not in _HEX for character in value)
                for value in self.terminal_commitments.values()
            )
        ):
            raise ValueError("admission bundle cohort or terminal commitments are invalid")
        return self


class TerminalReadinessBundle(_FrozenModel):
    """Fresh post-population admission and exact latest-state terminal evidence."""

    candidate_seal: ArtifactCommitment
    candidate_audit: ArtifactCommitment
    candidate_coverage: ArtifactCommitment
    bound_eligibility: ArtifactCommitment
    latest_population_receipt: ArtifactCommitment
    cohort_audit: ArtifactCommitment
    production_scope_ids: tuple[str, ...] = Field(min_length=1)
    terminal_commitments: dict[str, str]

    @model_validator(mode="after")
    def _cohort(self) -> Self:
        AdmissionBundle(
            candidate_audit=self.candidate_audit,
            candidate_coverage=self.candidate_coverage,
            bound_eligibility=self.bound_eligibility,
            production_scope_ids=self.production_scope_ids,
            terminal_commitments=self.terminal_commitments,
        )
        return self


class ActivationBoundaryRequirements(_FrozenModel):
    """Requirements a later live activation must satisfy with fresh evidence."""

    expected_task_paths: tuple[str, ...] = Field(min_length=1)
    expected_service_names: tuple[str, ...] = Field(min_length=1)
    expected_listener_endpoints: tuple[str, ...] = Field(min_length=1)
    requires_fresh_live_rollback_snapshot: Literal[True]
    requires_unexpired_quiescence_receipt: Literal[True]
    requires_restoration_receipt: Literal[True]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        for values in (
            self.expected_task_paths,
            self.expected_service_names,
            self.expected_listener_endpoints,
        ):
            if len(set(values)) != len(values):
                raise ValueError("activation boundary inventories must be unique")
        return self


class RestoreRoundtripEvidence(_FrozenModel):
    """Disposable restore/replay evidence without touching the admitted database."""

    rollback_database_sha256: str = Field(pattern=_SHA_PATTERN)
    mutated_database_sha256: str = Field(pattern=_SHA_PATTERN)
    restored_database_sha256: str = Field(pattern=_SHA_PATTERN)
    quick_check: str
    integrity_check: str
    foreign_key_violation_count: int = Field(ge=0)
    replay_equivalent: bool

    @model_validator(mode="after")
    def _clean(self) -> Self:
        if (
            self.rollback_database_sha256 != self.restored_database_sha256
            or self.mutated_database_sha256 == self.rollback_database_sha256
            or self.quick_check != "ok"
            or self.integrity_check != "ok"
            or self.foreign_key_violation_count
            or not self.replay_equivalent
        ):
            raise ValueError("restore roundtrip evidence is not exact and clean")
        return self


class ExactReplayEvidence(_FrozenModel):
    """Exact export/ledger replay proof across every mutating operator."""

    database_sha256_before: str = Field(pattern=_SHA_PATTERN)
    database_sha256_after: str = Field(pattern=_SHA_PATTERN)
    operator_receipt_sha256s: tuple[str, ...] = Field(min_length=4)
    database_ledger_match_count: int = Field(ge=4)
    no_clobber_replay_count: int = Field(ge=4)

    @field_validator("operator_receipt_sha256s")
    @classmethod
    def _hashes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            len(value) != 64 or any(character not in _HEX for character in value)
            for value in values
        ):
            raise ValueError("replay receipt commitment is malformed")
        return values

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if (
            self.database_sha256_before != self.database_sha256_after
            or self.database_ledger_match_count < len(self.operator_receipt_sha256s)
            or self.no_clobber_replay_count < len(self.operator_receipt_sha256s)
        ):
            raise ValueError("operator replay changed the database or lacked exact evidence")
        return self


class CandidateScopePerformance(_FrozenModel):
    """Per-scope public-read latency and query-plan evidence."""

    scope_id: str = Field(min_length=1, max_length=256)
    sample_count: int = Field(ge=3)
    fact_read_p95_milliseconds: float = Field(ge=0)
    narrative_read_p95_milliseconds: float = Field(ge=0)
    fact_query_uses_production_index: bool
    narrative_query_uses_fts_index: bool
    fact_query_plan: tuple[str, ...] = Field(min_length=1)
    narrative_query_plan: tuple[str, ...] = Field(min_length=1)


class CandidatePerformanceEvidence(_FrozenModel):
    """Candidate-specific read/no-op/query-plan evidence plus synthetic ratchets."""

    database_sha256: str = Field(pattern=_SHA_PATTERN)
    performance_request: ArtifactCommitment
    synthetic_benchmark_report: ArtifactCommitment
    candidate_no_op_receipt: ArtifactCommitment
    synthetic_benchmark_profile: Literal["production"]
    synthetic_benchmark_passed: bool
    no_op_current_write_count: int = Field(ge=0)
    measured_scope_ids: tuple[str, ...] = Field(min_length=1)
    scope_measurements: tuple[CandidateScopePerformance, ...] = Field(min_length=1)
    read_sample_count: int = Field(ge=1)
    fact_read_p95_milliseconds: float = Field(ge=0)
    narrative_read_p95_milliseconds: float = Field(ge=0)
    max_fact_read_p95_milliseconds: float = Field(gt=0)
    max_narrative_read_p95_milliseconds: float = Field(gt=0)
    fact_query_uses_production_index: bool
    narrative_query_uses_fts_index: bool
    fact_query_index_use_count: int = Field(ge=0)
    narrative_fts_index_use_count: int = Field(ge=0)
    fact_query_plan: tuple[str, ...] = Field(min_length=1)
    narrative_query_plan: tuple[str, ...] = Field(min_length=1)
    history_scale_ratio: float = Field(gt=0)
    max_history_scale_ratio: float = Field(gt=0)

    @model_validator(mode="after")
    def _passed(self) -> Self:
        measurement_scope_ids = tuple(item.scope_id for item in self.scope_measurements)
        max_fact_p95 = max(item.fact_read_p95_milliseconds for item in self.scope_measurements)
        max_narrative_p95 = max(
            item.narrative_read_p95_milliseconds for item in self.scope_measurements
        )
        if (
            self.measured_scope_ids != tuple(sorted(set(self.measured_scope_ids)))
            or measurement_scope_ids != self.measured_scope_ids
            or not self.performance_request.verify()
            or not self.synthetic_benchmark_report.verify()
            or not self.candidate_no_op_receipt.verify()
            or not self.synthetic_benchmark_passed
            or self.no_op_current_write_count
            or self.read_sample_count != sum(item.sample_count for item in self.scope_measurements)
            or self.fact_read_p95_milliseconds != max_fact_p95
            or self.narrative_read_p95_milliseconds != max_narrative_p95
            or self.fact_read_p95_milliseconds > self.max_fact_read_p95_milliseconds
            or self.narrative_read_p95_milliseconds > self.max_narrative_read_p95_milliseconds
            or not self.fact_query_uses_production_index
            or not self.narrative_query_uses_fts_index
            or self.fact_query_index_use_count != len(self.measured_scope_ids)
            or self.narrative_fts_index_use_count != len(self.measured_scope_ids)
            or any(
                item.fact_read_p95_milliseconds > self.max_fact_read_p95_milliseconds
                or item.narrative_read_p95_milliseconds > self.max_narrative_read_p95_milliseconds
                or not item.fact_query_uses_production_index
                or not item.narrative_query_uses_fts_index
                for item in self.scope_measurements
            )
            or self.history_scale_ratio > self.max_history_scale_ratio
        ):
            raise ValueError("candidate-specific performance evidence failed its ratchets")
        return self


class RehearsalReadinessReceipt(_FrozenModel):
    """Single immutable composite receipt that can enable a later activation gate."""

    schema_version: Literal["latest-governed-sealed-rehearsal-readiness/v2"] = (
        "latest-governed-sealed-rehearsal-readiness/v2"
    )
    status: Literal["ready"] = "ready"
    plan_sha256: str = Field(pattern=_SHA_PATTERN)
    database_path: str = Field(min_length=1, max_length=2_048)
    database_instance_id: str = Field(pattern=_DATABASE_INSTANCE_PATTERN)
    database_sha256: str = Field(pattern=_SHA_PATTERN)
    alembic_revision: str = Field(min_length=1, max_length=128)
    production_scope_ids: tuple[str, ...] = Field(min_length=1)
    table_counts: dict[str, int]
    stage_artifacts: tuple[ArtifactCommitment, ...] = Field(min_length=len(RehearsalStage) - 1)
    admission_bundle: AdmissionBundle
    terminal_bundle: TerminalReadinessBundle
    semantic_qualification: SemanticQualificationEvidence
    activation_boundary_requirements: ActivationBoundaryRequirements
    restore_roundtrip: RestoreRoundtripEvidence
    exact_replay_verified: bool
    candidate_performance_passed: bool
    exhaustive_parity_failure_count: Literal[0] = 0
    cross_scope_leakage_count: Literal[0] = 0
    retrieval_canary_failure_count: Literal[0] = 0
    fts_failure_count: Literal[0] = 0
    receipt_sha256: str = Field(pattern=_SHA_PATTERN)


def verify_rehearsal_readiness_receipt(receipt: RehearsalReadinessReceipt) -> bool:
    payload = receipt.model_dump(mode="json")
    stored = str(payload.pop("receipt_sha256"))
    return stored == _digest(payload)


def build_rehearsal_readiness_receipt(
    *,
    plan: RehearsalPlan,
    database_instance_id: str,
    database_sha256: str,
    alembic_revision: str,
    production_scope_ids: tuple[str, ...],
    table_counts: dict[str, int],
    stage_artifacts: tuple[ArtifactCommitment, ...],
    admission_bundle: AdmissionBundle,
    terminal_bundle: TerminalReadinessBundle,
    cohort_audit: LatestGovernedCohortAudit,
    semantic_qualification: SemanticQualificationEvidence,
    activation_boundary_requirements: ActivationBoundaryRequirements,
    restore_roundtrip: RestoreRoundtripEvidence,
    exact_replay_verified: bool,
    candidate_performance_passed: bool,
) -> RehearsalReadinessReceipt:
    """Build readiness only after every non-negotiable boundary is proven."""

    if not plan.verify_commitment():
        raise RehearsalError("rehearsal plan commitment mismatch")
    database = Path(plan.database_path)
    require_checkpointed_sidecars(database)
    identity_before = candidate_file_identity(database)
    observed_database_sha = _sha256(database)
    identity_after = candidate_file_identity(database)
    if identity_before != identity_after or observed_database_sha != database_sha256:
        raise RehearsalError("rehearsal database identity or hash is stale")
    if alembic_revision != plan.expected_target_revision:
        raise RehearsalError("rehearsal database revision differs from target")
    if (
        not production_scope_ids
        or production_scope_ids != tuple(sorted(set(production_scope_ids)))
        or semantic_qualification.production_scope_ids != production_scope_ids
    ):
        raise RehearsalError("production semantic scope cohort is incomplete or ambiguous")
    if (
        admission_bundle.production_scope_ids != production_scope_ids
        or terminal_bundle.production_scope_ids != production_scope_ids
        or admission_bundle.terminal_commitments != terminal_bundle.terminal_commitments
    ):
        raise RehearsalError("admission and terminal readiness cohorts differ")
    if (
        cohort_audit.scope_ids != production_scope_ids
        or cohort_audit.table_counts != table_counts
        or len(cohort_audit.scopes) != len(production_scope_ids)
    ):
        raise RehearsalError("latest cohort audit differs from composite readiness inputs")
    if set(table_counts) != set(_LATEST_TABLES):
        raise RehearsalError("latest-state table census is incomplete")
    if table_counts["latest_governed_refresh_stage"] != 0:
        raise RehearsalError("latest-state stage table is not terminally empty")
    durable = set(_LATEST_TABLES) - _TERMINALLY_EMPTY_TABLES
    if any(table_counts[name] <= 0 for name in durable):
        raise RehearsalError("durable latest-state planes must all be nonempty")
    if len(stage_artifacts) < len(RehearsalStage) - 1 or any(
        not artifact.verify() for artifact in stage_artifacts
    ):
        raise RehearsalError("rehearsal stage artifact chain is incomplete or stale")
    bundle_artifacts = (
        admission_bundle.candidate_audit,
        admission_bundle.candidate_coverage,
        admission_bundle.bound_eligibility,
        terminal_bundle.candidate_seal,
        terminal_bundle.candidate_audit,
        terminal_bundle.candidate_coverage,
        terminal_bundle.bound_eligibility,
        terminal_bundle.latest_population_receipt,
        terminal_bundle.cohort_audit,
        semantic_qualification.request_artifact,
        semantic_qualification.registry_artifact,
    )
    if any(not artifact.verify() for artifact in bundle_artifacts):
        raise RehearsalError("admission, terminal, or semantic artifact is stale")
    if semantic_qualification.database_sha256 != database_sha256:
        raise RehearsalError("semantic qualification names a different database")
    if not exact_replay_verified or not candidate_performance_passed:
        raise RehearsalError("rehearsal terminal gates are not all clean")
    core = {
        "alembic_revision": alembic_revision,
        "candidate_performance_passed": candidate_performance_passed,
        "cross_scope_leakage_count": 0,
        "database_instance_id": database_instance_id,
        "database_path": plan.database_path,
        "database_sha256": database_sha256,
        "exact_replay_verified": exact_replay_verified,
        "exhaustive_parity_failure_count": 0,
        "fts_failure_count": 0,
        "activation_boundary_requirements": activation_boundary_requirements.model_dump(
            mode="json"
        ),
        "admission_bundle": admission_bundle.model_dump(mode="json"),
        "plan_sha256": plan.plan_sha256,
        "production_scope_ids": production_scope_ids,
        "restore_roundtrip": restore_roundtrip.model_dump(mode="json"),
        "retrieval_canary_failure_count": 0,
        "schema_version": "latest-governed-sealed-rehearsal-readiness/v2",
        "semantic_qualification": semantic_qualification.model_dump(mode="json"),
        "stage_artifacts": [artifact.model_dump(mode="json") for artifact in stage_artifacts],
        "status": "ready",
        "table_counts": table_counts,
        "terminal_bundle": terminal_bundle.model_dump(mode="json"),
    }
    return RehearsalReadinessReceipt.model_validate(core | {"receipt_sha256": _digest(core)})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        return {str(key): _json_value(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        sequence = cast("list[object] | tuple[object, ...]", value)
        return [_json_value(item) for item in sequence]
    return value
