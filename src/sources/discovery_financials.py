"""Canonical reported inputs for the growth-inflection discovery screen.

The supported semantic slice is the existing normalized financial vocabulary. Native taxonomy expansion belongs to metric ontology.
No filesystem fallback, source ranking, currency conversion or financial write.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, time
from decimal import Decimal
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, ConfigDict

from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.fact_read_model import FactReadModel
from provenance.metric_ontology import MetricOntology

FinancialConcept = Literal[
    "revenue", "gross_profit", "operating_income", "free_cash_flow", "net_income"
]


class GrowthFactReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    concept: FinancialConcept
    metric_id: str
    metric_definition_revision_id: str
    canonical_metric_cell_id: str
    canonical_resolution_revision_id: str
    observation_id: str
    observation_payload_sha256: str
    document_version_id: str
    source_locator: dict[str, object]
    reporting_entity_id: str
    period_start: date
    period_end: date
    currency: str
    unit: str
    accounting_basis: str
    consolidation_scope: str
    value: Decimal


class GrowthFinancials(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ticker: str
    as_of: date
    status: Literal["available", "unavailable", "degraded"]
    reason_codes: tuple[str, ...]
    revenue_yoy: Decimal | None = None
    revenue_yoy_prior: Decimal | None = None
    gross_margin_ttm: Decimal | None = None
    latest_period_end: date | None = None
    references: tuple[GrowthFactReference, ...] = ()
    decision_grade: Literal[False] = False
    acquisition_completeness: Literal["unverified"] = "unverified"
    calculation_version: Literal["growth-inflection-canonical/v1"] = (
        "growth-inflection-canonical/v1"
    )


def calculate_growth_financials(
    ticker: str, as_of: date, facts: tuple[GrowthFactReference, ...]
) -> GrowthFinancials:
    """Compute only from exact, comparable quarterly windows; fiscal labels aren't guessed."""

    def unavailable(reason: str) -> GrowthFinancials:
        return GrowthFinancials(
            ticker=ticker,
            as_of=as_of,
            status="degraded" if facts else "unavailable",
            reason_codes=(reason,),
            references=facts,
        )

    revenue = sorted(
        (item for item in facts if item.concept == "revenue"),
        key=lambda item: item.period_end,
        reverse=True,
    )
    gross = sorted(
        (item for item in facts if item.concept == "gross_profit"),
        key=lambda item: item.period_end,
        reverse=True,
    )
    if len(revenue) < 9 or len(gross) < 4:
        return unavailable("insufficient_resolved_quarterly_history")
    revenue = revenue[:9]
    gross = gross[:4]
    selected = (*revenue, *gross)
    if (
        len(
            {
                (
                    item.reporting_entity_id,
                    item.currency,
                    item.unit,
                    item.accounting_basis,
                    item.consolidation_scope,
                )
                for item in selected
            }
        )
        != 1
    ):
        return unavailable("incomparable_source_coordinates")
    if (
        len({item.metric_id for item in revenue}) != 1
        or len({item.metric_id for item in gross}) != 1
    ):
        return unavailable("metric_definition_identity_break")
    if any(not 70 <= (item.period_end - item.period_start).days <= 105 for item in selected):
        return unavailable("nonquarterly_or_unsupported_duration")
    if (
        len({item.period_end for item in revenue}) != 9
        or len({item.period_end for item in gross}) != 4
    ):
        return unavailable("ambiguous_quarterly_values")
    if [(item.period_start, item.period_end) for item in revenue[:4]] != [
        (item.period_start, item.period_end) for item in gross
    ]:
        return unavailable("gross_profit_revenue_period_mismatch")
    if any(
        not 70 <= (left.period_end - right.period_end).days <= 110
        for left, right in pairwise(revenue)
    ):
        return unavailable("quarterly_history_gap")
    if any((newer.period_start - older.period_end).days != 1 for newer, older in pairwise(revenue)):
        return unavailable("quarterly_duration_gap_or_overlap")
    if any(
        abs((revenue[index].period_end - revenue[index + 4].period_end).days - 365) > 20
        for index in (0, 4)
    ):
        return unavailable("year_over_year_period_mismatch")
    total = sum((item.value for item in revenue[:4]), Decimal(0))
    if total <= 0 or revenue[4].value <= 0 or revenue[8].value <= 0:
        return unavailable("nonpositive_revenue_denominator")
    return GrowthFinancials(
        ticker=ticker,
        as_of=as_of,
        status="available",
        reason_codes=(),
        revenue_yoy=revenue[0].value / revenue[4].value - 1,
        revenue_yoy_prior=revenue[4].value / revenue[8].value - 1,
        gross_margin_ttm=sum((item.value for item in gross), Decimal(0)) / total,
        latest_period_end=revenue[0].period_end,
        references=selected,
    )


