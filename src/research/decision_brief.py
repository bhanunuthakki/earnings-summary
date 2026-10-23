"""Compose memo evidence from existing authorities without a second fact store.

A successful render is not evidence admission. This read-only boundary binds
the analyst's claim review, sealed research universe, canonical financial
references and valuation replay to the exact retained reader body.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Comment, Tag
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from dcf.grade_evidence import load_dcf_verification_evidence
from dcf.input_evidence import ModelInputReceipt, SourceReadContext, verify_source_coverage
from dcf.readiness import load_valuation_readiness
from provenance.canonical_fact_resolution import MAX_DERIVATION_DEPTH, MAX_DERIVATION_NODES
from provenance.evidence_native_candidates import (
    resolve_local_storage_uri,
    select_evidence_native_candidates_by_id,
)
from provenance.fact_read_model import FactReadModel
from provenance.immutable_artifact import (
    canonical_text_artifact_sha256,
    publish_text_no_clobber,
    read_stable_artifact,
)
from provenance.issuer_registry import IssuerRegistry
from provenance.research_snapshot import (
    ResearchSnapshotRequest,
    verify_research_snapshot,
)
from provenance.sec_filing_xbrl_ingest import file_uri_path
from report.artifacts import ReportArtifactRef, validate_report_artifact_path
from report.legacy_body import extract_legacy_reader_body
from research.memo_claim_support import (
    MemoClaimSupportError,
    MemoDerivedFigure,
    MemoReportedValue,
    MemoSourceExcerpt,
    financial_reader_value,
    read_evidence_node_texts,
    verified_value_display,
    verify_memo_analysis_text,
    verify_memo_numeric_population,
    verify_reported_memo_claim,
)
from research.memo_model_evidence import (
    MemoModelCommitmentReference,
    MemoModelEvidenceError,
    MemoModelValueReference,
    VerifiedMeliMemoModel,
    VerifiedMemoModel,
    load_verified_memo_model,
)
from research.memo_report_evidence import (
    DecisionBriefReadiness,
    MemoReportEvidencePopulation,
    verify_memo_report_evidence,
)
from research.nu_earnings_evidence import (
    NuEarningsEvidenceError,
    NuFinancialComparison,
    NuHistoricalEarningsContext,
    load_nu_pre_brief,
    verify_nu_comparison_operands,
    verify_nu_historical_context,
    verify_nu_inference_anchors,
    verify_nu_post_baselines,
)
from search.canonical_fact_projection import (
    admit_canonical_projection_for_read,
    read_canonical_facts_by_id,
)
from search.request_verification import RequestVerificationCache
from sources.report_financials import (
    FinancialEvidenceReference,
    FinancialTableCell,
    read_financial_evidence,
)


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoEvidenceError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class ReviewedMemoClaim(_Closed):
    """An exact passage with attributed support; analyst review is explicit."""

    block_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    passage: str = Field(min_length=1)
    kind: Literal[
        "reported_fact", "management_claim", "calculation", "analyst_inference", "presentation"
    ]
    evidence_node_ids: tuple[str, ...] = ()
    financial_references: tuple[FinancialEvidenceReference, ...] = ()
    calculation: MemoCalculation | None = None
    values: tuple[MemoReportedValue, ...] = ()
    rationale: str = Field(min_length=20)


class MemoCalculation(_Closed):
    operation: Literal["sum", "difference", "ratio", "growth"]
    operands: tuple[FinancialEvidenceReference, ...] = Field(min_length=2)
    displayed_value: str
    display_format: Literal["number1", "number2", "percentage1"]
    scale: Literal[1, 1000000] = 1


class MemoScopedSupport(_Closed):
    """Evidence from one separately admitted issuer snapshot, in passage order."""

    context_id: str = Field(min_length=1)
    evidence_node_ids: tuple[str, ...] = ()
    financial_references: tuple[FinancialEvidenceReference, ...] = ()
    values: tuple[MemoReportedValue, ...] = ()


class MemoObservationReference(_Closed):
    """Exact generic fact identity within the report's sealed financial projection."""

    observation_id: str = Field(min_length=1, max_length=128)
    canonical_metric_cell_id: str = Field(min_length=1, max_length=128)
    canonical_resolution_revision_id: str = Field(min_length=1, max_length=128)
    metric_definition_revision_id: str = Field(min_length=1, max_length=128)


class MemoObservationSum(_Closed):
    """Signed monetary sum; the shared reader supplies values in currency millions."""

    operands: tuple[MemoObservationReference, ...] = Field(min_length=2, max_length=8)
    displayed_value: str
    display_format: Literal["number1", "number2"]
    currency: Literal["USD"] = "USD"
    display_unit: Literal["millions"] = "millions"


class ReviewedMemoClaimV2(ReviewedMemoClaim):
    supporting_evidence: tuple[MemoScopedSupport, ...] = ()
    model_values: tuple[MemoModelValueReference, ...] = ()
    observation_sum: MemoObservationSum | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    # Analyst prose cites short verbatim excerpts; the whole node stays in the
    # report's evidence section. Omitted when empty to keep earlier hashes.
    excerpts: tuple[MemoSourceExcerpt, ...] = Field(default=(), exclude_if=lambda value: not value)
    derived_values: tuple[MemoDerivedFigure, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    # Verbatim spans of the review's pinned owner thesis, such as a break rule.
    owner_thesis_excerpts: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)


class MemoOwnerThesis(_Closed):
    """The owner's thesis as of the report's public cutoff: an input, not company evidence."""

    ticker: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def pinned(self) -> MemoOwnerThesis:
        if hashlib.sha256(self.text.encode()).hexdigest() != self.sha256:
            raise ValueError("owner_thesis_hash_mismatch")
        return self


class MemoReaderBlock(_Closed):
    block_id: str
    selector: str
    text: str
    presentation_only: bool
    visibly_inferred: bool


def memo_reader_blocks(body_html: str) -> tuple[MemoReaderBlock, ...]:
    """Enumerate every retained text block, including text outside paragraphs.

    No reviewer-supplied list determines the population. Source-chip popovers
    are excluded because their retained financial references have a separate
    reconstruction gate. Inert headings and controls are presentation only.
    """
    soup = BeautifulSoup(body_html, "html.parser")
    # Citation markers and their popovers point at evidence claimed elsewhere
    # in the body; the cited value itself (.cite-val) stays in the passage.
    for item in soup.select(
        "script,style,.src-chip,.src-pop,.cite,.cite-mark,.cite-pop,button,select,option"
    ):
        item.decompose()
    groups: dict[str, tuple[Tag, list[str]]] = {}
    boundaries = {
        "p",
        "li",
        "td",
        "th",
        "dd",
        "dt",
        "blockquote",
        "figcaption",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "div",
        "section",
        "main",
        "text",
    }
    for fragment in soup.find_all(string=True):
        if isinstance(fragment, Comment):
            continue
        text = " ".join(str(fragment).split())
        if not text:
            continue
        parent = fragment.parent
        while isinstance(parent, Tag) and parent.name not in boundaries:
            parent = parent.parent
        if not isinstance(parent, Tag) or parent.name == "[document]":
            continue
        parts: list[str] = []
        cursor: Tag | None = parent
        while cursor is not None and cursor.name != "[document]":
            siblings = cursor.find_previous_siblings(cursor.name)
            parts.append(f"{cursor.name}:nth-of-type({len(siblings) + 1})")
            cursor = cursor.parent if isinstance(cursor.parent, Tag) else None
        selector = " > ".join(reversed(parts))
        groups.setdefault(selector, (parent, []))[1].append(text)
    result: list[MemoReaderBlock] = []
    for selector, (element, fragments) in groups.items():
        text = " ".join(fragments)
        # A heading can carry a substantive claim. Formatting alone cannot
        # authorize an evidence-free disposition.
        presentation = text in {
            "Company",
            "Overview",
            "Financials",
            "Valuation",
            "Sources",
            "Risks",
            "Line item",
            "Metric",
            "Base",
            "Bull",
            "Bear",
            "Revenue",
            "Gross profit",
            "Operating income",
            "Net income",
            "Operating cash flow",
            "Capex",
            "Free cash flow",
            "EPS (diluted)",
            "QoQ",
            "YoY",
            "3y CAGR",
            "Bottom line",
            "Expectation bar",
            "Business drivers",
            "What to track",
            "PRE scorecard",
            "Valuation and model",
            "Model and baseline",
            "Model values",
            "Next quarter",
            "Baseline financials",
            "Bear case",
            "Questions for the print",
            "Calculations",
            "Evidence",
        }
        result.append(
            MemoReaderBlock(
                block_id=_sha((selector + "\0" + text).encode()),
                selector=selector,
                text=text,
                presentation_only=presentation,
                visibly_inferred=text.startswith("Analyst inference:")
                or element.find_parent(attrs={"data-voice": "analyst"}) is not None
                or element.get("data-voice") == "analyst",
            )
        )
    return tuple(result)


