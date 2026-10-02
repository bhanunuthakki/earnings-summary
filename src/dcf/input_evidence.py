"""Run-bound valuation inputs composed from existing sealed evidence authorities.

No fact writes, discovery, extraction, admission or automatic recovery lives here.
A model recipe declares the required population; callers cannot shorten it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.fact_read_model import FactReadModel
from provenance.metric_ontology import CanonicalDimension, MetricOntology, canonical_json
from provenance.research_snapshot import ResearchSnapshotRequest, verify_research_snapshot


class InputEvidenceError(ValueError):
    """A required model input or its coverage proof is unavailable or invalid."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class FactBinding(FrozenModel):
    canonical_metric_cell_id: str
    metric_id: str
    metric_definition_revision_id: str
    canonical_resolution_revision_id: str
    observation_id: str
    observation_payload_sha256: str


class AssumptionBasis(FrozenModel):
    value: float
    attribution: Literal["owner", "analyst"]
    rationale: str = Field(min_length=10)


class DriverReview(FrozenModel):
    observed: float
    forecast: float
    variance: float
    rationale: str = Field(min_length=20)


class AssumptionReview(FrozenModel):
    reviewed_at: AwareDatetime
    reviewer: str = Field(min_length=1)
    effective_inputs_sha256: str
    actuals_sha256: str
    drivers: dict[str, DriverReview]


class ModelCalculation(FrozenModel):
    key: str
    formula: str
    operands: tuple[str, ...]
    value: float


class ModelInputRequest(FrozenModel):
    recipe: str
    ticker: Literal["MELI"]
    research_snapshot_id: str
    financial_period_end: date
    # Canonical bindings are REPORTED only. TTM/residuals are model calculations.
    facts: dict[str, FactBinding]
    assumptions: dict[str, AssumptionBasis]
    assumption_review: AssumptionReview | None = None


class InputRequirement(FrozenModel):
    key: str
    role: str
    unit_key: str
    currency: str | None
    period_kind: Literal["instant", "duration"]
    consolidation_scope: Literal["consolidated"] = "consolidated"
    annual: bool = False
    accounting_basis: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    scale: Decimal = Decimal(1)
    definition_constraints: dict[str, object] = Field(default_factory=dict)


class ValuationRoleSelector(FrozenModel):
    """Reviewed role selection against the complete canonical dimension set."""

    canonical_dimensions: tuple[CanonicalDimension, ...]
    semantic_constraints: dict[str, object]

    @field_validator("canonical_dimensions")
    @classmethod
    def _unique_axes(
        cls, dimensions: tuple[CanonicalDimension, ...]
    ) -> tuple[CanonicalDimension, ...]:
        if len({item.axis_id for item in dimensions}) != len(dimensions):
            raise ValueError("valuation role selector has duplicate axes")
        return dimensions


_ROLE_SELECTORS = TypeAdapter(dict[str, ValuationRoleSelector])


def _role_matches(
    scope: dict[str, object],
    requirement: InputRequirement,
    dimensions: tuple[CanonicalDimension, ...],
) -> bool:
    if "valuation_role_selectors" in scope:
        if "valuation_role" in scope:
            return False
        try:
            selectors = _ROLE_SELECTORS.validate_python(scope["valuation_role_selectors"])
        except ValidationError:
            return False
        dimension_sets = [
            frozenset((item.axis_id, item.member_id) for item in selector.canonical_dimensions)
            for selector in selectors.values()
        ]
        if not selectors or len(set(dimension_sets)) != len(dimension_sets):
            return False
        selected = selectors.get(requirement.role)
        if selected is None or selected.semantic_constraints != requirement.definition_constraints:
            return False
        if any(
            key in scope and scope[key] != value
            for key, value in selected.semantic_constraints.items()
        ):
            return False
        return {(item.axis_id, item.member_id) for item in selected.canonical_dimensions} == {
            (item.axis_id, item.member_id) for item in dimensions
        }
    # The scalar legacy contract is safe only for a single undimensioned role.
    return (
        not dimensions
        and scope.get("valuation_role") == requirement.role
        and all(
            scope.get(key) == value for key, value in requirement.definition_constraints.items()
        )
    )


