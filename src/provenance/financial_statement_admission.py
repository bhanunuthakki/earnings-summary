"""Source-bound review of exact reported financial observations.

This boundary appends definitions and bindings. It does not change a reported
value, infer a fiscal coordinate, or turn a carve-out into a standalone issuer.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from provenance.companyfacts_source_proof import (
    exact_companyfacts_entry,
    read_companyfacts_snapshot,
)
from provenance.fact_plane_v2 import AccountingBasis, ConsolidationScope, FiscalPeriod
from provenance.fact_read_model import FactReadModel, ProvenanceBundle
from provenance.legacy_fact_evidence_match import CompanyFactsRelocatedLocator
from provenance.metric_ontology import (
    BindingRevision,
    CanonicalMetricDefinitionRevision,
    MetricOntology,
    canonical_json,
)

FinancialStatementConcept = Literal[
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "eps_diluted",
    "operating_cash_flow",
    "free_cash_flow",
    "capital_expenditure",
    "stock_based_compensation",
    "cash_and_equivalents",
    "total_financial_debt",
    "shares_outstanding",
]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FinancialStatementContextReview(_Closed):
    document_version_id: str = Field(min_length=1)
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    issuer_id: str = Field(min_length=1)
    reporting_entity_id: str = Field(min_length=1)
    evidence_node_id: str = Field(min_length=1)
    evidence_locator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_wording: str = Field(min_length=1)
    accounting_basis: AccountingBasis
    consolidation_scope: ConsolidationScope
    source_scope_label: Literal["consolidated", "combined_carve_out"]
    period_start: AwareDatetime | None = None
    period_end: AwareDatetime
    fiscal_year: int = Field(ge=1, le=9999)
    fiscal_period: FiscalPeriod
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    rationale: str = Field(min_length=20)

    @model_validator(mode="after")
    def _shape(self) -> FinancialStatementContextReview:
        expected = "consolidated" if self.source_scope_label == "consolidated" else "other"
        if self.consolidation_scope != expected or self.accounting_basis == "other":
            raise ValueError("review requires an exact accounting basis and source scope")
        if self.period_end > self.reviewed_at or (
            self.period_start is not None and self.period_start > self.period_end
        ):
            raise ValueError("reviewed statement period clocks are inconsistent")
        return self


class FinancialStatementConceptReview(_Closed):
    """An explicit economic-role mapping backed by retained source wording."""

    concept_namespace: str = Field(min_length=1)
    concept_name: str = Field(min_length=1)
    canonical_concept: FinancialStatementConcept
    evidence_node_id: str = Field(min_length=1)
    evidence_locator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_wording: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    rationale: str = Field(min_length=30)


_US_GAAP_ROLES: dict[str, FinancialStatementConcept] = {
    "Revenues": "revenue",
    "SalesRevenueNet": "revenue",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
    "GrossProfit": "gross_profit",
    "OperatingIncomeLoss": "operating_income",
    "NetIncomeLoss": "net_income",
    "EarningsPerShareDiluted": "eps_diluted",
    "NetCashProvidedByUsedInOperatingActivities": "operating_cash_flow",
    "PaymentsToAcquirePropertyPlantAndEquipment": "capital_expenditure",
    "PaymentsToAcquireProductiveAssets": "capital_expenditure",
    "ShareBasedCompensation": "stock_based_compensation",
    "CashAndCashEquivalentsAtCarryingValue": "cash_and_equivalents",
    "CommonStockSharesOutstanding": "shares_outstanding",
}


def _standard_concept(namespace: str, name: str) -> FinancialStatementConcept | None:
    if namespace == "urn:earnings-summary:legacy:financial":
        # Exact pre-existing normalized source roles; no new legacy population.
        legacy: dict[str, FinancialStatementConcept] = {
            value: value for value in _US_GAAP_ROLES.values()
        }
        legacy.update(
            {"free_cash_flow": "free_cash_flow", "total_financial_debt": "total_financial_debt"}
        )
        return legacy.get(name)
    if namespace == "urn:sec:companyfacts:us-gaap" or re.fullmatch(
        r"https?://fasb\.org/us-gaap/\d{4}", namespace
    ):
        return _US_GAAP_ROLES.get(name)
    if (
        namespace == "urn:sec:companyfacts:dei"
        or re.fullmatch(r"https?://xbrl\.sec\.gov/dei/\d{4}", namespace)
    ) and name == "EntityCommonStockSharesOutstanding":
        return "shares_outstanding"
    return None


class ReviewedFinancialStatementRole(_Closed):
    observation_id: str = Field(min_length=1)
    observation_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_definition_revision_id: str = Field(min_length=1)
    concept: FinancialStatementConcept
    context: FinancialStatementContextReview
    concept_review: FinancialStatementConceptReview | None = None


class FinancialStatementAdmissionRequest(_Closed):
    roles: tuple[ReviewedFinancialStatementRole, ...] = Field(min_length=1)
    recorded_at: AwareDatetime
    apply: bool = False

    @model_validator(mode="after")
    def _unique(self) -> FinancialStatementAdmissionRequest:
        ids = [item.observation_id for item in self.roles]
        if len(ids) != len(set(ids)):
            raise ValueError("a reported observation may have only one reviewed financial role")
        if any(item.context.reviewed_at > self.recorded_at for item in self.roles):
            raise ValueError("review is after admission clock")
        return self


class FinancialStatementAdmissionResult(_Closed):
    mode: Literal["dry_run", "apply"]
    observation_ids: tuple[str, ...]
    definition_revision_ids: tuple[str, ...]


def verify_financial_statement_context(
    conn: sqlite3.Connection, context: FinancialStatementContextReview, *, cutoff: datetime
) -> None:
    """Reconstruct the exact issuer document and retained context wording."""
    if cutoff.tzinfo is None or context.reviewed_at > cutoff:
        raise ValueError("financial statement review is outside cutoff")
    row = conn.execute(
        "SELECT document.blob_sha256,document.issuer_id,document.recorded_at,"
        "node.text,node.locator_sha256,node.recorded_at,run.completed_at,run.outcome "
        "FROM evidence_document_versions document JOIN evidence_extraction_runs run "
        "ON run.document_version_id=document.document_version_id "
        "JOIN evidence_nodes node ON node.extraction_run_id=run.extraction_run_id "
        "WHERE document.document_version_id=? AND node.node_id=?",
        (context.document_version_id, context.evidence_node_id),
    ).fetchone()
    if row is None or (
        str(row[0]) != context.document_sha256
        or str(row[1]) != context.issuer_id
        or str(row[4]) != context.evidence_locator_sha256
        or context.source_wording not in str(row[3])
        or str(row[7]) != "succeeded"
    ):
        raise ValueError("financial statement context lacks exact source evidence")
    for index in (2, 5, 6):
        clock = datetime.fromisoformat(str(row[index]))
        clock = clock.replace(tzinfo=UTC) if clock.tzinfo is None else clock.astimezone(UTC)
        if clock > context.reviewed_at:
            raise ValueError("financial statement context evidence is after review")
    entity = conn.execute(
        "SELECT issuer_id FROM reporting_entities WHERE reporting_entity_id=?",
        (context.reporting_entity_id,),
    ).fetchone()
    if entity is None or str(entity[0]) != context.issuer_id:
        raise ValueError("financial statement context issuer/entity mismatch")


def verify_reviewed_financial_role(
    conn: sqlite3.Connection,
    role: ReviewedFinancialStatementRole,
    bundle: ProvenanceBundle,
    *,
    cutoff: datetime,
) -> None:
    verify_financial_statement_context(conn, role.context, cutoff=cutoff)
    source, observation, context = bundle.cell, bundle.observation, role.context
    standard = _standard_concept(source.concept_namespace, source.concept_name)
    if (
        role.concept == "total_financial_debt"
        and source.concept_namespace.startswith(
            (
                "urn:sec:companyfacts:us-gaap",
                "http://fasb.org/us-gaap/",
                "https://fasb.org/us-gaap/",
            )
        )
        and source.concept_name
        in {
            "DebtCurrent",
            "LongTermDebtCurrent",
            "LongTermDebtNoncurrent",
            "LongTermDebt",
            "LongTermDebtAndCapitalLeaseObligations",
            "ShortTermBorrowings",
        }
    ):
        raise ValueError("financial_aggregate_component_is_not_reported_total")
    if standard is not None and standard != role.concept:
        raise ValueError("financial concept contradicts the reviewed exact standard mapping")
    if standard is None:
        mapping = role.concept_review
        if mapping is None or (
            mapping.concept_namespace,
            mapping.concept_name,
            mapping.canonical_concept,
        ) != (source.concept_namespace, source.concept_name, role.concept):
            raise ValueError("financial_concept_mapping_unreviewed")
        verify_financial_statement_context(
            conn,
            context.model_copy(
                update={
                    "evidence_node_id": mapping.evidence_node_id,
                    "evidence_locator_sha256": mapping.evidence_locator_sha256,
                    "source_wording": mapping.source_wording,
                    "reviewer": mapping.reviewer,
                    "reviewed_at": mapping.reviewed_at,
                    "rationale": mapping.rationale,
                }
            ),
            cutoff=cutoff,
        )
    same_document = (
        bundle.evidence is not None
        and bundle.evidence.document_version_id == context.document_version_id
    )
    if bundle.evidence is not None and not same_document:
        # CompanyFacts is an aggregate JSON snapshot. Statement wording belongs
        # to the exact filing, linked by the immutable matcher accession.
        locator = bundle.evidence.source_locator.root
        match = conn.execute(
            "SELECT match.issuer_id,match.matched_entry_sha256,match.relocated_locator_json,"
            "binding.document_version_id,filing.accession_number FROM v_legacy_fact_evidence_matches_accepted_current match "
            "JOIN legacy_document_evidence_binding_revisions binding "
            "ON binding.binding_revision_id=match.legacy_binding_revision_id "
            "JOIN evidence_document_versions filing ON filing.document_version_id=? "
            "WHERE match.match_revision_id=? AND match.outcome='accepted' "
            "AND match.issuer_check='pass' AND match.context_check='pass' "
            "AND match.unit_check='pass' AND match.sign_check='pass' "
            "AND match.fiscal_period_check='pass' AND match.value_check='pass'",
            (context.document_version_id, locator.get("companyfacts_match_revision_id")),
        ).fetchone()
        if match is not None:
            relocated = json.loads(str(match[2]))
            same_document = (
                str(match[0]) == context.issuer_id
                and str(match[1]) == bundle.evidence.source_entry_sha256
                and str(match[3]) == bundle.evidence.document_version_id
                and relocated.get("accession_number") == str(match[4])
                and relocated.get("json_path") == locator.get("companyfacts_json_path")
                and locator.get("statement_document_version_id") == context.document_version_id
            )
        elif locator.get("companyfacts_raw_entry_review") is not None:
            raw_locator = CompanyFactsRelocatedLocator.model_validate(
                locator["companyfacts_raw_entry_review"]
            )
            filing = conn.execute(
                "SELECT accession_number FROM evidence_document_versions WHERE document_version_id=?",
                (context.document_version_id,),
            ).fetchone()
            if (
                filing is not None
                and str(filing[0]) == raw_locator.accession_number
                and locator.get("statement_document_version_id") == context.document_version_id
            ):
                body = read_companyfacts_snapshot(
                    conn,
                    bundle.evidence.document_version_id,
                    issuer_id=context.issuer_id,
                    cutoff=cutoff,
                )
                entry = exact_companyfacts_entry(
                    body, raw_locator, entry_sha256=bundle.evidence.source_entry_sha256
                )
                same_document = (
                    entry.accn == str(filing[0])
                    and str(entry.val) == observation.raw_lexical_value
                    and locator.get("companyfacts_json_path") == raw_locator.json_path
                )
    if (
        observation.observation_id != role.observation_id
        or bundle.observation_payload_sha256 != role.observation_payload_sha256
        or observation.observation_kind != "reported"
        or observation.decimal_value is None
        or bundle.evidence is None
        or not same_document
        or source.reporting_entity_id != context.reporting_entity_id
        or source.scope_security_id is not None
        or source.dimensions
        or source.accounting_basis != context.accounting_basis
        or source.consolidation_scope != context.consolidation_scope
        or source.period_start != context.period_start
        or source.period_end != context.period_end
        or source.fiscal_year != context.fiscal_year
        or source.fiscal_period != context.fiscal_period
    ):
        raise ValueError("reviewed financial role does not match exact reported context")
    currency = observation.currency
    if role.concept == "shares_outstanding" and currency is not None:
        raise ValueError("reviewed financial share count must not carry a currency")
    units = (
        {"shares"}
        if role.concept == "shares_outstanding"
        else (
            {f"{currency}/shares", f"{currency}/share"}
            if role.concept == "eps_diluted"
            else {currency}
        )
    )
    if (
        currency is None and role.concept != "shares_outstanding"
    ) or observation.unit_key not in units:
        raise ValueError("reviewed financial role unit/currency mismatch")


def admit_reviewed_financial_statements(
    conn: sqlite3.Connection, request: FinancialStatementAdmissionRequest
) -> FinancialStatementAdmissionResult:
    """Append exact reviewed roles through the existing ontology authorities."""
    ontology, reader = MetricOntology(conn), FactReadModel(conn)
    prepared: dict[str, tuple[CanonicalMetricDefinitionRevision, dict[str, object]]] = {}
    bindings: list[BindingRevision] = []
    for role in request.roles:
        bundle = reader.provenance_bundle(role.observation_id, cutoff=request.recorded_at)
        verify_reviewed_financial_role(conn, role, bundle, cutoff=request.recorded_at)
        binding = ontology.binding_as_known(role.observation_id, request.recorded_at)
        if (
            binding is None
            or binding.binding_status != "bound"
            or binding.canonical_metric_cell_id is None
        ):
            raise ValueError("reviewed financial role needs existing exact canonical admission")
        row = conn.execute(
            "SELECT metric_id FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
            (binding.canonical_metric_cell_id,),
        ).fetchone()
        if row is None:
            raise ValueError("reviewed financial canonical cell is missing")
        definition = ontology.metric_definition_as_known(str(row[0]), request.recorded_at)
        if definition is None:
            raise ValueError("reviewed financial definition head changed")
        retained = definition.scope_constraints.get("financial_statement_reviews", {})
        if not isinstance(retained, dict):
            raise ValueError("financial statement review population is invalid")
        retained = cast(dict[str, object], retained)
        is_replay = retained.get(role.observation_id) == role.model_dump(mode="json")
        if (
            definition.metric_definition_revision_id != role.expected_definition_revision_id
            and not is_replay
        ):
            raise ValueError("reviewed financial definition head changed")
        if definition.lifecycle != "active":
            raise ValueError("reviewed financial definition is not active")
        scopes = dict(definition.scope_constraints)
        existing_role = scopes.get("financial_statement_concept")
        if existing_role is not None and existing_role != role.concept:
            raise ValueError("reviewed financial role conflicts with existing metric role")
        if definition.metric_id in prepared:
            definition, scopes = prepared[definition.metric_id]
        if scopes.get("financial_statement_concept") not in {None, role.concept}:
            raise ValueError("reviewed financial role conflicts with existing metric role")
        scopes["financial_statement_concept"] = role.concept
        if scopes.get("source_scope_label") not in {None, role.context.source_scope_label}:
            raise ValueError("reviewed financial source scope changed within one metric definition")
        scopes["source_scope_label"] = role.context.source_scope_label
        if role.concept in {
            "operating_cash_flow",
            "capital_expenditure",
            "stock_based_compensation",
            "cash_and_equivalents",
            "total_financial_debt",
            "shares_outstanding",
        }:
            scopes["valuation_role"] = f"cashflow_equity.{role.concept}"
        raw_reviews = scopes.get("financial_statement_reviews", {})
        if not isinstance(raw_reviews, dict):
            raise ValueError("financial statement review population is invalid")
        raw_reviews = cast(dict[str, object], raw_reviews)
        reviews = dict(raw_reviews)
        reviews[role.observation_id] = role.model_dump(mode="json")
        scopes["financial_statement_reviews"] = reviews
        prepared[definition.metric_id] = (definition, scopes)
        if not is_replay:
            bindings.append(binding)
    definitions: list[CanonicalMetricDefinitionRevision] = []
    for definition, scopes in prepared.values():
        if definition.scope_constraints == scopes:
            definitions.append(definition)
            continue
        identity = hashlib.sha256(canonical_json(scopes).encode()).hexdigest()
        revision_id = f"financial-definition:{identity}"
        definitions.append(
            CanonicalMetricDefinitionRevision.model_validate(
                {
                    **definition.model_dump(),
                    "metric_definition_revision_id": revision_id,
                    "idempotency_key": revision_id,
                    "revision": definition.revision + 1,
                    "supersedes_metric_definition_revision_id": definition.metric_definition_revision_id,
                    "scope_constraints": scopes,
                    "knowledge_at": request.recorded_at,
                    "recorded_at": request.recorded_at,
                }
            )
        )
    # A definition review population is immutable. Its new head must retain
    # admission clocks for the already-reviewed periods, not strand them on
    # the prior head. Reconstruct every retained role before appending bindings.
    rebound = {binding.source_observation_id for binding in bindings}
    for definition in definitions:
        prior_definition = prepared[definition.metric_id][0]
        if (
            definition.metric_definition_revision_id
            == prior_definition.metric_definition_revision_id
        ):
            continue
        retained_reviews = definition.scope_constraints.get("financial_statement_reviews")
        if not isinstance(retained_reviews, dict):
            raise ValueError("retained financial review population is invalid")
        retained_reviews = cast(dict[str, object], retained_reviews)
        for observation_id, raw_role in retained_reviews.items():
            retained_role = ReviewedFinancialStatementRole.model_validate(raw_role)
            if retained_role.observation_id != observation_id:
                raise ValueError("retained financial role membership differs")
            retained_bundle = reader.provenance_bundle(
                retained_role.observation_id, cutoff=request.recorded_at
            )
            verify_reviewed_financial_role(
                conn, retained_role, retained_bundle, cutoff=request.recorded_at
            )
            old_binding = ontology.binding_as_known(
                retained_role.observation_id, request.recorded_at
            )
            if (
                old_binding is None
                or old_binding.binding_status != "bound"
                or old_binding.canonical_metric_cell_id is None
            ):
                raise ValueError("retained financial role no longer has exact admission")
            metric = conn.execute(
                "SELECT metric_id FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
                (old_binding.canonical_metric_cell_id,),
            ).fetchone()
            if metric is None or str(metric[0]) != definition.metric_id:
                raise ValueError("retained financial role metric identity differs")
            if retained_role.observation_id not in rebound:
                bindings.append(old_binding)
                rebound.add(retained_role.observation_id)
    if request.apply:
        conn.execute("SAVEPOINT financial_statement_admission")
        try:
            for definition in definitions:
                ontology.persist_metric_definition(definition)
            for binding in bindings:
                identity = hashlib.sha256(
                    canonical_json(
                        {
                            "binding": binding.binding_revision_id,
                            "definitions": [
                                item.metric_definition_revision_id for item in definitions
                            ],
                        }
                    ).encode()
                ).hexdigest()
                revision_id = f"financial-binding:{identity}"
                ontology.persist_binding(
                    binding.model_copy(
                        update={
                            "binding_revision_id": revision_id,
                            "idempotency_key": revision_id,
                            "revision": binding.revision + 1,
                            "supersedes_binding_revision_id": binding.binding_revision_id,
                            "knowledge_at": request.recorded_at,
                            "recorded_at": request.recorded_at,
                        }
                    )
                )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT financial_statement_admission")
            conn.execute("RELEASE SAVEPOINT financial_statement_admission")
            raise
        conn.execute("RELEASE SAVEPOINT financial_statement_admission")
    return FinancialStatementAdmissionResult(
        mode="apply" if request.apply else "dry_run",
        observation_ids=tuple(item.observation_id for item in request.roles),
        definition_revision_ids=tuple(item.metric_definition_revision_id for item in definitions),
    )