def verify_memo_claim_population(
    soup: BeautifulSoup, review: MemoContextReview | MemoContextReviewV2
) -> None:
    blocks = {item.block_id: item for item in memo_reader_blocks(str(soup))}
    claimed: set[str] = set()
    for claim in review.claims:
        block = blocks.get(claim.block_id)
        if block is None or claim.passage != block.text:
            raise MemoEvidenceError("memo_claim_block_mismatch")
        if claim.block_id in claimed:
            raise MemoEvidenceError("memo_claim_block_duplicate")
        claimed.add(claim.block_id)
        if claim.kind == "presentation" and not block.presentation_only:
            raise MemoEvidenceError("memo_substantive_block_marked_presentation")
        if claim.kind == "analyst_inference" and not block.visibly_inferred:
            raise MemoEvidenceError("memo_inference_not_distinguished_in_reader")
        if claim.kind == "calculation":
            model_values = claim.model_values if isinstance(claim, ReviewedMemoClaimV2) else ()
            observation_sum = (
                claim.observation_sum if isinstance(claim, ReviewedMemoClaimV2) else None
            )
            derived = claim.derived_values if isinstance(claim, ReviewedMemoClaimV2) else ()
            if (
                claim.calculation is None
                and not model_values
                and observation_sum is None
                and not derived
            ):
                raise MemoEvidenceError("memo_calculation_reconstruction_missing")
            if derived:
                # Operand binding and replay run against retained evidence later.
                continue
            try:
                verify_memo_numeric_population(
                    claim.passage,
                    (
                        *((claim.calculation.displayed_value,) if claim.calculation else ()),
                        *((observation_sum.displayed_value,) if observation_sum else ()),
                        *(value.displayed_value for value in model_values),
                        *(value.displayed_value for value in claim.values),
                        *(
                            (
                                value.displayed_value
                                for support in claim.supporting_evidence
                                for value in support.values
                            )
                            if isinstance(claim, ReviewedMemoClaimV2)
                            else ()
                        ),
                    ),
                )
            except MemoClaimSupportError as exc:
                raise MemoEvidenceError(exc.reason_code) from exc
        elif claim.calculation is not None or (
            # Analyst prose may state verified model outputs; claim replay binds them.
            isinstance(claim, ReviewedMemoClaimV2)
            and claim.model_values
            and claim.kind != "analyst_inference"
        ):
            raise MemoEvidenceError("memo_calculation_kind_mismatch")
    if claimed != set(blocks):
        raise MemoEvidenceError("memo_claim_population_incomplete")


def verify_memo_section_population(soup: BeautifulSoup, section_ids: tuple[str, ...]) -> None:
    required = {"company", "synthesis", "financials", "bear", "valuation", "sources"}
    if not required.issubset(section_ids):
        raise MemoEvidenceError("memo_required_analytical_sections_missing")
    for section_id in sorted(required):
        matches = soup.select(f'[data-tab="{section_id}"], section[id="{section_id}"]')
        if len(matches) != 1:
            raise MemoEvidenceError("memo_required_section_body_missing_or_ambiguous")
        content = matches[0]
        if not content.get_text(" ", strip=True) or not content.select("p,li,table,blockquote"):
            raise MemoEvidenceError("memo_required_section_body_empty")
        if content.select(
            '.panel-empty,[data-status="missing_data"],[data-status="llm_pending"],[data-status="partial"]'
        ):
            raise MemoEvidenceError("memo_required_section_degraded")


def _read_memo_financial_cell(
    conn: sqlite3.Connection, reference: FinancialEvidenceReference
) -> FinancialTableCell | None:
    # Memo display and calculation verification use the report table contract.
    # Native series units and cadence need a separate reviewed memo contract.
    if reference.reader_kind != "report_table":
        raise MemoEvidenceError("memo_financial_series_reference_unsupported")
    cell = read_financial_evidence(conn, reference)
    if cell is not None and not isinstance(cell, FinancialTableCell):
        raise MemoEvidenceError("memo_financial_series_reference_unsupported")
    return cell


def _verify_report_pdf_node(
    conn: sqlite3.Connection, node_id: str, snapshot: ResearchSnapshotRequest
) -> None:
    analysis = snapshot.research_universe.analysis_scope
    scopes = () if analysis is None else analysis.request.report_pdf_tables
    if not scopes:
        return
    row = conn.execute(
        "SELECT run.document_version_id,node.locator_json FROM evidence_nodes node "
        "JOIN evidence_extraction_runs run USING(extraction_run_id) WHERE node.node_id=?",
        (node_id,),
    ).fetchone()
    if row is None:
        raise MemoEvidenceError("memo_report_pdf_anchor_missing")
    for scope in scopes:
        if scope.document_version_id != str(row[0]):
            continue
        page_number = json.loads(str(row[1])).get("page_number")
        if type(page_number) is not int or not 1 <= page_number <= len(scope.pages):
            raise MemoEvidenceError("memo_report_pdf_anchor_page_unknown")
        if scope.pages[page_number - 1].table_requirement != "required":
            raise MemoEvidenceError("memo_report_deferred_pdf_page_used")


def _verify_report_pdf_observation(
    conn: sqlite3.Connection,
    observation_id: str,
    snapshot: ResearchSnapshotRequest,
    *,
    cutoff: datetime,
) -> None:
    analysis = snapshot.research_universe.analysis_scope
    if analysis is None or not analysis.request.report_pdf_tables:
        return
    pending = [(observation_id, frozenset[str]())]
    verified: set[str] = set()
    reader = FactReadModel(conn)
    while pending:
        identity, ancestors = pending.pop()
        if identity in ancestors or len(ancestors) >= MAX_DERIVATION_DEPTH:
            raise MemoEvidenceError("memo_report_source_derivation_cycle_or_depth")
        if identity in verified:
            continue
        verified.add(identity)
        if len(verified) > MAX_DERIVATION_NODES:
            raise MemoEvidenceError("memo_report_source_derivation_node_limit")
        bundle = reader.provenance_bundle(identity, cutoff=cutoff)
        if bundle.evidence is not None:
            _verify_report_pdf_node(conn, bundle.evidence.evidence_node_id, snapshot)
        elif bundle.derivation is None:
            raise MemoEvidenceError("memo_report_source_leaf_unavailable")
        if bundle.derivation is not None:
            pending.extend(
                (source, ancestors | {identity})
                for source in bundle.derivation.input_observation_ids
            )


