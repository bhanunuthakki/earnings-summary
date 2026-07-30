"""Bridge governed legacy reports into the hardened source-fact plane.

The bridge plans one immutable, cutoff-bound publication manifest before it
writes. Caller commitments are assertions, never trusted inputs: every apply
recomputes and verifies the complete source and planned-publication graphs.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
    FactDimensionV2,
    ReportedFactObservationV2,
)
from provenance.population_completeness import (
    PopulationPlaneVerification,
    PopulationTemporalScope,
    digest_text,
    stream_population_artifact_set,
)
from provenance.population_completeness import (
    canonical_json as canonical_population_json,
)
from provenance.source_fact_repository import (
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)

_POLICY_NAME = "governed_legacy_reported_fact_bridge"
_POLICY_VERSION = "5"
_COMMITMENT_NAMESPACE_VERSION = "v4"
_SOURCE_TAXONOMY_VERSION = "legacy-observation-contract.v1"
_ELIGIBLE_DOCUMENT_TYPES = (
    "fmp_10k_json",
    "fmp_as_reported_balance",
    "fmp_as_reported_cashflow",
    "fmp_as_reported_financial",
    "fmp_as_reported_income",
    "fmp_balance_sheet",
    "fmp_cashflow",
    "fmp_income_statement",
    "ir_historical_spreadsheet",
    "ir_presentation",
    "ir_press_release",
    "sec_10k",
)
_EXCLUSION_REASONS = (
    "derived_without_formula_lineage",
    "llm_synthesized_source",
    "unapproved_document_type",
    "incomplete_extraction_run",
    "after_data_cutoff",
    "no_selected_subject_binding_as_of_cutoff",
)
_SEMANTIC_CELL_BATCH_SIZE = 400
_SEMANTIC_CELL_CACHE_SIZE = 4096


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceFactPopulationRequest(_FrozenModel):
    data_cutoff_at: datetime
    operation_recorded_at: datetime
    apply: bool = False
    after_extraction_run_id: str | None = None
    max_runs: int | None = Field(default=None, ge=1)
    input_commitment_sha256: str | None = None
    planned_output_commitment_sha256: str | None = None

    @field_validator("input_commitment_sha256", "planned_output_commitment_sha256")
    @classmethod
    def _sha(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("population commitment must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _request_contract(self) -> Self:
        if (self.input_commitment_sha256 is None) != (
            self.planned_output_commitment_sha256 is None
        ):
            raise ValueError("population commitments must be supplied together")
        if _utc(self.operation_recorded_at) < _utc(self.data_cutoff_at):
            raise ValueError("operation_recorded_at must not precede data_cutoff_at")
        if (
            self.apply
            and self.after_extraction_run_id is not None
            and self.input_commitment_sha256 is None
        ):
            raise ValueError("an apply resume requires manifest commitments")
        return self


class SourceFactPopulationResult(_FrozenModel):
    mode: Literal["dry_run", "apply"]
    policy_name: str
    policy_version: str
    policy_config_sha256: str = Field(min_length=64, max_length=64)
    expected_count: int
    eligible_count: int
    excluded_count: int
    exclusion_counts: dict[str, int]
    eligible_run_count: int
    processed_run_count: int
    processed_observation_count: int
    created_record_count: int
    exact_replay_run_count: int
    last_extraction_run_id: str | None
    input_commitment_sha256: str = Field(min_length=64, max_length=64)
    planned_output_commitment_sha256: str = Field(min_length=64, max_length=64)


class SourceFactPopulationBatchError(RuntimeError):
    """A failed batch with an exact, resumable last-commit receipt."""

    def __init__(
        self,
        *,
        failed_extraction_run_id: str,
        last_committed_extraction_run_id: str | None,
        processed_run_count: int,
        processed_observation_count: int,
        created_record_count: int,
        exact_replay_run_count: int,
        input_commitment_sha256: str,
        planned_output_commitment_sha256: str,
    ) -> None:
        super().__init__(
            "source-fact population failed after an atomic publication batch "
            f"for extraction run {failed_extraction_run_id}"
        )
        self.failed_extraction_run_id = failed_extraction_run_id
        self.last_committed_extraction_run_id = last_committed_extraction_run_id
        self.processed_run_count = processed_run_count
        self.processed_observation_count = processed_observation_count
        self.created_record_count = created_record_count
        self.exact_replay_run_count = exact_replay_run_count
        self.input_commitment_sha256 = input_commitment_sha256
        self.planned_output_commitment_sha256 = planned_output_commitment_sha256

    def checkpoint_payload(self) -> dict[str, JsonValue]:
        return {
            "created_record_count": self.created_record_count,
            "exact_replay_run_count": self.exact_replay_run_count,
            "failed_extraction_run_id": self.failed_extraction_run_id,
            "input_commitment_sha256": self.input_commitment_sha256,
            "last_committed_extraction_run_id": self.last_committed_extraction_run_id,
            "planned_output_commitment_sha256": (self.planned_output_commitment_sha256),
            "processed_observation_count": self.processed_observation_count,
            "processed_run_count": self.processed_run_count,
        }


@dataclass(frozen=True, slots=True)
class _RunPlan:
    extraction_run_id: str
    eligible_observation_count: int
    expected_node_count: int
    knowledge_at: datetime
    planned_output_commitment_sha256: str


@dataclass(slots=True)
class _MutableRunPlan:
    extraction_run_id: str
    first_capture: datetime
    eligible_observation_count: int = 0
    expected_node_count: int = 0
    knowledge_at: datetime | None = None
    output_fold: _CommitmentFold | None = None


@dataclass(frozen=True, slots=True)
class _ManifestFactCandidate:
    row: sqlite3.Row
    fact: ReportedSourceFact
    run_state: _MutableRunPlan
    ordinal: int


@dataclass(frozen=True, slots=True)
class _PopulationPlan:
    policy_config_sha256: str
    expected_count: int
    eligible_count: int
    exclusion_counts: dict[str, int]
    run_ids: tuple[str, ...]
    run_plans: dict[str, _RunPlan]
    input_commitment_sha256: str
    planned_output_commitment_sha256: str


@dataclass(frozen=True, slots=True)
class _ValidatedSemanticCell:
    cell: FactCellV2
    sealed_semantic_identity_json: str
    sealed_dimension_set_json: str


class _SemanticCellCache:
    """Bounded LRU of fully validated persisted semantic-cell envelopes."""

    def __init__(self, capacity: int = _SEMANTIC_CELL_CACHE_SIZE) -> None:
        if capacity < 1:
            raise ValueError("semantic-cell cache capacity must be positive")
        self._capacity = capacity
        self._values: OrderedDict[str, _ValidatedSemanticCell] = OrderedDict()

    def get(self, semantic_key: str) -> _ValidatedSemanticCell | None:
        value = self._values.get(semantic_key)
        if value is not None:
            self._values.move_to_end(semantic_key)
        return value

    def put(self, semantic_key: str, value: _ValidatedSemanticCell) -> None:
        self._values[semantic_key] = value
        self._values.move_to_end(semantic_key)
        if len(self._values) > self._capacity:
            self._values.popitem(last=False)


class _CommitmentFold:
    """Length-delimited canonical record fold with constant observation memory."""

    def __init__(self, namespace: str) -> None:
        self._digest = hashlib.sha256(namespace.encode())

    @staticmethod
    def encode(kind: str, payload: object) -> bytes:
        return _canonical_json([kind, payload]).encode()

    def add(self, kind: str, payload: object) -> None:
        self.add_encoded(self.encode(kind, payload))

    def add_encoded(self, encoded: bytes) -> None:
        self._digest.update(len(encoded).to_bytes(8, "big"))
        self._digest.update(encoded)

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


class _RunDependencySpill:
    """Disk-backed correction dependencies with bounded Python memory."""

    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> Self:
        self._conn = sqlite3.connect("")
        self._conn.executescript(
            """
            PRAGMA temp_store=FILE;
            CREATE TABLE run_nodes (
                run_id TEXT PRIMARY KEY,
                first_capture TEXT NOT NULL,
                indegree INTEGER NOT NULL DEFAULT 0,
                emitted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE run_edges (
                parent_run_id TEXT NOT NULL,
                child_run_id TEXT NOT NULL,
                PRIMARY KEY(parent_run_id,child_run_id)
            ) WITHOUT ROWID;
            CREATE INDEX ix_run_edges_child ON run_edges(child_run_id);
            CREATE TEMP TABLE ready_run_nodes (
                run_id TEXT PRIMARY KEY,
                first_capture TEXT NOT NULL
            ) WITHOUT ROWID;
            """
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("run dependency spill is not open")
        return self._conn

    def add_run(self, run_id: str, first_capture: datetime) -> None:
        conn = self._connection()
        stamp = _db_time(first_capture)
        existing = conn.execute(
            "SELECT first_capture FROM run_nodes WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO run_nodes (run_id,first_capture) VALUES (?,?)",
                (run_id, stamp),
            )
        elif _utc(_parse_required_datetime(existing[0])) > _utc(first_capture):
            conn.execute(
                "UPDATE run_nodes SET first_capture=? WHERE run_id=?",
                (stamp, run_id),
            )

    def add_edge(self, parent_run_id: str, child_run_id: str) -> None:
        if parent_run_id == child_run_id:
            return
        conn = self._connection()
        inserted = conn.execute(
            "INSERT OR IGNORE INTO run_edges (parent_run_id,child_run_id) VALUES (?,?)",
            (parent_run_id, child_run_id),
        ).rowcount
        if inserted:
            conn.execute(
                "UPDATE run_nodes SET indegree=indegree+1 WHERE run_id=?",
                (child_run_id,),
            )

    def topological_run_ids(self) -> tuple[str, ...]:
        conn = self._connection()
        ordered: list[str] = []
        total = int(conn.execute("SELECT COUNT(*) FROM run_nodes").fetchone()[0])
        while len(ordered) < total:
            conn.execute("DELETE FROM ready_run_nodes")
            conn.execute(
                "INSERT INTO ready_run_nodes (run_id,first_capture) "
                "SELECT run_id,first_capture FROM run_nodes "
                "WHERE emitted=0 AND indegree=0"
            )
            ready_count = int(conn.execute("SELECT COUNT(*) FROM ready_run_nodes").fetchone()[0])
            if ready_count == 0:
                raise ValueError("eligible observation lineage contains a run dependency cycle")
            for row in conn.execute(
                "SELECT run_id FROM ready_run_nodes ORDER BY first_capture,run_id"
            ):
                ordered.append(str(row[0]))
            conn.execute(
                "UPDATE run_nodes SET emitted=1 WHERE run_id IN "
                "(SELECT run_id FROM ready_run_nodes)"
            )
            conn.execute(
                "UPDATE run_nodes SET indegree=indegree-("
                "SELECT COUNT(*) FROM run_edges edge "
                "JOIN ready_run_nodes ready "
                "ON ready.run_id=edge.parent_run_id "
                "WHERE edge.child_run_id=run_nodes.run_id"
                ") WHERE emitted=0 AND EXISTS ("
                "SELECT 1 FROM run_edges edge "
                "JOIN ready_run_nodes ready "
                "ON ready.run_id=edge.parent_run_id "
                "WHERE edge.child_run_id=run_nodes.run_id)"
            )
        return tuple(ordered)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _record_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{_digest(*parts)}"


def populate_source_fact_plane(
    conn: sqlite3.Connection,
    request: SourceFactPopulationRequest,
) -> SourceFactPopulationResult:
    """Plan or publish bounded extraction-run units into the hardened plane."""

    semantic_cell_cache = _SemanticCellCache()
    plan = _population_plan(conn, request, semantic_cell_cache=semantic_cell_cache)
    _verify_caller_commitments(request, plan)
    run_ids = _bounded_run_ids(plan, request)
    processed_observations = 0
    created_records = 0
    replay_runs = 0
    processed_runs = 0
    last_run: str | None = None
    if request.apply:
        repository = SourceFactRepository(conn)
        for extraction_run_id in run_ids:
            try:
                publication, run_commitment = _build_run_publication(
                    conn,
                    request=request,
                    run_plan=plan.run_plans[extraction_run_id],
                    policy_sha=plan.policy_config_sha256,
                    semantic_cell_cache=semantic_cell_cache,
                )
                if (
                    run_commitment
                    != plan.run_plans[extraction_run_id].planned_output_commitment_sha256
                ):
                    raise ValueError("source-fact run output changed after manifest verification")
                with conn:
                    receipt = repository.publish(publication)
            except Exception as exc:
                raise SourceFactPopulationBatchError(
                    failed_extraction_run_id=extraction_run_id,
                    last_committed_extraction_run_id=last_run,
                    processed_run_count=processed_runs,
                    processed_observation_count=processed_observations,
                    created_record_count=created_records,
                    exact_replay_run_count=replay_runs,
                    input_commitment_sha256=plan.input_commitment_sha256,
                    planned_output_commitment_sha256=(plan.planned_output_commitment_sha256),
                ) from exc
            processed_runs += 1
            processed_observations += len(publication.reported_facts)
            created_records += len(receipt.created_record_ids)
            replay_runs += int(receipt.exact_replay)
            last_run = extraction_run_id
    return SourceFactPopulationResult(
        mode="apply" if request.apply else "dry_run",
        policy_name=_POLICY_NAME,
        policy_version=_POLICY_VERSION,
        policy_config_sha256=plan.policy_config_sha256,
        expected_count=plan.expected_count,
        eligible_count=plan.eligible_count,
        excluded_count=plan.expected_count - plan.eligible_count,
        exclusion_counts=plan.exclusion_counts,
        eligible_run_count=len(plan.run_ids),
        processed_run_count=processed_runs,
        processed_observation_count=processed_observations,
        created_record_count=created_records,
        exact_replay_run_count=replay_runs,
        last_extraction_run_id=last_run,
        input_commitment_sha256=plan.input_commitment_sha256,
        planned_output_commitment_sha256=plan.planned_output_commitment_sha256,
    )


def _policy() -> tuple[dict[str, JsonValue], str]:
    policy: dict[str, JsonValue] = {
        "canonical_cell_fiscal_period_rule": "unset_nonsemantic_legacy_label",
        "eligible_document_types": list(_ELIGIBLE_DOCUMENT_TYPES),
        "excluded_observation_statuses": ["derived"],
        "excluded_source_types": ["llm_extracted"],
        "lineage_rule": "nearest_prior_eligible_revision",
        "policy_name": _POLICY_NAME,
        "policy_version": _POLICY_VERSION,
        "publication_unit": "evidence_extraction_run",
        "run_outcome": "succeeded",
        "source_taxonomy_version": _SOURCE_TAXONOMY_VERSION,
        "subject_binding_rule": "latest_revision_as_of_knowledge_and_recorded_cutoffs",
    }
    return policy, _digest(_canonical_json(policy))


def _population_plan(
    conn: sqlite3.Connection,
    request: SourceFactPopulationRequest,
    *,
    semantic_cell_cache: _SemanticCellCache | None = None,
) -> _PopulationPlan:
    cache = semantic_cell_cache or _SemanticCellCache()
    policy, policy_sha = _policy()
    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        manifest_header = {
            "commitment_format": "length_delimited_canonical_records.v1",
            "data_cutoff_at": _db_time(request.data_cutoff_at),
            "operation_recorded_at": _db_time(request.operation_recorded_at),
            "policy": policy,
            "policy_config_sha256": policy_sha,
            "source_taxonomy_version": _SOURCE_TAXONOMY_VERSION,
        }
        input_fold = _CommitmentFold(f"population-source-input.{_COMMITMENT_NAMESPACE_VERSION}")
        output_fold = _CommitmentFold(f"population-source-output.{_COMMITMENT_NAMESPACE_VERSION}")
        input_fold.add("manifest_header", manifest_header)
        output_fold.add("manifest_header", manifest_header)
        exclusions = {reason: 0 for reason in _EXCLUSION_REASONS}
        run_states: dict[str, _MutableRunPlan] = {}
        source_run_ids: set[str] = set()
        expected_count = 0
        eligible_count = 0
        previous_coordinate: tuple[str, int] | None = None
        previous_eligible_observation_id: str | None = None
        previous_eligible_run_id: str | None = None
        pending_facts: list[_ManifestFactCandidate] = []
        with _RunDependencySpill() as dependencies:
            for row in _source_rows(conn, request):
                expected_count += 1
                run_id = str(row["extraction_run_id"])
                source_run_ids.add(run_id)
                coordinate = (str(row["fact_table"]), int(row["fact_row_id"]))
                if coordinate != previous_coordinate:
                    previous_coordinate = coordinate
                    previous_eligible_observation_id = None
                    previous_eligible_run_id = None
                reason = _exclusion_reason(row, request.data_cutoff_at)
                input_fold.add(
                    "source_row",
                    {
                        "classification": "eligible" if reason is None else reason,
                        "row": dict(zip(row.keys(), tuple(row), strict=True)),
                    },
                )
                if reason is not None:
                    exclusions[reason] += 1
                    continue
                eligible_count += 1
                captured = _parse_required_datetime(row["captured_at"])
                state = run_states.get(run_id)
                if state is None:
                    state = _MutableRunPlan(
                        extraction_run_id=run_id,
                        first_capture=captured,
                        output_fold=_CommitmentFold(
                            "population-source-run-output."
                            f"{_COMMITMENT_NAMESPACE_VERSION}\x1f{run_id}"
                        ),
                    )
                    run_states[run_id] = state
                    dependencies.add_run(run_id, captured)
                elif _utc(captured) < _utc(state.first_capture):
                    state.first_capture = captured
                    dependencies.add_run(run_id, captured)
                if previous_eligible_run_id is not None and previous_eligible_run_id != run_id:
                    dependencies.add_edge(previous_eligible_run_id, run_id)
                fact = _source_fact_from_row(
                    row,
                    policy_sha=policy_sha,
                    prior_observation_id=previous_eligible_observation_id,
                    operation_recorded_at=request.operation_recorded_at,
                )
                pending_facts.append(
                    _ManifestFactCandidate(
                        row,
                        fact,
                        state,
                        state.eligible_observation_count,
                    )
                )
                state.eligible_observation_count += 1
                knowledge_at = fact.observation.knowledge_at
                if state.knowledge_at is None or _utc(knowledge_at) > _utc(state.knowledge_at):
                    state.knowledge_at = knowledge_at
                previous_eligible_observation_id = str(row["observation_id"])
                previous_eligible_run_id = run_id
                if len(pending_facts) == _SEMANTIC_CELL_BATCH_SIZE:
                    _fold_manifest_fact_batch(conn, pending_facts, output_fold, cache)
                    pending_facts.clear()
            _fold_manifest_fact_batch(conn, pending_facts, output_fold, cache)
            for node in _node_rows(conn, request.operation_recorded_at):
                node_run_id = str(node[0])
                if node_run_id not in source_run_ids:
                    continue
                input_fold.add("evidence_node", list(node))
                state = run_states.get(node_run_id)
                if state is not None:
                    state.expected_node_count += 1
            run_ids = dependencies.topological_run_ids()
        immutable_run_plans: dict[str, _RunPlan] = {}
        for extraction_run_id in run_ids:
            state = run_states[extraction_run_id]
            if state.knowledge_at is None or state.output_fold is None:
                raise ValueError("eligible extraction run has no planned facts")
            _, envelope = _publication_envelope(
                extraction_run_id=extraction_run_id,
                policy_sha=policy_sha,
                expected_node_count=state.expected_node_count,
                knowledge_at=state.knowledge_at,
                operation_recorded_at=request.operation_recorded_at,
            )
            state.output_fold.add("publication_envelope", envelope)
            output_fold.add("publication_envelope", envelope)
            immutable_run_plans[extraction_run_id] = _RunPlan(
                extraction_run_id=extraction_run_id,
                eligible_observation_count=state.eligible_observation_count,
                expected_node_count=state.expected_node_count,
                knowledge_at=state.knowledge_at,
                planned_output_commitment_sha256=state.output_fold.hexdigest(),
            )
        return _PopulationPlan(
            policy_config_sha256=policy_sha,
            expected_count=expected_count,
            eligible_count=eligible_count,
            exclusion_counts=exclusions,
            run_ids=run_ids,
            run_plans=immutable_run_plans,
            input_commitment_sha256=input_fold.hexdigest(),
            planned_output_commitment_sha256=output_fold.hexdigest(),
        )
    finally:
        conn.row_factory = original_row_factory


def _verify_caller_commitments(
    request: SourceFactPopulationRequest,
    plan: _PopulationPlan,
) -> None:
    if request.input_commitment_sha256 is None:
        return
    if request.input_commitment_sha256 != plan.input_commitment_sha256:
        raise ValueError("source-fact input commitment does not match current manifest")
    if request.planned_output_commitment_sha256 is None:
        raise ValueError("planned source-fact output commitment is required for apply")
    if request.planned_output_commitment_sha256 != plan.planned_output_commitment_sha256:
        raise ValueError("source-fact planned output commitment does not match current manifest")


def _bounded_run_ids(
    plan: _PopulationPlan,
    request: SourceFactPopulationRequest,
) -> tuple[str, ...]:
    start = 0
    if request.after_extraction_run_id is not None:
        try:
            start = plan.run_ids.index(request.after_extraction_run_id) + 1
        except ValueError as exc:
            raise ValueError(
                "source-fact checkpoint extraction run is not in the committed manifest"
            ) from exc
    run_ids = plan.run_ids[start:]
    if request.max_runs is not None:
        run_ids = run_ids[: request.max_runs]
    return run_ids


def _source_rows(
    conn: sqlite3.Connection,
    request: SourceFactPopulationRequest,
    *,
    extraction_run_id: str | None = None,
    observation_id: str | None = None,
) -> sqlite3.Cursor:
    filters = ""
    filter_params: list[object] = []
    if extraction_run_id is not None:
        filters += " AND run.extraction_run_id=?"
        filter_params.append(extraction_run_id)
    if observation_id is not None:
        filters += " AND revision.observation_id=?"
        filter_params.append(observation_id)
    query = """
        SELECT revision.fact_table,revision.fact_row_id,revision.fact_revision,
               revision.observation_id,revision.source_document_id,
               revision.source_tier,revision.locator_json,revision.captured_at,
               observation.concept_key,observation.period_start,
               observation.period_end,observation.fiscal_period_type,
               observation.dimensions_json,observation.numeric_value,
               observation.text_value,observation.currency,observation.unit,
               observation.observation_status,observation.evidence_node_id,
               observation.available_at,observation.recorded_at,
               observation.method,observation.method_version,
               document.doc_type,document.source_type,
               node.locator_json AS node_locator_json,
               node.recorded_at AS node_recorded_at,
               run.extraction_run_id,run.document_version_id,
               run.input_sha256 AS extraction_input_sha256,
               run.extractor_name,run.extractor_config_sha256,
               run.extractor_code_version,
               run.output_sha256 AS extraction_output_sha256,
               run.started_at,run.completed_at,run.outcome AS run_outcome,
               version.issuer_id AS recorded_issuer_id,
               version.recorded_at AS document_recorded_at,
               source_observation.observed_at AS source_observed_at,
               source_observation.retrieved_at AS source_retrieved_at,
               subject.binding_revision_id,subject.idempotency_key
                 AS binding_idempotency_key,
               subject.revision AS binding_revision,
               subject.issuer_id AS bound_issuer_id,
               subject.reporting_entity_id,subject.security_id,
               subject.outcome AS binding_outcome,
               subject.decision_kind AS binding_decision_kind,
               subject.reason_code AS binding_reason_code,
               subject.reason_details_json AS binding_reason_details_json,
               subject.material_dissent AS binding_material_dissent,
               subject.effective_at AS binding_effective_at,
               subject.knowledge_at AS binding_knowledge_at,
               subject.recorded_at AS binding_recorded_at,
               subject.supersedes_binding_revision_id
        FROM fact_observation_revisions revision
        JOIN reported_observations observation
          ON observation.observation_id=revision.observation_id
        JOIN documents document ON document.id=revision.source_document_id
        JOIN evidence_nodes node ON node.node_id=observation.evidence_node_id
        JOIN evidence_extraction_runs run
          ON run.extraction_run_id=node.extraction_run_id
        JOIN evidence_document_versions version
          ON version.document_version_id=run.document_version_id
        JOIN evidence_source_observations source_observation
          ON source_observation.observation_id=version.observation_id
        LEFT JOIN recorded_subject_binding_revisions subject
          ON subject.binding_revision_id=(
              SELECT candidate.binding_revision_id
              FROM recorded_subject_binding_revisions candidate
              WHERE candidate.recorded_issuer_id=version.issuer_id
                AND datetime(candidate.knowledge_at)<=datetime(?)
                AND datetime(candidate.recorded_at)<=datetime(?)
              ORDER BY candidate.revision DESC,candidate.binding_revision_id DESC
              LIMIT 1
          )
        WHERE datetime(revision.captured_at)<=datetime(?)
          AND datetime(observation.recorded_at)<=datetime(?)
          AND datetime(node.recorded_at)<=datetime(?)
          AND datetime(version.recorded_at)<=datetime(?)
          AND datetime(observation.available_at)<=datetime(?)
          AND datetime(run.completed_at)<=datetime(?)
          AND datetime(source_observation.observed_at)<=datetime(?)
          AND datetime(source_observation.retrieved_at)<=datetime(?)
        """
    query += filters  # nosec B608 -- fixed equality clauses; values remain bound
    query += """
        ORDER BY revision.fact_table,revision.fact_row_id,
                 revision.fact_revision,revision.observation_id
        """
    return conn.execute(
        query,
        (
            _db_time(request.data_cutoff_at),
            _db_time(request.operation_recorded_at),
            *(_db_time(request.operation_recorded_at) for _ in range(4)),
            *(_db_time(request.data_cutoff_at) for _ in range(3)),
            _db_time(request.operation_recorded_at),
            *filter_params,
        ),
    )


def _exclusion_reason(
    row: sqlite3.Row,
    data_cutoff_at: datetime,
) -> str | None:
    if str(row["observation_status"]) == "derived":
        return _EXCLUSION_REASONS[0]
    if str(row["source_type"]) == "llm_extracted":
        return _EXCLUSION_REASONS[1]
    if str(row["doc_type"]) not in _ELIGIBLE_DOCUMENT_TYPES:
        return _EXCLUSION_REASONS[2]
    if row["completed_at"] is None or str(row["run_outcome"]) != "succeeded":
        return _EXCLUSION_REASONS[3]
    if _utc(_row_knowledge_at(row)) > _utc(data_cutoff_at):
        return _EXCLUSION_REASONS[4]
    if (
        row["binding_revision_id"] is None
        or str(row["binding_outcome"]) != "selected"
        or row["reporting_entity_id"] is None
    ):
        return _EXCLUSION_REASONS[5]
    return None


def _row_knowledge_at(row: sqlite3.Row) -> datetime:
    candidates = tuple(
        value
        for value in (
            _parse_datetime(row["available_at"]),
            _parse_datetime(row["completed_at"]),
            _parse_datetime(row["source_observed_at"]),
            _parse_datetime(row["binding_knowledge_at"]),
            _parse_datetime(row["period_end"]),
        )
        if value is not None
    )
    if not candidates:
        raise ValueError("reported observation has no knowledge clock")
    return max(candidates, key=_utc)


def _build_run_publication(
    conn: sqlite3.Connection,
    *,
    request: SourceFactPopulationRequest,
    run_plan: _RunPlan,
    policy_sha: str,
    semantic_cell_cache: _SemanticCellCache,
) -> tuple[SourceFactPublication, str]:
    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return _build_run_publication_rows(
            conn,
            request=request,
            run_plan=run_plan,
            policy_sha=policy_sha,
            semantic_cell_cache=semantic_cell_cache,
        )
    finally:
        conn.row_factory = original_row_factory


def _build_run_publication_rows(
    conn: sqlite3.Connection,
    *,
    request: SourceFactPopulationRequest,
    run_plan: _RunPlan,
    policy_sha: str,
    semantic_cell_cache: _SemanticCellCache,
) -> tuple[SourceFactPublication, str]:
    facts: list[ReportedSourceFact] = []
    pending: list[tuple[sqlite3.Row, ReportedSourceFact]] = []
    run_fold = _CommitmentFold(
        "population-source-run-output."
        f"{_COMMITMENT_NAMESPACE_VERSION}\x1f{run_plan.extraction_run_id}"
    )
    for row in _source_rows(
        conn,
        request,
        extraction_run_id=run_plan.extraction_run_id,
    ):
        if _exclusion_reason(row, request.data_cutoff_at) is not None:
            continue
        prior_observation_id = _nearest_prior_eligible_observation_id(
            conn,
            request,
            row,
        )
        pending.append(
            (
                row,
                _source_fact_from_row(
                    row,
                    policy_sha=policy_sha,
                    prior_observation_id=prior_observation_id,
                    operation_recorded_at=request.operation_recorded_at,
                ),
            )
        )
        if len(pending) == _SEMANTIC_CELL_BATCH_SIZE:
            _append_run_fact_batch(conn, pending, facts, run_fold, semantic_cell_cache)
            pending.clear()
    _append_run_fact_batch(conn, pending, facts, run_fold, semantic_cell_cache)
    if len(facts) != run_plan.eligible_observation_count:
        raise ValueError("source-fact run membership changed after manifest planning")
    seal, envelope = _publication_envelope(
        extraction_run_id=run_plan.extraction_run_id,
        policy_sha=policy_sha,
        expected_node_count=run_plan.expected_node_count,
        knowledge_at=run_plan.knowledge_at,
        operation_recorded_at=request.operation_recorded_at,
    )
    run_fold.add("publication_envelope", envelope)
    publication_id = str(envelope["publication_id"])
    publication = SourceFactPublication(
        publication_id=publication_id,
        idempotency_key=publication_id,
        created_at=run_plan.knowledge_at,
        recorded_at=request.operation_recorded_at,
        reported_facts=tuple(facts),
        extraction_seals=(seal,),
    )
    return publication, run_fold.hexdigest()


def _nearest_prior_eligible_observation_id(
    conn: sqlite3.Connection,
    request: SourceFactPopulationRequest,
    row: sqlite3.Row,
) -> str | None:
    revision = int(row["fact_revision"])
    if revision <= 1:
        return None
    candidates = conn.execute(
        "SELECT observation_id FROM fact_observation_revisions "
        "WHERE fact_table=? AND fact_row_id=? AND fact_revision<? "
        "ORDER BY fact_revision DESC,observation_id DESC",
        (
            str(row["fact_table"]),
            int(row["fact_row_id"]),
            revision,
        ),
    )
    for candidate in candidates:
        prior = _source_rows(
            conn,
            request,
            observation_id=str(candidate[0]),
        ).fetchone()
        if (
            prior is not None
            and _exclusion_reason(
                prior,
                request.data_cutoff_at,
            )
            is None
        ):
            return str(prior["observation_id"])
    return None


def _fact_output_payload(
    fact: ReportedSourceFact,
    row: sqlite3.Row,
    *,
    ordinal: int,
) -> dict[str, object]:
    observation = fact.observation
    return {
        "anchor": {
            "document_version_id": row["document_version_id"],
            "evidence_node_id": row["evidence_node_id"],
            "extraction_input_sha256": row["extraction_input_sha256"],
            "extraction_output_sha256": row["extraction_output_sha256"],
            "extraction_run_id": row["extraction_run_id"],
            "extractor_code_version": row["extractor_code_version"],
            "extractor_config_sha256": row["extractor_config_sha256"],
            "extractor_name": row["extractor_name"],
            "raw_entry_sha256": observation.source_entry_sha256,
            "source_locator_sha256": observation.source_locator_sha256,
            "source_taxonomy_version": observation.source_taxonomy_version,
            "subject_binding_revision_id": observation.subject_binding_revision_id,
        },
        "fact": fact.model_dump(mode="json"),
        "member_ordinal": ordinal,
        "observation_id": observation.observation_id,
    }


def _publication_envelope(
    *,
    extraction_run_id: str,
    policy_sha: str,
    expected_node_count: int,
    knowledge_at: datetime,
    operation_recorded_at: datetime,
) -> tuple[ExtractionRunCompletenessSealV2, dict[str, object]]:
    seal = ExtractionRunCompletenessSealV2(
        extraction_seal_id=_record_id(
            "extraction-seal",
            extraction_run_id,
            policy_sha,
        ),
        idempotency_key=_record_id(
            "extraction-seal-idempotency",
            extraction_run_id,
            policy_sha,
        ),
        extraction_run_id=extraction_run_id,
        expected_node_count=expected_node_count,
        completeness_policy_name=_POLICY_NAME,
        completeness_policy_version=_POLICY_VERSION,
        completeness_policy_sha256=policy_sha,
        knowledge_at=knowledge_at,
        recorded_at=operation_recorded_at,
    )
    publication_id = _record_id(
        "source-publication",
        extraction_run_id,
        policy_sha,
    )
    return seal, {
        "created_at": _db_time(knowledge_at),
        "extraction_seal": seal.model_dump(mode="json"),
        "idempotency_key": publication_id,
        "publication_id": publication_id,
        "recorded_at": _db_time(operation_recorded_at),
    }


def _node_rows(
    conn: sqlite3.Connection,
    operation_recorded_at: datetime,
) -> sqlite3.Cursor:
    return conn.execute(
        "SELECT extraction_run_id,node_id,locator_json,recorded_at "
        "FROM evidence_nodes WHERE datetime(recorded_at)<=datetime(?) "
        "ORDER BY extraction_run_id,node_id",
        (_db_time(operation_recorded_at),),
    )


def _source_fact_from_row(
    row: sqlite3.Row,
    *,
    policy_sha: str,
    prior_observation_id: str | None,
    operation_recorded_at: datetime,
) -> ReportedSourceFact:
    period_start = _parse_datetime(row["period_start"])
    period_end = _parse_datetime(row["period_end"])
    if period_end is None:
        raise ValueError("reported legacy observation is missing period_end")
    period_kind: Literal["instant", "duration"] = (
        "duration" if period_start is not None and period_start < period_end else "instant"
    )
    if period_kind == "instant":
        period_start = None
    knowledge_at = _row_knowledge_at(row)
    if _utc(operation_recorded_at) < _utc(knowledge_at):
        raise ValueError("operation_recorded_at precedes source knowledge")
    effective_at = period_end
    concept_namespace = (
        "urn:earnings-summary:legacy:kpi"
        if str(row["fact_table"]) == "kpi_facts"
        else "urn:earnings-summary:legacy:financial"
    )
    accounting_basis: Literal[
        "us_gaap",
        "ifrs",
        "local_gaap",
        "management",
        "non_gaap",
        "other",
    ] = "management" if str(row["fact_table"]) == "kpi_facts" else "other"
    unit_key = str(row["unit"] or "unknown")
    currency_value = row["currency"]
    currency = (
        str(currency_value).upper()
        if currency_value is not None
        and len(str(currency_value)) == 3
        and str(currency_value).isalpha()
        else None
    )
    cell_id = _planned_cell_id(row)
    dimensions = _dimensions(
        str(row["dimensions_json"]),
        operation_recorded_at,
        fact_cell_id=cell_id,
    )
    cell = FactCellV2(
        fact_cell_id=cell_id,
        idempotency_key=cell_id,
        reporting_entity_id=str(row["reporting_entity_id"]),
        scope_security_id=(None if row["security_id"] is None else str(row["security_id"])),
        concept_namespace=concept_namespace,
        concept_name=str(row["concept_key"]),
        taxonomy_name="earnings-summary-legacy",
        taxonomy_version=_SOURCE_TAXONOMY_VERSION,
        accounting_basis=accounting_basis,
        consolidation_scope="other",
        period_kind=period_kind,
        period_start=period_start,
        period_end=period_end,
        fiscal_year=period_end.year,
        fiscal_period=None,
        dimensions=dimensions,
        unit_key=unit_key,
        currency=currency,
        effective_at=effective_at,
        knowledge_at=knowledge_at,
        recorded_at=operation_recorded_at,
    )
    locator = _locator_payload(row)
    numeric = row["numeric_value"]
    text = row["text_value"]
    if numeric is not None:
        value_kind: Literal["numeric", "text", "nil"] = "numeric"
    elif text is not None and str(text):
        value_kind = "text"
    else:
        value_kind = "nil"
    observation_id = _v2_observation_id(str(row["observation_id"]))
    source_payload = {
        "fact_revision": int(row["fact_revision"]),
        "fact_row_id": int(row["fact_row_id"]),
        "fact_table": str(row["fact_table"]),
        "fiscal_period_type": str(row["fiscal_period_type"]),
        "legacy_observation_id": str(row["observation_id"]),
        "locator": locator,
        "numeric_value": None if numeric is None else str(numeric),
        "source_document_id": int(row["source_document_id"]),
        "text_value": None if text is None else str(text),
    }
    observation = ReportedFactObservationV2(
        observation_id=observation_id,
        idempotency_key=observation_id,
        fact_cell_id=cell.fact_cell_id,
        observation_kind="reported",
        value_kind=value_kind,
        numeric_value=None if value_kind != "numeric" else str(numeric),
        text_value=None if value_kind != "text" else str(text),
        is_nil=value_kind == "nil",
        raw_lexical_value=(
            None if value_kind == "nil" else str(numeric if numeric is not None else text)
        ),
        method_name=f"legacy-bridge:{row['method']}",
        method_version=str(row["method_version"]),
        method_config_sha256=policy_sha,
        revision_kind="initial" if prior_observation_id is None else "correction",
        supersedes_observation_id=(
            None if prior_observation_id is None else _v2_observation_id(prior_observation_id)
        ),
        effective_at=effective_at,
        knowledge_at=knowledge_at,
        recorded_at=operation_recorded_at,
        document_version_id=str(row["document_version_id"]),
        evidence_node_id=str(row["evidence_node_id"]),
        source_locator=CanonicalJSONObject(root=locator),
        source_entry_sha256=_digest(_canonical_json(source_payload)),
        subject_binding_revision_id=str(row["binding_revision_id"]),
        source_taxonomy_version=_SOURCE_TAXONOMY_VERSION,
        source_context_id=None,
        source_unit_id=unit_key,
        decimals=None,
        precision=None,
    )
    return ReportedSourceFact(cell=cell, observation=observation)


def _fold_manifest_fact_batch(
    conn: sqlite3.Connection,
    candidates: list[_ManifestFactCandidate],
    output_fold: _CommitmentFold,
    semantic_cell_cache: _SemanticCellCache,
) -> None:
    resolved = _reuse_existing_semantic_cells(
        conn,
        tuple(candidate.fact for candidate in candidates),
        semantic_cell_cache,
    )
    for candidate, fact in zip(candidates, resolved, strict=True):
        fact_payload = _fact_output_payload(
            fact,
            candidate.row,
            ordinal=candidate.ordinal,
        )
        encoded = _CommitmentFold.encode("reported_fact", fact_payload)
        output_fold.add_encoded(encoded)
        if candidate.run_state.output_fold is None:
            raise RuntimeError("source-fact manifest state is missing its output fold")
        candidate.run_state.output_fold.add_encoded(encoded)


def _append_run_fact_batch(
    conn: sqlite3.Connection,
    candidates: list[tuple[sqlite3.Row, ReportedSourceFact]],
    facts: list[ReportedSourceFact],
    run_fold: _CommitmentFold,
    semantic_cell_cache: _SemanticCellCache,
) -> None:
    resolved = _reuse_existing_semantic_cells(
        conn,
        tuple(fact for _, fact in candidates),
        semantic_cell_cache,
    )
    for (row, _), fact in zip(candidates, resolved, strict=True):
        run_fold.add(
            "reported_fact",
            _fact_output_payload(fact, row, ordinal=len(facts)),
        )
        facts.append(fact)


def _reuse_existing_semantic_cells(
    conn: sqlite3.Connection,
    facts: tuple[ReportedSourceFact, ...],
    semantic_cell_cache: _SemanticCellCache,
) -> tuple[ReportedSourceFact, ...]:
    if not facts:
        return ()
    if len(facts) > _SEMANTIC_CELL_BATCH_SIZE:
        raise ValueError("semantic-cell resolution batch exceeds the bounded query size")
    semantic_keys = tuple(dict.fromkeys(str(fact.cell.semantic_key_sha256) for fact in facts))
    existing_by_semantic_key = {
        semantic_key: cached
        for semantic_key in semantic_keys
        if (cached := semantic_cell_cache.get(semantic_key)) is not None
    }
    missing_keys = tuple(
        semantic_key
        for semantic_key in semantic_keys
        if semantic_key not in existing_by_semantic_key
    )
    if missing_keys:
        placeholders = ",".join("?" for _ in missing_keys)
        rows = conn.execute(
            f"""
            SELECT cell.*,
                   seal.semantic_key_version AS sealed_semantic_key_version,
                   seal.semantic_key_sha256 AS sealed_semantic_key_sha256,
                   seal.semantic_identity_json AS sealed_semantic_identity_json,
                   seal.dimension_set_json AS sealed_dimension_set_json
            FROM fact_cell_identity_seals_v2 seal
            JOIN fact_cells_v2 cell ON cell.fact_cell_id=seal.fact_cell_id
            WHERE seal.semantic_key_sha256 IN ({placeholders})
            """,  # nosec B608 -- fixed placeholders only; semantic keys remain bound
            missing_keys,
        ).fetchall()
    else:
        rows = []
    if not rows and not existing_by_semantic_key:
        return facts
    cell_ids = tuple(str(row["fact_cell_id"]) for row in rows)
    if cell_ids:
        cell_placeholders = ",".join("?" for _ in cell_ids)
        dimension_rows = conn.execute(
            "SELECT * FROM fact_dimensions_normalized_v2 "
            f"WHERE fact_cell_id IN ({cell_placeholders}) "  # nosec B608 -- bound IDs
            "ORDER BY fact_cell_id,dimension_ordinal",
            cell_ids,
        ).fetchall()
    else:
        dimension_rows = []
    dimensions_by_cell: dict[str, list[sqlite3.Row]] = {cell_id: [] for cell_id in cell_ids}
    for dimension in dimension_rows:
        dimensions_by_cell[str(dimension["fact_cell_id"])].append(dimension)
    for row in rows:
        semantic_key = str(row["sealed_semantic_key_sha256"])
        validated = _ValidatedSemanticCell(
            cell=_existing_cell(
                row,
                dimensions_by_cell[str(row["fact_cell_id"])],
            ),
            sealed_semantic_identity_json=str(row["sealed_semantic_identity_json"]),
            sealed_dimension_set_json=str(row["sealed_dimension_set_json"]),
        )
        semantic_cell_cache.put(semantic_key, validated)
        existing_by_semantic_key[semantic_key] = validated
    resolved: list[ReportedSourceFact] = []
    for fact in facts:
        match = existing_by_semantic_key.get(str(fact.cell.semantic_key_sha256))
        if match is None:
            resolved.append(fact)
            continue
        existing = match.cell
        if (
            existing.semantic_identity_json != fact.cell.semantic_identity_json
            or existing.dimensions_json != fact.cell.dimensions_json
            or match.sealed_semantic_identity_json != fact.cell.semantic_identity_json
            or match.sealed_dimension_set_json != fact.cell.dimensions_json
        ):
            raise ValueError("existing semantic cell commitment conflicts with normalized fact")
        observation = fact.observation.model_copy(update={"fact_cell_id": existing.fact_cell_id})
        resolved.append(ReportedSourceFact(cell=existing, observation=observation))
    return tuple(resolved)


def _existing_cell(
    row: sqlite3.Row,
    dimension_rows: list[sqlite3.Row],
) -> FactCellV2:
    dimensions: list[FactDimensionV2] = []
    for dimension in dimension_rows:
        typed = (
            None
            if dimension["typed_member_value_json"] is None
            else CanonicalJSONObject(
                root=cast(
                    dict[str, JsonValue],
                    json.loads(str(dimension["typed_member_value_json"])),
                )
            )
        )
        dimensions.append(
            FactDimensionV2(
                dimension_id=str(dimension["dimension_id"]),
                idempotency_key=str(dimension["idempotency_key"]),
                axis_namespace=str(dimension["axis_namespace"]),
                axis_name=str(dimension["axis_name"]),
                member_kind=cast(
                    Literal["explicit", "typed"],
                    str(dimension["member_kind"]),
                ),
                explicit_member_namespace=dimension["explicit_member_namespace"],
                explicit_member_name=dimension["explicit_member_name"],
                typed_member_value=typed,
                recorded_at=_parse_required_datetime(dimension["recorded_at"]),
            )
        )
    return FactCellV2(
        fact_cell_id=str(row["fact_cell_id"]),
        idempotency_key=str(row["idempotency_key"]),
        reporting_entity_id=str(row["reporting_entity_id"]),
        scope_security_id=row["scope_security_id"],
        semantic_key_version=cast(
            Literal["fact_cell_semantic_key.v3"],
            str(row["sealed_semantic_key_version"]),
        ),
        semantic_key_sha256=str(row["sealed_semantic_key_sha256"]),
        concept_namespace=str(row["concept_namespace"]),
        concept_name=str(row["concept_name"]),
        taxonomy_name=str(row["taxonomy_name"]),
        taxonomy_version=row["taxonomy_version"],
        accounting_basis=cast(
            Literal[
                "us_gaap",
                "ifrs",
                "local_gaap",
                "management",
                "non_gaap",
                "other",
            ],
            str(row["accounting_basis"]),
        ),
        consolidation_scope=cast(
            Literal[
                "consolidated",
                "parent_only",
                "subsidiary",
                "segment",
                "other",
            ],
            str(row["consolidation_scope"]),
        ),
        period_kind=cast(
            Literal["instant", "duration"],
            str(row["period_kind"]),
        ),
        period_start=_parse_datetime(row["period_start"]),
        period_end=_parse_required_datetime(row["period_end"]),
        fiscal_year=None if row["fiscal_year"] is None else int(row["fiscal_year"]),
        fiscal_period=row["fiscal_period"],
        dimensions=tuple(dimensions),
        unit_key=str(row["unit_key"]),
        currency=row["currency"],
        effective_at=_parse_required_datetime(row["effective_at"]),
        knowledge_at=_parse_required_datetime(row["knowledge_at"]),
        recorded_at=_parse_required_datetime(row["recorded_at"]),
    )


def _planned_cell_id(row: sqlite3.Row) -> str:
    period_start = _parse_datetime(row["period_start"])
    period_end = _parse_datetime(row["period_end"])
    if period_end is None:
        raise ValueError("reported legacy observation is missing period_end")
    period_kind = (
        "duration" if period_start is not None and period_start < period_end else "instant"
    )
    if period_kind == "instant":
        period_start = None
    currency_value = row["currency"]
    currency = (
        str(currency_value).upper()
        if currency_value is not None
        and len(str(currency_value)) == 3
        and str(currency_value).isalpha()
        else None
    )
    fact_table = str(row["fact_table"])
    semantic_identity = {
        "accounting_basis": "management" if fact_table == "kpi_facts" else "other",
        "consolidation_scope": "other",
        "currency": currency,
        "dimensions": _dimension_members(str(row["dimensions_json"])),
        "period_end": period_end.isoformat(),
        "period_kind": period_kind,
        "period_start": None if period_start is None else period_start.isoformat(),
        "concept_name": str(row["concept_key"]),
        "concept_namespace": (
            "urn:earnings-summary:legacy:kpi"
            if fact_table == "kpi_facts"
            else "urn:earnings-summary:legacy:financial"
        ),
        "reporting_entity_id": str(row["reporting_entity_id"]),
        "scope_security_id": (None if row["security_id"] is None else str(row["security_id"])),
        "semantic_key_version": "fact_cell_semantic_key.v3",
        "taxonomy_name": "earnings-summary-legacy",
        "unit_key": str(row["unit"] or "unknown"),
    }
    return _record_id("fact-cell-v2", _canonical_json(semantic_identity))


def _v2_observation_id(legacy_observation_id: str) -> str:
    return _record_id("fact-observation-v2", legacy_observation_id)


def _dimensions(
    raw_json: str,
    recorded_at: object,
    *,
    fact_cell_id: str,
) -> tuple[FactDimensionV2, ...]:
    members = _dimension_members(raw_json)
    stamp = _parse_datetime(recorded_at)
    if stamp is None:
        raise ValueError("legacy dimension record is missing recorded_at")
    dimensions: list[FactDimensionV2] = []
    for index, member_payload in enumerate(members):
        axis = str(member_payload["axis_name"])
        member_namespace = str(member_payload["explicit_member_namespace"])
        member_name = str(member_payload["explicit_member_name"])
        identity = _canonical_json(
            {
                "axis": axis,
                "fact_cell_id": fact_cell_id,
                "index": index,
                "member_name": member_name,
                "member_namespace": member_namespace,
            }
        )
        dimension_id = _record_id("fact-dimension-v2", identity)
        dimensions.append(
            FactDimensionV2(
                dimension_id=dimension_id,
                idempotency_key=dimension_id,
                axis_namespace="urn:earnings-summary:legacy-dimension",
                axis_name=axis,
                member_kind="explicit",
                explicit_member_namespace=member_namespace,
                explicit_member_name=member_name,
                recorded_at=stamp,
            )
        )
    return tuple(dimensions)


def _dimension_members(raw_json: str) -> list[dict[str, JsonValue]]:
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError("legacy dimensions_json is invalid") from exc
    if not isinstance(parsed, list):
        raise ValueError("legacy dimensions_json must be an array")
    members: list[dict[str, JsonValue]] = []
    for index, raw_item in enumerate(cast(list[object], parsed)):
        if not isinstance(raw_item, dict):
            raise ValueError("legacy dimension must be an object")
        item = cast(dict[str, object], raw_item)
        axis = str(item.get("key") or f"dimension_{index}")
        member_namespace = (
            "urn:earnings-summary:legacy-member:" + hashlib.sha256(axis.encode()).hexdigest()
        )
        member_name = _canonical_json(item.get("value"))
        members.append(
            {
                "axis_name": axis,
                "axis_namespace": "urn:earnings-summary:legacy-dimension",
                "explicit_member_name": member_name,
                "explicit_member_namespace": member_namespace,
                "member_kind": "explicit",
                "typed_member_value": None,
            }
        )
    return sorted(
        members,
        key=lambda item: (
            str(item["axis_namespace"]),
            str(item["axis_name"]),
            str(item["member_kind"]),
            _canonical_json(item),
        ),
    )


def _locator_payload(row: sqlite3.Row) -> dict[str, JsonValue]:
    return {
        "bridge_policy": f"{_POLICY_NAME}@{_POLICY_VERSION}",
        "fact_revision": int(row["fact_revision"]),
        "fact_row_id": int(row["fact_row_id"]),
        "fact_table": str(row["fact_table"]),
        "legacy_locator": _parse_json_or_text(row["locator_json"]),
        "legacy_observation_id": str(row["observation_id"]),
        "node_locator": _parse_json_or_text(row["node_locator_json"]),
        "source_document_id": int(row["source_document_id"]),
    }


def _parse_json_or_text(value: object) -> JsonValue:
    if value is None:
        return None
    try:
        return cast(JsonValue, json.loads(str(value)))
    except json.JSONDecodeError:
        return {"raw_text": str(value)}


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _parse_required_datetime(value: object) -> datetime:
    parsed = _parse_datetime(value)
    if parsed is None:
        raise ValueError("required population clock is missing")
    return parsed


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return _utc(value).isoformat()


def verify_source_fact_ontology(
    conn: sqlite3.Connection,
    scope: PopulationTemporalScope,
) -> PopulationPlaneVerification:
    """Verify source facts and their ontology admission from persisted evidence."""

    knowledge = _db_time(scope.knowledge_cutoff)
    observed = _db_time(scope.observed_through)
    source_set = stream_population_artifact_set(
        conn,
        table="fact_observations_v2",
        query="""
            SELECT observation.observation_id AS artifact_id,
                   payload.observation_payload_sha256 AS payload_sha256,
                   fact_sha256(json_object(
                       'anchor_payload_sha256',anchor.anchor_payload_sha256,
                       'cell_semantic_key_sha256',cell_seal.semantic_key_sha256,
                       'observation_set_sha256',completeness.observation_set_sha256
                   )) AS seal_sha256,
                   observation.knowledge_at,
                   observation.recorded_at
            FROM fact_observations_v2 observation
            JOIN fact_cells_v2 cell
              ON cell.fact_cell_id=observation.fact_cell_id
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
              AND datetime(cell.knowledge_at)<=datetime(?)
              AND datetime(cell.recorded_at)<=datetime(?)
              AND datetime(cell_seal.sealed_at)<=datetime(?)
              AND datetime(anchor.recorded_at)<=datetime(?)
              AND datetime(payload.committed_at)<=datetime(?)
              AND datetime(completeness.knowledge_at)<=datetime(?)
              AND datetime(completeness.recorded_at)<=datetime(?)
            ORDER BY observation.observation_id
        """,
        params=(
            knowledge,
            observed,
            knowledge,
            observed,
            observed,
            observed,
            observed,
            knowledge,
            observed,
        ),
        selection_policy_id="source-reported-observations-as-of-k-o.v1",
    )
    assertion_set = stream_population_artifact_set(
        conn,
        table="source_observation_taxonomy_assertions",
        query="""
            SELECT assertion.observation_id AS artifact_id,
                   assertion.commitment_sha256 AS payload_sha256,
                   fact_sha256(json_object(
                       'anchor_payload_sha256',assertion.anchor_payload_sha256,
                       'extraction_output_sha256',assertion.extraction_output_sha256,
                       'observation_payload_sha256',
                           assertion.observation_payload_sha256,
                       'observation_set_sha256',assertion.observation_set_sha256
                   )) AS seal_sha256,
                   assertion.knowledge_at,
                   assertion.recorded_at
            FROM source_observation_taxonomy_assertions assertion
            JOIN fact_observations_v2 observation
              ON observation.observation_id=assertion.observation_id
            WHERE observation.observation_kind='reported'
              AND datetime(observation.knowledge_at)<=datetime(?)
              AND datetime(observation.recorded_at)<=datetime(?)
              AND datetime(assertion.knowledge_at)<=datetime(?)
              AND datetime(assertion.recorded_at)<=datetime(?)
            ORDER BY assertion.observation_id
        """,
        params=(knowledge, observed, knowledge, observed),
        selection_policy_id="source-taxonomy-assertions-as-of-k-o.v1",
    )
    binding_set = stream_population_artifact_set(
        conn,
        table="fact_cell_canonical_binding_revisions",
        query="""
            WITH ranked AS (
                SELECT binding.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY binding.source_observation_id
                           ORDER BY binding.revision DESC,binding.binding_revision_id DESC
                       ) AS scope_rank
                FROM fact_cell_canonical_binding_revisions binding
                JOIN fact_observations_v2 observation
                  ON observation.observation_id=binding.source_observation_id
                WHERE observation.observation_kind='reported'
                  AND datetime(observation.knowledge_at)<=datetime(?)
                  AND datetime(observation.recorded_at)<=datetime(?)
                  AND datetime(binding.knowledge_at)<=datetime(?)
                  AND datetime(binding.recorded_at)<=datetime(?)
            )
            SELECT binding_revision_id AS artifact_id,
                   commitment_sha256 AS payload_sha256,
                   commitment_sha256 AS seal_sha256,
                   knowledge_at,
                   recorded_at
            FROM ranked
            WHERE scope_rank=1 AND binding_status='bound'
            ORDER BY binding_revision_id
        """,
        params=(knowledge, observed, knowledge, observed),
        selection_policy_id="canonical-bindings-latest-as-of-k-o.v1",
    )
    snapshot_rows = conn.execute(
        "SELECT header.ontology_snapshot_id "
        "FROM ontology_snapshot_headers header "
        "JOIN ontology_snapshot_seals seal "
        "ON seal.ontology_snapshot_id=header.ontology_snapshot_id "
        "WHERE datetime(header.cutoff_at)=datetime(?) "
        "AND datetime(header.recorded_at)<=datetime(?) "
        "AND datetime(seal.sealed_at)<=datetime(?) "
        "ORDER BY datetime(header.recorded_at) DESC,header.ontology_snapshot_id DESC",
        (knowledge, observed, observed),
    ).fetchall()
    if len(snapshot_rows) != 1:
        raise ValueError("ontology snapshot scope must resolve to exactly one sealed snapshot")
    snapshot_id = str(snapshot_rows[0][0])
    snapshot_set = stream_population_artifact_set(
        conn,
        table="ontology_snapshot_headers",
        query="""
            SELECT header.ontology_snapshot_id AS artifact_id,
                   seal.member_set_sha256 AS payload_sha256,
                   fact_sha256(json_object(
                       'member_count',seal.member_count,
                       'member_set_sha256',seal.member_set_sha256,
                       'ontology_snapshot_id',header.ontology_snapshot_id
                   )) AS seal_sha256,
                   header.cutoff_at AS knowledge_at,
                   header.recorded_at
            FROM ontology_snapshot_headers header
            JOIN ontology_snapshot_seals seal
              ON seal.ontology_snapshot_id=header.ontology_snapshot_id
            WHERE header.ontology_snapshot_id=?
              AND datetime(header.cutoff_at)=datetime(?)
              AND datetime(header.recorded_at)<=datetime(?)
              AND datetime(seal.sealed_at)<=datetime(?)
            ORDER BY header.ontology_snapshot_id
        """,
        params=(snapshot_id, knowledge, observed, observed),
        selection_policy_id="ontology-snapshot-exact-k-as-of-o.v1",
    )
    expected = source_set.row_count
    if expected <= 0:
        raise ValueError("source-fact ontology scope is empty")
    fully_materialized = int(
        conn.execute(
            """
            WITH latest_binding AS (
                SELECT binding.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY binding.source_observation_id
                           ORDER BY binding.revision DESC,binding.binding_revision_id DESC
                       ) AS scope_rank
                FROM fact_cell_canonical_binding_revisions binding
                WHERE datetime(binding.knowledge_at)<=datetime(?)
                  AND datetime(binding.recorded_at)<=datetime(?)
            )
            SELECT COUNT(*)
            FROM fact_observations_v2 observation
            JOIN source_observation_taxonomy_assertions assertion
              ON assertion.observation_id=observation.observation_id
            JOIN latest_binding binding
              ON binding.source_observation_id=observation.observation_id
             AND binding.scope_rank=1
             AND binding.binding_status='bound'
            WHERE observation.observation_kind='reported'
              AND datetime(observation.knowledge_at)<=datetime(?)
              AND datetime(observation.recorded_at)<=datetime(?)
              AND datetime(assertion.knowledge_at)<=datetime(?)
              AND datetime(assertion.recorded_at)<=datetime(?)
            """,
            (knowledge, observed, knowledge, observed, knowledge, observed),
        ).fetchone()[0]
    )
    if assertion_set.row_count > expected or binding_set.row_count > expected:
        raise ValueError("ontology admission contains out-of-scope artifacts")
    artifact_sets = tuple(
        sorted(
            (binding_set, source_set, snapshot_set, assertion_set),
            key=lambda item: (item.table, item.selection_policy_id),
        )
    )
    details: dict[str, JsonValue] = {
        "assertion_count": assertion_set.row_count,
        "binding_count": binding_set.row_count,
        "ontology_snapshot_id": snapshot_id,
        "source_observation_count": expected,
        "temporal_policy": "knowledge_at<=K;recorded_at<=O;post-O-ignored",
    }
    input_material: dict[str, JsonValue] = {
        "knowledge_cutoff": knowledge,
        "observed_through": observed,
        "source_artifact_set": cast(JsonValue, source_set.model_dump(mode="json")),
    }
    output_material = {
        "artifact_sets": [item.model_dump(mode="json") for item in artifact_sets],
        "details": details,
        "exclusion_counts": {},
        "expected_count": expected,
        "failed_count": expected - fully_materialized,
        "materialized_count": fully_materialized,
        "plane_name": "source_fact_ontology",
    }
    return PopulationPlaneVerification(
        plane_name="source_fact_ontology",
        expected_count=expected,
        materialized_count=fully_materialized,
        excluded_count=0,
        failed_count=expected - fully_materialized,
        exclusion_counts={},
        input_commitment_sha256=digest_text(canonical_population_json(input_material)),
        output_commitment_sha256=digest_text(canonical_population_json(output_material)),
        artifact_sets=artifact_sets,
        details=details,
    )
