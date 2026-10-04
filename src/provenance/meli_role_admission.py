"""Reviewed MELI economic roles on existing admitted reported cells.

This boundary appends definition revisions only. It never publishes facts,
repairs admission, seals a snapshot, supplies forecasts or qualifies a model.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime
from typing import Literal, Self, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from dcf import input_evidence, meli_inputs
from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.fact_read_model import FactReadModel
from provenance.metric_ontology import (
    CanonicalDimension,
    CanonicalMetricDefinitionRevision,
    MetricOntology,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RoleEvidence(_Frozen):
    node_id: str = Field(min_length=1)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RoleAssignment(_Frozen):
    key: str
    fact: input_evidence.FactBinding
    rationale: str = Field(min_length=20)
    evidence: tuple[RoleEvidence, ...] = Field(min_length=1)

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, value: str) -> str:
        if len(value.strip()) < 20:
            raise ValueError("role_review_rationale_required")
        return value


class RoleAdmissionRequest(_Frozen):
    issuer_id: str = Field(min_length=1)
    research_snapshot_id: str = Field(min_length=1)
    financial_period_end: date
    as_of: AwareDatetime
    assignments: tuple[RoleAssignment, ...]

    @model_validator(mode="after")
    def _population(self) -> Self:
        required = {item.key for item in meli_inputs.requirements_for(self.financial_period_end)}
        keys = [item.key for item in self.assignments]
        if len(keys) != len(required) or set(keys) != required:
            raise ValueError("required_role_population_mismatch")
        return self


class RoleDefinitionPlan(_Frozen):
    parent: CanonicalMetricDefinitionRevision
    # Preserve full source constraints; roles are reviewed analyst interpretations.
    scope_constraints: dict[str, object]

    def reviewed_successor(
        self, *, review_sha256: str, plan_sha256: str, applied_at: datetime
    ) -> CanonicalMetricDefinitionRevision:
        """Prepare a revision; this pure operation performs no admission or write."""
        if applied_at.tzinfo is None or applied_at.utcoffset() is None:
            raise ValueError("role_apply_timezone_required")
        applied_at = applied_at.astimezone(UTC)
        identity = "meli-role-definition:" + input_evidence.canonical_digest(
            {"review": review_sha256, "metric": self.parent.metric_id}
        )
        return CanonicalMetricDefinitionRevision.model_validate(
            self.parent.model_copy(
                update={
                    "metric_definition_revision_id": identity,
                    "idempotency_key": identity,
                    "revision": self.parent.revision + 1,
                    "supersedes_metric_definition_revision_id": (
                        self.parent.metric_definition_revision_id
                    ),
                    "scope_constraints": {
                        **self.scope_constraints,
                        "meli_role_review": {
                            "review_sha256": review_sha256,
                            "plan_sha256": plan_sha256,
                            "applied_at": applied_at.isoformat(),
                            "attribution": "reviewed_analyst_role_interpretation",
                        },
                    },
                    "knowledge_at": applied_at,
                    "recorded_at": applied_at,
                }
            ).model_dump(mode="json")
        )


class RoleAdmissionPlan(_Frozen):
    schema_version: Literal["meli-role-admission-plan/v1"] = "meli-role-admission-plan/v1"
    request: RoleAdmissionRequest
    state: Literal["planned", "blocked"]
    model_ready: Literal[False] = False
    blockers: tuple[str, ...]
    source_commitment_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    definitions: tuple[RoleDefinitionPlan, ...] = ()

    @property
    def commitment_sha256(self) -> str:
        return input_evidence.canonical_digest(self.model_dump(mode="json"))


class ReviewedRoleAdmission(_Frozen):
    plan: RoleAdmissionPlan
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    decision: Literal["approved"]

    @model_validator(mode="after")
    def _review(self) -> Self:
        if (
            self.plan.state != "planned"
            or self.plan.blockers
            or not self.plan.definitions
            or self.plan.source_commitment_sha256 is None
            or self.plan_sha256 != self.plan.commitment_sha256
        ):
            raise ValueError("review_requires_exact_planned_population")
        if self.reviewed_at < self.plan.request.as_of:
            raise ValueError("review_precedes_plan_cutoff")
        if not self.reviewer.strip():
            raise ValueError("role_reviewer_required")
        return self


class RoleAdmissionReceipt(_Frozen):
    schema_version: Literal["meli-role-admission-receipt/v1"] = "meli-role-admission-receipt/v1"
    review_sha256: str
    applied_at: AwareDatetime
    definition_ids: tuple[str, ...]
    definition_commitments: tuple[str, ...]
    inserted_revisions: int
    model_ready: Literal[False] = False
    next_step: Literal["new_ontology_and_research_snapshot_required"] = (
        "new_ontology_and_research_snapshot_required"
    )


def _source_plan(
    conn: sqlite3.Connection,
    request: RoleAdmissionRequest,
    cutoff: datetime,
    *,
    expected_parents: dict[str, CanonicalMetricDefinitionRevision] | None = None,
) -> tuple[str, tuple[RoleDefinitionPlan, ...]]:
    probe = input_evidence.ModelInputRequest(
        recipe=meli_inputs.RECIPE,
        ticker="MELI",
        research_snapshot_id=request.research_snapshot_id,
        financial_period_end=request.financial_period_end,
        facts={},
        assumptions={},
    )
    snapshot, snapshot_sha, inventory_ids = input_evidence.verify_source_coverage(
        conn, probe, cutoff
    )
    if snapshot.research_universe.issuer_id != request.issuer_id:
        raise ValueError("role_issuer_mismatch")
    reader, ontology, resolver = (
        FactReadModel(conn),
        MetricOntology(conn),
        CanonicalFactResolutionEngine(conn),
    )
    requirements = {
        item.key: item for item in meli_inputs.requirements_for(request.financial_period_end)
    }
    parents: dict[str, CanonicalMetricDefinitionRevision] = {}
    scopes: dict[str, dict[str, object]] = {}
    dimensions_by_key: dict[str, tuple[CanonicalDimension, ...]] = {}
    commitments: list[object] = []
    for assignment in sorted(request.assignments, key=lambda item: item.key):
        ref, requirement = assignment.fact, requirements[assignment.key]
        definition = (
            expected_parents.get(ref.metric_id)
            if expected_parents is not None
            else ontology.metric_definition_as_known(ref.metric_id, cutoff)
        )
        resolution = resolver.as_known(ref.canonical_metric_cell_id, cutoff)
        binding = ontology.binding_as_known(ref.observation_id, cutoff)
        bundle = reader.provenance_bundle(ref.observation_id, cutoff=cutoff)
        fact, source = bundle.observation, bundle.evidence
        member = conn.execute(
            "SELECT canonical_resolution_revision_id FROM canonical_fact_resolution_snapshot_members "
            "WHERE resolution_snapshot_id=? AND canonical_metric_cell_id=?",
            (snapshot.canonical_fact_resolution_snapshot_id, ref.canonical_metric_cell_id),
        ).fetchone()
        cell = conn.execute(
            "SELECT metric_id,reporting_entity_id,scope_security_id,accounting_basis,"
            "consolidation_scope,dimension_count,dimension_set_json,dimension_set_sha256,unit_family "
            "FROM canonical_metric_cells JOIN canonical_metric_cell_seals "
            "USING(canonical_metric_cell_id) WHERE canonical_metric_cell_id=?",
            (ref.canonical_metric_cell_id,),
        ).fetchone()
        dimensions = tuple(
            CanonicalDimension(axis_id=str(row[0]), member_id=str(row[1]))
            for row in conn.execute(
                "SELECT axis_id,member_id FROM canonical_metric_cell_dimensions "
                "WHERE canonical_metric_cell_id=? ORDER BY dimension_ordinal",
                (ref.canonical_metric_cell_id,),
            )
        )
        dimension_json = input_evidence.canonical_json([item.model_dump() for item in dimensions])
        dimensions_by_key[assignment.key] = dimensions
        if (
            definition is None
            or definition.lifecycle != "active"
            or definition.value_kind != "numeric"
            or definition.metric_definition_revision_id != ref.metric_definition_revision_id
            or resolution is None
            or resolution.status != "resolved"
            or resolution.selected_observation_id != ref.observation_id
            or resolution.canonical_resolution_revision_id != ref.canonical_resolution_revision_id
            or member is None
            or str(member[0]) != ref.canonical_resolution_revision_id
            or binding is None
            or binding.binding_status != "bound"
            or binding.canonical_metric_cell_id != ref.canonical_metric_cell_id
            or cell is None
            or str(cell[0]) != ref.metric_id
            or str(cell[1]) != bundle.cell.reporting_entity_id
            or cell[2] is not None
            or str(cell[3]) != definition.accounting_basis
            or str(cell[3]) != bundle.cell.accounting_basis
            or str(cell[4]) != requirement.consolidation_scope
            or int(cell[5]) != len(dimensions)
            or str(cell[6]) != dimension_json
            or str(cell[7]) != hashlib.sha256(dimension_json.encode()).hexdigest()
            or str(cell[8]) != definition.unit_family
            or bundle.observation_payload_sha256 != ref.observation_payload_sha256
            or definition.period_kind != requirement.period_kind
        ):
            raise ValueError(f"role_current_admission_mismatch:{assignment.key}")
        if (
            source is None
            or source.canonical_issuer_id != snapshot.research_universe.issuer_id
            or source.document_version_id not in snapshot.research_universe.document_version_ids
            or bundle.cell.reporting_entity_id
            not in snapshot.research_universe.reporting_entity_ids
            or bundle.cell.scope_security_id is not None
            or bundle.cell.consolidation_scope != requirement.consolidation_scope
            or fact.observation_kind != "reported"
            or fact.decimal_value is None
            or fact.unit_key != requirement.unit_key
            or fact.currency != requirement.currency
            or fact.period_kind != requirement.period_kind
            or fact.period_end.date() != (requirement.period_end or request.financial_period_end)
            or fact.period_end > cutoff
            or (
                requirement.period_start is not None
                and (
                    fact.period_start is None
                    or fact.period_start.date() != requirement.period_start
                )
            )
            or (
                requirement.accounting_basis is not None
                and definition.accounting_basis != requirement.accounting_basis
            )
        ):
            raise ValueError(f"role_source_coordinate_mismatch:{assignment.key}")
        # Refuse new assertions that the frozen canonical snapshot did not
        # resolve. The resolver owns this graph; this adapter adds no policy.
        candidates = resolver.candidate_manifest(
            ref.canonical_metric_cell_id, cutoff, observed_through=cutoff
        )
        frozen_candidates = resolver.candidate_manifest(
            ref.canonical_metric_cell_id,
            snapshot.cutoff_at,
            observed_through=snapshot.recorded_at,
        )
        if candidates != frozen_candidates:
            raise ValueError(f"role_candidate_graph_changed:{assignment.key}")
        mapping = ontology.mapping_as_known(str(binding.source_component_id), cutoff)
        if mapping is None or mapping.mapping_revision_id != binding.mapping_revision_id:
            raise ValueError(f"role_current_mapping_mismatch:{assignment.key}")
        for evidence in assignment.evidence:
            row = conn.execute(
                "SELECT text,locator_sha256,recorded_at FROM evidence_nodes "
                "WHERE node_id=? AND extraction_run_id=?",
                (evidence.node_id, source.extraction_run_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"role_evidence_changed:{assignment.key}")
            recorded_at = datetime.fromisoformat(str(row[2]))
            if (
                recorded_at.utcoffset() is None
                or recorded_at.astimezone(UTC) > cutoff
                or hashlib.sha256(str(row[0]).encode()).hexdigest() != evidence.text_sha256
                or str(row[1]) != evidence.locator_sha256
            ):
                raise ValueError(f"role_evidence_changed:{assignment.key}")
        parents[ref.metric_id] = definition
        scope = scopes.setdefault(ref.metric_id, dict(definition.scope_constraints))
        legacy = scope.pop("valuation_role", None)
        if legacy is not None and legacy != requirement.role:
            raise ValueError(f"role_scope_conflict:{assignment.key}")
        raw = scope.get("valuation_role_selectors", {})
        if not isinstance(raw, dict):
            raise ValueError(f"role_scope_conflict:{assignment.key}")
        selectors = dict(cast(dict[str, object], raw))
        selector = input_evidence.ValuationRoleSelector(
            canonical_dimensions=dimensions,
            semantic_constraints=requirement.definition_constraints,
        ).model_dump(mode="json")
        if requirement.role in selectors and selectors[requirement.role] != selector:
            raise ValueError(f"role_scope_conflict:{assignment.key}")
        selectors[requirement.role] = selector
        scope["valuation_role_selectors"] = selectors
        commitments.append(
            {
                "key": assignment.key,
                "bundle": bundle.model_dump(mode="json"),
                "binding": binding.model_dump(mode="json"),
                "mapping": mapping.model_dump(mode="json"),
                "cell": list(cell),
                "dimensions": [item.model_dump() for item in dimensions],
                "candidate_manifest": [item.model_dump(mode="json") for item in candidates],
            }
        )
    for assignment in request.assignments:
        requirement = requirements[assignment.key]
        scope = scopes[assignment.fact.metric_id]
        if not input_evidence.input_role_matches(
            scope, requirement, dimensions_by_key[assignment.key]
        ):
            raise ValueError(f"role_selector_conflict:{assignment.key}")
    return (
        input_evidence.canonical_digest(
            {"snapshot": snapshot_sha, "inventories": inventory_ids, "inputs": commitments}
        ),
        tuple(
            RoleDefinitionPlan(parent=parents[key], scope_constraints=scopes[key])
            for key in sorted(parents)
        ),
    )


def plan_meli_role_admission(
    conn: sqlite3.Connection, request: RoleAdmissionRequest
) -> RoleAdmissionPlan:
    """Return a complete immutable candidate or blocked receipt without writes."""
    if not conn.in_transaction:
        raise ValueError("role_plan_requires_caller_read_transaction")
    request = RoleAdmissionRequest.model_validate(request.model_dump(mode="json"))
    try:
        source_sha, definitions = _source_plan(conn, request, request.as_of)
    except (ValueError, RuntimeError) as exc:
        return RoleAdmissionPlan(request=request, state="blocked", blockers=(str(exc),))
    return RoleAdmissionPlan(
        request=request,
        state="planned",
        blockers=(),
        source_commitment_sha256=source_sha,
        definitions=definitions,
    )


def apply_reviewed_meli_role_admission(
    conn: sqlite3.Connection,
    review: ReviewedRoleAdmission,
    *,
    as_of: datetime,
) -> RoleAdmissionReceipt:
    """Recheck the whole reviewed batch and append atomically; caller commits."""
    if not conn.in_transaction:
        raise ValueError("role_apply_requires_caller_write_transaction")
    review = ReviewedRoleAdmission.model_validate(review.model_dump(mode="json"))
    if as_of.tzinfo is None or as_of.utcoffset() is None or as_of < review.reviewed_at:
        raise ValueError("role_apply_cutoff_precedes_review")
    review_sha = input_evidence.canonical_digest(review.model_dump(mode="json"))
    ontology = MetricOntology(conn)
    definitions: list[CanonicalMetricDefinitionRevision] = []
    own: list[bool] = []
    for item in review.plan.definitions:
        parent = item.parent
        current = ontology.metric_definition_as_known(parent.metric_id, as_of)
        marker = None if current is None else current.scope_constraints.get("meli_role_review")
        applied_at = as_of.astimezone(UTC)
        if isinstance(marker, dict) and marker.get("review_sha256") == review_sha:
            marker = cast(dict[str, object], marker)
            applied_at = datetime.fromisoformat(str(marker["applied_at"]))
            if (
                applied_at.tzinfo is None
                or applied_at.utcoffset() is None
                or not review.reviewed_at <= applied_at <= as_of
            ):
                raise ValueError("role_replay_clock_conflict")
        successor = item.reviewed_successor(
            review_sha256=review_sha,
            plan_sha256=review.plan_sha256,
            applied_at=applied_at,
        )
        replay = current == successor
        if not replay and current != parent:
            raise ValueError("role_definition_head_changed")
        definitions.append(successor)
        own.append(replay)
    if any(own) and not all(own):
        raise ValueError("role_partial_replay_refused")
    source_sha, planned = _source_plan(
        conn,
        review.plan.request,
        as_of,
        expected_parents={item.parent.metric_id: item.parent for item in review.plan.definitions},
    )
    if source_sha != review.plan.source_commitment_sha256 or planned != review.plan.definitions:
        raise ValueError("role_reviewed_source_or_plan_changed")
    if len({item.recorded_at for item in definitions}) != 1:
        raise ValueError("role_replay_clock_conflict")
    conn.execute("SAVEPOINT meli_role_admission")
    try:
        if not all(own):
            for definition in definitions:
                ontology.persist_metric_definition(definition)
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT meli_role_admission")
        conn.execute("RELEASE SAVEPOINT meli_role_admission")
        raise
    conn.execute("RELEASE SAVEPOINT meli_role_admission")
    return RoleAdmissionReceipt(
        review_sha256=review_sha,
        applied_at=definitions[0].recorded_at,
        definition_ids=tuple(item.metric_definition_revision_id for item in definitions),
        definition_commitments=tuple(
            input_evidence.canonical_digest(item.model_dump(mode="json")) for item in definitions
        ),
        inserted_revisions=0 if all(own) else len(definitions),
    )