class CanonicalFinancialHistory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ticker: str
    as_of: date
    references: tuple[GrowthFactReference, ...] = ()
    unresolved: tuple[tuple[str, date], ...] = ()
    reason_codes: tuple[str, ...] = ()


def read_financial_history(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    as_of: date,
    concepts: tuple[FinancialConcept, ...] = (
        "revenue",
        "gross_profit",
        "operating_income",
        "free_cash_flow",
    ),
) -> CanonicalFinancialHistory:
    """Resolve existing canonical cells then cross the sealed fact read boundary."""
    ticker = ticker.upper()
    cutoff = datetime.combine(as_of, time.max, tzinfo=UTC)
    facts: list[GrowthFactReference] = []
    unresolved: list[tuple[str, date]] = []
    original_factory = conn.row_factory
    owns_snapshot = not conn.in_transaction
    try:
        if owns_snapshot:
            conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT DISTINCT binding.canonical_metric_cell_id,source.concept_name,source.period_end "
            "FROM fact_cell_canonical_binding_revisions binding "
            "JOIN fact_cells_v2 source ON source.fact_cell_id=binding.fact_cell_id "
            "JOIN fact_observations_v2 observation "
            "ON observation.observation_id=binding.source_observation_id "
            "JOIN evidence_document_versions document "
            "ON document.document_version_id=observation.document_version_id "
            "WHERE document.ticker=? "
            "AND source.concept_namespace='urn:earnings-summary:legacy:financial' "
            "AND source.concept_name IN (SELECT value FROM json_each(?)) "
            "AND binding.binding_status='bound' "
            "ORDER BY source.concept_name,binding.canonical_metric_cell_id",
            (ticker, json.dumps(concepts)),
        ).fetchall()
        resolver = CanonicalFactResolutionEngine(conn)
        reader = FactReadModel(conn)
        ontology = MetricOntology(conn)
        for cell_id, concept, period_end in rows:
            candidate_end = date.fromisoformat(str(period_end)[:10])
            if candidate_end > as_of:
                continue
            resolution = resolver.as_known(str(cell_id), cutoff)
            if (
                resolution is None
                or resolution.status != "resolved"
                or resolution.selected_observation_id is None
            ):
                unresolved.append((str(concept), candidate_end))
                continue
            bundle = reader.provenance_bundle(resolution.selected_observation_id, cutoff=cutoff)
            value = bundle.observation
            if bundle.cell.dimensions or bundle.cell.scope_security_id is not None:
                continue
            if (
                bundle.evidence is None
                or value.decimal_value is None
                or value.period_start is None
                or value.currency is None
            ):
                unresolved.append((str(concept), candidate_end))
                continue
            if value.period_end.date() > as_of:
                continue
            row = conn.execute(
                "SELECT metric_id FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
                (cell_id,),
            ).fetchone()
            if row is None:
                raise ValueError("canonical cell disappeared")
            metric_id = str(row[0])
            definition = ontology.metric_definition_as_known(metric_id, cutoff)
            if definition is None or definition.lifecycle != "active":
                unresolved.append((str(concept), candidate_end))
                continue
            binding = ontology.binding_as_known(value.observation_id, cutoff)
            admitted_definition = (
                ontology.metric_definition_as_known(metric_id, binding.recorded_at)
                if binding is not None
                else None
            )
            semantic_fields = {
                "lifecycle",
                "definition_text",
                "value_kind",
                "period_kind",
                "unit_family",
                "accounting_basis",
                "scope_constraints",
            }
            if (
                binding is None
                or binding.binding_status != "bound"
                or binding.canonical_metric_cell_id != str(cell_id)
                or admitted_definition is None
                or admitted_definition.model_dump(include=semantic_fields)
                != definition.model_dump(include=semantic_fields)
                or definition.value_kind != "numeric"
                or definition.period_kind != "duration"
                or definition.unit_family != "currency"
                or definition.accounting_basis != bundle.cell.accounting_basis
                or definition.scope_constraints.get("reporting_entity_id")
                != bundle.cell.reporting_entity_id
                or definition.scope_constraints.get("consolidation_scope")
                != bundle.cell.consolidation_scope
            ):
                unresolved.append((str(concept), candidate_end))
                continue
            facts.append(
                GrowthFactReference.model_validate(
                    {
                        "concept": concept,
                        "metric_id": metric_id,
                        "metric_definition_revision_id": definition.metric_definition_revision_id,
                        "canonical_metric_cell_id": str(cell_id),
                        "canonical_resolution_revision_id": resolution.canonical_resolution_revision_id,
                        "observation_id": value.observation_id,
                        "observation_payload_sha256": value.observation_payload_sha256,
                        "document_version_id": bundle.evidence.document_version_id,
                        "source_locator": bundle.evidence.source_locator.root,
                        "reporting_entity_id": bundle.cell.reporting_entity_id,
                        "period_start": value.period_start.date(),
                        "period_end": value.period_end.date(),
                        "currency": value.currency,
                        "unit": value.unit_key,
                        "accounting_basis": bundle.cell.accounting_basis,
                        "consolidation_scope": bundle.cell.consolidation_scope,
                        "value": value.decimal_value,
                    }
                )
            )
    except (sqlite3.Error, ValueError, RuntimeError):
        return CanonicalFinancialHistory(
            ticker=ticker,
            as_of=as_of,
            reason_codes=("canonical_financial_evidence_unavailable_or_invalid",),
        )
    finally:
        conn.row_factory = original_factory
        if owns_snapshot and conn.in_transaction:
            conn.rollback()
    return CanonicalFinancialHistory(
        ticker=ticker, as_of=as_of, references=tuple(facts), unresolved=tuple(unresolved)
    )


