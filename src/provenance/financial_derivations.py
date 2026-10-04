"""Reviewed financial transforms publish immutable formula and operand graphs."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    DerivationInputV2,
    DerivationSealV2,
    DerivedFactObservationV2,
    FactCellV2,
    ObservationRelationV2,
)
from provenance.fact_read_model import FactReadModel, ProvenanceBundle
from provenance.metric_ontology import (
    BindingRevision,
    CanonicalMetric,
    CanonicalMetricCell,
    CanonicalMetricDefinitionRevision,
    MappingRevision,
    MetricOntology,
    SourceTaxonomyComponent,
    canonical_json,
)
from provenance.source_fact_repository import (
    DerivedSourceFact,
    PublicationReceipt,
    SourceFactPublication,
    SourceFactRepository,
)

FORMULA_ID = "financial.ytd_to_discrete_quarter"
FORMULA_VERSION = "1"
FORMULA_DEFINITION = (
    "quarter=cumulative-prior_cumulative; equal issuer, fiscal year, year start, "
    "source concept, taxonomy version, native unit/currency, basis, scope and dimensions; "
    "contiguous standalone quarter; signed source values preserved"
)
FORMULA_SHA256 = hashlib.sha256(FORMULA_DEFINITION.encode()).hexdigest()


class _FinancialDerivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    knowledge_cutoff: datetime
    recorded_at: datetime
    expected_prior_derived_observation_id: str | None = None

    @model_validator(mode="after")
    def _clocks(self) -> _FinancialDerivationRequest:
        if self.knowledge_cutoff.tzinfo is None or self.recorded_at.tzinfo is None:
            raise ValueError("financial derivation clocks must be aware")
        if self.recorded_at < self.knowledge_cutoff:
            raise ValueError("financial derivation cannot predate its cutoff")
        return self


class QuarterDerivationRequest(_FinancialDerivationRequest):
    cumulative_observation_id: str = Field(min_length=1)
    prior_cumulative_observation_id: str = Field(min_length=1)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _selected_operand(
    conn: sqlite3.Connection, observation_id: str, cutoff: datetime
) -> tuple[ProvenanceBundle, str, str]:
    bundle = FactReadModel(conn).provenance_bundle(observation_id, cutoff=cutoff)
    if not CanonicalFactResolutionEngine(conn).observation_lineage_current(observation_id, cutoff):
        raise ValueError("financial operand lineage is no longer current")
    binding = MetricOntology(conn).binding_as_known(observation_id, cutoff)
    if binding is None or binding.binding_status != "bound" or not binding.canonical_metric_cell_id:
        raise ValueError("financial operand requires its current reviewed canonical binding")
    resolution = CanonicalFactResolutionEngine(conn).as_known(
        binding.canonical_metric_cell_id, cutoff
    )
    if (
        resolution is None
        or resolution.status != "resolved"
        or resolution.selected_observation_id != observation_id
    ):
        raise ValueError("financial operand is not the current canonical selection")
    row = conn.execute(
        "SELECT metric_id FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
        (binding.canonical_metric_cell_id,),
    ).fetchone()
    if row is None:
        raise ValueError("financial operand metric is unavailable")
    value = bundle.observation
    if (
        value.value_kind != "numeric"
        or value.decimal_value is None
        or not value.decimal_value.is_finite()
    ):
        raise ValueError("financial operand must carry a finite numeric value")
    return bundle, str(row[0]), resolution.canonical_resolution_revision_id


def publish_discrete_quarter(
    conn: sqlite3.Connection, request: QuarterDerivationRequest
) -> tuple[DerivedSourceFact, PublicationReceipt]:
    """Publish source subtraction; canonical admission needs a separate reviewed binding."""
    cutoff = request.knowledge_cutoff.astimezone(UTC)
    cumulative, metric_id, cumulative_resolution = _selected_operand(
        conn, request.cumulative_observation_id, cutoff
    )
    prior, prior_metric_id, prior_resolution = _selected_operand(
        conn, request.prior_cumulative_observation_id, cutoff
    )
    left, right = cumulative.cell, prior.cell
    if metric_id != prior_metric_id:
        raise ValueError("financial operands have different canonical metrics")
    fields = (
        "reporting_entity_id",
        "scope_security_id",
        "concept_namespace",
        "concept_name",
        "taxonomy_name",
        "taxonomy_version",
        "accounting_basis",
        "consolidation_scope",
        "period_kind",
        "period_start",
        "fiscal_year",
        "dimensions",
        "unit_key",
        "currency",
    )
    if any(getattr(left, field) != getattr(right, field) for field in fields):
        raise ValueError("financial cumulative operands have incomparable source coordinates")
    if (
        left.period_kind != "duration"
        or left.period_start is None
        or right.period_start is None
        or left.fiscal_year is None
    ):
        raise ValueError("financial cumulative operands require duration and fiscal year")
    expected = {
        "Q2": ("Q1", 175, 200),
        "H1": ("Q1", 175, 200),
        "Q3": ("Q2", 260, 295),
        "FY": ("Q3", 345, 385),
    }
    if left.fiscal_period not in expected:
        raise ValueError("only H1, nine-month and FY cumulative flows produce later quarters")
    prior_label, minimum, maximum = expected[left.fiscal_period]
    if (
        right.fiscal_period != prior_label
        and not (prior_label == "Q2" and right.fiscal_period == "H1")
    ) or not minimum <= (left.period_end - left.period_start).days + 1 <= maximum:
        raise ValueError("financial cumulative period does not match its fiscal label")
    prior_duration = (right.period_end - right.period_start).days + 1
    prior_range = {"Q1": (70, 105), "Q2": (175, 200), "Q3": (260, 295)}[prior_label]
    if not prior_range[0] <= prior_duration <= prior_range[1]:
        raise ValueError("prior cumulative period does not match its fiscal label")
    quarter_start = right.period_end + timedelta(days=1)
    if not 70 <= (left.period_end - quarter_start).days + 1 <= 105:
        raise ValueError("financial subtraction does not produce a standalone quarter")
    if left.currency is None or left.unit_key != left.currency:
        raise ValueError(
            "financial derivation requires explicitly normalized native currency units"
        )
    left_value, right_value = cumulative.observation.decimal_value, prior.observation.decimal_value
    assert left_value is not None and right_value is not None
    metric = conn.execute(
        "SELECT canonical_name FROM canonical_metrics WHERE metric_id=?", (metric_id,)
    ).fetchone()
    if (
        metric is not None
        and str(metric[0]) == "capital_expenditure"
        and (left_value > 0 or right_value > 0)
    ):
        raise ValueError("capital expenditure requires explicit signed outflow observations")
    effective_at = max(
        left.period_end, cumulative.observation.effective_at, prior.observation.effective_at
    )
    quarter = (
        "Q4"
        if left.fiscal_period == "FY"
        else "Q2"
        if left.fiscal_period == "H1"
        else left.fiscal_period
    )
    cell = FactCellV2.model_validate(
        {
            **left.model_dump(),
            "fact_cell_id": "pending",
            "idempotency_key": "pending",
            "semantic_key_sha256": None,
            "concept_namespace": "urn:earnings-summary:derived:financial",
            "taxonomy_name": "earnings-summary-financial-formulas",
            "taxonomy_version": FORMULA_VERSION,
            "period_start": quarter_start,
            "fiscal_period": quarter,
            "effective_at": effective_at,
            "knowledge_at": cutoff,
            "recorded_at": request.recorded_at,
        }
    )
    return _publish_transform(
        conn,
        cell=cell,
        value=left_value - right_value,
        operands=(
            (cumulative, cumulative_resolution, "cumulative"),
            (prior, prior_resolution, "prior_cumulative"),
        ),
        formula_id=FORMULA_ID,
        formula_sha256=FORMULA_SHA256,
        cutoff=cutoff,
        recorded_at=request.recorded_at,
        expected_prior=request.expected_prior_derived_observation_id,
    )


class MonetaryScaleRequest(_FinancialDerivationRequest):
    """Scale one selected source-native monetary value; no unit inference."""

    source_scale: Literal["millions", "billions"]
    source_observation_id: str = Field(min_length=1)


def publish_monetary_scale(
    conn: sqlite3.Connection, request: MonetaryScaleRequest
) -> tuple[DerivedSourceFact, PublicationReceipt]:
    source, _, resolution = _selected_operand(
        conn, request.source_observation_id, request.knowledge_cutoff
    )
    cell = source.cell
    if cell.currency is None or cell.unit_key != request.source_scale:
        raise ValueError("monetary scale must match the exact admitted source-native unit")
    assert source.observation.decimal_value is not None
    factor = Decimal(1000000 if request.source_scale == "millions" else 1000000000)
    formula_id = "financial.monetary_scale_to_currency"
    formula_sha = _digest(
        {
            "formula": "value=source*factor",
            "source_scale": request.source_scale,
            "factor": str(factor),
        }
    )
    output = FactCellV2.model_validate(
        {
            **cell.model_dump(),
            "fact_cell_id": "pending",
            "idempotency_key": "pending",
            "semantic_key_sha256": None,
            "concept_namespace": "urn:earnings-summary:derived:financial",
            "taxonomy_name": "earnings-summary-financial-formulas",
            "taxonomy_version": "1",
            "unit_key": cell.currency,
            "knowledge_at": request.knowledge_cutoff,
            "recorded_at": request.recorded_at,
        }
    )
    return _publish_transform(
        conn,
        cell=output,
        value=source.observation.decimal_value * factor,
        operands=((source, resolution, "source_native_monetary"),),
        formula_id=formula_id,
        formula_sha256=formula_sha,
        cutoff=request.knowledge_cutoff,
        recorded_at=request.recorded_at,
        expected_prior=request.expected_prior_derived_observation_id,
    )


def _publish_transform(
    conn: sqlite3.Connection,
    *,
    cell: FactCellV2,
    value: Decimal,
    operands: tuple[tuple[ProvenanceBundle, str, str], ...],
    formula_id: str,
    formula_sha256: str,
    cutoff: datetime,
    recorded_at: datetime,
    expected_prior: str | None,
) -> tuple[DerivedSourceFact, PublicationReceipt]:
    identity = _digest(
        {
            "formula": formula_sha256,
            "cutoff": cutoff.isoformat(),
            "recorded_at": recorded_at.isoformat(),
            "inputs": [item[0].observation_payload_sha256 for item in operands],
            "resolutions": [item[1] for item in operands],
        }
    )
    cell_id = f"financial-derived-cell:{cell.derive_semantic_key()}"
    cell = cell.model_copy(update={"fact_cell_id": cell_id, "idempotency_key": cell_id})
    if (
        conn.execute("SELECT 1 FROM fact_cells_v2 WHERE fact_cell_id=?", (cell_id,)).fetchone()
        is not None
    ):
        cell = FactReadModel(conn).cell(cell_id, cutoff=recorded_at)
    observation_id = f"financial-derived-observation:{identity}"
    prior = conn.execute(
        "SELECT observation_id FROM fact_observations_v2 WHERE fact_cell_id=? "
        "AND observation_kind='derived' ORDER BY recorded_at DESC,observation_id DESC LIMIT 1",
        (cell_id,),
    ).fetchone()
    replay = (
        conn.execute(
            "SELECT 1 FROM fact_observations_v2 WHERE observation_id=?", (observation_id,)
        ).fetchone()
        is not None
    )
    prior_id = None if prior is None else str(prior[0])
    if not replay and prior_id != expected_prior:
        raise ValueError("financial derived head changed; explicit expected predecessor required")
    # Exact replay retains the originally approved predecessor.
    predecessor = expected_prior
    effective_at = max(item[0].observation.effective_at for item in operands)
    observation = DerivedFactObservationV2(
        observation_id=observation_id,
        idempotency_key=observation_id,
        fact_cell_id=cell_id,
        observation_kind="derived",
        value_kind="numeric",
        numeric_value=str(value),
        method_name=formula_id,
        method_version="1",
        method_config_sha256=formula_sha256,
        revision_kind="initial" if predecessor is None else "restatement",
        supersedes_observation_id=predecessor,
        effective_at=effective_at,
        knowledge_at=cutoff,
        recorded_at=recorded_at,
        formula_id=formula_id,
        formula_version="1",
    )
    fact = DerivedSourceFact(cell=cell, observation=observation)
    inputs = tuple(
        DerivationInputV2(
            edge_id=f"financial-edge:{identity}:{index}",
            idempotency_key=f"financial-edge:{identity}:{index}",
            derived_observation_id=observation_id,
            input_position=index,
            input_observation_id=operand.observation.observation_id,
            input_canonical_resolution_revision_id=resolution,
            input_role=role,
            recorded_at=recorded_at,
        )
        for index, (operand, resolution, role) in enumerate(operands)
    )
    seal_id = f"financial-derivation:{identity}"
    seal = DerivationSealV2(
        derivation_seal_id=seal_id,
        idempotency_key=seal_id,
        derived_observation_id=observation_id,
        ordered_inputs=inputs,
        input_basis="as_known",
        formula_definition_sha256=formula_sha256,
        formula_config_sha256=formula_sha256,
        seal_method=formula_id,
        seal_method_version="1",
        effective_at=effective_at,
        knowledge_at=cutoff,
        recorded_at=recorded_at,
    )
    relations = (
        ()
        if predecessor is None
        else (
            ObservationRelationV2(
                relation_id=f"financial-replacement:{identity}",
                idempotency_key=f"financial-replacement:{identity}",
                subject_observation_id=observation_id,
                object_observation_id=predecessor,
                relation_kind="supersedes_for_as_known",
                reason_code="derived_operands_revised",
                reason_details=CanonicalJSONObject.model_validate({"formula": formula_id}),
                policy_name="reviewed_financial_derivation",
                policy_version="1",
                policy_config_sha256=formula_sha256,
                effective_at=effective_at,
                knowledge_at=cutoff,
                recorded_at=recorded_at,
            ),
        )
    )
    publication_id = f"financial-derived-publication:{identity}"
    receipt = SourceFactRepository(conn).publish(
        SourceFactPublication(
            publication_id=publication_id,
            idempotency_key=publication_id,
            derived_facts=(fact,),
            derivations=(seal,),
            relations=relations,
            created_at=cutoff,
            recorded_at=recorded_at,
        )
    )
    return fact, receipt


class _ReviewClock(TypedDict):
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime


class FinancialMetricBindingReview(_FinancialDerivationRequest):
    """Explicit reviewed target; source labels never choose a financial alias."""

    observation_id: str = Field(min_length=1)
    metric_id: str = Field(min_length=1, max_length=128)
    canonical_name: str = Field(min_length=1, max_length=256)
    definition_text: str = Field(min_length=1)
    reviewer_identity: str = Field(min_length=1)
    review_evidence: dict[str, object] = Field(min_length=1)
    expected_prior_binding_revision_id: str | None = None


def bind_reviewed_financial_observation(
    conn: sqlite3.Connection, review: FinancialMetricBindingReview
) -> str:
    """Bind one retained source observation atomically; never duplicate its value."""
    bundle = FactReadModel(conn).provenance_bundle(
        review.observation_id, cutoff=review.knowledge_cutoff
    )
    cell = bundle.cell
    if (
        cell.currency is None
        or bundle.observation.value_kind != "numeric"
        or cell.dimensions
        or cell.scope_security_id is not None
    ):
        raise ValueError("financial binding requires a numeric unsegmented monetary observation")
    ontology = MetricOntology(conn)
    prior = ontology.binding_as_known(review.observation_id, review.knowledge_cutoff)
    if prior is not None and prior.binding_status == "quarantined":
        raise ValueError("terminally quarantined source bindings cannot be promoted")
    current_prior = None if prior is None else prior.binding_revision_id
    if current_prior != review.expected_prior_binding_revision_id:
        # A complete immutable replay keeps its committed binding identity.
        if (
            prior is not None
            and prior.binding_status == "bound"
            and prior.canonical_metric_cell_id is not None
        ):
            target = conn.execute(
                "SELECT cell.metric_id,metric.canonical_name FROM canonical_metric_cells cell "
                "JOIN canonical_metrics metric ON metric.metric_id=cell.metric_id "
                "WHERE cell.canonical_metric_cell_id=?",
                (prior.canonical_metric_cell_id,),
            ).fetchone()
            if (
                target is not None
                and str(target[0]) == review.metric_id
                and str(target[1]) == review.canonical_name
            ):
                definition = ontology.metric_definition_as_known(
                    review.metric_id, review.knowledge_cutoff
                )
                replay_id = f"financial-binding:{_digest([prior.canonical_metric_cell_id, review.model_dump(mode='json')])}"
                mapping = (
                    None
                    if prior.source_component_id is None
                    else ontology.mapping_as_known(
                        prior.source_component_id, review.knowledge_cutoff
                    )
                )
                formula = (
                    None
                    if bundle.derivation is None
                    else {
                        "formula_id": bundle.derivation.formula_id,
                        "formula_version": bundle.derivation.formula_version,
                        "formula_definition_sha256": bundle.derivation.formula_definition_sha256,
                    }
                )
                expected_constraints: dict[str, object] = {"source_currency": cell.currency}
                if formula is not None:
                    expected_constraints["derived_formula"] = formula
                if (
                    definition is not None
                    and definition.lifecycle == "active"
                    and definition.definition_text == review.definition_text
                    and definition.scope_constraints
                    == {
                        "reporting_entity_id": cell.reporting_entity_id,
                        "consolidation_scope": cell.consolidation_scope,
                        "currency": cell.currency,
                    }
                    and mapping is not None
                    and mapping.mapping_revision_id == prior.mapping_revision_id
                    and mapping.policy_name == "reviewed_financial_metric"
                    and mapping.metric_id == review.metric_id
                    and mapping.reviewer_identity == review.reviewer_identity
                    and mapping.evidence == review.review_evidence
                    and mapping.constraints == expected_constraints
                    and prior.knowledge_at == review.knowledge_cutoff
                    and prior.recorded_at == review.recorded_at
                    and definition.accounting_basis == cell.accounting_basis
                    and definition.period_kind == cell.period_kind
                    and prior.binding_revision_id == replay_id
                ):
                    return prior.canonical_metric_cell_id
        raise ValueError("financial source binding head changed")
    if cell.taxonomy_version is None:
        raise ValueError("financial source requires an exact taxonomy version")
    effective = min(cell.effective_at, bundle.observation.effective_at)
    clock: _ReviewClock = {
        "effective_at": effective,
        "knowledge_at": review.knowledge_cutoff,
        "recorded_at": review.recorded_at,
    }
    conn.execute("SAVEPOINT reviewed_financial_binding")
    try:
        existing_metric = conn.execute(
            "SELECT canonical_name FROM canonical_metrics WHERE metric_id=?", (review.metric_id,)
        ).fetchone()
        if existing_metric is None:
            ontology.persist_metric(
                CanonicalMetric(
                    metric_id=review.metric_id,
                    idempotency_key=review.metric_id,
                    canonical_name=review.canonical_name,
                    **clock,
                )
            )
        elif str(existing_metric[0]) != review.canonical_name:
            raise ValueError("financial metric ID has a different canonical name")
        existing_definition = ontology.metric_definition_as_known(
            review.metric_id, review.knowledge_cutoff
        )
        scope: dict[str, object] = {
            "reporting_entity_id": cell.reporting_entity_id,
            "consolidation_scope": cell.consolidation_scope,
            "currency": cell.currency,
        }
        if existing_definition is None:
            definition_id = f"financial-definition:{_digest([review.metric_id, review.definition_text, cell.accounting_basis, scope])}"
            ontology.persist_metric_definition(
                CanonicalMetricDefinitionRevision(
                    metric_definition_revision_id=definition_id,
                    idempotency_key=definition_id,
                    metric_id=review.metric_id,
                    revision=1,
                    lifecycle="active",
                    definition_text=review.definition_text,
                    value_kind="numeric",
                    period_kind=cell.period_kind,
                    unit_family="currency",
                    accounting_basis=cell.accounting_basis,
                    scope_constraints=scope,
                    **clock,
                )
            )
        elif (
            existing_definition.lifecycle != "active"
            or existing_definition.definition_text != review.definition_text
            or existing_definition.accounting_basis != cell.accounting_basis
            or existing_definition.scope_constraints != scope
            or existing_definition.period_kind != cell.period_kind
            or existing_definition.unit_family != "currency"
        ):
            raise ValueError(
                "financial metric definition changed; explicit revision review required"
            )
        formula = (
            None
            if bundle.derivation is None
            else {
                "formula_id": bundle.derivation.formula_id,
                "formula_version": bundle.derivation.formula_version,
                "formula_definition_sha256": bundle.derivation.formula_definition_sha256,
            }
        )
        if bundle.evidence is not None:
            if prior is None or prior.source_component_id is None:
                raise ValueError(
                    "reported financial review requires its admitted source taxonomy binding"
                )
            component_id = prior.source_component_id
        else:
            if formula is None:
                raise ValueError("financial derived source requires sealed formula lineage")
            qualifier = _digest(
                {
                    "formula": formula,
                    "reporting_entity_id": cell.reporting_entity_id,
                    "accounting_basis": cell.accounting_basis,
                    "consolidation_scope": cell.consolidation_scope,
                    "concept_namespace": cell.concept_namespace,
                    "concept_name": cell.concept_name,
                }
            )
            component_id = f"financial-derived-component:{qualifier}"
            existing_component = conn.execute(
                "SELECT 1 FROM source_taxonomy_components WHERE component_id=?", (component_id,)
            ).fetchone()
            if existing_component is None:
                ontology.persist_source_component(
                    SourceTaxonomyComponent(
                        component_id=component_id,
                        idempotency_key=component_id,
                        component_kind="concept",
                        taxonomy_namespace=cell.concept_namespace,
                        local_name=cell.concept_name,
                        taxonomy_name=cell.taxonomy_name,
                        taxonomy_version=cell.taxonomy_version,
                        reporting_entity_id=cell.reporting_entity_id,
                        is_extension=True,
                        definition_qualifier_sha256=qualifier,
                        standard_label=cell.concept_name,
                        definition_text="Reviewed financial formula output; original source inputs retained in sealed lineage.",
                        evidence_locator={"formula": formula},
                        **clock,
                    )
                )
        prior_mapping = ontology.mapping_as_known(component_id, review.knowledge_cutoff)
        constraints: dict[str, object] = {"source_currency": cell.currency}
        if formula is not None:
            constraints["derived_formula"] = formula
        if (
            prior_mapping is not None
            and prior_mapping.metric_id == review.metric_id
            and prior_mapping.constraints == constraints
            and prior_mapping.reviewer_identity == review.reviewer_identity
            and prior_mapping.evidence == review.review_evidence
        ):
            mapping = prior_mapping
        else:
            mapping_id = (
                f"financial-mapping:{_digest([component_id, review.model_dump(mode='json')])}"
            )
            mapping = MappingRevision(
                mapping_revision_id=mapping_id,
                idempotency_key=mapping_id,
                source_component_id=component_id,
                revision=1 if prior_mapping is None else prior_mapping.revision + 1,
                supersedes_mapping_revision_id=None
                if prior_mapping is None
                else prior_mapping.mapping_revision_id,
                metric_id=review.metric_id,
                disposition="derived" if formula else "equivalent",
                policy_name="reviewed_financial_metric",
                policy_version="1",
                policy_config_sha256=_digest({"reviewed_mapping": 1}),
                method_name="explicit_review",
                method_version="1",
                reviewer_identity=review.reviewer_identity,
                evidence=review.review_evidence,
                constraints=constraints,
                **clock,
            )
            ontology.persist_mapping(mapping)
        target = CanonicalMetricCell(
            canonical_metric_cell_id="pending",
            idempotency_key="pending",
            metric_id=review.metric_id,
            reporting_entity_id=cell.reporting_entity_id,
            scope_security_id=cell.scope_security_id,
            period_kind=cell.period_kind,
            period_start=cell.period_start,
            period_end=cell.period_end,
            unit_family="currency",
            accounting_basis=cell.accounting_basis,
            consolidation_scope=cell.consolidation_scope,
            **clock,
        )
        target_id = f"financial-target:{_digest(target.semantic_identity)}"
        if (
            conn.execute(
                "SELECT 1 FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
                (target_id,),
            ).fetchone()
            is None
        ):
            ontology.persist_canonical_metric_cell(
                target.model_copy(
                    update={"canonical_metric_cell_id": target_id, "idempotency_key": target_id}
                )
            )
        binding_id = f"financial-binding:{_digest([target_id, review.model_dump(mode='json')])}"
        ontology.persist_binding(
            BindingRevision(
                binding_revision_id=binding_id,
                idempotency_key=binding_id,
                fact_cell_id=cell.fact_cell_id,
                source_observation_id=review.observation_id,
                revision=1 if prior is None else prior.revision + 1,
                supersedes_binding_revision_id=current_prior,
                canonical_metric_cell_id=target_id,
                mapping_revision_id=mapping.mapping_revision_id,
                source_component_id=component_id,
                **clock,
            )
        )
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT reviewed_financial_binding")
        conn.execute("RELEASE SAVEPOINT reviewed_financial_binding")
        raise
    conn.execute("RELEASE SAVEPOINT reviewed_financial_binding")
    return target_id
