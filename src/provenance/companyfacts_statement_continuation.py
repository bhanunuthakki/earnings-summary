"""Qualify exact immutable CompanyFacts matches using filing-backed context.

The aggregate snapshot does not contain statement headings. A review must name
the actual filing for the same issuer and accession. New extraction and source
publication identities retain raw entries; prior observations remain untouched.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.companyfacts_source_proof import (
    exact_companyfacts_entry,
    read_companyfacts_snapshot,
)
from provenance.evidence_ledger import EvidenceLedger, EvidenceLocator, EvidenceNode, ExtractionRun
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
    ReportedFactObservationV2,
)
from provenance.fact_read_model import FactReadModel
from provenance.financial_statement_admission import (
    FinancialStatementAdmissionRequest,
    FinancialStatementConcept,
    FinancialStatementConceptReview,
    FinancialStatementContextReview,
    ReviewedFinancialStatementRole,
    admit_reviewed_financial_statements,
    verify_financial_statement_context,
)
from provenance.legacy_fact_evidence_match import (
    CompanyFactsRelocatedLocator,
    LegacyFactEvidenceMatchRevision,
)
from provenance.metric_ontology import MetricOntology, canonical_json
from provenance.population_canonical_resolution import complete_sealed_assertion_policy
from provenance.population_metric_ontology import admit_exact_reported_observations
from provenance.reporting_entity_registry import ReportingEntityRegistry
from provenance.source_fact_repository import (
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewedCompanyFactsStatementFact(_Closed):
    match_revision_id: str = Field(min_length=1)
    concept: FinancialStatementConcept
    context: FinancialStatementContextReview
    concept_review: FinancialStatementConceptReview | None = None


class ReviewedRawCompanyFactsStatementFact(_Closed):
    snapshot_document_version_id: str = Field(min_length=1)
    locator: CompanyFactsRelocatedLocator
    source_entry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    concept: FinancialStatementConcept
    context: FinancialStatementContextReview
    concept_review: FinancialStatementConceptReview | None = None


class CompanyFactsStatementContinuationRequest(_Closed):
    facts: tuple[ReviewedCompanyFactsStatementFact | ReviewedRawCompanyFactsStatementFact, ...] = (
        Field(min_length=1, max_length=500)
    )
    recorded_at: AwareDatetime
    apply: bool = False


class CompanyFactsStatementContinuationResult(_Closed):
    mode: Literal["dry_run", "apply"]
    publication_id: str
    observation_ids: tuple[str, ...]
    match_revision_ids: tuple[str, ...]
    source_document_version_id: str
    canonical_metric_cell_ids: tuple[str, ...] = ()


def _sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _clock(value: object) -> datetime:
    result = datetime.fromisoformat(str(value))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def _load_match(
    conn: sqlite3.Connection, match_id: str, cutoff: datetime
) -> tuple[LegacyFactEvidenceMatchRevision, str, bytes]:
    original_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT match.* FROM v_legacy_fact_evidence_matches_accepted_current match "
            "WHERE match.match_revision_id=? AND match.outcome='accepted'",
            (match_id,),
        ).fetchone()
        if row is None:
            raise ValueError("CompanyFacts continuation requires an accepted immutable match")
        payload = dict(row)
        for name in (
            "fact_payload",
            "original_locator",
            "relocated_locator",
            "candidate_manifest",
            "reason_details",
        ):
            raw = payload.pop(f"{name}_json")
            payload[name] = None if raw is None else json.loads(str(raw))
        match = LegacyFactEvidenceMatchRevision.model_validate(payload)
        if _clock(match.recorded_at) > cutoff or match.fact_table != "financial_facts":
            raise ValueError("CompanyFacts match is outside the requested source boundary")
        latest = conn.execute(
            "SELECT match_revision_id FROM legacy_fact_evidence_match_revisions "
            "WHERE fact_table=? AND fact_row_id=? AND julianday(recorded_at)<=julianday(?) "
            "ORDER BY revision DESC LIMIT 1",
            (match.fact_table, match.fact_row_id, cutoff),
        ).fetchone()
        if latest is None or str(latest[0]) != match_id:
            raise ValueError("CompanyFacts match has been superseded")
        source = conn.execute(
            "SELECT binding.document_version_id FROM legacy_document_evidence_binding_revisions binding WHERE binding.binding_revision_id=?",
            (match.legacy_binding_revision_id,),
        ).fetchone()
        if source is None:
            raise ValueError("CompanyFacts matched snapshot source is absent")
        document_id = str(source[0])
        body = read_companyfacts_snapshot(
            conn, document_id, issuer_id=match.issuer_id, cutoff=cutoff
        )
        return match, document_id, body
    finally:
        conn.row_factory = original_factory


def continue_companyfacts_statements(
    conn: sqlite3.Connection, request: CompanyFactsStatementContinuationRequest
) -> CompanyFactsStatementContinuationResult:
    """Publish and admit one exact reviewed snapshot set in caller transaction."""
    match_ids = [
        item.match_revision_id
        for item in request.facts
        if isinstance(item, ReviewedCompanyFactsStatementFact)
    ]
    if len(set(match_ids)) != len(match_ids):
        raise ValueError("CompanyFacts continuation match membership must be unique")
    config = canonical_json(request.model_dump(mode="json", exclude={"apply", "recorded_at"}))
    identity = _sha(config)
    run_id, publication_id = (
        f"companyfacts-review-run:{identity}",
        f"companyfacts-review:{identity}",
    )
    prior = conn.execute(
        "SELECT started_at FROM evidence_extraction_runs WHERE extraction_run_id=?", (run_id,)
    ).fetchone()
    if prior is not None:
        request = request.model_copy(update={"recorded_at": _clock(prior[0])})
    records: list[ReportedSourceFact] = []
    nodes: list[EvidenceNode] = []
    document_id: str | None = None
    input_sha: str | None = None
    selected_entries: set[tuple[str, str]] = set()
    for index, item in enumerate(request.facts):
        verify_financial_statement_context(conn, item.context, cutoff=request.recorded_at)
        if isinstance(item, ReviewedCompanyFactsStatementFact):
            match, snapshot_id, body = _load_match(
                conn, item.match_revision_id, request.recorded_at
            )
            locator = match.relocated_locator
            if locator is None or match.issuer_id != item.context.issuer_id:
                raise ValueError("CompanyFacts filing review issuer mismatch")
            expected_entry_sha = match.matched_entry_sha256
            selection_identity = match.match_revision_id
        else:
            snapshot_id = item.snapshot_document_version_id
            body = read_companyfacts_snapshot(
                conn, snapshot_id, issuer_id=item.context.issuer_id, cutoff=request.recorded_at
            )
            locator = item.locator
            expected_entry_sha = item.source_entry_sha256
            selection_identity = locator.canonical_sha256
        if expected_entry_sha is None:
            raise ValueError("CompanyFacts reviewed source entry hash is required")
        selection = (snapshot_id, locator.json_path)
        if selection in selected_entries:
            raise ValueError("CompanyFacts raw entry selection must be unique")
        selected_entries.add(selection)
        if document_id is not None and document_id != snapshot_id:
            raise ValueError("CompanyFacts continuation must retain one exact snapshot")
        document_id, input_sha = snapshot_id, _sha(body)
        filing = conn.execute(
            "SELECT accession_number FROM evidence_document_versions WHERE document_version_id=?",
            (item.context.document_version_id,),
        ).fetchone()
        if filing is None or str(filing[0]) != locator.accession_number:
            raise ValueError("CompanyFacts filing review accession mismatch")
        # Both branches reconstruct actual snapshot identity and exact raw bytes.
        body = read_companyfacts_snapshot(
            conn, snapshot_id, issuer_id=item.context.issuer_id, cutoff=request.recorded_at
        )
        entry = exact_companyfacts_entry(body, locator, entry_sha256=expected_entry_sha)
        entry_json = canonical_json(entry.model_dump(mode="json", exclude_none=False))
        context = item.context
        entry_start = None if entry.start is None else _clock(entry.start)
        if entry_start != context.period_start or _clock(entry.end) != context.period_end:
            raise ValueError("CompanyFacts exact period does not match reviewed fiscal context")
        point_concepts = {"cash_and_equivalents", "total_financial_debt", "shares_outstanding"}
        if (context.period_start is None) != (item.concept in point_concepts):
            raise ValueError("CompanyFacts instant/duration differs from the reviewed role")
        currency = None if item.concept == "shares_outstanding" else locator.unit.split("/")[0]
        expected_unit = (
            "shares"
            if currency is None
            else f"{currency}/shares"
            if item.concept == "eps_diluted"
            else currency
        )
        if (currency is not None and len(currency) != 3) or locator.unit != expected_unit:
            raise ValueError("CompanyFacts reported unit does not match reviewed role")
        subject = ReportingEntityRegistry(conn).canonicalize_recorded_subject(
            context.issuer_id, knowledge_at=request.recorded_at
        )
        if (
            subject.reporting_entity_id != context.reporting_entity_id
            or subject.material_dissent
            or subject.security_id is not None
        ):
            raise ValueError("CompanyFacts reporting subject is outside reviewed issuer scope")
        suffix = _sha(f"{identity}|{index}|{selection_identity}")
        cell = FactCellV2(
            fact_cell_id=f"companyfacts-cell:{suffix}",
            idempotency_key=f"companyfacts-cell:{suffix}",
            reporting_entity_id=context.reporting_entity_id,
            concept_namespace=f"urn:sec:companyfacts:{locator.namespace}",
            concept_name=locator.concept,
            taxonomy_name="SEC CompanyFacts",
            taxonomy_version="companyfacts-source-contract.v1",
            accounting_basis=context.accounting_basis,
            consolidation_scope=context.consolidation_scope,
            period_kind="instant" if context.period_start is None else "duration",
            period_start=context.period_start,
            period_end=context.period_end,
            fiscal_year=context.fiscal_year,
            fiscal_period=context.fiscal_period,
            unit_key=locator.unit,
            currency=currency,
            effective_at=context.period_end,
            knowledge_at=request.recorded_at,
            recorded_at=request.recorded_at,
        )
        old_cell = conn.execute(
            "SELECT fact_cell_id FROM fact_cell_identity_seals_v2 WHERE semantic_key_sha256=?",
            (cell.semantic_key_sha256,),
        ).fetchone()
        if old_cell is not None:
            cell = FactReadModel(conn).cell(str(old_cell[0]), cutoff=request.recorded_at)
        source_locator = {
            **(
                {"companyfacts_match_revision_id": item.match_revision_id}
                if isinstance(item, ReviewedCompanyFactsStatementFact)
                else {"companyfacts_raw_entry_review": locator.model_dump(mode="json")}
            ),
            "companyfacts_json_path": locator.json_path,
            "accession_number": locator.accession_number,
            "statement_document_version_id": context.document_version_id,
        }
        node_id = f"companyfacts-review-node:{suffix}"
        node_locator = EvidenceLocator(source_ref=locator.json_path)
        nodes.append(
            EvidenceNode(
                node_id=node_id,
                evidence_key=node_id,
                revision=1,
                extraction_run_id=run_id,
                node_kind="table_cell",
                text=entry_json,
                locator=node_locator,
                locator_sha256=node_locator.canonical_sha256,
                recorded_at=request.recorded_at,
            )
        )
        observation = ReportedFactObservationV2(
            observation_id=f"companyfacts-reviewed:{suffix}",
            idempotency_key=f"companyfacts-reviewed:{suffix}",
            fact_cell_id=cell.fact_cell_id,
            observation_kind="reported",
            value_kind="numeric",
            numeric_value=str(Decimal(str(entry.val))),
            is_nil=False,
            raw_lexical_value=str(entry.val),
            method_name="exact-companyfacts-with-filing-context",
            method_version="1",
            method_config_sha256=identity,
            revision_kind="initial",
            effective_at=context.period_end,
            knowledge_at=request.recorded_at,
            recorded_at=request.recorded_at,
            document_version_id=snapshot_id,
            evidence_node_id=node_id,
            source_locator=CanonicalJSONObject.model_validate(source_locator),
            source_entry_sha256=_sha(entry_json),
            subject_binding_revision_id=subject.binding_revision_id,
            source_taxonomy_version="companyfacts-source-contract.v1",
            source_unit_id=locator.unit,
            # The legacy-match FK denotes its original exact evidence node.
            # This is a new reviewed extraction node, with the immutable match
            # retained and reconstructed in source_locator instead.
            legacy_match_revision_id=None,
        )
        records.append(ReportedSourceFact(cell=cell, observation=observation))
    assert document_id is not None and input_sha is not None
    result = CompanyFactsStatementContinuationResult(
        mode="apply" if request.apply else "dry_run",
        publication_id=publication_id,
        observation_ids=tuple(item.observation.observation_id for item in records),
        match_revision_ids=tuple(match_ids),
        source_document_version_id=document_id,
    )
    if not request.apply:
        return result
    conn.execute("SAVEPOINT companyfacts_statement_continuation")
    try:
        ledger = EvidenceLedger(conn)
        output_sha = _sha(canonical_json([record.model_dump(mode="json") for record in records]))
        ledger.persist(
            ExtractionRun(
                extraction_run_id=run_id,
                idempotency_key=run_id,
                document_version_id=document_id,
                input_sha256=input_sha,
                extractor_name="reviewed-companyfacts-statement",
                extractor_config_sha256=identity,
                extractor_code_version="1",
                output_sha256=output_sha,
                started_at=request.recorded_at,
                completed_at=request.recorded_at,
                outcome="succeeded",
            )
        )
        for node in nodes:
            ledger.persist(node)
        SourceFactRepository(conn).publish(
            SourceFactPublication(
                publication_id=publication_id,
                idempotency_key=publication_id,
                created_at=request.recorded_at,
                recorded_at=request.recorded_at,
                reported_facts=tuple(records),
                extraction_seals=(
                    ExtractionRunCompletenessSealV2(
                        extraction_seal_id=f"companyfacts-review-seal:{identity}",
                        idempotency_key=f"companyfacts-review-seal:{identity}",
                        extraction_run_id=run_id,
                        expected_node_count=len(records),
                        completeness_policy_name="exact-reviewed-entry-membership",
                        completeness_policy_version="1",
                        completeness_policy_sha256=identity,
                        knowledge_at=request.recorded_at,
                        recorded_at=request.recorded_at,
                    ),
                ),
            )
        )
        admission = admit_exact_reported_observations(
            conn,
            result.observation_ids,
            knowledge_cutoff=request.recorded_at,
            operation_recorded_at=request.recorded_at,
            apply=True,
        )
        roles: list[ReviewedFinancialStatementRole] = []
        ontology = MetricOntology(conn)
        for record, item in zip(records, request.facts, strict=True):
            binding = ontology.binding_as_known(
                record.observation.observation_id, request.recorded_at
            )
            assert binding is not None
            metric = conn.execute(
                "SELECT metric_id FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
                (binding.canonical_metric_cell_id,),
            ).fetchone()
            definition = ontology.metric_definition_as_known(str(metric[0]), request.recorded_at)
            assert definition is not None
            bundle = FactReadModel(conn).provenance_bundle(
                record.observation.observation_id, cutoff=request.recorded_at
            )
            retained_reviews = definition.scope_constraints.get("financial_statement_reviews", {})
            if not isinstance(retained_reviews, dict):
                raise ValueError("CompanyFacts retained role population is invalid")
            retained_reviews = cast(dict[str, object], retained_reviews)
            retained_role = retained_reviews.get(record.observation.observation_id)
            if retained_role is not None:
                roles.append(ReviewedFinancialStatementRole.model_validate(retained_role))
                continue
            roles.append(
                ReviewedFinancialStatementRole(
                    observation_id=record.observation.observation_id,
                    observation_payload_sha256=bundle.observation_payload_sha256,
                    expected_definition_revision_id=definition.metric_definition_revision_id,
                    concept=item.concept,
                    context=item.context,
                    concept_review=item.concept_review,
                )
            )
        admit_reviewed_financial_statements(
            conn,
            FinancialStatementAdmissionRequest(
                roles=tuple(roles), recorded_at=request.recorded_at, apply=True
            ),
        )
        resolver = CanonicalFactResolutionEngine(conn)
        for cell_id in admission.canonical_metric_cell_ids:
            resolver.resolve(
                cell_id,
                request.recorded_at,
                complete_sealed_assertion_policy(),
                recorded_at=request.recorded_at,
            )
        conn.execute("RELEASE SAVEPOINT companyfacts_statement_continuation")
        return result.model_copy(
            update={"canonical_metric_cell_ids": admission.canonical_metric_cell_ids}
        )
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT companyfacts_statement_continuation")
        conn.execute("RELEASE SAVEPOINT companyfacts_statement_continuation")
        raise