def verify_memo_snapshot_reference(
    conn: sqlite3.Connection,
    reference: FinancialEvidenceReference,
    snapshot: ResearchSnapshotRequest,
    ticker: str,
    cutoff: datetime,
) -> None:
    _verify_report_pdf_observation(conn, reference.observation_id, snapshot, cutoff=reference.as_of)
    observed = reference.resolution_observed_through
    if observed is not None and (
        reference.as_of != snapshot.cutoff_at
        or observed != snapshot.recorded_at
        or observed > cutoff
    ):
        raise MemoEvidenceError("memo_financial_resolution_observation_clock_mismatch")
    cell = _read_memo_financial_cell(
        conn, reference.model_copy(update={"as_of": snapshot.cutoff_at})
    )
    member = conn.execute(
        "SELECT canonical_resolution_revision_id FROM canonical_fact_resolution_snapshot_members "
        "WHERE resolution_snapshot_id=? AND canonical_metric_cell_id=?",
        (snapshot.canonical_fact_resolution_snapshot_id, reference.canonical_metric_cell_id),
    ).fetchone()
    definition = conn.execute(
        "SELECT 1 FROM ontology_snapshot_members WHERE ontology_snapshot_id=? "
        "AND member_kind='metric_definition' AND member_id=?",
        (snapshot.ontology_snapshot_id, reference.metric_definition_revision_id),
    ).fetchone()
    if (
        reference.ticker != ticker
        or reference.as_of > cutoff
        or cell is None
        or cell.provenance is None
        or cell.provenance.evidence is None
        or cell.provenance.evidence.document_version_id
        not in snapshot.research_universe.document_version_ids
        or definition is None
        or member is None
        or str(member[0]) != reference.canonical_resolution_revision_id
    ):
        raise MemoEvidenceError("memo_financial_evidence_outside_exact_snapshot")


def read_memo_observation_sum(
    conn: sqlite3.Connection,
    operands: tuple[MemoObservationReference, ...],
    snapshot: ResearchSnapshotRequest,
) -> Decimal:
    """Sum exact admitted reported monetary facts in signed USD millions.

    The caller admits the complete research snapshot. This boundary replays its
    financial projection and source lineage; it creates no metric or alias.
    """
    if not 2 <= len(operands) <= 8 or len({ref.observation_id for ref in operands}) != len(
        operands
    ):
        raise MemoEvidenceError("memo_observation_sum_duplicate_or_invalid_operands")
    verification = RequestVerificationCache(conn)
    generation = admit_canonical_projection_for_read(
        conn, snapshot.canonical_fact_projection_run_id, verification
    )
    if (
        generation.cutoff_at != snapshot.cutoff_at
        or generation.recorded_at != snapshot.recorded_at
        or generation.ontology_snapshot_id != snapshot.ontology_snapshot_id
        or generation.resolution_snapshot_id != snapshot.canonical_fact_resolution_snapshot_id
    ):
        raise MemoEvidenceError("memo_observation_sum_snapshot_identity_mismatch")
    hits = read_canonical_facts_by_id(
        conn,
        generation_id=generation.generation_id,
        canonical_metric_cell_ids=tuple(ref.canonical_metric_cell_id for ref in operands),
        verification=verification,
    )
    values: list[Decimal] = []
    identities: list[tuple[object, ...]] = []
    for ref, hit in zip(operands, hits, strict=True):
        if hit is None or any(
            hit.lineage[key] != expected
            for key, expected in (
                ("selected_observation_id", ref.observation_id),
                ("canonical_resolution_revision_id", ref.canonical_resolution_revision_id),
                ("metric_definition_revision_id", ref.metric_definition_revision_id),
            )
        ):
            raise MemoEvidenceError("memo_observation_sum_operand_identity_mismatch")
        bundle = FactReadModel(conn).provenance_bundle(
            ref.observation_id, cutoff=snapshot.cutoff_at
        )
        cell, value = bundle.cell, bundle.observation
        if (
            cell.reporting_entity_id not in snapshot.research_universe.reporting_entity_ids
            or bundle.evidence is None
            or bundle.evidence.document_version_id
            not in snapshot.research_universe.document_version_ids
            or value.observation_kind != "reported"
            or value.decimal_value is None
            or not value.decimal_value.is_finite()
            or hit.value_kind != "numeric"
            or hit.canonical_value is None
            or Decimal(hit.canonical_value) != value.decimal_value
            or cell.accounting_basis not in {"ifrs", "us_gaap"}
            or cell.consolidation_scope == "other"
            or cell.currency != "USD"
            or cell.unit_key != "USD"
        ):
            raise MemoEvidenceError("memo_observation_sum_source_scope_mismatch")
        _verify_report_pdf_observation(
            conn, ref.observation_id, snapshot, cutoff=snapshot.cutoff_at
        )
        identities.append(
            (
                cell.reporting_entity_id,
                cell.scope_security_id,
                cell.accounting_basis,
                cell.consolidation_scope,
                cell.period_kind,
                cell.period_start,
                cell.period_end,
                cell.dimensions,
            )
        )
        values.append(value.decimal_value / Decimal(1000000))
    if any(identity != identities[0] for identity in identities[1:]):
        raise MemoEvidenceError("memo_observation_sum_semantic_scope_mismatch")
    return sum(values, Decimal(0))


def verify_memo_observation_sum(
    conn: sqlite3.Connection, calculation: MemoObservationSum, snapshot: ResearchSnapshotRequest
) -> None:
    calculation = MemoObservationSum.model_validate(calculation.model_dump())
    value = read_memo_observation_sum(conn, calculation.operands, snapshot)
    expected = format(value, ".1f" if calculation.display_format == "number1" else ".2f")
    if expected != calculation.displayed_value:
        raise MemoEvidenceError("memo_observation_sum_value_mismatch")


def verify_memo_calculation(
    conn: sqlite3.Connection,
    calculation: MemoCalculation,
    *,
    nu_comparison: NuFinancialComparison | None = None,
) -> None:
    cells = [_read_memo_financial_cell(conn, reference) for reference in calculation.operands]
    if any(cell is None or cell.display_value is None for cell in cells):
        raise MemoEvidenceError("memo_calculation_operand_unavailable")
    values = [Decimal(str(cell.display_value)) for cell in cells if cell is not None]
    bundles = [
        cell.provenance for cell in cells if cell is not None and cell.provenance is not None
    ]
    identities = {
        (
            item.cell.reporting_entity_id,
            item.cell.currency,
            item.cell.unit_key,
            item.cell.accounting_basis,
            item.cell.consolidation_scope,
        )
        for item in bundles
    }
    if len(identities) != 1:
        raise MemoEvidenceError("memo_calculation_semantic_scope_mismatch")
    if nu_comparison is not None:
        verify_nu_comparison_operands(
            conn, nu_comparison, calculation, tuple(cell for cell in cells if cell is not None)
        )
    if nu_comparison is None and (
        calculation.operation == "growth"
        and len(
            {
                (cell.metric_id, cell.metric_definition_revision_id, cell.cadence)
                for cell in cells
                if cell is not None
            }
        )
        != 1
    ):
        raise MemoEvidenceError("memo_growth_comparability_unverified")
    if calculation.operation != "sum" and len(values) != 2:
        raise MemoEvidenceError("memo_calculation_operand_count_mismatch")
    coordinates = {(item.cell.period_start, item.cell.period_end) for item in bundles}
    if calculation.operation != "growth" and len(coordinates) != 1:
        raise MemoEvidenceError("memo_calculation_period_alignment_unverified")
    if calculation.operation == "growth":
        newest, prior = bundles
        if (
            newest.cell.period_end <= prior.cell.period_end
            or newest.cell.period_start is None
            or prior.cell.period_start is None
        ):
            raise MemoEvidenceError("memo_growth_period_alignment_unverified")
        newest_span = (newest.cell.period_end - newest.cell.period_start).days
        prior_span = (prior.cell.period_end - prior.cell.period_start).days
        if abs(newest_span - prior_span) > 7:
            raise MemoEvidenceError("memo_growth_period_alignment_unverified")
    if calculation.operation == "sum":
        value = sum(values, Decimal(0))
    elif calculation.operation == "difference":
        value = values[0] - values[1]
    else:
        if not values[1]:
            raise MemoEvidenceError("memo_calculation_denominator_zero")
        value = values[0] / values[1]
        if calculation.operation == "growth":
            value -= 1
    value *= calculation.scale
    if calculation.display_format == "percentage1":
        expected = f"{value * 100:.1f}%"
    else:
        expected = f"{value:.{2 if calculation.display_format == 'number2' else 1}f}"
    if expected != calculation.displayed_value:
        raise MemoEvidenceError("memo_calculation_value_mismatch")