class VerifiedInput(FrozenModel):
    key: str
    value: float
    reference: FactBinding
    period_start: date | None
    period_end: date
    document_version_id: str | None
    reporting_entity_id: str
    unit_key: str
    currency: str | None
    observation_kind: str
    knowledge_at: datetime
    recorded_at: datetime
    document_recorded_at: datetime | None = None
    extraction_completed_at: datetime | None = None


class ModelInputReceipt(FrozenModel):
    schema_version: Literal["dcf_model_inputs.v2"] = "dcf_model_inputs.v2"
    recipe: str
    request: ModelInputRequest
    verified_at: AwareDatetime
    snapshot_member_sha256: str
    effective_inputs_sha256: str
    required_keys: tuple[str, ...]
    inputs: tuple[VerifiedInput, ...]
    inventory_snapshot_ids: tuple[str, ...]
    calculations: tuple[ModelCalculation, ...] = ()
    actuals_sha256: str | None = None
    model_output_sha256: str | None = None
    assumptions_source_path: str | None = None
    assumptions_source_sha256: str | None = None
    coverage_policy: Literal["current-authoritative-inventory-24h/v1"] = (
        "current-authoritative-inventory-24h/v1"
    )


def canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _time(raw: object) -> datetime:
    return _utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))


def verify_source_coverage(
    conn: sqlite3.Connection, request: ModelInputRequest, cutoff: datetime
) -> tuple[ResearchSnapshotRequest, str, tuple[str, ...]]:
    """Verify an existing sealed snapshot and its current inventory membership.

    Twenty-four hours is a bounded *inventory* observation policy, not a claim
    about quarterly fact age. A newer inventory invalidates the old proof even
    when the old inventory is within that window. No FMP cache participates.
    """
    admission = verify_research_snapshot(conn, request.research_snapshot_id)
    row = conn.execute(
        "SELECT request_json FROM research_snapshot_headers WHERE research_snapshot_id=?",
        (request.research_snapshot_id,),
    ).fetchone()
    if row is None:
        raise InputEvidenceError("research_snapshot_missing")
    snapshot = ResearchSnapshotRequest.model_validate_json(str(row[0]))
    if _utc(snapshot.recorded_at) > cutoff or _utc(snapshot.cutoff_at) > cutoff:
        raise InputEvidenceError("research_snapshot_after_cutoff")
    inventories: dict[str, tuple[str, datetime]] = {}
    for corpus in snapshot.corpus_bundles:
        rows = conn.execute(
            """SELECT inventory.snapshot_id,inventory.inventory_key,
            inventory.issuer_id,inventory.ticker,inventory.source_kind,inventory.outcome,
            inventory.authoritative,inventory.completed_at,inventory.recorded_at
            FROM search_manifest_source_inventories member
            JOIN source_inventory_snapshots inventory ON inventory.snapshot_id=member.snapshot_id
            WHERE member.manifest_id=?""",
            (corpus.corpus_manifest_id,),
        ).fetchall()
        for item in rows:
            if (
                str(item[2]) != snapshot.research_universe.issuer_id
                or str(item[3]).upper() != request.ticker
                or str(item[5]) != "succeeded"
                or not bool(item[6])
                or _time(item[8]) > cutoff
            ):
                raise InputEvidenceError("source_inventory_not_authoritative_complete")
            completed = _time(item[7])
            if not timedelta(0) <= cutoff - completed <= timedelta(hours=24):
                raise InputEvidenceError("source_inventory_stale_or_future")
            latest = conn.execute(
                """SELECT snapshot_id FROM source_inventory_snapshots
                WHERE inventory_key=? AND datetime(recorded_at)<=datetime(?)
                ORDER BY revision DESC LIMIT 1""",
                (str(item[1]), cutoff.isoformat()),
            ).fetchone()
            if latest is None or str(latest[0]) != str(item[0]):
                raise InputEvidenceError("source_inventory_superseded")
            inventories[str(item[0])] = (str(item[4]), completed)
    if not any(kind == "sec_submissions" for kind, _ in inventories.values()):
        raise InputEvidenceError("current_sec_inventory_missing")
    # Expected periodic documents, rather than today's calendar quarter, name
    # the latest financial period. Snapshot verification proves document closure.
    latest_period: date | None = None
    for inventory_id in inventories:
        for row in conn.execute(
            """SELECT period_end FROM expected_documents
                WHERE snapshot_id=? AND form_type IN ('10-K','10-K/A','10-Q','10-Q/A')
                AND period_end IS NOT NULL AND datetime(recorded_at)<=datetime(?)""",
            (inventory_id, cutoff.isoformat()),
        ):
            end = _time(row[0]).date()
            latest_period = max(latest_period, end) if latest_period is not None else end
    if latest_period != request.financial_period_end:
        raise InputEvidenceError("financial_anchor_not_latest_published_period")
    return snapshot, admission.member_set_sha256, tuple(sorted(inventories))


