"""Explicit ONON role review on immutable, sealed reported-source coordinates.

This annotates metric definitions through their owning append-only API. It does
not map prose, change metric meaning, calculate values, seal replacement research
snapshots, or grant owner/trade authority. Apply requires the caller's transaction.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import AwareDatetime, Field, TypeAdapter

from dcf.input_evidence import (
    FactBinding,
    FrozenModel,
    InputRequirement,
    ValuationRoleSelector,
    canonical_digest,
)
from dcf.onon_inputs import RECIPE, requirements_for
from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.fact_read_model import ExactEvidenceReference, FactReadModel
from provenance.issuer_registry import evidence_document_relation
from provenance.metric_ontology import (
    CanonicalDimension,
    CanonicalMetricDefinitionRevision,
    MetricOntology,
    canonical_json,
    sha256_json,
)

_SHA = r"^[0-9a-f]{64}$"
_SELECTORS = TypeAdapter(dict[str, ValuationRoleSelector])
_DOCUMENT_BLOB_QUERIES = {
    "v_evidence_document_versions_canonical": (
        "SELECT blob_sha256 FROM v_evidence_document_versions_canonical WHERE document_version_id=?"
    ),
    "evidence_document_versions": (
        "SELECT blob_sha256 FROM evidence_document_versions WHERE document_version_id=?"
    ),
}


class ReviewedInputRoleError(ValueError):
    """Exact role review or its expected prior state could not be proved."""


class InputRoleReview(FrozenModel):
    requirement: InputRequirement
    reference: FactBinding
    selector: ValuationRoleSelector
    source_evidence: ExactEvidenceReference
    source_document_sha256: str = Field(pattern=_SHA)
    canonical_cell_sha256: str = Field(pattern=_SHA)
    prior_definition_sha256: str = Field(pattern=_SHA)
    binding_sha256: str = Field(pattern=_SHA)
    mapping_sha256: str = Field(pattern=_SHA)
    rationale: str = Field(min_length=20)


class InputRoleManifest(FrozenModel):
    schema_version: Literal["reviewed_onon_input_roles.v1"] = "reviewed_onon_input_roles.v1"
    ticker: Literal["ONON"] = "ONON"
    recipe: Literal["onon-cash-rent-sbc-inputs/v1"] = RECIPE
    issuer_id: str = Field(min_length=1)
    financial_period_end: date
    ontology_snapshot_id: str = Field(min_length=1)
    canonical_resolution_snapshot_id: str = Field(min_length=1)
    source_cutoff_at: AwareDatetime
    reviewer: str = Field(min_length=1)
    attribution: Literal["analyst"] = "analyst"
    reviewed_at: AwareDatetime
    recorded_at: AwareDatetime
    inputs: dict[str, InputRoleReview]


class ReviewedInputRoleReceipt(FrozenModel):
    schema_version: Literal["reviewed_onon_input_roles_receipt.v1"] = (
        "reviewed_onon_input_roles_receipt.v1"
    )
    mode: Literal["dry_run", "apply"]
    review_manifest_sha256: str
    plan_sha256: str
    reviewer: str
    attribution: Literal["analyst"] = "analyst"
    reviewed_at: AwareDatetime
    definitions: tuple[CanonicalMetricDefinitionRevision, ...]
    updated_facts: dict[str, FactBinding]
    observation_ids: tuple[str, ...]
    definitions_created: int
    exact_replay: bool
    requires_new_ontology_and_research_snapshots: Literal[True] = True


def describe_input_role_source(
    conn: sqlite3.Connection,
    *,
    requirement: InputRequirement,
    reference: FactBinding,
    cutoff: datetime,
    rationale: str,
) -> InputRoleReview:
    """Describe an analyst-selected reference, without interpreting source prose.

    The analyst must inspect and affirm the typed role/constraint selection and
    rationale in the complete manifest. This helper does not find/select facts.
    """
    original = conn.row_factory
    try:
        ontology = MetricOntology(conn)
        bundle = FactReadModel(conn).provenance_bundle(reference.observation_id, cutoff=cutoff)
        definition = ontology.metric_definition_as_known(reference.metric_id, cutoff)
        binding = ontology.binding_as_known(reference.observation_id, cutoff)
        if bundle.evidence is None or definition is None or binding is None:
            raise ReviewedInputRoleError("role_source_lineage_unavailable")
        mapping = ontology.mapping_as_known(binding.source_component_id or "", cutoff)
        blob = conn.execute(
            _DOCUMENT_BLOB_QUERIES[evidence_document_relation(conn)],
            (bundle.evidence.document_version_id,),
        ).fetchone()
        seal = conn.execute(
            "SELECT semantic_key_sha256 FROM canonical_metric_cell_seals WHERE canonical_metric_cell_id=?",
            (reference.canonical_metric_cell_id,),
        ).fetchone()
        if blob is None or seal is None or mapping is None:
            raise ReviewedInputRoleError("role_source_lineage_unavailable")
        dimensions = tuple(
            CanonicalDimension(axis_id=str(row[0]), member_id=str(row[1]))
            for row in conn.execute(
                "SELECT axis_id,member_id FROM canonical_metric_cell_dimensions "
                "WHERE canonical_metric_cell_id=? ORDER BY dimension_ordinal",
                (reference.canonical_metric_cell_id,),
            )
        )
        return InputRoleReview(
            requirement=requirement,
            reference=reference,
            selector=ValuationRoleSelector(
                canonical_dimensions=dimensions,
                semantic_constraints=requirement.definition_constraints,
            ),
            source_evidence=bundle.evidence,
            source_document_sha256=str(blob[0]),
            canonical_cell_sha256=str(seal[0]),
            prior_definition_sha256=sha256_json(definition.commitment_payload),
            binding_sha256=sha256_json(binding.commitment_payload),
            mapping_sha256=sha256_json(mapping.commitment_payload),
            rationale=rationale,
        )
    finally:
        conn.row_factory = original


def _database_clock(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _validate_source(
    conn: sqlite3.Connection,
    manifest: InputRoleManifest,
    item: InputRoleReview,
    ontology: MetricOntology,
    resolver: CanonicalFactResolutionEngine,
    as_of: datetime,
) -> CanonicalMetricDefinitionRevision:
    ref, req, cutoff = item.reference, item.requirement, manifest.source_cutoff_at
    bundle = FactReadModel(conn).provenance_bundle(ref.observation_id, cutoff=cutoff)
    fact, evidence = bundle.observation, bundle.evidence
    definition = ontology.metric_definition_as_known(ref.metric_id, cutoff)
    binding = ontology.binding_as_known(ref.observation_id, cutoff)
    resolution = resolver.as_known(ref.canonical_metric_cell_id, cutoff)
    if (
        definition is None
        or definition.lifecycle != "active"
        or definition.value_kind != "numeric"
        or definition.metric_definition_revision_id != ref.metric_definition_revision_id
        or sha256_json(definition.commitment_payload) != item.prior_definition_sha256
        or definition.period_kind != req.period_kind
        or definition.accounting_basis != req.accounting_basis
        or definition.unit_family != ("currency" if req.currency else req.unit_key)
        or binding is None
        or binding.binding_status != "bound"
        or binding.canonical_metric_cell_id != ref.canonical_metric_cell_id
        or sha256_json(binding.commitment_payload) != item.binding_sha256
        or resolution is None
        or resolution.status != "resolved"
        or not resolution.exact_replay
        or resolution.selected_observation_id != ref.observation_id
        or resolution.canonical_resolution_revision_id != ref.canonical_resolution_revision_id
    ):
        raise ReviewedInputRoleError("role_semantic_or_resolution_commitment_changed")
    mapping = ontology.mapping_as_known(binding.source_component_id or "", cutoff)
    if (
        mapping is None
        or mapping.mapping_revision_id != binding.mapping_revision_id
        or sha256_json(mapping.commitment_payload) != item.mapping_sha256
    ):
        raise ReviewedInputRoleError("role_mapping_commitment_changed")
    current_binding = ontology.binding_as_known(ref.observation_id, as_of)
    current_mapping = ontology.mapping_as_known(binding.source_component_id or "", as_of)
    current_resolution = resolver.as_known(ref.canonical_metric_cell_id, as_of)
    if (
        current_binding != binding
        or current_mapping != mapping
        or current_resolution is None
        or current_resolution.status != "resolved"
        or not current_resolution.exact_replay
        or current_resolution.canonical_resolution_revision_id
        != ref.canonical_resolution_revision_id
        or current_resolution.selected_observation_id != ref.observation_id
    ):
        raise ReviewedInputRoleError("role_current_source_selection_drift")
    if (
        fact.observation_kind != "reported"
        or fact.value_kind != "numeric"
        or fact.is_nil
        or fact.decimal_value is None
        or not fact.decimal_value.is_finite()
        or fact.unit_key != req.unit_key
        or fact.currency != req.currency
        or fact.period_kind != req.period_kind
        or fact.period_end.date() != (req.period_end or manifest.financial_period_end)
        or (None if fact.period_start is None else fact.period_start.date()) != req.period_start
        or fact.period_end > cutoff
        or bundle.observation_payload_sha256 != ref.observation_payload_sha256
        or bundle.cell.accounting_basis != req.accounting_basis
        or bundle.cell.consolidation_scope != req.consolidation_scope
        or bundle.cell.scope_security_id is not None
        or evidence is None
        or evidence.extraction_seal_id is None
        or evidence.canonical_issuer_id != manifest.issuer_id
        or evidence != item.source_evidence
    ):
        raise ReviewedInputRoleError("role_reported_source_coordinate_changed")
    if (
        req.definition_constraints.get("reported_sign")
        in {"negative_expense", "negative_cash_outflow"}
        and fact.decimal_value > 0
    ):
        raise ReviewedInputRoleError("role_source_sign_conflict")
    doc = conn.execute(
        _DOCUMENT_BLOB_QUERIES[evidence_document_relation(conn)],
        (evidence.document_version_id,),
    ).fetchone()
    if doc is None or str(doc[0]) != item.source_document_sha256:
        raise ReviewedInputRoleError("role_source_document_changed")
    cell = conn.execute(
        "SELECT cell.metric_id,cell.reporting_entity_id,cell.scope_security_id,"
        "cell.accounting_basis,cell.consolidation_scope,seal.semantic_key_sha256,"
        "seal.dimension_set_json,seal.dimension_set_sha256,cell.dimension_count "
        "FROM canonical_metric_cells cell JOIN canonical_metric_cell_seals seal USING(canonical_metric_cell_id) "
        "WHERE canonical_metric_cell_id=?",
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
    encoded = canonical_json([d.model_dump(mode="json") for d in dimensions])
    member = conn.execute(
        "SELECT canonical_resolution_revision_id FROM canonical_fact_resolution_snapshot_members "
        "WHERE resolution_snapshot_id=? AND canonical_metric_cell_id=?",
        (manifest.canonical_resolution_snapshot_id, ref.canonical_metric_cell_id),
    ).fetchone()
    prior_member = conn.execute(
        "SELECT member_sha256 FROM ontology_snapshot_members WHERE ontology_snapshot_id=? "
        "AND member_kind='metric_definition' AND member_id=?",
        (manifest.ontology_snapshot_id, ref.metric_definition_revision_id),
    ).fetchone()
    if (
        cell is None
        or str(cell[0]) != ref.metric_id
        or str(cell[1]) != bundle.cell.reporting_entity_id
        or cell[2] is not None
        or str(cell[3]) != req.accounting_basis
        or str(cell[4]) != req.consolidation_scope
        or str(cell[5]) != item.canonical_cell_sha256
        or str(cell[6]) != encoded
        or str(cell[7]) != hashlib.sha256(encoded.encode()).hexdigest()
        or int(cell[8]) != len(dimensions)
        or item.selector.canonical_dimensions != dimensions
        or item.selector.semantic_constraints != req.definition_constraints
        or member is None
        or str(member[0]) != ref.canonical_resolution_revision_id
        or prior_member is None
        or str(prior_member[0]) != item.prior_definition_sha256
    ):
        raise ReviewedInputRoleError("role_outside_sealed_semantic_population")
    if any(
        key in definition.scope_constraints and definition.scope_constraints[key] != value
        for key, value in req.definition_constraints.items()
    ):
        raise ReviewedInputRoleError("role_definition_constraint_conflict")
    return definition


def review_input_roles(
    conn: sqlite3.Connection,
    review_manifest: bytes,
    *,
    as_of: datetime,
    apply: bool = False,
    expected_plan_sha256: str | None = None,
) -> ReviewedInputRoleReceipt:
    """Dry-run by default. Apply only inside a caller-owned transaction; never commit.

    The exact raw JSON review hash and deterministic revision vector bind apply.
    Replaying the same review verifies the existing revisions without extra writes.
    Definition metadata changes require new ontology/research snapshots afterwards.
    """
    manifest = InputRoleManifest.model_validate_json(review_manifest)
    requirements = requirements_for(manifest.financial_period_end)
    if set(manifest.inputs) != {r.key for r in requirements}:
        raise ReviewedInputRoleError("role_review_population_mismatch")
    for req in requirements:
        if manifest.inputs[req.key].requirement != req:
            raise ReviewedInputRoleError("role_review_requirement_mismatch")
    if (
        as_of.tzinfo is None
        or not manifest.source_cutoff_at < manifest.reviewed_at <= manifest.recorded_at <= as_of
    ):
        raise ReviewedInputRoleError("role_review_clock_invalid")
    if apply and not conn.in_transaction:
        raise ReviewedInputRoleError("role_apply_requires_caller_transaction")
    review_sha = hashlib.sha256(review_manifest).hexdigest()
    original = conn.row_factory
    conn.execute("SAVEPOINT reviewed_input_roles")
    try:
        ontology, resolver = MetricOntology(conn), CanonicalFactResolutionEngine(conn)
        ontology.verify_snapshot(manifest.ontology_snapshot_id)
        header = conn.execute(
            "SELECT cutoff_at,recorded_at FROM ontology_snapshot_headers WHERE ontology_snapshot_id=?",
            (manifest.ontology_snapshot_id,),
        ).fetchone()
        if (
            header is None
            or _database_clock(str(header[0])) != manifest.source_cutoff_at
            or _database_clock(str(header[1])) > manifest.source_cutoff_at
        ):
            raise ReviewedInputRoleError("role_ontology_snapshot_clock_mismatch")
        snapshot = resolver.verify_snapshot(
            manifest.canonical_resolution_snapshot_id, manifest.source_cutoff_at
        )
        if snapshot.scope.issuer_id != manifest.issuer_id:
            raise ReviewedInputRoleError("role_resolution_issuer_mismatch")
        definitions: dict[str, CanonicalMetricDefinitionRevision] = {}
        selected: dict[str, dict[str, ValuationRoleSelector]] = {}
        for key in sorted(manifest.inputs):
            item = manifest.inputs[key]
            definition = _validate_source(conn, manifest, item, ontology, resolver, as_of)
            metric = definition.metric_id
            if metric in definitions and definitions[metric] != definition:
                raise ReviewedInputRoleError("role_prior_definition_conflict")
            definitions[metric] = definition
            selectors = selected.setdefault(metric, {})
            prior = selectors.get(item.requirement.role)
            if prior is not None and prior != item.selector:
                raise ReviewedInputRoleError("role_same_metric_selector_conflict")
            selectors[item.requirement.role] = item.selector
        revised: list[CanonicalMetricDefinitionRevision] = []
        for metric in sorted(definitions):
            prior = definitions[metric]
            scope = dict(prior.scope_constraints)
            if "valuation_role" in scope:
                raise ReviewedInputRoleError("role_legacy_scalar_requires_owning_revision")
            raw = scope.get("valuation_role_selectors", {})
            existing = _SELECTORS.validate_python(raw)
            for role, selector in selected[metric].items():
                if role in existing and existing[role] != selector:
                    raise ReviewedInputRoleError("role_existing_selector_conflict")
                existing[role] = selector
            sets = [
                frozenset((d.axis_id, d.member_id) for d in selector.canonical_dimensions)
                for selector in existing.values()
            ]
            if len(set(sets)) != len(sets):
                raise ReviewedInputRoleError("role_same_metric_dimension_conflict")
            scope["valuation_role_selectors"] = {
                role: existing[role].model_dump(mode="json") for role in sorted(existing)
            }
            scope["valuation_role_review"] = {
                "schema_version": manifest.schema_version,
                "review_manifest_sha256": review_sha,
                "reviewer": manifest.reviewer,
                "attribution": manifest.attribution,
                "reviewed_at": manifest.reviewed_at.isoformat(),
                "source_ontology_snapshot_id": manifest.ontology_snapshot_id,
                "source_resolution_snapshot_id": manifest.canonical_resolution_snapshot_id,
                "inputs": {
                    key: item.model_dump(mode="json")
                    for key, item in sorted(manifest.inputs.items())
                    if item.reference.metric_id == metric
                },
            }
            identity = "reviewed-role-definition:" + canonical_digest(
                {
                    "review": review_sha,
                    "metric": metric,
                    "prior": prior.metric_definition_revision_id,
                }
            )
            revised.append(
                prior.model_copy(
                    update={
                        "metric_definition_revision_id": identity,
                        "idempotency_key": identity,
                        "revision": prior.revision + 1,
                        "supersedes_metric_definition_revision_id": prior.metric_definition_revision_id,
                        "scope_constraints": scope,
                        "effective_at": manifest.reviewed_at,
                        "knowledge_at": manifest.reviewed_at,
                        "recorded_at": manifest.recorded_at,
                    }
                )
            )
        plan_sha = canonical_digest(
            {
                "review_manifest_sha256": review_sha,
                "definitions": [d.model_dump(mode="json") for d in revised],
            }
        )
        if apply and expected_plan_sha256 != plan_sha:
            raise ReviewedInputRoleError("role_apply_plan_commitment_mismatch")
        created = 0
        for desired in revised:
            latest = conn.execute(
                "SELECT metric_definition_revision_id FROM canonical_metric_definition_revisions WHERE metric_id=? ORDER BY revision DESC LIMIT 1",
                (desired.metric_id,),
            ).fetchone()
            if latest is None or str(latest[0]) not in {
                desired.supersedes_metric_definition_revision_id,
                desired.metric_definition_revision_id,
            }:
                raise ReviewedInputRoleError("role_expected_prior_definition_drift")
            if str(latest[0]) == desired.metric_definition_revision_id:
                stored = ontology.metric_definition_as_known(
                    desired.metric_id, manifest.recorded_at
                )
                if stored != desired:
                    raise ReviewedInputRoleError("role_replay_revision_mismatch")
            elif apply:
                ontology.persist_metric_definition(desired)
                created += 1
        revised_ids = {d.metric_id: d.metric_definition_revision_id for d in revised}
        receipt = ReviewedInputRoleReceipt(
            mode="apply" if apply else "dry_run",
            review_manifest_sha256=review_sha,
            plan_sha256=plan_sha,
            reviewer=manifest.reviewer,
            reviewed_at=manifest.reviewed_at,
            definitions=tuple(revised),
            updated_facts={
                key: item.reference.model_copy(
                    update={"metric_definition_revision_id": revised_ids[item.reference.metric_id]}
                )
                for key, item in manifest.inputs.items()
            },
            observation_ids=tuple(
                sorted({item.reference.observation_id for item in manifest.inputs.values()})
            ),
            definitions_created=created,
            exact_replay=apply and created == 0,
        )
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT reviewed_input_roles")
        conn.execute("RELEASE SAVEPOINT reviewed_input_roles")
        raise
    else:
        if not apply:
            conn.execute("ROLLBACK TO SAVEPOINT reviewed_input_roles")
        conn.execute("RELEASE SAVEPOINT reviewed_input_roles")
        return receipt
    finally:
        conn.row_factory = original
