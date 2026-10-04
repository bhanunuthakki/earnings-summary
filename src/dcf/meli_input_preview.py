"""Select reported MELI inputs from one verified snapshot, without writes or priors."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from dcf import input_evidence, meli_inputs
from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.fact_read_model import FactReadModel
from provenance.metric_ontology import CanonicalDimension, MetricOntology


class InputSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    role: str
    period_end: date
    period_start: date | None
    state: Literal["matched", "missing", "ambiguous"]
    candidate_cell_ids: tuple[str, ...]


class MeliInputPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["meli-input-preview/v1"] = "meli-input-preview/v1"
    research_snapshot_id: str
    snapshot_member_sha256: str
    financial_period_end: date
    as_of: AwareDatetime
    state: Literal["incomplete", "reported_inputs_verified_not_model_ready"]
    model_ready: Literal[False] = False
    slots: tuple[InputSlot, ...]
    # An incomplete selection emits no usable fact bindings.
    facts: dict[str, input_evidence.FactBinding]


def preview_meli_inputs(
    conn: sqlite3.Connection,
    *,
    research_snapshot_id: str,
    financial_period_end: date,
    as_of: datetime,
) -> MeliInputPreview:
    """Require a unique exact role/period match for every fixed recipe input.

    Current source coverage and canonical admission still govern this read. It
    cannot assign economic roles, fetch missing data, accept assumptions, run a
    valuation, or persist a model-readiness receipt.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise input_evidence.InputEvidenceError("timezone_required")
    cutoff = as_of.astimezone(UTC)
    requirements = meli_inputs.requirements_for(financial_period_end)
    probe = input_evidence.ModelInputRequest(
        recipe=meli_inputs.RECIPE,
        ticker="MELI",
        research_snapshot_id=research_snapshot_id,
        financial_period_end=financial_period_end,
        facts={},
        assumptions={},
    )
    snapshot, member_sha, _inventories = input_evidence.verify_source_coverage(conn, probe, cutoff)
    rows = conn.execute(
        "SELECT member.canonical_metric_cell_id,member.canonical_resolution_revision_id,"
        "cell.metric_id FROM canonical_fact_resolution_snapshot_members member "
        "JOIN canonical_metric_cells cell USING(canonical_metric_cell_id) "
        "WHERE member.resolution_snapshot_id=? ORDER BY member.canonical_metric_cell_id",
        (snapshot.canonical_fact_resolution_snapshot_id,),
    ).fetchall()
    resolver = CanonicalFactResolutionEngine(conn)
    ontology = MetricOntology(conn)
    reader = FactReadModel(conn)
    matches: dict[str, list[input_evidence.FactBinding]] = {item.key: [] for item in requirements}
    for cell_id, revision_id, metric_id in rows:
        definition = ontology.metric_definition_as_known(str(metric_id), cutoff)
        if definition is None or definition.lifecycle != "active":
            continue
        dimension_rows = conn.execute(
            "SELECT axis_id,member_id FROM canonical_metric_cell_dimensions "
            "WHERE canonical_metric_cell_id=? ORDER BY dimension_ordinal",
            (str(cell_id),),
        ).fetchall()
        dimensions = tuple(
            CanonicalDimension(axis_id=str(axis), member_id=str(member))
            for axis, member in dimension_rows
        )
        roles = [
            item
            for item in requirements
            if input_evidence.input_role_matches(definition.scope_constraints, item, dimensions)
        ]
        if not roles:
            continue
        resolution = resolver.as_known(str(cell_id), cutoff)
        if (
            resolution is None
            or resolution.status != "resolved"
            or resolution.canonical_resolution_revision_id != str(revision_id)
            or resolution.selected_observation_id is None
        ):
            continue
        binding = ontology.binding_as_known(resolution.selected_observation_id, cutoff)
        if (
            binding is None
            or binding.binding_status != "bound"
            or binding.canonical_metric_cell_id != str(cell_id)
        ):
            continue
        bundle = reader.provenance_bundle(resolution.selected_observation_id, cutoff=cutoff)
        fact = bundle.observation
        for item in roles:
            if (
                fact.observation_kind != "reported"
                or fact.decimal_value is None
                or fact.unit_key != item.unit_key
                or fact.currency != item.currency
                or fact.period_kind != item.period_kind
                or fact.period_end.date() != (item.period_end or financial_period_end)
                or (
                    item.period_start is not None
                    and (fact.period_start is None or fact.period_start.date() != item.period_start)
                )
                or bundle.cell.consolidation_scope != item.consolidation_scope
                or bundle.cell.scope_security_id is not None
                or bundle.cell.reporting_entity_id
                not in snapshot.research_universe.reporting_entity_ids
                or (
                    item.accounting_basis is not None
                    and (
                        definition.accounting_basis != item.accounting_basis
                        or bundle.cell.accounting_basis != item.accounting_basis
                    )
                )
            ):
                continue
            matches[item.key].append(
                input_evidence.FactBinding(
                    canonical_metric_cell_id=str(cell_id),
                    metric_id=str(metric_id),
                    metric_definition_revision_id=definition.metric_definition_revision_id,
                    canonical_resolution_revision_id=str(revision_id),
                    observation_id=fact.observation_id,
                    observation_payload_sha256=bundle.observation_payload_sha256,
                )
            )
    slots = tuple(
        InputSlot(
            key=item.key,
            role=item.role,
            period_start=item.period_start,
            period_end=item.period_end or financial_period_end,
            state="matched"
            if len(matches[item.key]) == 1
            else "missing"
            if not matches[item.key]
            else "ambiguous",
            candidate_cell_ids=tuple(ref.canonical_metric_cell_id for ref in matches[item.key]),
        )
        for item in requirements
    )
    complete = all(slot.state == "matched" for slot in slots)
    facts = {key: candidates[0] for key, candidates in matches.items()} if complete else {}
    if complete:
        # Reuse the actual model-input verifier, including cell seals, source
        # identity, latest inventory, fiscal coordinates and payload commitments.
        # No assumptions or readiness receipt leave this reported-input preview.
        input_evidence.verify_model_inputs(
            conn,
            probe.model_copy(update={"facts": facts}),
            recipe=meli_inputs.RECIPE,
            requirements=requirements,
            effective_inputs={},
            assumption_keys=frozenset(),
            as_of=cutoff,
        )
    return MeliInputPreview(
        research_snapshot_id=research_snapshot_id,
        snapshot_member_sha256=member_sha,
        financial_period_end=financial_period_end,
        as_of=cutoff,
        state="reported_inputs_verified_not_model_ready" if complete else "incomplete",
        slots=slots,
        facts=facts,
    )