def verify_memo_snapshot_node(
    conn: sqlite3.Connection,
    node_id: str,
    snapshot: ResearchSnapshotRequest,
    cutoff: datetime,
) -> None:
    _verify_report_pdf_node(conn, node_id, snapshot)
    processing_snapshot_ids = json.dumps(snapshot.processing_snapshot_ids)
    row = conn.execute(
        "SELECT node.recorded_at,run.outcome,run.document_version_id FROM evidence_nodes node "
        "JOIN evidence_extraction_runs run USING(extraction_run_id) "
        "JOIN document_processing_evidence_members native ON native.native_table='evidence_nodes' "
        "AND native.native_id=node.node_id "
        "JOIN document_processing_evidence_headers output USING(evidence_seal_id) "
        "JOIN document_processing_disposition_members disposition "
        "ON disposition.evidence_table='document_processing_evidence_seals' "
        "AND disposition.evidence_id=output.evidence_seal_id "
        "JOIN document_processing_snapshot_members member USING(processing_disposition_id) "
        "WHERE node.node_id=? AND output.extraction_run_id=run.extraction_run_id "
        "AND output.document_version_id=run.document_version_id "
        "AND member.document_version_id=run.document_version_id "
        "AND member.processing_snapshot_id IN (SELECT value FROM json_each(?))",
        (node_id, processing_snapshot_ids),
    ).fetchone()
    if row is None:
        # Native numeric nodes require the exact retained raw fact and the
        # filing disposition seal selected by the processing snapshot.
        row = conn.execute(
            "SELECT node.recorded_at,run.outcome,run.document_version_id FROM evidence_nodes node "
            "JOIN evidence_extraction_runs run USING(extraction_run_id) "
            "JOIN filing_xbrl_raw_fact_commitments raw ON raw.evidence_node_id=node.node_id "
            "AND raw.extraction_run_id=run.extraction_run_id "
            "JOIN filing_xbrl_extraction_disposition_seals output ON output.extraction_run_id=run.extraction_run_id "
            "JOIN document_processing_disposition_members disposition "
            "ON disposition.evidence_table='filing_xbrl_extraction_disposition_seals' "
            "AND disposition.evidence_id=output.disposition_seal_id "
            "JOIN document_processing_snapshot_members member USING(processing_disposition_id) "
            "WHERE node.node_id=? AND member.document_version_id=run.document_version_id "
            "AND member.processing_snapshot_id IN (SELECT value FROM json_each(?))",
            (node_id, processing_snapshot_ids),
        ).fetchone()
    if row is None or str(row[2]) not in snapshot.research_universe.document_version_ids:
        raise MemoEvidenceError("memo_claim_outside_exact_processing_snapshot")
    clock = datetime.fromisoformat(str(row[0]))
    if (clock.replace(tzinfo=UTC) if clock.tzinfo is None else clock) > cutoff or str(
        row[1]
    ) != "succeeded":
        raise MemoEvidenceError("memo_claim_extraction_not_admitted")


class MemoContextReview(_Closed):
    artifact_id: str
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    research_snapshot_id: str = Field(min_length=1)
    claims: tuple[ReviewedMemoClaim, ...] = Field(min_length=1)
    reviewed_section_ids: tuple[str, ...] = Field(min_length=1)
    # Whole-body review covers claims outside the financial table. It is not
    # an owner thesis approval and cannot authorize publication or a trade.
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    rationale: str = Field(min_length=20)
    report_evidence: MemoReportEvidencePopulation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class MemoSupportingContext(_Closed):
    context_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    issuer_id: str = Field(min_length=1)
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,31}$")
    research_snapshot_id: str = Field(min_length=1)
    member_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MemoContextReviewV2(_Closed):
    schema_version: Literal["memo_context_review.v2"] = "memo_context_review.v2"
    artifact_id: str
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    research_snapshot_id: str = Field(min_length=1)
    claims: tuple[ReviewedMemoClaimV2, ...] = Field(min_length=1)
    reviewed_section_ids: tuple[str, ...] = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    rationale: str = Field(min_length=20)
    report_evidence: MemoReportEvidencePopulation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    supporting_contexts: tuple[MemoSupportingContext, ...] = Field(default=(), max_length=8)
    model: MemoModelCommitmentReference | None = None
    earnings: NuHistoricalEarningsContext | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    owner_thesis: MemoOwnerThesis | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def distinct_contexts(self) -> MemoContextReviewV2:
        if self.owner_thesis is None and any(claim.owner_thesis_excerpts for claim in self.claims):
            raise ValueError("owner_thesis_excerpt_without_pinned_thesis")
        if self.earnings is not None and (
            self.model is not None
            or self.supporting_contexts
            or self.report_evidence is not None
            or any(claim.model_values or claim.supporting_evidence for claim in self.claims)
        ):
            raise ValueError("nu_qualitative_earnings_cannot_claim_model_or_other_contexts")
        ids = tuple(context.context_id for context in self.supporting_contexts)
        snapshots = tuple(context.research_snapshot_id for context in self.supporting_contexts)
        if (
            ids != tuple(sorted(set(ids)))
            or "primary" in ids
            or len(set(snapshots)) != len(snapshots)
            or self.research_snapshot_id in snapshots
            or len({context.issuer_id for context in self.supporting_contexts})
            != len(self.supporting_contexts)
        ):
            raise ValueError("memo_supporting_context_membership_invalid")
        return self


def parse_memo_context_review(raw: str | bytes) -> MemoContextReview | MemoContextReviewV2:
    """An absent version is legacy V1; explicit unknown versions never downgrade."""
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("memo_context_review_object_required")
    if "schema_version" not in payload:
        return MemoContextReview.model_validate_json(raw)
    return MemoContextReviewV2.model_validate_json(raw)


def verify_memo_supporting_context(
    conn: sqlite3.Connection, context: MemoSupportingContext, cutoff: datetime
) -> ResearchSnapshotRequest:
    """Keep the existing single-issuer universe and exact member seal intact."""
    admission = verify_research_snapshot(conn, context.research_snapshot_id)
    row = conn.execute(
        "SELECT request_json FROM research_snapshot_headers WHERE research_snapshot_id=?",
        (context.research_snapshot_id,),
    ).fetchone()
    if row is None:
        raise MemoEvidenceError("memo_supporting_snapshot_missing")
    snapshot = ResearchSnapshotRequest.model_validate_json(str(row[0]))
    issuer = IssuerRegistry(conn).canonicalize_recorded_issuer(
        f"legacy-ticker:{context.ticker}", knowledge_at=cutoff
    )
    if snapshot.cutoff_at > cutoff or snapshot.recorded_at > cutoff:
        raise MemoEvidenceError("memo_supporting_snapshot_after_cutoff")
    if (
        issuer.issuer_id != context.issuer_id
        or snapshot.research_universe.issuer_id != context.issuer_id
        or admission.member_set_sha256 != context.member_set_sha256
    ):
        raise MemoEvidenceError("memo_supporting_context_identity_or_commitment_mismatch")
    return snapshot


