"""Deterministically populate and seal the metric ontology over source facts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.immutable_artifact import canonical_text_artifact_sha256
from provenance.metric_ontology import (
    BindingRevision,
    CanonicalAxis,
    CanonicalDimension,
    CanonicalMember,
    CanonicalMetric,
    CanonicalMetricCell,
    CanonicalMetricDefinitionRevision,
    MappingRevision,
    MetricOntology,
    OntologySnapshot,
    SourceDimensionMappingRevision,
    SourceObservationTaxonomyAssertion,
    SourceTaxonomyComponent,
)
from provenance.population_completeness import PopulationTemporalScope

_POLICY_NAME = "deterministic_legacy_metric_admission"
_POLICY_VERSION = "7"
_TAXONOMY_NAME = "earnings-summary-legacy"
_TAXONOMY_VERSION = "legacy-observation-contract.v2"
_AUDITED_POLICY_PATH = "src/provenance/population_metric_ontology.py"
_INPUT_MANIFEST_TABLES = (
    "evidence_extraction_runs",
    "fact_cells_v2",
    "fact_cell_identity_seals_v2",
    "fact_dimensions_normalized_v2",
    "fact_observations_v2",
    "fact_reported_observation_anchors_v2",
    "fact_observation_payload_commitments_v2",
    "fact_extraction_run_completeness_seals_v2",
)
_OUTPUT_MANIFEST_TABLES = (
    "canonical_metrics",
    "canonical_axes",
    "canonical_members",
    "source_taxonomy_components",
    "source_observation_taxonomy_assertions",
    "canonical_metric_definition_revisions",
    "source_dimension_mapping_revisions",
    "metric_mapping_revisions",
    "canonical_metric_cells",
    "canonical_metric_cell_dimensions",
    "canonical_metric_cell_seals",
    "fact_cell_canonical_binding_revisions",
    "ontology_snapshot_headers",
    "ontology_snapshot_members",
    "ontology_snapshot_seals",
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _SourceDefinitionIdentity(_FrozenModel):
    schema_version: Literal["source-definition-identity/v1"] = "source-definition-identity/v1"
    reporting_entity_id: str
    taxonomy_name: str
    taxonomy_version: str
    concept_namespace: str
    concept_name: str
    accounting_basis: str
    consolidation_scope: str
    period_kind: str
    unit_family: str
    value_kind: str


class MetricOntologyPopulationRequest(_FrozenModel):
    knowledge_cutoff: datetime
    operation_recorded_at: datetime
    apply: bool = False
    phase: Literal["registry", "assertions", "bindings", "snapshot", "all"] = "all"
    after_observation_id: str | None = None
    max_observations: int | None = Field(default=None, ge=1)
    input_commitment_sha256: str | None = None
    plan_commitment_sha256: str | None = None

    @field_validator("input_commitment_sha256", "plan_commitment_sha256")
    @classmethod
    def _sha(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("population commitment must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _request_contract(self) -> Self:
        if (
            self.knowledge_cutoff.tzinfo is None
            or self.knowledge_cutoff.utcoffset() is None
            or self.operation_recorded_at.tzinfo is None
            or self.operation_recorded_at.utcoffset() is None
        ):
            raise ValueError("ontology temporal scope must include a timezone")
        if (self.input_commitment_sha256 is None) != (self.plan_commitment_sha256 is None):
            raise ValueError("population commitments must be supplied together")
        bounded = self.after_observation_id is not None or self.max_observations is not None
        if self.apply and bounded and self.input_commitment_sha256 is None:
            raise ValueError("a bounded ontology apply requires manifest commitments")
        if self.phase != "all" and (
            self.after_observation_id is not None or self.max_observations is not None
        ):
            raise ValueError("bounded ontology population requires phase='all'")
        if _utc(self.operation_recorded_at) < _utc(self.knowledge_cutoff):
            raise ValueError("operation_recorded_at must not precede knowledge_cutoff")
        return self


class MetricOntologyPopulationResult(_FrozenModel):
    mode: Literal["dry_run", "apply"]
    phase: str
    outcome: Literal["planned", "applied", "checkpoint", "blocked"]
    reason_codes: tuple[str, ...]
    snapshot_eligible: bool
    source_cell_count: int
    source_observation_count: int
    metric_count: int
    source_component_count: int
    canonical_cell_count: int
    assertion_count: int
    binding_count: int
    missing_assertion_count: int
    missing_binding_count: int
    processed_observation_count: int
    last_observation_id: str | None
    remaining_observation_count: int
    safe_to_seal: bool
    snapshot_id: str | None
    policy_config_sha256: str
    plan_commitment_sha256: str
    input_commitment_sha256: str
    post_state_commitment_sha256: str
    output_commitment_sha256: str

    @model_validator(mode="after")
    def _checkpoint_contract(self) -> Self:
        if self.safe_to_seal != self.snapshot_eligible:
            raise ValueError("ontology seal safety does not match snapshot eligibility")
        if self.outcome == "checkpoint" and self.safe_to_seal:
            raise ValueError("ontology checkpoint cannot be safe to seal")
        if self.snapshot_id is not None and not self.safe_to_seal:
            raise ValueError("ontology snapshot requires terminal seal safety")
        return self


MetricOntologyReceiptOutcome = Literal[
    "planned",
    "applied",
    "checkpoint",
    "blocked",
    "complete",
]


class MetricOntologyOperationReceipt(_FrozenModel):
    schema_version: Literal["metric-ontology-operation-receipt/v1"] = (
        "metric-ontology-operation-receipt/v1"
    )
    database_path: str = Field(min_length=1, max_length=1_024)
    database_instance_id: str = Field(
        min_length=50,
        max_length=64,
        pattern=r"^database-instance:[0-9a-f]{32}$",
    )
    operation_id: str = Field(
        min_length=90,
        max_length=90,
        pattern=r"^metric-ontology-operation:[0-9a-f]{64}$",
    )
    alembic_revision: str = Field(min_length=1, max_length=128)
    request: MetricOntologyPopulationRequest
    result: MetricOntologyPopulationResult
    outcome: MetricOntologyReceiptOutcome
    blocker_counts: dict[str, int]
    prior_checkpoint_receipt_sha256: str | None
    admission_receipt_sha256: str | None
    request_sha256: str
    result_sha256: str
    receipt_sha256: str

    @field_validator(
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
        bounded = (
            self.request.after_observation_id is not None
            or self.request.max_observations is not None
        )
        if self.request.apply != (self.result.mode == "apply"):
            raise ValueError("ontology operation receipt mode does not match its result")
        if self.result.phase != self.request.phase:
            raise ValueError("ontology operation result phase does not match its request")
        if self.request.apply and bounded and self.result.outcome != "checkpoint":
            raise ValueError("bounded ontology apply must produce a checkpoint")
        if self.request.apply and not bounded and self.result.outcome == "checkpoint":
            raise ValueError("unbounded ontology apply cannot produce a checkpoint")
        if self.request.apply != (self.admission_receipt_sha256 is not None):
            raise ValueError("an ontology apply receipt requires one dry-run admission")
        if (
            self.request.after_observation_id is not None
            and self.prior_checkpoint_receipt_sha256 is None
        ):
            raise ValueError("an ontology resume must bind one prior checkpoint")
        if (
            self.prior_checkpoint_receipt_sha256 is not None
            and not bounded
            and (self.request.phase not in {"snapshot", "all"})
        ):
            raise ValueError("an unbounded ontology successor must be a sealing phase")
        if self.request_sha256 != _model_sha(self.request):
            raise ValueError("ontology operation request commitment does not match")
        if self.result_sha256 != _model_sha(self.result):
            raise ValueError("ontology operation result commitment does not match")
        if self.operation_id != metric_ontology_operation_id(
            database_instance_id=self.database_instance_id,
            request=self.request,
            admission_receipt_sha256=self.admission_receipt_sha256,
            prior_checkpoint_receipt_sha256=self.prior_checkpoint_receipt_sha256,
        ):
            raise ValueError("ontology operation identity does not match")
        if self.blocker_counts != _ontology_blocker_counts(self.result):
            raise ValueError("ontology blocker census does not match")
        if self.outcome != _ontology_receipt_outcome(self.request, self.result):
            raise ValueError("ontology operation outcome does not match")
        if self.receipt_sha256 != _ontology_receipt_sha(self):
            raise ValueError("ontology operation receipt commitment does not match")
        return self


def build_metric_ontology_receipt(
    *,
    database_path: str,
    database_instance_id: str,
    alembic_revision: str,
    request: MetricOntologyPopulationRequest,
    result: MetricOntologyPopulationResult,
    prior_checkpoint_receipt_sha256: str | None,
    admission_receipt_sha256: str | None,
) -> MetricOntologyOperationReceipt:
    """Bind one exact ontology population attempt to immutable evidence."""

    payload: dict[str, object] = {
        "schema_version": "metric-ontology-operation-receipt/v1",
        "database_path": database_path,
        "database_instance_id": database_instance_id,
        "alembic_revision": alembic_revision,
        "request": request,
        "result": result,
        "outcome": _ontology_receipt_outcome(request, result),
        "blocker_counts": _ontology_blocker_counts(result),
        "prior_checkpoint_receipt_sha256": prior_checkpoint_receipt_sha256,
        "admission_receipt_sha256": admission_receipt_sha256,
        "request_sha256": _model_sha(request),
        "result_sha256": _model_sha(result),
    }
    payload["operation_id"] = metric_ontology_operation_id(
        database_instance_id=database_instance_id,
        request=request,
        admission_receipt_sha256=admission_receipt_sha256,
        prior_checkpoint_receipt_sha256=prior_checkpoint_receipt_sha256,
    )
    payload["receipt_sha256"] = hashlib.sha256(
        _canonical_json(
            {
                key: value.model_dump(mode="json") if isinstance(value, BaseModel) else value
                for key, value in payload.items()
            }
        ).encode()
    ).hexdigest()
    return MetricOntologyOperationReceipt.model_validate(payload)


def verify_metric_ontology_receipt(receipt: MetricOntologyOperationReceipt) -> bool:
    """Return whether every nested and top-level ontology commitment agrees."""

    try:
        MetricOntologyOperationReceipt.model_validate(receipt.model_dump(mode="json"))
    except ValueError:
        return False
    return True


def verify_metric_ontology_receipt_current_result(
    receipt: MetricOntologyOperationReceipt,
    current: MetricOntologyPopulationResult,
    *,
    historical_checkpoint: bool = False,
) -> None:
    """Compare replay evidence with checkpoint-stable or terminal-current commitments."""

    if not receipt.request.apply:
        raise ValueError("stored ontology replay receipt is not applied")
    expected = receipt.result
    if historical_checkpoint:
        if receipt.outcome != "checkpoint":
            raise ValueError("only an ontology checkpoint can be verified as historical")
        stable_expected = (
            expected.input_commitment_sha256,
            expected.plan_commitment_sha256,
            expected.policy_config_sha256,
            expected.source_cell_count,
            expected.source_observation_count,
        )
        stable_current = (
            current.input_commitment_sha256,
            current.plan_commitment_sha256,
            current.policy_config_sha256,
            current.source_cell_count,
            current.source_observation_count,
        )
        if stable_current != stable_expected:
            raise ValueError("stored ontology checkpoint changed its stable source universe")
        return
    current_plane: dict[str, object] = {
        "assertion_count": current.assertion_count,
        "binding_count": current.binding_count,
        "canonical_cell_count": current.canonical_cell_count,
        "input_commitment_sha256": current.input_commitment_sha256,
        "metric_count": current.metric_count,
        "missing_assertion_count": current.missing_assertion_count,
        "missing_binding_count": current.missing_binding_count,
        "output_commitment_sha256": current.output_commitment_sha256,
        "plan_commitment_sha256": current.plan_commitment_sha256,
        "policy_config_sha256": current.policy_config_sha256,
        "post_state_commitment_sha256": current.post_state_commitment_sha256,
        "source_cell_count": current.source_cell_count,
        "source_component_count": current.source_component_count,
        "source_observation_count": current.source_observation_count,
    }
    expected_plane: dict[str, object] = {
        "assertion_count": expected.assertion_count,
        "binding_count": expected.binding_count,
        "canonical_cell_count": expected.canonical_cell_count,
        "input_commitment_sha256": expected.input_commitment_sha256,
        "metric_count": expected.metric_count,
        "missing_assertion_count": expected.missing_assertion_count,
        "missing_binding_count": expected.missing_binding_count,
        "output_commitment_sha256": expected.output_commitment_sha256,
        "plan_commitment_sha256": expected.plan_commitment_sha256,
        "policy_config_sha256": expected.policy_config_sha256,
        "post_state_commitment_sha256": expected.post_state_commitment_sha256,
        "source_cell_count": expected.source_cell_count,
        "source_component_count": expected.source_component_count,
        "source_observation_count": expected.source_observation_count,
    }
    if current_plane != expected_plane:
        raise ValueError("stored ontology receipt no longer matches current planes")


def verify_metric_ontology_receipt_current(
    conn: sqlite3.Connection,
    receipt: MetricOntologyOperationReceipt,
) -> None:
    """Recompute current ontology planes and verify one stored replay receipt."""

    chain = _metric_ontology_receipt_verification_chain(conn, receipt)
    for position, item in enumerate(chain):
        current = populate_metric_ontology(
            conn,
            item.request.model_copy(
                update={
                    "apply": False,
                    "input_commitment_sha256": None,
                    "plan_commitment_sha256": None,
                }
            ),
        )
        verify_metric_ontology_receipt_current_result(
            item,
            current,
            historical_checkpoint=position < len(chain) - 1,
        )


def database_instance_id(conn: sqlite3.Connection) -> str:
    """Return the immutable identity installed for this database lineage."""

    rows = conn.execute(
        "SELECT database_instance_id FROM database_runtime_identity WHERE singleton=1"
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("ontology database identity is missing or ambiguous")
    value = str(rows[0][0])
    suffix = value.removeprefix("database-instance:")
    if (
        len(value) != 50
        or len(suffix) != 32
        or any(character not in "0123456789abcdef" for character in suffix)
    ):
        raise ValueError("ontology database identity is invalid")
    return value


def persist_metric_ontology_receipt(
    conn: sqlite3.Connection,
    receipt: MetricOntologyOperationReceipt,
) -> bool:
    """Persist one immutable apply receipt inside the caller transaction."""

    if not receipt.request.apply or not verify_metric_ontology_receipt(receipt):
        raise ValueError("only a valid ontology apply receipt can enter the ledger")
    payload = receipt.model_dump_json()
    values = (
        receipt.operation_id,
        receipt.operation_id,
        receipt.database_instance_id,
        receipt.request_sha256,
        receipt.result_sha256,
        receipt.receipt_sha256,
        payload,
    )
    cursor = conn.execute(
        "INSERT OR IGNORE INTO metric_ontology_operation_ledger "
        "(operation_id,idempotency_key,database_instance_id,request_sha256,"
        "result_sha256,receipt_sha256,receipt_json) VALUES (?,?,?,?,?,?,?)",
        values,
    )
    if cursor.rowcount == 1:
        return True
    existing = conn.execute(
        "SELECT operation_id,idempotency_key,database_instance_id,request_sha256,"
        "result_sha256,receipt_sha256,receipt_json "
        "FROM metric_ontology_operation_ledger WHERE operation_id=?",
        (receipt.operation_id,),
    ).fetchone()
    if existing is None or tuple(existing) != values:
        raise ValueError("ontology operation replay changed immutable evidence")
    return False


def load_metric_ontology_receipt(
    conn: sqlite3.Connection,
    operation_id: str,
) -> MetricOntologyOperationReceipt | None:
    """Load and verify the canonical receipt for one ontology operation."""

    row = conn.execute(
        "SELECT receipt_json FROM metric_ontology_operation_ledger WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    receipt = MetricOntologyOperationReceipt.model_validate_json(str(row[0]))
    if not verify_metric_ontology_receipt(receipt) or receipt.operation_id != operation_id:
        raise ValueError("stored ontology operation receipt is invalid")
    return receipt


def _metric_ontology_receipt_verification_chain(
    conn: sqlite3.Connection,
    receipt: MetricOntologyOperationReceipt,
) -> tuple[MetricOntologyOperationReceipt, ...]:
    rows = conn.execute("SELECT receipt_json FROM metric_ontology_operation_ledger").fetchall()
    ledger = tuple(MetricOntologyOperationReceipt.model_validate_json(str(row[0])) for row in rows)
    if any(not verify_metric_ontology_receipt(item) for item in ledger):
        raise ValueError("ontology ledger contains an invalid receipt")
    if sum(item == receipt for item in ledger) != 1:
        raise ValueError("ontology replay receipt is not the canonical ledger receipt")
    chain = [receipt]
    seen = {receipt.receipt_sha256}
    while chain[0].prior_checkpoint_receipt_sha256 is not None:
        child = chain[0]
        parents = tuple(
            item
            for item in ledger
            if _ontology_receipt_artifact_sha(item) == child.prior_checkpoint_receipt_sha256
        )
        if len(parents) != 1:
            raise ValueError("ontology checkpoint parent is missing or ambiguous")
        parent = parents[0]
        if parent.outcome != "checkpoint":
            raise ValueError("ontology successor parent is not a checkpoint")
        if parent.receipt_sha256 in seen:
            raise ValueError("ontology checkpoint ancestry is cyclic")
        siblings = tuple(
            item
            for item in ledger
            if item.prior_checkpoint_receipt_sha256 == _ontology_receipt_artifact_sha(parent)
        )
        if len(siblings) != 1 or siblings[0] != child:
            raise ValueError("ontology checkpoint successor is ambiguous")
        _validate_metric_ontology_receipt_successor(parent, child)
        chain.insert(0, parent)
        seen.add(parent.receipt_sha256)
    while chain[-1].outcome == "checkpoint":
        parent = chain[-1]
        successors = tuple(
            item
            for item in ledger
            if item.prior_checkpoint_receipt_sha256 == _ontology_receipt_artifact_sha(parent)
        )
        if not successors:
            break
        if len(successors) != 1:
            raise ValueError("ontology checkpoint successor is ambiguous")
        successor = successors[0]
        if successor.receipt_sha256 in seen:
            raise ValueError("ontology checkpoint successor chain is cyclic")
        _validate_metric_ontology_receipt_successor(parent, successor)
        chain.append(successor)
        seen.add(successor.receipt_sha256)
    if len(chain) > 1 and not _ontology_receipt_is_terminal_success(chain[-1]):
        raise ValueError("ontology checkpoint successor chain is not terminal")
    return tuple(chain)


def _validate_metric_ontology_receipt_successor(
    parent: MetricOntologyOperationReceipt,
    successor: MetricOntologyOperationReceipt,
) -> None:
    if (
        successor.database_path != parent.database_path
        or successor.database_instance_id != parent.database_instance_id
        or successor.alembic_revision != parent.alembic_revision
    ):
        raise ValueError("ontology checkpoint successor database changed")
    if (
        successor.request.knowledge_cutoff != parent.request.knowledge_cutoff
        or successor.request.operation_recorded_at != parent.request.operation_recorded_at
    ):
        raise ValueError("ontology checkpoint successor scope changed")
    successor_bounded = (
        successor.request.after_observation_id is not None
        or successor.request.max_observations is not None
    )
    if successor_bounded:
        if (
            successor.request.phase != parent.request.phase
            or successor.request.max_observations != parent.request.max_observations
        ):
            raise ValueError("ontology checkpoint successor batch shape changed")
        if successor.request.after_observation_id != parent.result.last_observation_id:
            raise ValueError("ontology checkpoint successor cursor changed")
    elif (
        successor.request.phase not in {"snapshot", "all"}
        or parent.result.remaining_observation_count != 0
    ):
        raise ValueError("ontology sealing successor is not ready")
    if (
        successor.result.input_commitment_sha256 != parent.result.input_commitment_sha256
        or successor.result.policy_config_sha256 != parent.result.policy_config_sha256
    ):
        raise ValueError("ontology checkpoint successor commitments changed")


def _ontology_receipt_is_terminal_success(receipt: MetricOntologyOperationReceipt) -> bool:
    bounded = (
        receipt.request.after_observation_id is not None
        or receipt.request.max_observations is not None
    )
    return receipt.request.apply and not bounded and receipt.outcome == "complete"


def _ontology_receipt_artifact_sha(receipt: MetricOntologyOperationReceipt) -> str:
    return canonical_text_artifact_sha256(receipt.model_dump_json())


def metric_ontology_operation_id(
    *,
    database_instance_id: str,
    request: MetricOntologyPopulationRequest,
    admission_receipt_sha256: str | None,
    prior_checkpoint_receipt_sha256: str | None,
) -> str:
    material = _canonical_json(
        {
            "admission_receipt_sha256": admission_receipt_sha256,
            "database_instance_id": database_instance_id,
            "prior_checkpoint_receipt_sha256": prior_checkpoint_receipt_sha256,
            "request_sha256": _model_sha(request),
        }
    )
    return "metric-ontology-operation:" + hashlib.sha256(material.encode()).hexdigest()


def _ontology_receipt_outcome(
    request: MetricOntologyPopulationRequest,
    result: MetricOntologyPopulationResult,
) -> MetricOntologyReceiptOutcome:
    if result.outcome == "blocked":
        return "blocked"
    if not request.apply:
        return "planned"
    if result.outcome == "checkpoint":
        return "checkpoint"
    if request.phase in {"snapshot", "all"} and result.snapshot_eligible:
        return "complete"
    return "applied"


def _ontology_blocker_counts(result: MetricOntologyPopulationResult) -> dict[str, int]:
    counts = {
        "missing_assertion": result.missing_assertion_count,
        "missing_binding": result.missing_binding_count,
    }
    counts.update(
        {
            reason: 1
            for reason in result.reason_codes
            if reason
            not in {
                "bounded_population_checkpoint",
                "ontology_assertions_incomplete",
                "ontology_bindings_incomplete",
            }
        }
    )
    return {key: value for key, value in sorted(counts.items()) if value}


def _model_sha(model: BaseModel) -> str:
    return hashlib.sha256(_canonical_json(model.model_dump(mode="json")).encode()).hexdigest()


def _ontology_receipt_sha(receipt: MetricOntologyOperationReceipt) -> str:
    payload = receipt.model_dump(mode="json")
    payload.pop("receipt_sha256")
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _record_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{_digest(*parts)}"


class _CommitmentFold:
    """Length-delimited canonical records without corpus-sized serialization."""

    def __init__(self, namespace: str) -> None:
        self._digest = hashlib.sha256(namespace.encode())

    def add(self, kind: str, payload: object) -> None:
        encoded = _canonical_json([kind, payload]).encode()
        self._digest.update(len(encoded).to_bytes(8, "big"))
        self._digest.update(encoded)

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


class _CanonicalArrayFold:
    """Hash canonical JSON array members incrementally."""

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._digest.update(b"[")
        self._count = 0

    def add(self, value: object) -> None:
        if self._count:
            self._digest.update(b",")
        self._digest.update(_canonical_json(value).encode())
        self._count += 1

    @property
    def count(self) -> int:
        return self._count

    def hexdigest(self) -> str:
        digest = self._digest.copy()
        digest.update(b"]")
        return digest.hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse_time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _scope_bounds(
    knowledge_cutoff: datetime | None,
    observed_through: datetime | None,
) -> tuple[str, str]:
    if knowledge_cutoff is None or observed_through is None:
        raise ValueError("both ontology temporal clocks are required")
    knowledge, observed = _utc(knowledge_cutoff), _utc(observed_through)
    if observed < knowledge:
        raise ValueError("ontology observed_through precedes knowledge_cutoff")
    return knowledge.isoformat(), observed.isoformat()


def populate_metric_ontology(
    conn: sqlite3.Connection,
    request: MetricOntologyPopulationRequest,
) -> MetricOntologyPopulationResult:
    """Populate one bounded ontology phase and return exact coverage counts."""

    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        repository = MetricOntology(conn)
        source_cell_count = _count_source_cells_at_scope(
            conn, request.knowledge_cutoff, request.operation_recorded_at
        )
        source_observation_count = _count_reported_observations(
            conn, request.knowledge_cutoff, request.operation_recorded_at
        )
        if source_cell_count == 0 or source_observation_count == 0:
            raise ValueError("metric ontology population requires a nonempty source-fact plane")
        source_reason_codes = _source_cell_reason_codes(
            conn, request.knowledge_cutoff, request.operation_recorded_at
        )
        input_sha = _input_commitment_at_scope(
            conn, request.knowledge_cutoff, request.operation_recorded_at
        )
        policy_sha = _policy_sha()
        plan_sha = _plan_commitment(
            conn,
            input_sha,
            policy_sha,
            request.knowledge_cutoff,
            request.operation_recorded_at,
        )
        _verify_caller_commitments(request, input_sha=input_sha, plan_sha=plan_sha)
        bounded = request.after_observation_id is not None or request.max_observations is not None
        if source_reason_codes:
            return _result(
                conn,
                request=request,
                outcome="blocked",
                reason_codes=source_reason_codes,
                snapshot_eligible=False,
                source_cell_count=source_cell_count,
                source_observation_count=source_observation_count,
                processed=0,
                last_observation_id=None,
                remaining_observation_count=source_observation_count,
                safe_to_seal=False,
                snapshot_id=None,
                policy_sha=policy_sha,
                plan_sha=plan_sha,
                input_sha=input_sha,
            )
        processed = 0
        last_observation_id: str | None = request.after_observation_id
        remaining_observation_count = _remaining_observation_count(
            conn,
            after_observation_id=request.after_observation_id,
            knowledge_cutoff=request.knowledge_cutoff,
            observed_through=request.operation_recorded_at,
        )
        safe_to_seal = False
        snapshot_id: str | None = None
        if request.apply:
            with _atomic_population(conn):
                if (
                    _input_commitment_at_scope(
                        conn,
                        request.knowledge_cutoff,
                        request.operation_recorded_at,
                    )
                    != input_sha
                    or _policy_sha() != policy_sha
                    or _plan_commitment(
                        conn,
                        input_sha,
                        policy_sha,
                        request.knowledge_cutoff,
                        request.operation_recorded_at,
                    )
                    != plan_sha
                ):
                    raise ValueError("metric ontology pre-write commitment changed")
                if request.phase in {"registry", "all"}:
                    _populate_registry(
                        conn,
                        repository,
                        knowledge_cutoff=request.knowledge_cutoff,
                        operation_recorded_at=request.operation_recorded_at,
                    )
                if request.phase in {"assertions", "all"}:
                    rows = _observation_rows(
                        conn,
                        after_observation_id=request.after_observation_id,
                        max_observations=request.max_observations,
                        knowledge_cutoff=request.knowledge_cutoff,
                        observed_through=request.operation_recorded_at,
                    )
                    for row in rows:
                        repository.persist_observation_taxonomy_assertion(_taxonomy_assertion(row))
                        processed += 1
                        observation_id = str(row["observation_id"])
                        if last_observation_id is None or observation_id > last_observation_id:
                            last_observation_id = observation_id
                if request.phase in {"bindings", "all"}:
                    rows = _binding_rows(
                        conn,
                        after_observation_id=request.after_observation_id,
                        max_observations=request.max_observations,
                        knowledge_cutoff=request.knowledge_cutoff,
                        observed_through=request.operation_recorded_at,
                    )
                    for row in rows:
                        repository.persist_binding(_binding(row))
                        if request.phase == "bindings":
                            processed += 1
                        observation_id = str(row["observation_id"])
                        if last_observation_id is None or observation_id > last_observation_id:
                            last_observation_id = observation_id
                remaining_observation_count = _remaining_observation_count(
                    conn,
                    after_observation_id=last_observation_id,
                    knowledge_cutoff=request.knowledge_cutoff,
                    observed_through=request.operation_recorded_at,
                )
                safe_to_seal = (
                    not bounded
                    and (
                        request.phase == "snapshot"
                        or (request.phase == "all" and remaining_observation_count == 0)
                    )
                    and not _count_missing_assertions(
                        conn, request.knowledge_cutoff, request.operation_recorded_at
                    )
                    and not _count_missing_bindings(
                        conn, request.knowledge_cutoff, request.operation_recorded_at
                    )
                )
                if request.phase == "snapshot" or safe_to_seal:
                    snapshot_id = _seal_snapshot(
                        conn,
                        repository,
                        policy_sha,
                        knowledge_cutoff=request.knowledge_cutoff,
                        operation_recorded_at=request.operation_recorded_at,
                    )
                if (
                    _input_commitment_at_scope(
                        conn,
                        request.knowledge_cutoff,
                        request.operation_recorded_at,
                    )
                    != input_sha
                ):
                    raise ValueError("metric ontology source input changed during write")
        if not request.apply:
            safe_to_seal = (
                request.phase in {"snapshot", "all"}
                and not bounded
                and not _count_missing_assertions(
                    conn, request.knowledge_cutoff, request.operation_recorded_at
                )
                and not _count_missing_bindings(
                    conn, request.knowledge_cutoff, request.operation_recorded_at
                )
            )
        checkpoint = request.apply and request.phase == "all" and bounded
        return _result(
            conn,
            request=request,
            outcome=("checkpoint" if checkpoint else "applied" if request.apply else "planned"),
            reason_codes=(("bounded_population_checkpoint",) if checkpoint else ()),
            snapshot_eligible=safe_to_seal,
            source_cell_count=source_cell_count,
            source_observation_count=source_observation_count,
            processed=processed,
            last_observation_id=last_observation_id,
            remaining_observation_count=remaining_observation_count,
            safe_to_seal=safe_to_seal,
            snapshot_id=snapshot_id,
            policy_sha=policy_sha,
            plan_sha=plan_sha,
            input_sha=input_sha,
        )
    finally:
        conn.row_factory = original_row_factory


def _result(
    conn: sqlite3.Connection,
    *,
    request: MetricOntologyPopulationRequest,
    outcome: Literal["planned", "applied", "checkpoint", "blocked"],
    reason_codes: tuple[str, ...],
    snapshot_eligible: bool,
    source_cell_count: int,
    source_observation_count: int,
    processed: int,
    last_observation_id: str | None,
    remaining_observation_count: int,
    safe_to_seal: bool,
    snapshot_id: str | None,
    policy_sha: str,
    plan_sha: str,
    input_sha: str,
) -> MetricOntologyPopulationResult:
    missing_assertions = _count_missing_assertions(
        conn,
        request.knowledge_cutoff,
        request.operation_recorded_at,
    )
    missing_bindings = _count_missing_bindings(
        conn,
        request.knowledge_cutoff,
        request.operation_recorded_at,
    )
    reasons = set(reason_codes)
    if missing_assertions:
        reasons.add("ontology_assertions_incomplete")
    if missing_bindings:
        reasons.add("ontology_bindings_incomplete")
    post_state_sha = _output_commitment_at_scope(
        conn,
        request.knowledge_cutoff,
        request.operation_recorded_at,
    )
    return MetricOntologyPopulationResult(
        mode="apply" if request.apply else "dry_run",
        phase=request.phase,
        outcome=outcome,
        reason_codes=tuple(sorted(reasons)),
        snapshot_eligible=snapshot_eligible,
        source_cell_count=source_cell_count,
        source_observation_count=source_observation_count,
        metric_count=_count(conn, "canonical_metrics"),
        source_component_count=_count(conn, "source_taxonomy_components"),
        canonical_cell_count=_count(conn, "canonical_metric_cells"),
        assertion_count=_count(conn, "source_observation_taxonomy_assertions"),
        binding_count=_count(conn, "fact_cell_canonical_binding_revisions"),
        missing_assertion_count=missing_assertions,
        missing_binding_count=missing_bindings,
        processed_observation_count=processed,
        last_observation_id=last_observation_id,
        remaining_observation_count=remaining_observation_count,
        safe_to_seal=safe_to_seal,
        snapshot_id=snapshot_id,
        policy_config_sha256=policy_sha,
        plan_commitment_sha256=plan_sha,
        input_commitment_sha256=input_sha,
        post_state_commitment_sha256=post_state_sha,
        output_commitment_sha256=post_state_sha,
    )


def _verify_caller_commitments(
    request: MetricOntologyPopulationRequest,
    *,
    input_sha: str,
    plan_sha: str,
) -> None:
    if request.input_commitment_sha256 is not None and request.input_commitment_sha256 != input_sha:
        raise ValueError("metric ontology input commitment does not match")
    if request.plan_commitment_sha256 is not None and request.plan_commitment_sha256 != plan_sha:
        raise ValueError("metric ontology plan commitment does not match")


@contextmanager
def _atomic_population(conn: sqlite3.Connection) -> Generator[None, None, None]:
    conn.execute("SAVEPOINT populate_metric_ontology")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT populate_metric_ontology")
        conn.execute("RELEASE SAVEPOINT populate_metric_ontology")
        raise
    conn.execute("RELEASE SAVEPOINT populate_metric_ontology")


def _source_cells(
    conn: sqlite3.Connection,
    knowledge_cutoff: datetime | None = None,
    observed_through: datetime | None = None,
) -> Iterator[dict[str, object]]:
    scope_filter = ""
    params: tuple[object, ...] = ()
    if knowledge_cutoff is not None or observed_through is not None:
        knowledge, observed = _scope_bounds(knowledge_cutoff, observed_through)
        scope_filter = (
            "WHERE datetime(cell.knowledge_at)<=datetime(?) "
            "AND datetime(cell.recorded_at)<=datetime(?) "
            "AND datetime(observation.knowledge_at)<=datetime(?) "
            "AND datetime(observation.recorded_at)<=datetime(?) "
            "AND datetime(seal.sealed_at)<=datetime(?) "
            "AND datetime(anchor.recorded_at)<=datetime(?) "
        )
        params = (knowledge, observed, knowledge, observed, observed, observed)
    query = """
        SELECT cell.*,seal.semantic_key_sha256,anchor.source_taxonomy_version,
               entity.created_at AS reporting_entity_created_at,
               MIN(CASE WHEN observation.value_kind<>'nil'
                        THEN observation.value_kind END) AS substantive_value_kind,
               COUNT(DISTINCT CASE WHEN observation.value_kind<>'nil'
                                   THEN observation.value_kind END)
                   AS substantive_value_kind_count
        FROM fact_cells_v2 cell
        JOIN reporting_entities entity
          ON entity.reporting_entity_id=cell.reporting_entity_id
        JOIN fact_cell_identity_seals_v2 seal
          ON seal.fact_cell_id=cell.fact_cell_id
        JOIN fact_observations_v2 observation
          ON observation.fact_cell_id=cell.fact_cell_id
         AND observation.observation_kind='reported'
        JOIN fact_reported_observation_anchors_v2 anchor
          ON anchor.observation_id=observation.observation_id
    """
    query += scope_filter
    query += (
        " GROUP BY cell.fact_cell_id,anchor.source_taxonomy_version "
        "ORDER BY cell.fact_cell_id,anchor.source_taxonomy_version"
    )
    rows = conn.execute(
        query,  # nosec B608 -- only the fixed dual-clock predicate is appended
        params,
    )
    for row in rows:
        count = int(row["substantive_value_kind_count"])
        if count != 1:
            continue
        value_kind = str(row["substantive_value_kind"])
        if value_kind not in {"numeric", "text"}:
            continue
        cell = dict(row)
        cell["value_kind"] = value_kind
        yield cell


def _source_cell_reason_codes(
    conn: sqlite3.Connection,
    knowledge_cutoff: datetime,
    observed_through: datetime,
) -> tuple[str, ...]:
    knowledge, observed = _scope_bounds(knowledge_cutoff, observed_through)
    reasons: set[str] = set()
    summary_count = 0
    for row in conn.execute(
        """
        SELECT cell.fact_cell_id,
               MIN(CASE WHEN observation.value_kind<>'nil'
                        THEN observation.value_kind END) AS substantive_value_kind,
               COUNT(DISTINCT CASE WHEN observation.value_kind<>'nil'
                                   THEN observation.value_kind END)
                   AS substantive_value_kind_count
        FROM fact_cells_v2 cell
        JOIN fact_observations_v2 observation
          ON observation.fact_cell_id=cell.fact_cell_id
         AND observation.observation_kind='reported'
        WHERE datetime(cell.knowledge_at)<=datetime(?)
          AND datetime(cell.recorded_at)<=datetime(?)
          AND datetime(observation.knowledge_at)<=datetime(?)
          AND datetime(observation.recorded_at)<=datetime(?)
        GROUP BY cell.fact_cell_id
        """,
        (knowledge, observed, knowledge, observed),
    ):
        summary_count += 1
        count = int(row["substantive_value_kind_count"])
        if count == 0:
            reasons.add("all_nil_fact_cell_unresolved")
        elif count != 1:
            reasons.add("mixed_numeric_text_fact_cell_unresolved")
        elif str(row["substantive_value_kind"]) not in {"numeric", "text"}:
            reasons.add("unsupported_substantive_value_kind")
    if summary_count != _count_source_cells_at_scope(conn, knowledge_cutoff, observed_through):
        reasons.add("fact_cell_missing_reported_observation")
    if int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_dimensions_normalized_v2 dimension "
            "JOIN fact_cells_v2 cell ON cell.fact_cell_id=dimension.fact_cell_id "
            "WHERE member_kind<>'explicit' "
            "AND datetime(cell.knowledge_at)<=datetime(?) "
            "AND datetime(cell.recorded_at)<=datetime(?)",
            (knowledge, observed),
        ).fetchone()[0]
    ):
        reasons.add("typed_source_dimension_requires_review")
    return tuple(sorted(reasons))


def _populate_registry(
    conn: sqlite3.Connection,
    repository: MetricOntology,
    *,
    knowledge_cutoff: datetime,
    operation_recorded_at: datetime,
) -> None:
    policy_sha = _policy_sha()
    for row in _registry_rows(conn, knowledge_cutoff, operation_recorded_at):
        clock = _cell_clock(row)
        component_clock = _component_clock(row)
        _persist_metric_stack(
            repository,
            row,
            metric_clock=clock,
            component_clock=component_clock,
            policy_sha=policy_sha,
        )
    _persist_dimension_registry(
        conn,
        repository,
        policy_sha,
        knowledge_cutoff=knowledge_cutoff,
        operation_recorded_at=operation_recorded_at,
    )
    for row in _source_cells(conn, knowledge_cutoff, operation_recorded_at):
        repository.persist_canonical_metric_cell(
            _canonical_cell(
                conn,
                row,
                operation_recorded_at=operation_recorded_at,
            )
        )


def _registry_rows(
    conn: sqlite3.Connection,
    knowledge_cutoff: datetime | None = None,
    observed_through: datetime | None = None,
) -> sqlite3.Cursor:
    scope_filter = ""
    params: tuple[object, ...] = ()
    if knowledge_cutoff is not None or observed_through is not None:
        knowledge, observed = _scope_bounds(knowledge_cutoff, observed_through)
        scope_filter = (
            "WHERE datetime(cell.knowledge_at)<=datetime(?) "
            "AND datetime(cell.recorded_at)<=datetime(?) "
            "AND datetime(observation.knowledge_at)<=datetime(?) "
            "AND datetime(observation.recorded_at)<=datetime(?) "
            "AND datetime(anchor.recorded_at)<=datetime(?) "
        )
        params = (knowledge, observed, knowledge, observed, observed)
    query = """
        WITH source_cells AS (
            SELECT cell.*,seal.semantic_key_sha256,
                   anchor.source_taxonomy_version,
                   entity.created_at AS reporting_entity_created_at,
                   MIN(CASE WHEN observation.value_kind<>'nil'
                            THEN observation.value_kind END) AS value_kind,
                   COUNT(DISTINCT CASE WHEN observation.value_kind<>'nil'
                                       THEN observation.value_kind END) AS kind_count
            FROM fact_cells_v2 cell
            JOIN reporting_entities entity
              ON entity.reporting_entity_id=cell.reporting_entity_id
            JOIN fact_cell_identity_seals_v2 seal
              ON seal.fact_cell_id=cell.fact_cell_id
            JOIN fact_observations_v2 observation
              ON observation.fact_cell_id=cell.fact_cell_id
             AND observation.observation_kind='reported'
            JOIN fact_reported_observation_anchors_v2 anchor
              ON anchor.observation_id=observation.observation_id
    """
    query += scope_filter
    query += """
            GROUP BY cell.fact_cell_id,anchor.source_taxonomy_version
        ),
        ranked AS (
            SELECT source_cells.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY
                           reporting_entity_id,
                           concept_namespace,
                           concept_name,
                           accounting_basis,
                           consolidation_scope,
                           period_kind,
                           CASE
                               WHEN currency IS NOT NULL THEN 'currency'
                               WHEN lower(unit_key) IN ('pure','shares')
                                   THEN lower(unit_key)
                               ELSE unit_key
                           END,
                           value_kind,
                           taxonomy_name,
                           source_taxonomy_version
                       ORDER BY datetime(recorded_at),datetime(knowledge_at),
                                datetime(effective_at),fact_cell_id
                   ) AS source_rank
            FROM source_cells
            WHERE kind_count=1
        )
        SELECT * FROM ranked WHERE source_rank=1
        ORDER BY reporting_entity_id,concept_namespace,concept_name
    """
    return conn.execute(
        query,  # nosec B608 -- only the fixed dual-clock predicate is appended
        params,
    )


def _persist_metric_stack(
    repository: MetricOntology,
    row: dict[str, object],
    *,
    metric_clock: tuple[datetime, datetime, datetime],
    component_clock: tuple[datetime, datetime, datetime],
    policy_sha: str,
) -> None:
    metric_id = _metric_id(row)
    component_id = _concept_component_id(row)
    repository.persist_metric(
        CanonicalMetric(
            metric_id=metric_id,
            idempotency_key=metric_id,
            canonical_name=_bounded_name(
                str(row["concept_name"]),
                _metric_signature(row),
            ),
            effective_at=metric_clock[0],
            knowledge_at=metric_clock[1],
            recorded_at=metric_clock[2],
        )
    )
    definition_id = _record_id(
        "metric-definition",
        metric_id,
        _POLICY_VERSION,
        "1",
    )
    repository.persist_metric_definition(
        CanonicalMetricDefinitionRevision(
            metric_definition_revision_id=definition_id,
            idempotency_key=definition_id,
            metric_id=metric_id,
            revision=1,
            lifecycle="active",
            definition_text=(
                "Exact deterministic admission of source concept "
                f"{row['concept_namespace']}:{row['concept_name']} with preserved "
                "period, unit family, and accounting basis."
            ),
            aliases=(str(row["concept_name"]),),
            value_kind=cast(
                Literal["numeric", "text", "nil"],
                str(row["value_kind"]),
            ),
            period_kind=cast(
                Literal["instant", "duration"],
                str(row["period_kind"]),
            ),
            unit_family=_unit_family(row),
            accounting_basis=str(row["accounting_basis"]),
            scope_constraints={
                "consolidation_scope": str(row["consolidation_scope"]),
                "reporting_entity_id": str(row["reporting_entity_id"]),
                "source_concept_namespace": str(row["concept_namespace"]),
                "source_definition_commitment_sha256": (_source_definition_commitment(row)),
            },
            effective_at=metric_clock[0],
            knowledge_at=metric_clock[1],
            recorded_at=metric_clock[2],
        )
    )
    repository.persist_source_component(
        SourceTaxonomyComponent(
            component_id=component_id,
            idempotency_key=component_id,
            component_kind="concept",
            taxonomy_namespace=str(row["concept_namespace"]),
            local_name=str(row["concept_name"]),
            taxonomy_name=str(row["taxonomy_name"]),
            taxonomy_version=str(row["source_taxonomy_version"]),
            definition_qualifier_sha256=_source_definition_commitment(row),
            reporting_entity_id=str(row["reporting_entity_id"]),
            is_extension=True,
            data_type=str(row["value_kind"]),
            period_type=cast(
                Literal["instant", "duration"],
                str(row["period_kind"]),
            ),
            balance=None,
            is_abstract=False,
            standard_label=str(row["concept_name"]),
            definition_text="Legacy concept preserved without semantic relabeling.",
            references=(),
            evidence_locator={
                "fact_cell_semantic_key_sha256": str(row["semantic_key_sha256"]),
                "policy_name": _POLICY_NAME,
                "policy_version": _POLICY_VERSION,
                "source_exact_local_name": str(row["concept_name"]),
                "source_exact_taxonomy_name": str(row["taxonomy_name"]),
                "source_exact_taxonomy_version": str(row["source_taxonomy_version"]),
                "source_definition_commitment_sha256": (_source_definition_commitment(row)),
            },
            effective_at=component_clock[0],
            knowledge_at=component_clock[1],
            recorded_at=component_clock[2],
        )
    )
    mapping_id = _mapping_id(row)
    repository.persist_mapping(
        MappingRevision(
            mapping_revision_id=mapping_id,
            idempotency_key=mapping_id,
            source_component_id=component_id,
            revision=1,
            metric_id=metric_id,
            disposition="exact",
            policy_name=_POLICY_NAME,
            policy_version=_POLICY_VERSION,
            policy_config_sha256=policy_sha,
            method_name="deterministic_exact_coordinate",
            method_version=_POLICY_VERSION,
            constraints={
                "accounting_basis": str(row["accounting_basis"]),
                "period_kind": str(row["period_kind"]),
                "unit_family": _unit_family(row),
                "value_kind": str(row["value_kind"]),
                "reporting_entity_id": str(row["reporting_entity_id"]),
                "source_definition_commitment_sha256": (_source_definition_commitment(row)),
            },
            evidence={
                "concept_name": str(row["concept_name"]),
                "concept_namespace": str(row["concept_namespace"]),
                "reporting_entity_id": str(row["reporting_entity_id"]),
                "source_definition_commitment_sha256": (_source_definition_commitment(row)),
            },
            reviewer_identity=None,
            audited_policy_path=_AUDITED_POLICY_PATH,
            effective_at=max(metric_clock[0], component_clock[0], key=_utc),
            knowledge_at=max(metric_clock[1], component_clock[1], key=_utc),
            recorded_at=max(metric_clock[2], component_clock[2], key=_utc),
        )
    )


def _persist_dimension_registry(
    conn: sqlite3.Connection,
    repository: MetricOntology,
    policy_sha: str,
    *,
    knowledge_cutoff: datetime,
    operation_recorded_at: datetime,
) -> None:
    knowledge, observed = _scope_bounds(knowledge_cutoff, operation_recorded_at)
    typed_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_dimensions_normalized_v2 dimension "
            "JOIN fact_cells_v2 cell ON cell.fact_cell_id=dimension.fact_cell_id "
            "WHERE member_kind<>'explicit' "
            "AND datetime(cell.knowledge_at)<=datetime(?) "
            "AND datetime(cell.recorded_at)<=datetime(?)",
            (knowledge, observed),
        ).fetchone()[0]
    )
    if typed_count:
        raise ValueError("typed source dimensions are not eligible for automatic admission")
    for row in _axis_registry_rows(conn, knowledge_cutoff, operation_recorded_at):
        axis_id = _axis_id(row)
        clock = _dimension_clock(row)
        repository.persist_axis(
            CanonicalAxis(
                axis_id=axis_id,
                idempotency_key=axis_id,
                canonical_name=_bounded_name(
                    str(row["axis_name"]),
                    f"{row['axis_namespace']}:{row['axis_name']}",
                ),
                effective_at=clock[0],
                knowledge_at=clock[1],
                recorded_at=clock[2],
            )
        )
        component_id = _axis_component_id(row)
        repository.persist_source_component(
            SourceTaxonomyComponent(
                component_id=component_id,
                idempotency_key=component_id,
                component_kind="axis",
                taxonomy_namespace=str(row["axis_namespace"]),
                local_name=str(row["axis_name"]),
                taxonomy_name=str(row["taxonomy_name"]),
                taxonomy_version=str(row["source_taxonomy_version"]),
                definition_qualifier_sha256=_dimension_definition_qualifier("axis", row),
                reporting_entity_id=None,
                is_extension=False,
                evidence_locator={"policy_config_sha256": policy_sha},
                effective_at=clock[0],
                knowledge_at=clock[1],
                recorded_at=clock[2],
            )
        )
        mapping_id = _dimension_mapping_id(component_id)
        repository.persist_dimension_mapping(
            SourceDimensionMappingRevision(
                dimension_mapping_revision_id=mapping_id,
                idempotency_key=mapping_id,
                source_component_id=component_id,
                revision=1,
                disposition="exact",
                canonical_axis_id=axis_id,
                canonical_member_id=None,
                policy_name=_POLICY_NAME,
                policy_version=_POLICY_VERSION,
                policy_config_sha256=policy_sha,
                evidence={"axis_name": str(row["axis_name"])},
                reviewer_identity=None,
                audited_policy_path=_AUDITED_POLICY_PATH,
                effective_at=clock[0],
                knowledge_at=clock[1],
                recorded_at=clock[2],
            )
        )
    for row in _member_registry_rows(conn, knowledge_cutoff, operation_recorded_at):
        member_id = _member_id(row)
        axis_id = _axis_id(row)
        clock = _dimension_clock(row)
        repository.persist_member(
            CanonicalMember(
                member_id=member_id,
                idempotency_key=member_id,
                axis_id=axis_id,
                canonical_name=_bounded_name(
                    str(row["explicit_member_name"]),
                    (f"{row['explicit_member_namespace']}:{row['explicit_member_name']}"),
                ),
                effective_at=clock[0],
                knowledge_at=clock[1],
                recorded_at=clock[2],
            )
        )
        component_id = _member_component_id(row)
        repository.persist_source_component(
            SourceTaxonomyComponent(
                component_id=component_id,
                idempotency_key=component_id,
                component_kind="member",
                taxonomy_namespace=str(row["explicit_member_namespace"]),
                local_name=str(row["explicit_member_name"]),
                taxonomy_name=str(row["taxonomy_name"]),
                taxonomy_version=str(row["source_taxonomy_version"]),
                definition_qualifier_sha256=_dimension_definition_qualifier("member", row),
                reporting_entity_id=None,
                is_extension=False,
                evidence_locator={"policy_config_sha256": policy_sha},
                effective_at=clock[0],
                knowledge_at=clock[1],
                recorded_at=clock[2],
            )
        )
        mapping_id = _dimension_mapping_id(component_id)
        repository.persist_dimension_mapping(
            SourceDimensionMappingRevision(
                dimension_mapping_revision_id=mapping_id,
                idempotency_key=mapping_id,
                source_component_id=component_id,
                revision=1,
                disposition="exact",
                canonical_axis_id=axis_id,
                canonical_member_id=member_id,
                policy_name=_POLICY_NAME,
                policy_version=_POLICY_VERSION,
                policy_config_sha256=policy_sha,
                evidence={
                    "member_name": str(row["explicit_member_name"]),
                    "member_namespace": str(row["explicit_member_namespace"]),
                },
                reviewer_identity=None,
                audited_policy_path=_AUDITED_POLICY_PATH,
                effective_at=clock[0],
                knowledge_at=clock[1],
                recorded_at=clock[2],
            )
        )


def _axis_registry_rows(
    conn: sqlite3.Connection,
    knowledge_cutoff: datetime | None = None,
    observed_through: datetime | None = None,
) -> sqlite3.Cursor:
    scope_filter = ""
    params: tuple[object, ...] = ()
    if knowledge_cutoff is not None or observed_through is not None:
        knowledge, observed = _scope_bounds(knowledge_cutoff, observed_through)
        scope_filter = (
            "WHERE datetime(cell.knowledge_at)<=datetime(?) "
            "AND datetime(cell.recorded_at)<=datetime(?) "
            "AND datetime(dimension.recorded_at)<=datetime(?) "
            "AND datetime(observation.knowledge_at)<=datetime(?) "
            "AND datetime(observation.recorded_at)<=datetime(?) "
            "AND datetime(anchor.recorded_at)<=datetime(?) "
        )
        params = (knowledge, observed, observed, knowledge, observed, observed)
    query = """
        SELECT * FROM (
            SELECT dimension.fact_cell_id,dimension.axis_namespace,
                   dimension.axis_name,cell.taxonomy_name,
                   anchor.source_taxonomy_version,
                   cell.effective_at AS source_effective_at,
                   cell.knowledge_at AS source_knowledge_at,
                   cell.recorded_at AS source_recorded_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY dimension.axis_namespace,dimension.axis_name,
                                    cell.taxonomy_name,
                                    anchor.source_taxonomy_version
                       ORDER BY datetime(cell.recorded_at),
                                datetime(cell.knowledge_at),
                                datetime(cell.effective_at),
                                dimension.fact_cell_id
                   ) AS source_rank
            FROM fact_dimensions_normalized_v2 dimension
            JOIN fact_cells_v2 cell
              ON cell.fact_cell_id=dimension.fact_cell_id
            JOIN fact_observations_v2 observation
              ON observation.fact_cell_id=cell.fact_cell_id
             AND observation.observation_kind='reported'
            JOIN fact_reported_observation_anchors_v2 anchor
              ON anchor.observation_id=observation.observation_id
    """
    query += scope_filter
    query += """
        ) WHERE source_rank=1
        ORDER BY axis_namespace,axis_name
    """
    return conn.execute(
        query,  # nosec B608 -- only the fixed dual-clock predicate is appended
        params,
    )


def _member_registry_rows(
    conn: sqlite3.Connection,
    knowledge_cutoff: datetime | None = None,
    observed_through: datetime | None = None,
) -> sqlite3.Cursor:
    scope_filter = ""
    params: tuple[object, ...] = ()
    if knowledge_cutoff is not None or observed_through is not None:
        knowledge, observed = _scope_bounds(knowledge_cutoff, observed_through)
        scope_filter = (
            "WHERE datetime(cell.knowledge_at)<=datetime(?) "
            "AND datetime(cell.recorded_at)<=datetime(?) "
            "AND datetime(dimension.recorded_at)<=datetime(?) "
            "AND datetime(observation.knowledge_at)<=datetime(?) "
            "AND datetime(observation.recorded_at)<=datetime(?) "
            "AND datetime(anchor.recorded_at)<=datetime(?) "
        )
        params = (knowledge, observed, observed, knowledge, observed, observed)
    query = """
        SELECT * FROM (
            SELECT dimension.fact_cell_id,dimension.axis_namespace,
                   dimension.axis_name,dimension.explicit_member_namespace,
                   dimension.explicit_member_name,cell.taxonomy_name,
                   anchor.source_taxonomy_version,
                   cell.effective_at AS source_effective_at,
                   cell.knowledge_at AS source_knowledge_at,
                   cell.recorded_at AS source_recorded_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY dimension.axis_namespace,dimension.axis_name,
                                    dimension.explicit_member_namespace,
                                    dimension.explicit_member_name,
                                    cell.taxonomy_name,
                                    anchor.source_taxonomy_version
                       ORDER BY datetime(cell.recorded_at),
                                datetime(cell.knowledge_at),
                                datetime(cell.effective_at),
                                dimension.fact_cell_id
                   ) AS source_rank
            FROM fact_dimensions_normalized_v2 dimension
            JOIN fact_cells_v2 cell
              ON cell.fact_cell_id=dimension.fact_cell_id
            JOIN fact_observations_v2 observation
              ON observation.fact_cell_id=cell.fact_cell_id
             AND observation.observation_kind='reported'
            JOIN fact_reported_observation_anchors_v2 anchor
              ON anchor.observation_id=observation.observation_id
            WHERE dimension.member_kind='explicit'
    """
    if scope_filter:
        query += " AND " + scope_filter.removeprefix("WHERE ")
    query += """
        ) WHERE source_rank=1
        ORDER BY axis_namespace,axis_name,
                 explicit_member_namespace,explicit_member_name
    """
    return conn.execute(
        query,  # nosec B608 -- only the fixed dual-clock predicate is appended
        params,
    )


def _dimension_clock(row: sqlite3.Row) -> tuple[datetime, datetime, datetime]:
    return (
        _parse_time(row["source_effective_at"]),
        _parse_time(row["source_knowledge_at"]),
        _parse_time(row["source_recorded_at"]),
    )


def _canonical_cell(
    conn: sqlite3.Connection,
    row: Mapping[str, object],
    *,
    operation_recorded_at: datetime,
) -> CanonicalMetricCell:
    source_clock = _component_clock(row)
    dimensions = tuple(
        CanonicalDimension(
            axis_id=_axis_id(dimension),
            member_id=_member_id(dimension),
        )
        for dimension in conn.execute(
            "SELECT axis_namespace,axis_name,explicit_member_namespace,"
            "explicit_member_name FROM fact_dimensions_normalized_v2 "
            "WHERE fact_cell_id=? AND datetime(recorded_at)<=datetime(?) "
            "ORDER BY dimension_ordinal",
            (str(row["fact_cell_id"]), _utc(operation_recorded_at).isoformat()),
        )
    )
    cell_id = _canonical_cell_id(row)
    return CanonicalMetricCell(
        canonical_metric_cell_id=cell_id,
        idempotency_key=cell_id,
        metric_id=_metric_id(row),
        reporting_entity_id=str(row["reporting_entity_id"]),
        scope_security_id=(
            None if row["scope_security_id"] is None else str(row["scope_security_id"])
        ),
        period_kind=cast(
            Literal["instant", "duration"],
            str(row["period_kind"]),
        ),
        period_start=(None if row["period_start"] is None else _parse_time(row["period_start"])),
        period_end=_parse_time(row["period_end"]),
        dimensions=dimensions,
        unit_family=_unit_family(row),
        accounting_basis=str(row["accounting_basis"]),
        consolidation_scope=str(row["consolidation_scope"]),
        effective_at=source_clock[0],
        knowledge_at=source_clock[1],
        recorded_at=source_clock[2],
    )


def _observation_rows(
    conn: sqlite3.Connection,
    *,
    after_observation_id: str | None,
    max_observations: int | None,
    knowledge_cutoff: datetime,
    observed_through: datetime,
) -> sqlite3.Cursor:
    knowledge, observed = _scope_bounds(knowledge_cutoff, observed_through)
    params: list[object] = [knowledge, observed, observed, knowledge, observed]
    after = ""
    if after_observation_id is not None:
        after = "AND observation.observation_id>?"
        params.append(after_observation_id)
    limit = ""
    if max_observations is not None:
        limit = " LIMIT ?"
        params.append(max_observations)
    query = """
        SELECT observation.observation_id,observation.recorded_at,
               cell.taxonomy_name,cell_seal.semantic_key_sha256,
               anchor.extraction_run_id,anchor.source_taxonomy_version,
               anchor.anchor_payload_sha256,anchor.extraction_output_sha256,
               anchor.raw_entry_sha256,payload.observation_payload_sha256,
               completeness.observation_set_sha256
        FROM fact_observations_v2 observation
        JOIN fact_cells_v2 cell ON cell.fact_cell_id=observation.fact_cell_id
        JOIN fact_cell_identity_seals_v2 cell_seal
          ON cell_seal.fact_cell_id=cell.fact_cell_id
        JOIN fact_reported_observation_anchors_v2 anchor
          ON anchor.observation_id=observation.observation_id
        JOIN fact_observation_payload_commitments_v2 payload
          ON payload.observation_id=observation.observation_id
        JOIN fact_extraction_run_completeness_seals_v2 completeness
          ON completeness.extraction_run_id=anchor.extraction_run_id
        WHERE observation.observation_kind='reported'
          AND datetime(observation.knowledge_at)<=datetime(?)
          AND datetime(observation.recorded_at)<=datetime(?)
          AND datetime(payload.committed_at)<=datetime(?)
          AND datetime(completeness.knowledge_at)<=datetime(?)
          AND datetime(completeness.recorded_at)<=datetime(?)
        """
    query += after  # nosec B608 -- fixed cursor clause; value remains bound
    query += " ORDER BY observation.observation_id"
    query += limit  # nosec B608 -- fixed LIMIT clause; value remains bound
    return conn.execute(query, tuple(params))


def _remaining_observation_count(
    conn: sqlite3.Connection,
    *,
    after_observation_id: str | None,
    knowledge_cutoff: datetime,
    observed_through: datetime,
) -> int:
    knowledge, observed = _scope_bounds(knowledge_cutoff, observed_through)
    params: list[object] = [knowledge, observed, observed, knowledge, observed]
    after = ""
    if after_observation_id is not None:
        after = "AND observation.observation_id>?"
        params.append(after_observation_id)
    row = conn.execute(
        "SELECT COUNT(*) "
        "FROM fact_observations_v2 observation "
        "JOIN fact_cells_v2 cell ON cell.fact_cell_id=observation.fact_cell_id "
        "JOIN fact_cell_identity_seals_v2 cell_seal "
        "ON cell_seal.fact_cell_id=cell.fact_cell_id "
        "JOIN fact_reported_observation_anchors_v2 anchor "
        "ON anchor.observation_id=observation.observation_id "
        "JOIN fact_observation_payload_commitments_v2 payload "
        "ON payload.observation_id=observation.observation_id "
        "JOIN fact_extraction_run_completeness_seals_v2 completeness "
        "ON completeness.extraction_run_id=anchor.extraction_run_id "
        "WHERE observation.observation_kind='reported' "
        "AND datetime(observation.knowledge_at)<=datetime(?) "
        "AND datetime(observation.recorded_at)<=datetime(?) "
        "AND datetime(payload.committed_at)<=datetime(?) "
        "AND datetime(completeness.knowledge_at)<=datetime(?) "
        "AND datetime(completeness.recorded_at)<=datetime(?) " + after,  # nosec B608 -- only the fixed cursor clause is appended
        tuple(params),
    ).fetchone()
    return int(row[0])


def _taxonomy_assertion(
    row: Mapping[str, object],
) -> SourceObservationTaxonomyAssertion:
    clock = _parse_time(row["recorded_at"])
    observation_id = str(row["observation_id"])
    return SourceObservationTaxonomyAssertion(
        observation_id=observation_id,
        idempotency_key=_record_id(
            "taxonomy-assertion",
            observation_id,
            _TAXONOMY_VERSION,
        ),
        extraction_run_id=str(row["extraction_run_id"]),
        taxonomy_name=str(row["taxonomy_name"]),
        taxonomy_version=str(row["source_taxonomy_version"]),
        fact_cell_semantic_key_sha256=str(row["semantic_key_sha256"]),
        anchor_payload_sha256=str(row["anchor_payload_sha256"]),
        observation_payload_sha256=str(row["observation_payload_sha256"]),
        extraction_output_sha256=str(row["extraction_output_sha256"]),
        raw_entry_sha256=str(row["raw_entry_sha256"]),
        observation_set_sha256=str(row["observation_set_sha256"]),
        knowledge_at=clock,
        recorded_at=clock,
    )


def _binding_rows(
    conn: sqlite3.Connection,
    *,
    after_observation_id: str | None,
    max_observations: int | None,
    knowledge_cutoff: datetime,
    observed_through: datetime,
) -> sqlite3.Cursor:
    knowledge, observed = _scope_bounds(knowledge_cutoff, observed_through)
    params: list[object] = [knowledge, observed, knowledge, observed]
    after = ""
    if after_observation_id is not None:
        after = "AND observation.observation_id>?"
        params.append(after_observation_id)
    limit = ""
    if max_observations is not None:
        limit = " LIMIT ?"
        params.append(max_observations)
    query = """
        SELECT observation.observation_id,
               observation.recorded_at AS observation_recorded_at,
               cell.recorded_at AS cell_recorded_at,
               anchor.source_taxonomy_version,
               kind.value_kind,
               cell.*
        FROM fact_observations_v2 observation
        JOIN fact_cells_v2 cell ON cell.fact_cell_id=observation.fact_cell_id
        JOIN (
            SELECT fact_cell_id,MIN(value_kind) AS value_kind
            FROM fact_observations_v2
            WHERE observation_kind='reported' AND value_kind<>'nil'
            GROUP BY fact_cell_id
            HAVING COUNT(DISTINCT value_kind)=1
        ) kind ON kind.fact_cell_id=cell.fact_cell_id
        JOIN fact_reported_observation_anchors_v2 anchor
          ON anchor.observation_id=observation.observation_id
        JOIN source_observation_taxonomy_assertions assertion
          ON assertion.observation_id=observation.observation_id
        WHERE observation.observation_kind='reported'
          AND datetime(observation.knowledge_at)<=datetime(?)
          AND datetime(observation.recorded_at)<=datetime(?)
          AND datetime(assertion.knowledge_at)<=datetime(?)
          AND datetime(assertion.recorded_at)<=datetime(?)
        """
    query += after  # nosec B608 -- fixed cursor clause; value remains bound
    query += " ORDER BY observation.observation_id"
    query += limit  # nosec B608 -- fixed LIMIT clause; value remains bound
    return conn.execute(query, tuple(params))


def _binding(
    row: Mapping[str, object],
) -> BindingRevision:
    observation_id = str(row["observation_id"])
    clock = max(
        _parse_time(row["observation_recorded_at"]),
        _parse_time(row["cell_recorded_at"]),
        key=_utc,
    )
    binding_id = _binding_id(observation_id)
    return BindingRevision(
        binding_revision_id=binding_id,
        idempotency_key=binding_id,
        fact_cell_id=str(row["fact_cell_id"]),
        source_observation_id=observation_id,
        revision=1,
        canonical_metric_cell_id=_canonical_cell_id(row),
        mapping_revision_id=_mapping_id(row),
        source_component_id=_concept_component_id(row),
        binding_status="bound",
        effective_at=clock,
        knowledge_at=clock,
        recorded_at=clock,
    )


def _seal_snapshot(
    conn: sqlite3.Connection,
    repository: MetricOntology,
    policy_sha: str,
    *,
    knowledge_cutoff: datetime,
    operation_recorded_at: datetime,
) -> str:
    cutoff = knowledge_cutoff
    _verify_snapshot_population_closure(
        conn,
        PopulationTemporalScope(
            knowledge_cutoff=knowledge_cutoff,
            observed_through=operation_recorded_at,
        ),
    )
    member_fold = _CanonicalArrayFold()
    for kind, member_id, digest in _snapshot_members_at_cutoff(
        conn,
        cutoff,
        operation_recorded_at,
    ):
        member_fold.add({"id": member_id, "kind": kind, "sha256": digest})
    member_set_sha = member_fold.hexdigest()
    snapshot_id = _snapshot_id(
        cutoff,
        operation_recorded_at,
        policy_sha,
        member_set_sha,
    )
    snapshot = OntologySnapshot(
        ontology_snapshot_id=snapshot_id,
        idempotency_key=snapshot_id,
        cutoff_at=cutoff,
        recorded_at=operation_recorded_at,
    )
    repository.seal_snapshot(snapshot)
    repository.verify_snapshot(snapshot_id)
    seal = conn.execute(
        "SELECT member_set_sha256 FROM ontology_snapshot_seals WHERE ontology_snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if seal is None or str(seal[0]) != member_set_sha:
        raise ValueError("ontology snapshot does not match its pre-write member commitment")
    return snapshot_id


def _cell_clock(
    row: Mapping[str, object] | sqlite3.Row,
) -> tuple[datetime, datetime, datetime]:
    return (
        _parse_time(row["effective_at"]),
        _parse_time(row["knowledge_at"]),
        _parse_time(row["recorded_at"]),
    )


def _component_clock(
    row: Mapping[str, object] | sqlite3.Row,
) -> tuple[datetime, datetime, datetime]:
    source = _cell_clock(row)
    parent_created_at = _parse_time(row["reporting_entity_created_at"])
    return (
        max(source[0], parent_created_at, key=_utc),
        max(source[1], parent_created_at, key=_utc),
        max(source[2], parent_created_at, key=_utc),
    )


def _earliest_clock(
    clocks: list[tuple[datetime, datetime, datetime]],
) -> tuple[datetime, datetime, datetime]:
    if not clocks:
        raise ValueError("ontology object has no source clock")
    return min(
        clocks,
        key=lambda item: (
            _utc(item[2]),
            _utc(item[1]),
            _utc(item[0]),
        ),
    )


def _object_clocks(
    cells: Iterable[Mapping[str, object]],
    identity: Callable[[Mapping[str, object]], str],
) -> dict[str, tuple[datetime, datetime, datetime]]:
    earliest: dict[str, tuple[datetime, datetime, datetime]] = {}
    for cell in cells:
        key = identity(cell)
        candidate = _cell_clock(cell)
        existing = earliest.get(key)
        if existing is None or (
            _utc(candidate[2]),
            _utc(candidate[1]),
            _utc(candidate[0]),
        ) < (
            _utc(existing[2]),
            _utc(existing[1]),
            _utc(existing[0]),
        ):
            earliest[key] = candidate
    return dict(sorted(earliest.items()))


def _metric_signature(row: Mapping[str, object] | sqlite3.Row) -> str:
    return _canonical_json(_source_definition_identity(row).model_dump(mode="json"))


def _source_definition_identity(
    row: Mapping[str, object] | sqlite3.Row,
) -> _SourceDefinitionIdentity:
    return _SourceDefinitionIdentity(
        reporting_entity_id=str(row["reporting_entity_id"]),
        taxonomy_name=str(row["taxonomy_name"]),
        taxonomy_version=str(row["source_taxonomy_version"]),
        concept_namespace=str(row["concept_namespace"]),
        concept_name=str(row["concept_name"]),
        accounting_basis=str(row["accounting_basis"]),
        consolidation_scope=str(row["consolidation_scope"]),
        period_kind=str(row["period_kind"]),
        unit_family=_unit_family(row),
        value_kind=str(row["value_kind"]),
    )


def _source_definition_commitment(
    row: Mapping[str, object] | sqlite3.Row,
) -> str:
    return _model_sha(_source_definition_identity(row))


def _metric_id(row: Mapping[str, object] | sqlite3.Row) -> str:
    return _record_id("canonical-metric", _metric_signature(row))


def _concept_component_id(row: Mapping[str, object] | sqlite3.Row) -> str:
    return _record_id(
        "source-concept",
        str(row["reporting_entity_id"]),
        str(row["concept_namespace"]),
        str(row["concept_name"]),
        str(row["taxonomy_name"]),
        str(row["source_taxonomy_version"]),
        _source_definition_commitment(row),
    )


def _dimension_definition_qualifier(
    component_kind: Literal["axis", "member"],
    row: Mapping[str, object] | sqlite3.Row,
) -> str:
    if component_kind == "axis":
        namespace = str(row["axis_namespace"])
        local_name = str(row["axis_name"])
    else:
        namespace = str(row["explicit_member_namespace"])
        local_name = str(row["explicit_member_name"])
    return _digest(
        _canonical_json(
            {
                "component_kind": component_kind,
                "local_name": local_name,
                "taxonomy_name": str(row["taxonomy_name"]),
                "taxonomy_namespace": namespace,
                "taxonomy_version": str(row["source_taxonomy_version"]),
            }
        )
    )


def _mapping_id(row: Mapping[str, object] | sqlite3.Row) -> str:
    return _record_id(
        "metric-mapping",
        _concept_component_id(row),
        _POLICY_VERSION,
        "1",
    )


def _dimension_mapping_id(component_id: str) -> str:
    return _record_id(
        "dimension-mapping",
        component_id,
        _POLICY_VERSION,
        "1",
    )


def _binding_id(observation_id: str) -> str:
    return _record_id(
        "canonical-binding",
        observation_id,
        _POLICY_VERSION,
        "1",
    )


def _canonical_cell_id(row: Mapping[str, object] | sqlite3.Row) -> str:
    return _record_id(
        "canonical-metric-cell",
        str(row["semantic_key_sha256"]),
        _metric_id(row),
    )


def _axis_id(row: Mapping[str, object] | sqlite3.Row) -> str:
    return _record_id(
        "canonical-axis",
        str(row["axis_namespace"]),
        str(row["axis_name"]),
    )


def _member_id(row: Mapping[str, object] | sqlite3.Row) -> str:
    return _record_id(
        "canonical-member",
        _axis_id(row),
        str(row["explicit_member_namespace"]),
        str(row["explicit_member_name"]),
    )


def _axis_component_id(row: Mapping[str, object] | sqlite3.Row) -> str:
    return _record_id(
        "source-axis",
        str(row["axis_namespace"]),
        str(row["axis_name"]),
        str(row["taxonomy_name"]),
        str(row["source_taxonomy_version"]),
        _dimension_definition_qualifier("axis", row),
    )


def _member_component_id(row: Mapping[str, object] | sqlite3.Row) -> str:
    return _record_id(
        "source-member",
        str(row["explicit_member_namespace"]),
        str(row["explicit_member_name"]),
        str(row["taxonomy_name"]),
        str(row["source_taxonomy_version"]),
        _dimension_definition_qualifier("member", row),
    )


def _unit_family(row: Mapping[str, object] | sqlite3.Row) -> str:
    if row["currency"] is not None:
        return "currency"
    unit = str(row["unit_key"])
    return unit.lower() if unit.lower() in {"pure", "shares"} else unit


def _bounded_name(prefix: str, identity: str) -> str:
    normalized = " ".join(prefix.split()) or "unnamed"
    suffix = _digest(identity)[:16]
    return f"{normalized[:230]} [{suffix}]"


def _policy_sha() -> str:
    return _digest(
        _canonical_json(
            {
                "audited_policy_path": _AUDITED_POLICY_PATH,
                "knowledge_clock_policy": "earliest_source_object_clock",
                "recording_clock_policy": "stable_source_derived_object_clocks",
                "cross_issuer_policy": "issuer_scoped_provisional_metrics",
                "input_manifest_version": "metric-ontology-input.v2",
                "mapping_rule": "exact_preserved_source_definition_coordinate",
                "nil_policy": "nil_is_absence_not_metric_type",
                "output_manifest_version": "metric-ontology-output.v3",
                "policy_name": _POLICY_NAME,
                "policy_version": _POLICY_VERSION,
                "snapshot_identity": "cutoff_observed_through_policy_member_set",
                "taxonomy_version": _TAXONOMY_VERSION,
            }
        )
    )


def _snapshot_id(
    cutoff: datetime,
    observed_through: datetime,
    policy_sha: str,
    member_set_sha: str,
) -> str:
    return _record_id(
        "ontology-snapshot",
        _utc(cutoff).isoformat(),
        _utc(observed_through).isoformat(),
        policy_sha,
        member_set_sha,
    )


def _count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) FROM {table}"  # nosec B608 -- internal fixed table names
    ).fetchone()
    return 0 if row is None else int(row[0])


def _count_source_cells_at_scope(
    conn: sqlite3.Connection,
    knowledge_cutoff: datetime,
    observed_through: datetime,
) -> int:
    knowledge, observed = _scope_bounds(knowledge_cutoff, observed_through)
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_cells_v2 "
            "WHERE datetime(knowledge_at)<=datetime(?) "
            "AND datetime(recorded_at)<=datetime(?)",
            (knowledge, observed),
        ).fetchone()[0]
    )


def _count_reported_observations(
    conn: sqlite3.Connection,
    knowledge_cutoff: datetime,
    observed_through: datetime,
) -> int:
    knowledge, observed = _scope_bounds(knowledge_cutoff, observed_through)
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_observations_v2 "
            "WHERE observation_kind='reported' "
            "AND datetime(knowledge_at)<=datetime(?) "
            "AND datetime(recorded_at)<=datetime(?)",
            (knowledge, observed),
        ).fetchone()[0]
    )


def _count_missing_assertions(
    conn: sqlite3.Connection,
    knowledge_cutoff: datetime,
    observed_through: datetime,
) -> int:
    knowledge, observed = _scope_bounds(knowledge_cutoff, observed_through)
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_observations_v2 observation "
            "LEFT JOIN source_observation_taxonomy_assertions assertion "
            "ON assertion.observation_id=observation.observation_id "
            "WHERE observation.observation_kind='reported' "
            "AND datetime(observation.knowledge_at)<=datetime(?) "
            "AND datetime(observation.recorded_at)<=datetime(?) "
            "AND (assertion.observation_id IS NULL "
            "OR datetime(assertion.knowledge_at)>datetime(?) "
            "OR datetime(assertion.recorded_at)>datetime(?))",
            (knowledge, observed, knowledge, observed),
        ).fetchone()[0]
    )


def _count_missing_bindings(
    conn: sqlite3.Connection,
    knowledge_cutoff: datetime,
    observed_through: datetime,
) -> int:
    knowledge, observed = _scope_bounds(knowledge_cutoff, observed_through)
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_observations_v2 observation "
            "LEFT JOIN fact_cell_canonical_binding_revisions binding "
            "ON binding.source_observation_id=observation.observation_id "
            "WHERE observation.observation_kind='reported' "
            "AND datetime(observation.knowledge_at)<=datetime(?) "
            "AND datetime(observation.recorded_at)<=datetime(?) "
            "AND (binding.source_observation_id IS NULL "
            "OR datetime(binding.knowledge_at)>datetime(?) "
            "OR datetime(binding.recorded_at)>datetime(?))",
            (knowledge, observed, knowledge, observed),
        ).fetchone()[0]
    )


def _verify_snapshot_population_closure(
    conn: sqlite3.Connection,
    scope: PopulationTemporalScope,
) -> None:
    knowledge_text = _utc(scope.knowledge_cutoff).isoformat()
    observed_text = _utc(scope.observed_through).isoformat()
    assertion_mismatch = int(
        conn.execute(
            """
            WITH expected AS (
                SELECT observation_id
                FROM fact_observations_v2
                WHERE observation_kind='reported'
                  AND datetime(knowledge_at)<=datetime(?)
                  AND datetime(recorded_at)<=datetime(?)
            ),
            actual AS (
                SELECT observation_id
                FROM source_observation_taxonomy_assertions
                WHERE datetime(knowledge_at)<=datetime(?)
                  AND datetime(recorded_at)<=datetime(?)
            )
            SELECT EXISTS(SELECT observation_id FROM expected
                          EXCEPT SELECT observation_id FROM actual)
                OR EXISTS(SELECT observation_id FROM actual
                          EXCEPT SELECT observation_id FROM expected)
            """,
            (knowledge_text, observed_text, knowledge_text, observed_text),
        ).fetchone()[0]
    )
    if assertion_mismatch:
        raise ValueError("ontology snapshot assertion set is not exact")
    binding_mismatch = int(
        conn.execute(
            """
            WITH expected AS (
                SELECT observation_id
                FROM fact_observations_v2
                WHERE observation_kind='reported'
                  AND datetime(knowledge_at)<=datetime(?)
                  AND datetime(recorded_at)<=datetime(?)
            ),
            actual AS (
                SELECT binding.source_observation_id AS observation_id,
                       binding.binding_status
                FROM fact_cell_canonical_binding_revisions binding
                WHERE datetime(binding.effective_at)<=datetime(?)
                  AND datetime(binding.knowledge_at)<=datetime(?)
                  AND datetime(binding.recorded_at)<=datetime(?)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM fact_cell_canonical_binding_revisions newer
                      WHERE newer.source_observation_id=
                            binding.source_observation_id
                        AND newer.revision>binding.revision
                        AND datetime(newer.effective_at)<=datetime(?)
                        AND datetime(newer.knowledge_at)<=datetime(?)
                        AND datetime(newer.recorded_at)<=datetime(?)
                  )
            )
            SELECT EXISTS(SELECT observation_id FROM expected
                          EXCEPT SELECT observation_id FROM actual)
                OR EXISTS(SELECT observation_id FROM actual
                          EXCEPT SELECT observation_id FROM expected)
                OR EXISTS(SELECT 1 FROM actual WHERE binding_status<>'bound')
            """,
            (
                knowledge_text,
                observed_text,
                knowledge_text,
                observed_text,
                knowledge_text,
                observed_text,
                knowledge_text,
                observed_text,
            ),
        ).fetchone()[0]
    )
    if binding_mismatch:
        raise ValueError("ontology snapshot latest binding set is not exact and active")


def _snapshot_members_at_cutoff(
    conn: sqlite3.Connection,
    cutoff: datetime,
    observed_through: datetime,
) -> Iterator[tuple[str, str, str]]:
    cutoff_text = _utc(cutoff).isoformat()
    observed_text = _utc(observed_through).isoformat()
    immutable = (
        ("canonical_axis", "canonical_axes", "axis_id"),
        ("canonical_member", "canonical_members", "member_id"),
        ("canonical_metric", "canonical_metrics", "metric_id"),
        ("source_component", "source_taxonomy_components", "component_id"),
    )
    revisioned = (
        (
            "binding",
            "fact_cell_canonical_binding_revisions",
            "binding_revision_id",
            "source_observation_id",
        ),
        (
            "dimension_mapping",
            "source_dimension_mapping_revisions",
            "dimension_mapping_revision_id",
            "source_component_id",
        ),
        (
            "metric_definition",
            "canonical_metric_definition_revisions",
            "metric_definition_revision_id",
            "metric_id",
        ),
        (
            "metric_mapping",
            "metric_mapping_revisions",
            "mapping_revision_id",
            "source_component_id",
        ),
    )
    for kind, table, identity, coordinate in revisioned[:1]:
        yield from (
            (kind, str(row[0]), str(row[1]))
            for row in conn.execute(
                f"SELECT record.{identity},record.commitment_sha256 "  # nosec B608 -- fixed internal ontology schema
                f"FROM {table} record "  # nosec B608 -- fixed internal ontology schema
                "WHERE record.effective_at<=? "
                "AND record.knowledge_at<=? "
                "AND record.recorded_at<=? "
                f"AND NOT EXISTS (SELECT 1 FROM {table} newer "  # nosec B608 -- fixed internal ontology schema
                f"WHERE newer.{coordinate}=record.{coordinate} "  # nosec B608 -- fixed internal ontology schema
                "AND newer.revision>record.revision "
                "AND newer.effective_at<=? "
                "AND newer.knowledge_at<=? "
                "AND newer.recorded_at<=?) "
                f"ORDER BY record.{identity}",  # nosec B608 -- fixed internal ontology schema
                (
                    cutoff_text,
                    cutoff_text,
                    observed_text,
                    cutoff_text,
                    cutoff_text,
                    observed_text,
                ),
            )
        )
    for kind, table, identity in immutable[:-1]:
        yield from (
            (kind, str(row[0]), str(row[1]))
            for row in conn.execute(
                f"SELECT {identity},commitment_sha256 FROM {table} "  # nosec B608 -- fixed internal ontology tables
                "WHERE effective_at<=? AND knowledge_at<=? AND recorded_at<=? "
                f"ORDER BY {identity}",  # nosec B608 -- fixed identity columns
                (cutoff_text, cutoff_text, observed_text),
            )
        )
        if kind == "canonical_axis":
            yield from (
                ("canonical_cell", str(row[0]), str(row[1]))
                for row in conn.execute(
                    "SELECT seal.canonical_metric_cell_id,seal.semantic_key_sha256 "
                    "FROM canonical_metric_cell_seals seal "
                    "JOIN canonical_metric_cells cell "
                    "ON cell.canonical_metric_cell_id=seal.canonical_metric_cell_id "
                    "WHERE cell.effective_at<=? AND cell.knowledge_at<=? "
                    "AND cell.recorded_at<=? AND seal.sealed_at<=? "
                    "ORDER BY seal.canonical_metric_cell_id",
                    (cutoff_text, cutoff_text, observed_text, observed_text),
                )
            )
    for kind, table, identity, coordinate in revisioned[1:]:
        yield from (
            (kind, str(row[0]), str(row[1]))
            for row in conn.execute(
                f"SELECT record.{identity},record.commitment_sha256 "  # nosec B608 -- fixed internal ontology schema
                f"FROM {table} record "  # nosec B608 -- fixed internal ontology schema
                "WHERE record.effective_at<=? AND record.knowledge_at<=? "
                "AND record.recorded_at<=? "
                f"AND NOT EXISTS (SELECT 1 FROM {table} newer "  # nosec B608 -- fixed internal ontology schema
                f"WHERE newer.{coordinate}=record.{coordinate} "  # nosec B608 -- fixed internal ontology schema
                "AND newer.revision>record.revision "
                "AND newer.effective_at<=? AND newer.knowledge_at<=? "
                "AND newer.recorded_at<=?) "
                f"ORDER BY record.{identity}",  # nosec B608 -- fixed internal ontology schema
                (
                    cutoff_text,
                    cutoff_text,
                    observed_text,
                    cutoff_text,
                    cutoff_text,
                    observed_text,
                ),
            )
        )
    kind, table, identity = immutable[-1]
    yield from (
        (kind, str(row[0]), str(row[1]))
        for row in conn.execute(
            f"SELECT {identity},commitment_sha256 FROM {table} "  # nosec B608 -- fixed internal ontology tables
            "WHERE effective_at<=? AND knowledge_at<=? AND recorded_at<=? "
            f"ORDER BY {identity}",  # nosec B608 -- fixed identity columns
            (cutoff_text, cutoff_text, observed_text),
        )
    )
    yield from (
        ("source_taxonomy_assertion", str(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT observation_id,commitment_sha256 "
            "FROM source_observation_taxonomy_assertions "
            "WHERE datetime(knowledge_at)<=datetime(?) "
            "AND datetime(recorded_at)<=datetime(?) ORDER BY observation_id",
            (cutoff_text, observed_text),
        )
    )


def _plan_commitment(
    conn: sqlite3.Connection,
    input_sha: str,
    policy_sha: str,
    knowledge_cutoff: datetime,
    operation_recorded_at: datetime,
) -> str:
    """Commit to every deterministic object identity before any write."""

    fold = _CommitmentFold("metric-ontology-plan.v5")
    fold.add(
        "manifest",
        {
            "input_commitment_sha256": input_sha,
            "knowledge_cutoff": _utc(knowledge_cutoff).isoformat(),
            "operation_recorded_at": _utc(operation_recorded_at).isoformat(),
            "plan_version": "metric-ontology-plan.v5",
            "policy_config_sha256": policy_sha,
        },
    )
    for row in _registry_rows(conn, knowledge_cutoff, operation_recorded_at):
        clock = _cell_clock(row)
        component_clock = _component_clock(row)
        metric_id = _metric_id(row)
        component_id = _concept_component_id(row)
        fold.add(
            "metric_stack",
            {
                "concept_component_id": component_id,
                "metric_clock": _clock_manifest(clock),
                "metric_definition_revision_id": _record_id(
                    "metric-definition",
                    metric_id,
                    _POLICY_VERSION,
                    "1",
                ),
                "metric_id": metric_id,
                "mapping_revision_id": _mapping_id(row),
                "recorded_at": _utc(clock[2]).isoformat(),
                "source_component_clock": _clock_manifest(component_clock),
                "source_definition_commitment_sha256": (_source_definition_commitment(row)),
            },
        )
    for row in _axis_registry_rows(conn, knowledge_cutoff, operation_recorded_at):
        component_id = _axis_component_id(row)
        clock = _dimension_clock(row)
        fold.add(
            "axis",
            {
                "axis_id": _axis_id(row),
                "dimension_mapping_revision_id": _dimension_mapping_id(component_id),
                "recorded_at": _utc(clock[2]).isoformat(),
                "source_component_id": component_id,
                "source_clock": _clock_manifest(_dimension_clock(row)),
            },
        )
    for row in _member_registry_rows(conn, knowledge_cutoff, operation_recorded_at):
        component_id = _member_component_id(row)
        clock = _dimension_clock(row)
        fold.add(
            "member",
            {
                "axis_id": _axis_id(row),
                "dimension_mapping_revision_id": _dimension_mapping_id(component_id),
                "member_id": _member_id(row),
                "recorded_at": _utc(clock[2]).isoformat(),
                "source_component_id": component_id,
                "source_clock": _clock_manifest(_dimension_clock(row)),
            },
        )
    for cell in _source_cells(conn, knowledge_cutoff, operation_recorded_at):
        canonical_cell_clock = _component_clock(cell)
        metric_id = _metric_id(cell)
        component_id = _concept_component_id(cell)
        canonical_cell_id = _canonical_cell_id(cell)
        fold.add(
            "canonical_cell",
            {
                "canonical_cell_id": canonical_cell_id,
                "concept_component_id": component_id,
                "fact_cell_id": str(cell["fact_cell_id"]),
                "mapping_revision_id": _mapping_id(cell),
                "metric_id": metric_id,
                "canonical_cell_clock": _clock_manifest(canonical_cell_clock),
                "source_definition_commitment_sha256": (_source_definition_commitment(cell)),
            },
        )
        for dimension in conn.execute(
            "SELECT axis_namespace,axis_name,explicit_member_namespace,"
            "explicit_member_name FROM fact_dimensions_normalized_v2 "
            "WHERE fact_cell_id=? AND datetime(recorded_at)<=datetime(?) "
            "ORDER BY dimension_ordinal",
            (str(cell["fact_cell_id"]), _utc(operation_recorded_at).isoformat()),
        ):
            fold.add(
                "canonical_cell_dimension",
                {
                    "axis_id": _axis_id(dimension),
                    "canonical_cell_id": canonical_cell_id,
                    "member_id": _member_id(dimension),
                },
            )
    knowledge, observed = _scope_bounds(knowledge_cutoff, operation_recorded_at)
    for row in conn.execute(
        "SELECT observation.observation_id,observation.recorded_at,"
        "cell.recorded_at FROM fact_observations_v2 observation "
        "JOIN fact_cells_v2 cell ON cell.fact_cell_id=observation.fact_cell_id "
        "WHERE observation.observation_kind='reported' "
        "AND datetime(observation.knowledge_at)<=datetime(?) "
        "AND datetime(observation.recorded_at)<=datetime(?) "
        "ORDER BY observation.observation_id",
        (knowledge, observed),
    ):
        observation_id = str(row[0])
        assertion_recorded_at = _parse_time(row[1])
        binding_recorded_at = max(assertion_recorded_at, _parse_time(row[2]), key=_utc)
        fold.add(
            "observation",
            {
                "binding_revision_id": _binding_id(observation_id),
                "observation_id": observation_id,
                "binding_recorded_at": _utc(binding_recorded_at).isoformat(),
                "taxonomy_assertion_recorded_at": _utc(assertion_recorded_at).isoformat(),
                "taxonomy_assertion_idempotency_key": _record_id(
                    "taxonomy-assertion",
                    observation_id,
                    _TAXONOMY_VERSION,
                ),
            },
        )
    return fold.hexdigest()


def _clock_manifest(
    clock: tuple[datetime, datetime, datetime],
) -> dict[str, str]:
    return {
        "effective_at": _utc(clock[0]).isoformat(),
        "knowledge_at": _utc(clock[1]).isoformat(),
        "recorded_at": _utc(clock[2]).isoformat(),
    }


def _manifest_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"type": "bytes", "value": value.hex()}
    if isinstance(value, float):
        return {"type": "float", "value": value.hex()}
    if value is None:
        return {"type": "null"}
    return {"type": type(value).__name__, "value": value}


def _table_manifest_sha256(conn: sqlite3.Connection, table: str) -> str:
    """Hash a fixed internal table's schema and complete ordered row multiset."""

    columns = tuple(
        str(row[1])
        for row in conn.execute(
            f"PRAGMA table_info({table})"  # nosec B608 -- fixed internal manifest table
        )
    )
    if not columns:
        raise ValueError(f"required manifest table is missing: {table}")
    column_sql = ",".join(f'"{column}"' for column in columns)
    digest = hashlib.sha256()
    digest.update(
        _canonical_json(
            {
                "columns": columns,
                "manifest_version": "exact-table.v1",
                "table": table,
            }
        ).encode()
    )
    row_count = 0
    for row in conn.execute(
        f"SELECT {column_sql} FROM {table} ORDER BY {column_sql}"  # nosec B608 -- fixed internal manifest table and schema-derived columns
    ):
        digest.update(_canonical_json([_manifest_value(value) for value in row]).encode())
        row_count += 1
    digest.update(_canonical_json({"row_count": row_count}).encode())
    return digest.hexdigest()


