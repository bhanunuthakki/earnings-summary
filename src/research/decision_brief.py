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
from provenance.evidence_native_candidates import (
    resolve_local_storage_uri,
    select_evidence_native_candidates_by_id,
)
from provenance.immutable_artifact import (
    canonical_text_artifact_sha256,
    publish_text_no_clobber,
    read_stable_artifact,
)
from provenance.issuer_registry import IssuerRegistry
from provenance.research_snapshot import ResearchSnapshotRequest, verify_research_snapshot
from provenance.sec_filing_xbrl_ingest import file_uri_path
from report.artifacts import ReportArtifactRef, validate_report_artifact_path
from report.legacy_body import extract_legacy_reader_body
from research.memo_claim_support import (
    MemoClaimSupportError,
    MemoReportedValue,
    financial_reader_value,
    verify_memo_numeric_population,
    verify_reported_memo_claim,
)
from research.memo_model_evidence import (
    MemoModelCommitment,
    MemoModelEvidenceError,
    MemoModelValue,
    load_verified_memo_model,
)
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


class ReviewedMemoClaimV2(ReviewedMemoClaim):
    supporting_evidence: tuple[MemoScopedSupport, ...] = ()
    model_values: tuple[MemoModelValue, ...] = ()


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
    for item in soup.select("script,style,.src-chip,.src-pop,button,select,option"):
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
    for selector, (_element, fragments) in groups.items():
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
        }
        result.append(
            MemoReaderBlock(
                block_id=_sha((selector + "\0" + text).encode()),
                selector=selector,
                text=text,
                presentation_only=presentation,
                visibly_inferred=text.startswith("Analyst inference:"),
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
            if claim.calculation is None and not model_values:
                raise MemoEvidenceError("memo_calculation_reconstruction_missing")
            try:
                verify_memo_numeric_population(
                    claim.passage,
                    (
                        *((claim.calculation.displayed_value,) if claim.calculation else ()),
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
            isinstance(claim, ReviewedMemoClaimV2) and claim.model_values
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


def verify_memo_snapshot_reference(
    conn: sqlite3.Connection,
    reference: FinancialEvidenceReference,
    snapshot: ResearchSnapshotRequest,
    ticker: str,
    cutoff: datetime,
) -> None:
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


def verify_memo_calculation(conn: sqlite3.Connection, calculation: MemoCalculation) -> None:
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
    if (
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
    supporting_contexts: tuple[MemoSupportingContext, ...] = Field(default=(), max_length=8)
    model: MemoModelCommitment | None = None

    @model_validator(mode="after")
    def distinct_contexts(self) -> MemoContextReviewV2:
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
        return MemoContextReview.model_validate(payload)
    return MemoContextReviewV2.model_validate(payload)


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


class DecisionBriefReadiness(_Closed):
    schema_version: Literal["decision_brief_readiness.v1"] = "decision_brief_readiness.v1"
    artifact_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,191}$")
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,31}$")
    body_sha256: str | None
    evaluated_at: AwareDatetime
    status: Literal["verified", "degraded"] = "degraded"
    decision_grade: bool = False
    reason_codes: tuple[str, ...]
    research_snapshot_id: str | None = None
    snapshot_member_sha256: str | None = None
    financial_reference_count: int = 0
    reconstructed_document_count: int = 0
    valuation_run_id: int | None = None
    context_review_sha256: str | None = None
    # Approval-bound portfolio fit and allocation are separate authorities.
    owner_thesis_required: Literal[False] = False


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

    snapshot_id = context_review.research_snapshot_id if context_review is not None else None
    member_sha: str | None = None
    reconstructed = 0
    if context_review is None:
        reasons.add("memo_claim_context_review_missing")
    else:
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
            for claim in context_review.claims:
                scoped_support = (
                    claim.supporting_evidence if isinstance(claim, ReviewedMemoClaimV2) else ()
                )
                model_values = claim.model_values if isinstance(claim, ReviewedMemoClaimV2) else ()
                if claim.kind not in {"analyst_inference", "presentation"} and not (
                    claim.evidence_node_ids
                    or claim.financial_references
                    or claim.calculation
                    or claim.values
                    or scoped_support
                    or model_values
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
                    verify_memo_snapshot_reference(
                        conn, reference, snapshot, artifact.ticker, cutoff
                    )
                all_nodes = list(claim.evidence_node_ids)
                all_values = list(claim.values)
                for support in scoped_support:
                    if support.context_id not in supporting_contexts:
                        raise MemoEvidenceError("memo_claim_context_unknown")
                    if not (
                        support.evidence_node_ids or support.financial_references or support.values
                    ):
                        raise MemoEvidenceError("memo_claim_context_support_empty")
                    used_contexts.add(support.context_id)
                    context_snapshot, context_ticker = snapshots[support.context_id]
                    for node_id in support.evidence_node_ids:
                        verify_memo_snapshot_node(conn, node_id, context_snapshot, cutoff)
                    for reference in support.financial_references + tuple(
                        value.reference for value in support.values
                    ):
                        verify_memo_snapshot_reference(
                            conn, reference, context_snapshot, context_ticker, cutoff
                        )
                    all_nodes.extend(support.evidence_node_ids)
                    all_values.extend(support.values)
                if claim.calculation is not None:
                    verify_memo_calculation(conn, claim.calculation)
                calculated_values = (
                    (claim.calculation.displayed_value,) if claim.calculation else ()
                )
                if model_values:
                    if verified_model is None:
                        raise MemoEvidenceError("memo_model_reference_population_mismatch")
                    calculated_values += tuple(
                        verified_model.display(value) for value in model_values
                    )
                if claim.kind in {"reported_fact", "management_claim", "calculation"}:
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
            if used_contexts != set(supporting_contexts):
                raise MemoEvidenceError("memo_supporting_context_population_mismatch")
        except MemoEvidenceError as exc:
            reasons.add(exc.reason_code)
        except MemoModelEvidenceError as exc:
            reasons.add(exc.reason_code)
        except (ValueError, RuntimeError, sqlite3.Error, OSError):
            reasons.add("memo_source_context_reconstruction_failed")

    valuation = load_valuation_readiness(
        conn, artifact.ticker, as_of=cutoff, purpose="analyst_memo", source_context=source_context
    )
    if not valuation.ready:
        reasons.update(f"valuation_{reason}" for reason in valuation.reason_codes)
        if not valuation.reason_codes:
            reasons.add("memo_valuation_not_verified")
    # The memo and valuation must use one source universe. A sealed but
    # unrelated current model cannot certify this report's evidence.
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
        decision_grade=not reasons,
        reason_codes=tuple(sorted(reasons)),
        research_snapshot_id=snapshot_id,
        snapshot_member_sha256=member_sha,
        financial_reference_count=len(references),
        reconstructed_document_count=reconstructed,
        valuation_run_id=valuation.run_id,
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