def verify_memo_context_source_bytes(
    conn: sqlite3.Connection,
    document_ids: tuple[str, ...],
    source_context: SourceReadContext | None,
) -> tuple[int, int]:
    """Read one complete sealed context; return document count and bytes consumed."""
    if source_context is None:
        raise MemoEvidenceError("memo_source_read_context_missing")
    ids = tuple(sorted(set(document_ids)))
    if not ids or len(ids) > source_context.max_documents:
        raise MemoEvidenceError("memo_source_document_population_limit")
    factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        candidates = select_evidence_native_candidates_by_id(
            conn, document_version_ids=ids, include_legacy=True
        )
    finally:
        conn.row_factory = factory
    if sorted(item.document_version_id for item in candidates) != list(ids):
        raise MemoEvidenceError("memo_source_document_population_mismatch")
    remaining = source_context.max_total_bytes
    for candidate in candidates:
        limit = min(source_context.max_document_bytes, remaining)
        if limit <= 0 or candidate.byte_size > limit:
            raise MemoEvidenceError("memo_source_byte_limit")
        path = resolve_local_storage_uri(
            candidate.storage_uri, allowed_roots=source_context.content_roots, follow_links=False
        )
        if path is None:
            raise MemoEvidenceError("memo_source_location_unapproved")
        root = next(root for root in source_context.content_roots if path.is_relative_to(root))
        if path.lstat().st_nlink != 1:
            raise MemoEvidenceError("memo_source_multiple_links")
        source, _raw = read_stable_artifact(
            path, max_bytes=min(limit, max(1, candidate.byte_size)), allowed_root=root
        )
        if (
            path.lstat().st_nlink != 1
            or source.file_sha256 != candidate.blob_sha256
            or source.size_bytes != candidate.byte_size
        ):
            raise MemoEvidenceError("memo_source_bytes_unavailable_or_changed")
        remaining -= source.size_bytes
    return len(ids), source_context.max_total_bytes - remaining


def verify_memo_composed_source_bytes(
    conn: sqlite3.Connection,
    document_contexts: tuple[tuple[str, ...], ...],
    source_context: SourceReadContext | None,
) -> int:
    """Keep 28 documents per sealed context and one composed-population budget.

    The primary context and at most eight supporting contexts are independently
    complete. A large primary context cannot be split. Every read in this
    composed source-population pass, including repeated documents, consumes the
    original caller's shared total byte budget. This is not a global I/O quota:
    model and readiness replays retain their existing independent per-call
    source bounds and fresh byte checks. Their documents must be members of the
    primary snapshot; supporting contexts cannot extend the DCF universe.
    """
    if not document_contexts or len(document_contexts) > 9:
        raise MemoEvidenceError("memo_source_context_population_limit")
    if source_context is None:
        raise MemoEvidenceError("memo_source_read_context_missing")
    remaining = source_context.max_total_bytes
    all_ids: set[str] = set()
    for document_ids in document_contexts:
        if remaining <= 0:
            raise MemoEvidenceError("memo_source_byte_limit")
        bounded = SourceReadContext.model_validate(
            {**source_context.model_dump(), "max_total_bytes": remaining}
        )
        _count, consumed = verify_memo_context_source_bytes(conn, document_ids, bounded)
        remaining -= consumed
        all_ids.update(document_ids)
    return len(all_ids)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _raw_documents(conn: sqlite3.Connection, document_ids: tuple[str, ...]) -> int:
    """Recheck retained exact bytes, rather than trust a capture row's label."""
    for document_id in document_ids:
        row = conn.execute(
            "SELECT blob.sha256,blob.byte_size,blob.storage_uri "
            "FROM evidence_document_versions document JOIN evidence_content_blobs blob "
            "ON blob.sha256=document.blob_sha256 WHERE document.document_version_id=?",
            (document_id,),
        ).fetchone()
        if row is None:
            raise MemoEvidenceError("memo_source_document_missing")
        locations = [str(row[2])]
        locations.extend(
            str(item[0])
            for item in conn.execute(
                "SELECT storage_uri FROM evidence_blob_location_observations "
                "WHERE blob_sha256=? AND location_kind='local' AND availability_state='present' "
                "ORDER BY recorded_at DESC",
                (str(row[0]),),
            )
        )
        reconstructed = False
        for location in locations:
            # No network or alternate checkout lookup. Only recorded paths.
            if "://" in location and not location.startswith("file://"):
                continue
            path = file_uri_path(location) if location.startswith("file://") else Path(location)
            if not path.is_absolute() or not path.is_file():
                continue
            raw = path.read_bytes()
            if len(raw) == int(row[1]) and _sha(raw) == str(row[0]):
                reconstructed = True
                break
        if not reconstructed:
            raise MemoEvidenceError("memo_source_bytes_unavailable_or_changed")
    return len(document_ids)


def _verify_nu_earnings(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    artifact: ReportArtifactRef,
    review: MemoContextReviewV2,
    snapshot: ResearchSnapshotRequest,
    verified_claim_ids: frozenset[str],
    source_context: SourceReadContext | None,
) -> None:
    context = review.earnings
    if context is None:
        raise NuEarningsEvidenceError("nu_earnings_context_required")
    verify_nu_inference_anchors(context, review.claims, verified_claim_ids)
    if context.phase == "pre":
        return
    commitment = context.pre_brief
    if commitment is None:
        raise NuEarningsEvidenceError("nu_post_pre_commitment_required")
    pre_artifact, raw_review, pre_readiness = load_nu_pre_brief(repo_root, commitment)
    pre_review = parse_memo_context_review(raw_review)
    if (
        not isinstance(pre_review, MemoContextReviewV2)
        or pre_review.earnings is None
        or pre_review.earnings.phase != "pre"
        or pre_review.earnings.public_cutoff_at != commitment.public_cutoff_at
        or commitment.public_cutoff_at >= context.public_cutoff_at
        or pre_artifact.generated_at >= artifact.generated_at
        or pre_review.reviewed_at > artifact.generated_at
        or pre_readiness.evaluated_at >= snapshot.cutoff_at
        or pre_readiness.context_review_sha256 != _sha(pre_review.model_dump_json().encode())
    ):
        raise NuEarningsEvidenceError("nu_post_pre_knowledge_order_or_commitment_mismatch")
    # The loaded review must be PRE, whose closed schema forbids another baseline.
    # This replays every shared gate rather than trusting a caller's "sealed" label.
    replayed = _assess_decision_brief(
        conn,
        repo_root=repo_root,
        artifact=pre_artifact,
        as_of=pre_readiness.evaluated_at,
        context_review=pre_review,
        source_context=source_context,
    )
    if replayed.model_dump() != pre_readiness.model_dump():
        raise NuEarningsEvidenceError("nu_post_pre_verification_replay_mismatch")
    verify_nu_post_baselines(conn, context, review.claims, verified_claim_ids, pre_review.claims)