def _scoped_table_manifest_sha256(
    conn: sqlite3.Connection,
    table: str,
    knowledge_cutoff: datetime,
    observed_through: datetime,
) -> str:
    """Hash one fixed manifest table after applying its persisted K/O clocks."""

    knowledge, observed = _scope_bounds(knowledge_cutoff, observed_through)
    columns = tuple(
        str(row[1])
        for row in conn.execute(
            f"PRAGMA table_info({table})"  # nosec B608 -- fixed internal manifest table
        )
    )
    if not columns:
        raise ValueError(f"required manifest table is missing: {table}")
    column_sql = ",".join(f'record."{column}"' for column in columns)
    from_sql = f"{table} record"
    predicates: list[str] = []
    params: list[object] = []
    if table in {"fact_cell_identity_seals_v2", "fact_dimensions_normalized_v2"}:
        from_sql += " JOIN fact_cells_v2 parent ON parent.fact_cell_id=record.fact_cell_id"
        predicates.extend(
            (
                "datetime(parent.knowledge_at)<=datetime(?)",
                "datetime(parent.recorded_at)<=datetime(?)",
            )
        )
        params.extend((knowledge, observed))
    elif table in {
        "fact_reported_observation_anchors_v2",
        "fact_observation_payload_commitments_v2",
    }:
        from_sql += (
            " JOIN fact_observations_v2 parent ON parent.observation_id=record.observation_id"
        )
        predicates.extend(
            (
                "datetime(parent.knowledge_at)<=datetime(?)",
                "datetime(parent.recorded_at)<=datetime(?)",
            )
        )
        params.extend((knowledge, observed))
    elif table in {
        "canonical_metric_cell_dimensions",
        "canonical_metric_cell_seals",
    }:
        from_sql += (
            " JOIN canonical_metric_cells parent "
            "ON parent.canonical_metric_cell_id=record.canonical_metric_cell_id"
        )
        predicates.extend(
            (
                "datetime(parent.knowledge_at)<=datetime(?)",
                "datetime(parent.recorded_at)<=datetime(?)",
            )
        )
        params.extend((knowledge, observed))
    elif table in {"ontology_snapshot_members", "ontology_snapshot_seals"}:
        from_sql += (
            " JOIN ontology_snapshot_headers parent "
            "ON parent.ontology_snapshot_id=record.ontology_snapshot_id"
        )
        predicates.extend(
            (
                "datetime(parent.cutoff_at)<=datetime(?)",
                "datetime(parent.recorded_at)<=datetime(?)",
            )
        )
        params.extend((knowledge, observed))
    if "knowledge_at" in columns:
        predicates.append("datetime(record.knowledge_at)<=datetime(?)")
        params.append(knowledge)
    if "cutoff_at" in columns:
        predicates.append("datetime(record.cutoff_at)<=datetime(?)")
        params.append(knowledge)
    for clock in ("recorded_at", "sealed_at", "committed_at"):
        if clock in columns:
            predicates.append(f"datetime(record.{clock})<=datetime(?)")
            params.append(observed)
    if table == "evidence_extraction_runs":
        predicates.append("datetime(record.completed_at)<=datetime(?)")
        params.append(knowledge)
    where = "" if not predicates else " WHERE " + " AND ".join(predicates)
    query = (
        f"SELECT {column_sql} FROM {from_sql}"  # nosec B608 -- fixed manifest joins and schema-derived columns
        f"{where} ORDER BY {column_sql}"  # nosec B608 -- fixed predicates and schema-derived columns
    )
    digest = hashlib.sha256()
    digest.update(
        _canonical_json(
            {
                "columns": columns,
                "knowledge_cutoff": knowledge,
                "manifest_version": "exact-table-k-o.v1",
                "observed_through": observed,
                "table": table,
            }
        ).encode()
    )
    row_count = 0
    for row in conn.execute(query, tuple(params)):
        digest.update(_canonical_json([_manifest_value(value) for value in row]).encode())
        row_count += 1
    digest.update(_canonical_json({"row_count": row_count}).encode())
    return digest.hexdigest()