def read_growth_financials(
    conn: sqlite3.Connection, ticker: str, *, as_of: date
) -> GrowthFinancials:
    history = read_financial_history(
        conn, ticker, as_of=as_of, concepts=("revenue", "gross_profit")
    )
    if history.reason_codes:
        return GrowthFinancials(
            ticker=history.ticker,
            as_of=as_of,
            status="unavailable",
            reason_codes=history.reason_codes,
        )
    result = calculate_growth_financials(history.ticker, as_of, history.references)
    if result.status == "available" and any(
        end >= min(item.period_end for item in result.references if item.concept == concept)
        for concept, end in history.unresolved
    ):
        return result.model_copy(
            update={"status": "degraded", "reason_codes": ("unresolved_canonical_period",)}
        )
    return result


def growth_parity_receipt(
    canonical: GrowthFinancials,
    *,
    legacy_values: tuple[float | None, float | None, float | None],
    legacy_source_hashes: dict[str, str],
) -> dict[str, object]:
    """Exact field shadowing with a rounding tolerance, never a source-authority claim."""
    from math import isfinite

    fields = ("revenue_yoy", "revenue_yoy_prior", "gross_margin_ttm")
    normalized = (canonical.revenue_yoy, canonical.revenue_yoy_prior, canonical.gross_margin_ttm)
    missing = (
        any(value is None or not isfinite(value) for value in legacy_values)
        or any(value is None for value in normalized)
        or not legacy_source_hashes
    )
    differences: list[str] = []
    for field_name, legacy, current in zip(fields, legacy_values, normalized, strict=True):
        if (
            legacy is not None
            and current is not None
            and isfinite(legacy)
            and abs(Decimal(str(legacy)) - current) > Decimal("0.000000000001")
        ):
            differences.append(field_name)
    return {
        "schema_version": "growth-screen-dual-read/v1",
        "ticker": canonical.ticker,
        "as_of": canonical.as_of.isoformat(),
        "status": "INDETERMINATE_UNAVAILABLE"
        if missing
        else "VERIFIED_DIVERGENCE"
        if differences
        else "VERIFIED_MATCH",
        "scope": "calculated_growth_fields_only",
        "legacy_source_hashes": legacy_source_hashes,
        "legacy_values": {
            name: value if value is not None and isfinite(value) else None
            for name, value in zip(fields, legacy_values, strict=True)
        },
        "canonical_values": {
            name: str(value) if value is not None else None
            for name, value in zip(fields, normalized, strict=True)
        },
        "divergent_fields": differences,
        "absolute_float_rounding_tolerance": "0.000000000001",
        "canonical_observation_ids": [item.observation_id for item in canonical.references],
    }