def verify_memo_claims(
    conn: sqlite3.Connection,
    *,
    review: MemoContextReview | MemoContextReviewV2,
    snapshot: ResearchSnapshotRequest,
    ticker: str,
    snapshots: dict[str, tuple[ResearchSnapshotRequest, str]],
    supporting_contexts: dict[str, MemoSupportingContext],
    used_contexts: set[str],
    cutoff: datetime,
    verified_model: VerifiedMemoModel | VerifiedMeliMemoModel | None,
    earnings: NuHistoricalEarningsContext | None,
) -> tuple[frozenset[str], frozenset[str]]:
    """Replay the shared claim gates for ordinary and historical reports."""
    verified_claim_ids: set[str] = set()
    verified_model_claim_ids: set[str] = set()
    for claim in review.claims:
        scoped_support = claim.supporting_evidence if isinstance(claim, ReviewedMemoClaimV2) else ()
        model_values = claim.model_values if isinstance(claim, ReviewedMemoClaimV2) else ()
        observation_sum = claim.observation_sum if isinstance(claim, ReviewedMemoClaimV2) else None
        excerpts = claim.excerpts if isinstance(claim, ReviewedMemoClaimV2) else ()
        if claim.kind not in {"analyst_inference", "presentation"} and not (
            excerpts
            or claim.evidence_node_ids
            or claim.financial_references
            or claim.calculation
            or claim.values
            or scoped_support
            or model_values
            or observation_sum
            or (
                isinstance(claim, ReviewedMemoClaimV2)
                and claim.kind == "calculation"
                and isinstance(review, MemoContextReviewV2)
                and review.owner_thesis is not None
                and claim.owner_thesis_excerpts
                and claim.derived_values
            )
        ):
            raise MemoEvidenceError("memo_reported_claim_support_missing")
        for node_id in claim.evidence_node_ids:
            verify_memo_snapshot_node(conn, node_id, snapshot, cutoff)
        claim_references = (
            claim.financial_references
            + (claim.calculation.operands if claim.calculation else ())
            + tuple(value.reference for value in claim.values)
        )
        for reference in claim_references:
            verify_memo_snapshot_reference(conn, reference, snapshot, ticker, cutoff)
        all_nodes = list(claim.evidence_node_ids)
        all_values = list(claim.values)
        for support in scoped_support:
            if support.context_id not in supporting_contexts:
                raise MemoEvidenceError("memo_claim_context_unknown")
            if not (support.evidence_node_ids or support.financial_references or support.values):
                raise MemoEvidenceError("memo_claim_context_support_empty")
            used_contexts.add(support.context_id)
            context_snapshot, context_ticker = snapshots[support.context_id]
            for node_id in support.evidence_node_ids:
                _verify_report_pdf_node(conn, node_id, snapshot)
                verify_memo_snapshot_node(conn, node_id, context_snapshot, cutoff)
            for reference in support.financial_references + tuple(
                value.reference for value in support.values
            ):
                _verify_report_pdf_observation(
                    conn, reference.observation_id, snapshot, cutoff=reference.as_of
                )
                verify_memo_snapshot_reference(
                    conn, reference, context_snapshot, context_ticker, cutoff
                )
            all_nodes.extend(support.evidence_node_ids)
            all_values.extend(support.values)
        if claim.calculation is not None:
            comparison = (
                next(
                    (
                        item
                        for item in earnings.comparisons
                        if item.claim_block_id == claim.block_id
                    ),
                    None,
                )
                if earnings is not None
                else None
            )
            if comparison is None:
                verify_memo_calculation(conn, claim.calculation)
            else:
                verify_memo_calculation(conn, claim.calculation, nu_comparison=comparison)
        calculated_values = (claim.calculation.displayed_value,) if claim.calculation else ()
        if observation_sum is not None:
            if claim.kind != "calculation":
                raise MemoEvidenceError("memo_observation_sum_claim_kind_mismatch")
            verify_memo_observation_sum(conn, observation_sum, snapshot)
            calculated_values += (observation_sum.displayed_value,)
        if model_values:
            if verified_model is None:
                raise MemoEvidenceError("memo_model_reference_population_mismatch")
            calculated_values += tuple(verified_model.display(value) for value in model_values)
            verified_model_claim_ids.add(claim.block_id)
        derived = claim.derived_values if isinstance(claim, ReviewedMemoClaimV2) else ()
        for excerpt in excerpts:
            for node_id in excerpt.evidence_node_ids:
                verify_memo_snapshot_node(conn, node_id, snapshot, cutoff)
        if (
            isinstance(claim, ReviewedMemoClaimV2)
            and claim.owner_thesis_excerpts
            and claim.kind not in {"calculation", "analyst_inference"}
        ):
            # The owner's thesis frames analysis; it never supports a reported fact.
            raise MemoEvidenceError("memo_owner_thesis_claim_kind_mismatch")
        if claim.kind == "analyst_inference" or derived:
            # Analyst prose: quoted spans and figures must bind to cited support.
            if derived and claim.kind not in {"calculation", "analyst_inference"}:
                raise MemoEvidenceError("memo_derived_claim_kind_mismatch")
            try:
                thesis = review.owner_thesis if isinstance(review, MemoContextReviewV2) else None
                owner = (
                    claim.owner_thesis_excerpts if isinstance(claim, ReviewedMemoClaimV2) else ()
                )
                verify_memo_analysis_text(
                    claim.passage,
                    (
                        *(
                            (
                                item.text,
                                tuple(read_evidence_node_texts(conn, item.evidence_node_ids)),
                            )
                            for item in excerpts
                        ),
                        *((text, (thesis.text,)) for text in owner if thesis is not None),
                    ),
                    (
                        *calculated_values,
                        *(verified_value_display(conn, value) for value in all_values),
                    ),
                    derived,
                )
            except MemoClaimSupportError as exc:
                raise MemoEvidenceError(exc.reason_code) from exc
        elif claim.kind in {"reported_fact", "management_claim", "calculation"}:
            try:
                verify_reported_memo_claim(
                    conn,
                    claim.passage,
                    evidence_node_ids=tuple(all_nodes),
                    values=tuple(all_values),
                    calculated_values=calculated_values,
                )
            except MemoClaimSupportError as exc:
                raise MemoEvidenceError(exc.reason_code) from exc
        verified_claim_ids.add(claim.block_id)
    return frozenset(verified_claim_ids), frozenset(verified_model_claim_ids)


def _verify_report_source_population(
    conn: sqlite3.Connection,
    snapshot: ResearchSnapshotRequest,
    review: MemoContextReview | MemoContextReviewV2,
    cutoff: datetime,
) -> None:
    analysis = snapshot.research_universe.analysis_scope
    scopes = () if analysis is None else analysis.request.report_pdf_tables
    if not scopes:
        return
    populations = {item.source_population.population_sha256 for item in scopes}
    if (
        len(populations) != 1
        or review.report_evidence is None
        or (review.report_evidence.source_population_sha256 not in populations)
    ):
        raise MemoEvidenceError("memo_report_source_population_mismatch")
    population = scopes[0].source_population
    claims = {claim.block_id: claim for claim in review.claims}
    declared = {item.requirement: item for item in population.requirements}
    for requirement in population.requirements:
        for node in requirement.evidence_node_ids:
            verify_memo_snapshot_node(conn, node, snapshot, cutoff)
        for observation in requirement.observation_ids:
            _verify_report_pdf_observation(conn, observation, snapshot, cutoff=snapshot.cutoff_at)
            if (
                conn.execute(
                    "SELECT 1 FROM canonical_fact_resolution_snapshot_members member "
                    "JOIN canonical_fact_resolution_revisions resolution "
                    "USING(canonical_resolution_revision_id) WHERE member.resolution_snapshot_id=? "
                    "AND resolution.selected_observation_id=?",
                    (snapshot.canonical_fact_resolution_snapshot_id, observation),
                ).fetchone()
                is None
            ):
                raise MemoEvidenceError("memo_report_source_observation_not_in_snapshot")
    for table_scope in scopes:
        all_nodes = {node for item in population.requirements for node in item.evidence_node_ids}
        all_observations = {
            value for item in population.requirements for value in item.observation_ids
        }
        for page in table_scope.pages:
            if (
                not set(page.replacement_evidence_node_ids) <= all_nodes
                or not set(page.replacement_observation_ids) <= all_observations
            ):
                raise MemoEvidenceError("memo_report_pdf_replacement_outside_source_population")
    for item in review.report_evidence.requirements:
        sources = declared[item.requirement]
        source_found = False
        for identity in item.claim_block_ids:
            claim = claims[identity]
            supports = claim.supporting_evidence if isinstance(claim, ReviewedMemoClaimV2) else ()
            nodes = set(claim.evidence_node_ids)
            observations = {reference.observation_id for reference in claim.financial_references}
            observations.update(value.reference.observation_id for value in claim.values)
            if claim.calculation is not None:
                observations.update(value.observation_id for value in claim.calculation.operands)
            if isinstance(claim, ReviewedMemoClaimV2) and claim.observation_sum is not None:
                observations.update(
                    value.observation_id for value in claim.observation_sum.operands
                )
            for support in supports:
                nodes.update(support.evidence_node_ids)
                observations.update(value.observation_id for value in support.financial_references)
                observations.update(value.reference.observation_id for value in support.values)
            if nodes.intersection(sources.evidence_node_ids) or observations.intersection(
                sources.observation_ids
            ):
                source_found = True
        if not source_found:
            raise MemoEvidenceError("memo_report_requirement_declared_source_unused")