def _manifest_commitment(
    conn: sqlite3.Connection,
    *,
    version: str,
    tables: tuple[str, ...],
) -> str:
    payload = {
        "manifest_version": version,
        "tables": [
            {
                "sha256": _table_manifest_sha256(conn, table),
                "table": table,
            }
            for table in tables
        ],
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _input_commitment(conn: sqlite3.Connection) -> str:
    return _manifest_commitment(
        conn,
        version="metric-ontology-input.v2",
        tables=_INPUT_MANIFEST_TABLES,
    )


def _output_commitment(conn: sqlite3.Connection) -> str:
    return _manifest_commitment(
        conn,
        version="metric-ontology-output.v2",
        tables=_OUTPUT_MANIFEST_TABLES,
    )


def _scoped_manifest_commitment(
    conn: sqlite3.Connection,
    *,
    version: str,
    tables: tuple[str, ...],
    knowledge_cutoff: datetime,
    observed_through: datetime,
) -> str:
    payload = {
        "knowledge_cutoff": _utc(knowledge_cutoff).isoformat(),
        "manifest_version": version,
        "observed_through": _utc(observed_through).isoformat(),
        "tables": [
            {
                "sha256": _scoped_table_manifest_sha256(
                    conn,
                    table,
                    knowledge_cutoff,
                    observed_through,
                ),
                "table": table,
            }
            for table in tables
        ],
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _input_commitment_at_scope(
    conn: sqlite3.Connection,
    knowledge_cutoff: datetime,
    observed_through: datetime,
) -> str:
    return _scoped_manifest_commitment(
        conn,
        version="metric-ontology-input-k-o.v1",
        tables=_INPUT_MANIFEST_TABLES,
        knowledge_cutoff=knowledge_cutoff,
        observed_through=observed_through,
    )


def _output_commitment_at_scope(
    conn: sqlite3.Connection,
    knowledge_cutoff: datetime,
    observed_through: datetime,
) -> str:
    return _scoped_manifest_commitment(
        conn,
        version="metric-ontology-output-k-o.v1",
        tables=_OUTPUT_MANIFEST_TABLES,
        knowledge_cutoff=knowledge_cutoff,
        observed_through=observed_through,
    )
