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
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from dcf.grade_evidence import load_dcf_grade_evidence
from dcf.input_evidence import ModelInputReceipt, verify_source_coverage
from dcf.readiness import load_valuation_readiness
from provenance.immutable_artifact import canonical_text_artifact_sha256, publish_text_no_clobber
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


def verify_memo_claim_population(soup: BeautifulSoup, review: MemoContextReview) -> None:
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
            if claim.calculation is None:
                raise MemoEvidenceError("memo_calculation_reconstruction_missing")
            try:
                verify_memo_numeric_population(
                    claim.passage,
                    (
                        claim.calculation.displayed_value,
                        *(value.displayed_value for value in claim.values),
                    ),
                )
            except MemoClaimSupportError as exc:
                raise MemoEvidenceError(exc.reason_code) from exc
        elif claim.calculation is not None:
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
    context_review: MemoContextReview | None = None,
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
    references: dict[str, FinancialEvidenceReference] = {}
    for anchor in soup.select('a[href*="/api/peek/canonical-financial?"]'):
        try:
            href = str(anchor.get("href", ""))
            value = parse_qs(urlsplit(href).query)["reference"][0]
            reference = FinancialEvidenceReference.model_validate_json(value)
            if reference.ticker != artifact.ticker or reference.as_of > cutoff:
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
            references[reference.model_dump_json()] = reference
        except MemoEvidenceError as exc:
            reasons.add(exc.reason_code)
        except (ValueError, KeyError, IndexError, sqlite3.Error):
            reasons.add("memo_financial_reference_not_admitted")
    if not references:
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
            reconstructed = _raw_documents(conn, snapshot.research_universe.document_version_ids)
            verify_memo_claim_population(soup, context_review)
            verify_memo_section_population(soup, artifact.section_ids)
            for reference in references.values():
                verify_memo_snapshot_reference(conn, reference, snapshot, artifact.ticker, cutoff)
            for claim in context_review.claims:
                if claim.kind not in {"analyst_inference", "presentation"} and not (
                    claim.evidence_node_ids
                    or claim.financial_references
                    or claim.calculation
                    or claim.values
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
                if claim.calculation is not None:
                    verify_memo_calculation(conn, claim.calculation)
                if claim.kind in {"reported_fact", "management_claim", "calculation"}:
                    try:
                        verify_reported_memo_claim(
                            conn,
                            claim.passage,
                            evidence_node_ids=claim.evidence_node_ids,
                            values=claim.values,
                            calculated_values=(
                                (claim.calculation.displayed_value,) if claim.calculation else ()
                            ),
                        )
                    except MemoClaimSupportError as exc:
                        raise MemoEvidenceError(exc.reason_code) from exc
        except MemoEvidenceError as exc:
            reasons.add(exc.reason_code)
        except (ValueError, RuntimeError, sqlite3.Error, OSError):
            reasons.add("memo_source_context_reconstruction_failed")

    valuation = load_valuation_readiness(
        conn, artifact.ticker, as_of=cutoff, purpose="analyst_memo"
    )
    if not valuation.ready:
        reasons.update(f"valuation_{reason}" for reason in valuation.reason_codes)
        if not valuation.reason_codes:
            reasons.add("memo_valuation_not_verified")
    # The memo and valuation must use one source universe. A sealed but
    # unrelated current model cannot certify this report's evidence.
    try:
        grade = load_dcf_grade_evidence(conn, artifact.ticker)
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
    context_review: MemoContextReview | None = None,
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