def verify_model_inputs(
    conn: sqlite3.Connection,
    request: ModelInputRequest,
    *,
    recipe: str,
    requirements: tuple[InputRequirement, ...],
    effective_inputs: Mapping[str, float],
    assumption_keys: frozenset[str],
    as_of: datetime,
) -> ModelInputReceipt:
    """Reconstruct every required actual through the canonical sealed read APIs."""
    if as_of.tzinfo is None:
        raise InputEvidenceError("timezone_required")
    cutoff = as_of.astimezone(UTC)
    if request.recipe != recipe:
        raise InputEvidenceError("input_recipe_mismatch")
    required = {item.key for item in requirements}
    if set(request.facts) != required:
        raise InputEvidenceError("required_input_population_mismatch")
    if frozenset(request.assumptions) != assumption_keys:
        raise InputEvidenceError("assumption_population_mismatch")
    for key, assumption in request.assumptions.items():
        if effective_inputs.get(key) != assumption.value:
            raise InputEvidenceError(f"assumption_value_mismatch:{key}")
    snapshot, member_sha, inventories = verify_source_coverage(conn, request, cutoff)
    reader, resolver, ontology = (
        FactReadModel(conn),
        CanonicalFactResolutionEngine(conn),
        MetricOntology(conn),
    )
    inputs: list[VerifiedInput] = []
    for requirement in requirements:
        ref = request.facts[requirement.key]
        resolution = resolver.as_known(ref.canonical_metric_cell_id, cutoff)
        definition = ontology.metric_definition_as_known(ref.metric_id, cutoff)
        binding = ontology.binding_as_known(ref.observation_id, cutoff)
        bundle = reader.provenance_bundle(ref.observation_id, cutoff=cutoff)
        fact = bundle.observation
        identity = conn.execute(
            "SELECT cell.metric_id,cell.dimension_count,seal.dimension_set_json,"
            "seal.dimension_set_sha256,cell.consolidation_scope,cell.scope_security_id "
            "FROM canonical_metric_cells cell "
            "JOIN canonical_metric_cell_seals seal USING(canonical_metric_cell_id) "
            "WHERE cell.canonical_metric_cell_id=?",
            (ref.canonical_metric_cell_id,),
        ).fetchone()
        dimension_rows = conn.execute(
            "SELECT axis_id,member_id FROM canonical_metric_cell_dimensions "
            "WHERE canonical_metric_cell_id=? ORDER BY dimension_ordinal",
            (ref.canonical_metric_cell_id,),
        ).fetchall()
        dimensions = tuple(
            CanonicalDimension(axis_id=str(row[0]), member_id=str(row[1])) for row in dimension_rows
        )
        dimension_json = canonical_json([item.model_dump() for item in dimensions])
        member = conn.execute(
            "SELECT canonical_resolution_revision_id FROM canonical_fact_resolution_snapshot_members "
            "WHERE resolution_snapshot_id=? AND canonical_metric_cell_id=?",
            (snapshot.canonical_fact_resolution_snapshot_id, ref.canonical_metric_cell_id),
        ).fetchone()
        if (
            identity is None
            or str(identity[0]) != ref.metric_id
            or int(identity[1]) != len(dimensions)
            or str(identity[2]) != dimension_json
            or str(identity[3]) != hashlib.sha256(dimension_json.encode()).hexdigest()
            or str(identity[4]) != requirement.consolidation_scope
            or identity[5] is not None
            or len({item.axis_id for item in dimensions}) != len(dimensions)
            or member is None
            or str(member[0]) != ref.canonical_resolution_revision_id
        ):
            raise InputEvidenceError(f"input_outside_canonical_snapshot:{requirement.key}")
        if (
            resolution is None
            or resolution.status != "resolved"
            or resolution.selected_observation_id != ref.observation_id
            or resolution.canonical_resolution_revision_id != ref.canonical_resolution_revision_id
            or binding is None
            or binding.binding_status != "bound"
            or binding.canonical_metric_cell_id != ref.canonical_metric_cell_id
            or definition is None
            or definition.lifecycle != "active"
            or definition.metric_definition_revision_id != ref.metric_definition_revision_id
            or not _role_matches(definition.scope_constraints, requirement, dimensions)
            or (
                requirement.accounting_basis is not None
                and (
                    definition.accounting_basis != requirement.accounting_basis
                    or bundle.cell.accounting_basis != requirement.accounting_basis
                )
            )
            or bundle.observation_payload_sha256 != ref.observation_payload_sha256
            or bundle.cell.consolidation_scope != requirement.consolidation_scope
            or bundle.cell.scope_security_id is not None
        ):
            raise InputEvidenceError(f"input_semantic_admission_failed:{requirement.key}")
        if (
            bundle.cell.reporting_entity_id not in snapshot.research_universe.reporting_entity_ids
            or fact.decimal_value is None
            or fact.unit_key != requirement.unit_key
            or fact.currency != requirement.currency
            or fact.period_kind != requirement.period_kind
            or fact.observation_kind != "reported"
            or fact.period_end.date() != (requirement.period_end or request.financial_period_end)
            or (
                requirement.period_start is not None
                and (
                    fact.period_start is None
                    or fact.period_start.date() != requirement.period_start
                )
            )
            or fact.period_end > cutoff
        ):
            raise InputEvidenceError(f"input_coordinate_mismatch:{requirement.key}")
        if (
            bundle.evidence is None
            or bundle.evidence.canonical_issuer_id != snapshot.research_universe.issuer_id
            or bundle.evidence.document_version_id
            not in snapshot.research_universe.document_version_ids
        ):
            raise InputEvidenceError(f"input_outside_source_universe:{requirement.key}")
        if requirement.annual and (
            fact.period_start is None
            or not 350 <= (fact.period_end - fact.period_start).days <= 380
        ):
            raise InputEvidenceError(f"annual_actual_required:{requirement.key}")
        value = float(fact.decimal_value * requirement.scale)
        if requirement.key in effective_inputs and effective_inputs[requirement.key] != value:
            raise InputEvidenceError(f"reported_input_value_mismatch:{requirement.key}")
        inputs.append(
            VerifiedInput(
                key=requirement.key,
                value=value,
                reference=ref,
                period_start=fact.period_start.date() if fact.period_start else None,
                period_end=fact.period_end.date(),
                document_version_id=(
                    bundle.evidence.document_version_id if bundle.evidence else None
                ),
                reporting_entity_id=bundle.cell.reporting_entity_id,
                unit_key=fact.unit_key,
                currency=fact.currency,
                observation_kind=fact.observation_kind,
                knowledge_at=fact.knowledge_at,
                recorded_at=fact.recorded_at,
                document_recorded_at=bundle.evidence.document_recorded_at
                if bundle.evidence
                else None,
                extraction_completed_at=bundle.evidence.extraction_completed_at
                if bundle.evidence
                else None,
            )
        )
    return ModelInputReceipt(
        recipe=recipe,
        request=request,
        verified_at=cutoff,
        snapshot_member_sha256=member_sha,
        effective_inputs_sha256=canonical_digest(dict(effective_inputs)),
        required_keys=tuple(sorted(required)),
        inputs=tuple(inputs),
        inventory_snapshot_ids=inventories,
    )