def _assess_decision_brief(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    artifact: ReportArtifactRef,
    as_of: datetime,
    context_review: MemoContextReview | MemoContextReviewV2 | None = None,
    source_context: SourceReadContext | None = None,
) -> DecisionBriefReadiness:
    """Check one immutable artifact. Missing proof stays separate and visible."""
    if as_of.tzinfo is None:
        raise ValueError("memo cutoff must have a timezone")
    cutoff = as_of.astimezone(UTC)
    reasons: set[str] = set()
    if artifact.publication_status == "draft_preview":
        raise MemoEvidenceError("memo_draft_preview_not_accepted")
    body_path = repo_root / (artifact.body_path or "")
    try:
        validate_report_artifact_path(repo_root, body_path)
    except ValueError:
        raise MemoEvidenceError("memo_body_path_outside_retained_state") from None
    raw = body_path.read_bytes() if artifact.body_path and body_path.is_file() else b""
    if not raw or _sha(raw) != artifact.body_sha256:
        reasons.add("memo_body_missing_or_changed")
    standalone = repo_root / artifact.standalone_path
    try:
        validate_report_artifact_path(repo_root, standalone)
    except ValueError:
        raise MemoEvidenceError("memo_standalone_path_outside_retained_state") from None
    if not standalone.is_file() or _sha(standalone.read_bytes()) != artifact.workspace_sha256:
        reasons.add("memo_standalone_missing_or_changed")
    elif raw:
        reader = extract_legacy_reader_body(
            standalone.read_text(encoding="utf-8"), artifact_id=artifact.artifact_id
        )
        if reader.body_sha256 != artifact.body_sha256:
            reasons.add("memo_reader_body_parity_failed")
    soup = BeautifulSoup(raw, "html.parser")
    supporting_contexts = (
        {context.context_id: context for context in context_review.supporting_contexts}
        if isinstance(context_review, MemoContextReviewV2)
        else {}
    )
    references: dict[tuple[str, str], FinancialEvidenceReference] = {}
    used_contexts: set[str] = set()
    for anchor in soup.select('a[href*="/api/peek/canonical-financial?"]'):
        try:
            href = str(anchor.get("href", ""))
            value = parse_qs(urlsplit(href).query)["reference"][0]
            reference = FinancialEvidenceReference.model_validate_json(value)
            context_id = str(anchor.get("data-memo-evidence-context", "primary"))
            if context_id != "primary" and context_id not in supporting_contexts:
                raise MemoEvidenceError("memo_financial_reference_context_unknown")
            ticker = (
                artifact.ticker
                if context_id == "primary"
                else supporting_contexts[context_id].ticker
            )
            if reference.ticker != ticker or reference.as_of > cutoff:
                raise MemoEvidenceError("memo_financial_reference_identity_mismatch")
            cell = _read_memo_financial_cell(conn, reference)
            if cell is None:
                raise MemoEvidenceError("memo_financial_reference_not_admitted")
            # A chip on a row label supplies provenance for a calculated
            # growth series. A chip inside a numeric cell must reconstruct
            # that reported number under the renderer's display contract.
            td = anchor.find_parent("td")
            classes = td.get("class") if td is not None else None
            if td is not None and classes is not None and "num" in classes:
                if cell.display_value is None:
                    raise MemoEvidenceError("memo_reader_value_unavailable")
                expected = financial_reader_value(cell.display_value, reference.concept)
                visible = "".join(
                    str(item) for item in td.find_all(string=True, recursive=False)
                ).strip()
                if visible != expected:
                    raise MemoEvidenceError("memo_reader_value_mismatch")
            references[(context_id, reference.model_dump_json())] = reference
            if context_id != "primary":
                used_contexts.add(context_id)
        except MemoEvidenceError as exc:
            reasons.add(exc.reason_code)
        except (ValueError, KeyError, IndexError, sqlite3.Error):
            reasons.add("memo_financial_reference_not_admitted")
    if not any(context_id == "primary" for context_id, _ref in references):
        reasons.add("memo_canonical_financial_references_missing")

    readiness_profile: Literal["complete_report_evidence/v1"] | None = None
    report_evidence: MemoReportEvidencePopulation | None = None
    historical_bounded = False
    earnings = context_review.earnings if isinstance(context_review, MemoContextReviewV2) else None
    earnings_verified = False
    snapshot_id = context_review.research_snapshot_id if context_review is not None else None
    member_sha: str | None = None
    reconstructed = 0
    if context_review is None:
        reasons.add("memo_claim_context_review_missing")
    else:
        if earnings is not None:
            # Revalidate the external review, including callers that used model_copy.
            context_review = MemoContextReviewV2.model_validate(context_review.model_dump())
        if (
            context_review.artifact_id != artifact.artifact_id
            or context_review.body_sha256 != artifact.body_sha256
            or context_review.reviewed_at > cutoff
            or context_review.reviewed_at < artifact.generated_at
            or sorted(context_review.reviewed_section_ids) != sorted(artifact.section_ids)
        ):
            reasons.add("memo_claim_context_review_mismatch")
        try:
            admission = verify_research_snapshot(conn, context_review.research_snapshot_id)
            row = conn.execute(
                "SELECT request_json FROM research_snapshot_headers WHERE research_snapshot_id=?",
                (context_review.research_snapshot_id,),
            ).fetchone()
            if row is None:
                raise MemoEvidenceError("memo_research_snapshot_missing")
            snapshot = ResearchSnapshotRequest.model_validate_json(str(row[0]))
            analysis = snapshot.research_universe.analysis_scope
            readiness_profile = None if analysis is None else analysis.request.readiness_profile
            if (
                analysis is not None
                and analysis.request.report_pdf_tables
                and (snapshot.recorded_at > artifact.generated_at)
            ):
                raise MemoEvidenceError("memo_report_source_population_not_fixed_before_generation")
            historical_bounded = (
                analysis is not None and analysis.request.historical_reconstruction is not None
            )
            if earnings is not None:
                verify_nu_historical_context(earnings, artifact, snapshot)
            if (
                historical_bounded
                and earnings is None
                and (readiness_profile is None or artifact.ticker != "MELI")
            ):
                reasons.add("historical_report_evidence_recipe_absent")
            if readiness_profile is None and context_review.report_evidence is not None:
                raise MemoEvidenceError("memo_report_evidence_profile_required")
            if (
                earnings is None
                and readiness_profile is not None
                and context_review.report_evidence is None
            ):
                raise MemoEvidenceError("memo_report_evidence_population_missing")
            if snapshot.recorded_at > cutoff or snapshot.cutoff_at > cutoff:
                raise MemoEvidenceError("memo_research_snapshot_after_cutoff")
            member_sha = admission.member_set_sha256
            snapshots = {"primary": (snapshot, artifact.ticker)}
            supporting_issuers: set[str] = set()
            for context_id, context in supporting_contexts.items():
                if (
                    context.issuer_id == snapshot.research_universe.issuer_id
                    or context.ticker == artifact.ticker
                ):
                    raise MemoEvidenceError("memo_supporting_primary_issuer_forbidden")
                if context.issuer_id in supporting_issuers:
                    raise MemoEvidenceError("memo_supporting_issuer_population_invalid")
                supporting_issuers.add(context.issuer_id)
                snapshots[context_id] = (
                    verify_memo_supporting_context(conn, context, cutoff),
                    context.ticker,
                )
            if isinstance(context_review, MemoContextReviewV2):
                documents = tuple(
                    context_snapshot.research_universe.document_version_ids
                    for context_snapshot, _ticker in snapshots.values()
                )
                reconstructed = verify_memo_composed_source_bytes(conn, documents, source_context)
            else:
                reconstructed = _raw_documents(
                    conn, snapshot.research_universe.document_version_ids
                )
            verify_memo_claim_population(soup, context_review)
            verify_memo_section_population(soup, artifact.section_ids)
            for (context_id, _reference_key), reference in references.items():
                context_snapshot, context_ticker = snapshots[context_id]
                _verify_report_pdf_observation(
                    conn, reference.observation_id, snapshot, cutoff=reference.as_of
                )
                verify_memo_snapshot_reference(
                    conn, reference, context_snapshot, context_ticker, cutoff
                )
            has_model_values = any(
                isinstance(claim, ReviewedMemoClaimV2) and claim.model_values
                for claim in context_review.claims
            )
            verified_model = None
            if isinstance(context_review, MemoContextReviewV2):
                if has_model_values != (context_review.model is not None):
                    raise MemoEvidenceError("memo_model_reference_population_mismatch")
                if context_review.model is not None:
                    verified_model = load_verified_memo_model(
                        conn,
                        context_review.model,
                        primary_snapshot_id=context_review.research_snapshot_id,
                        primary_member_sha256=member_sha,
                        ticker=artifact.ticker,
                        as_of=cutoff,
                        source_context=source_context,
                    )
            verified_claim_ids, verified_model_claim_ids = verify_memo_claims(
                conn,
                review=context_review,
                ticker=artifact.ticker,
                snapshot=snapshot,
                snapshots=snapshots,
                supporting_contexts=supporting_contexts,
                used_contexts=used_contexts,
                cutoff=cutoff,
                verified_model=verified_model,
                earnings=earnings,
            )
            if isinstance(context_review, MemoContextReviewV2) and earnings is not None:
                _verify_nu_earnings(
                    conn,
                    repo_root=repo_root,
                    artifact=artifact,
                    review=context_review,
                    snapshot=snapshot,
                    verified_claim_ids=frozenset(verified_claim_ids),
                    source_context=source_context,
                )
                earnings_verified = True
            if not earnings_verified:
                report_evidence = verify_memo_report_evidence(
                    context_review,
                    scope_request=None if analysis is None else analysis.request,
                    ticker=artifact.ticker,
                    verified_claim_ids=frozenset(verified_claim_ids),
                    verified_model_claim_ids=frozenset(verified_model_claim_ids),
                )
                _verify_report_source_population(conn, snapshot, context_review, cutoff)
            if used_contexts != set(supporting_contexts):
                raise MemoEvidenceError("memo_supporting_context_population_mismatch")
        except MemoEvidenceError as exc:
            reasons.add(exc.reason_code)
        except NuEarningsEvidenceError as exc:
            reasons.add(exc.reason_code)
        except MemoModelEvidenceError as exc:
            reasons.add(exc.reason_code)
        except MemoClaimSupportError as exc:
            reasons.add(exc.reason_code)
        except (ValueError, RuntimeError, sqlite3.Error, OSError):
            reasons.add("memo_source_context_reconstruction_failed")

    valuation_run_id: int | None = None
    if not earnings_verified:
        valuation = load_valuation_readiness(
            conn,
            artifact.ticker,
            as_of=cutoff,
            purpose="analyst_memo",
            source_context=source_context,
        )
        valuation_run_id = valuation.run_id
        if not valuation.ready:
            reasons.update(f"valuation_{reason}" for reason in valuation.reason_codes)
            if not valuation.reason_codes:
                reasons.add("memo_valuation_not_verified")
        # Ordinary investment memos retain the exact model/source closure.
        try:
            grade = load_dcf_verification_evidence(conn, artifact.ticker)
            receipt_raw = (grade.provenance or {}).get("model_input_receipt")
            if receipt_raw is None:
                raise MemoEvidenceError("memo_model_input_receipt_missing")
            receipt = ModelInputReceipt.model_validate(receipt_raw)
            if snapshot_id != receipt.request.research_snapshot_id:
                raise MemoEvidenceError("memo_model_source_universe_mismatch")
            verified, verified_member_sha, _inventories = verify_source_coverage(
                conn, receipt.request, cutoff
            )
            if verified_member_sha != member_sha:
                raise MemoEvidenceError("memo_model_source_commitment_mismatch")
            for fact in receipt.request.facts.values():
                _verify_report_pdf_observation(
                    conn, fact.observation_id, verified, cutoff=verified.cutoff_at
                )
            if not verified.research_universe.document_version_ids:
                raise MemoEvidenceError("memo_model_source_universe_empty")
        except MemoEvidenceError as exc:
            reasons.add(exc.reason_code)
        except (ValueError, RuntimeError, sqlite3.Error):
            reasons.add("memo_current_source_and_model_closure_unverified")
    return DecisionBriefReadiness(
        artifact_id=artifact.artifact_id,
        ticker=artifact.ticker,
        body_sha256=artifact.body_sha256,
        evaluated_at=cutoff,
        status="degraded" if reasons else "verified",
        decision_grade=not reasons and readiness_profile is None and not historical_bounded,
        reason_codes=tuple(sorted(reasons)),
        research_snapshot_id=snapshot_id,
        snapshot_member_sha256=member_sha,
        readiness_profile=readiness_profile,
        readiness_scope="historical_bounded"
        if historical_bounded
        else "bounded_report"
        if readiness_profile is not None
        else None,
        joint_mvp_status="not_verified"
        if readiness_profile is not None or historical_bounded
        else None,
        report_evidence_population=report_evidence,
        report_evidence_recipe=None if report_evidence is None else report_evidence.recipe,
        report_evidence_sha256=None
        if report_evidence is None
        else report_evidence.population_sha256,
        report_evidence_requirement_count=None
        if report_evidence is None
        else len(report_evidence.requirements),
        scope_limitation=(
            "Historical NU earnings verification covers the selected public-information set and qualitative valuation only. Whole-issuer decision grade and a joint DCF/workbook qualification are not claimed."
            if earnings_verified
            else "Historical reconstruction verifies only the selected public-information set when status is verified. Whole-filing/issuer decision grade and the joint brief, DCF, and recalculated-workbook deliverable remain unverified."
            if historical_bounded
            else "Complete selected report evidence is verified only when status is verified; whole filing/issuer coverage and native XBRL processing remain deferred. Analytical coverage mapping is reviewed, not an automated proof of semantic completeness. The joint brief and DCF deliverable is not verified; native recalculated-workbook parity remains required."
            if readiness_profile is not None
            else None
        ),
        financial_reference_count=len(references),
        reconstructed_document_count=reconstructed,
        valuation_run_id=valuation_run_id,
        context_review_sha256=_sha(context_review.model_dump_json().encode())
        if context_review
        else None,
    )


def assess_decision_brief(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    artifact: ReportArtifactRef,
    as_of: datetime,
    context_review: MemoContextReview | MemoContextReviewV2 | None = None,
    source_context: SourceReadContext | None = None,
) -> DecisionBriefReadiness:
    """Hold one read snapshot across every gate; preserve caller transactions."""
    if as_of.tzinfo is None:
        raise ValueError("memo cutoff must have a timezone")
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN")
    try:
        try:
            return _assess_decision_brief(
                conn,
                repo_root=repo_root,
                artifact=artifact,
                as_of=as_of,
                context_review=context_review,
                source_context=source_context,
            )
        except MemoEvidenceError as exc:
            return DecisionBriefReadiness(
                artifact_id=artifact.artifact_id,
                ticker=artifact.ticker,
                body_sha256=artifact.body_sha256,
                evaluated_at=as_of,
                reason_codes=(exc.reason_code,),
            )
        except (OSError, UnicodeError, ValueError, RuntimeError, sqlite3.Error):
            return DecisionBriefReadiness(
                artifact_id=artifact.artifact_id,
                ticker=artifact.ticker,
                body_sha256=artifact.body_sha256,
                evaluated_at=as_of,
                reason_codes=("memo_retained_evidence_unreadable_or_invalid",),
            )
    finally:
        if owns_snapshot and conn.in_transaction:
            conn.rollback()


def persist_decision_brief_readiness(repo_root: Path, receipt: DecisionBriefReadiness) -> Path:
    """Append a content-addressed proof without changing a historical report."""
    payload = receipt.model_dump_json(indent=2) + "\n"
    path = (
        repo_root
        / "output"
        / "research"
        / receipt.ticker
        / "artifacts"
        / receipt.artifact_id
        / "readiness"
        / f"{canonical_text_artifact_sha256(payload)}.json"
    )
    try:
        validate_report_artifact_path(repo_root, path)
    except ValueError:
        raise ValueError("readiness artifact escaped retained state") from None
    publish_text_no_clobber(path.resolve(), payload)
    return path
