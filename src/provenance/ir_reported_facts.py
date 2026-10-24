"""Publish reviewed-coordinate IR PDF source rows in the existing fact plane.

This boundary preserves reported rows and their evidence. It does not approve a
KPI definition, resolve a source conflict, or claim document financial recall.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from llm.structured import parse_json_payload
from models.facts import Currency, Unit
from pipeline.kpi_semantics import (
    KpiUnitScale,
    normalize_source_numeric,
    parse_source_numeric,
    source_numeric_token_occurs,
    validate_admitted_unit_scale,
)
from pipeline.locators import locate_char_span
from provenance.document_processing_evidence import (
    GovernedOCRPageProjection,
    resolve_governed_ocr_pages,
)
from provenance.evidence_ledger import EvidenceLedger, EvidenceLocator, EvidenceNode, ExtractionRun
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
    FactDimensionV2,
    ReportedFactObservationV2,
)
from provenance.fulltext_backfill import project_pdf_page_texts
from provenance.ocr_layout import OCRPageLayout, OCRWord
from provenance.reporting_entity_registry import ReportingEntityRegistry
from provenance.source_fact_publication import verify_source_fact_publication
from provenance.source_fact_repository import (
    PublicationReceipt,
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)
from provenance.source_fact_stream import publication_event_for_publication
from runtime.deterministic import canonical_json as _json
from runtime.deterministic import content_sha256 as _sha

_RECIPE = "ir-reported-facts.v1"


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class IrSourceDimension(BaseModel):
    """A source-qualified PDF dimension; the caller supplies its semantic axis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    axis_namespace: str = Field(min_length=1)
    axis_name: str = Field(min_length=1)
    member_label: str = Field(min_length=1)


class IrReportedCandidate(BaseModel):
    """One complete source row. Incomplete rows stay with the producer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_row_key: str = Field(min_length=1, max_length=256)
    page_number: int = Field(gt=0)
    source_quote: str = Field(min_length=1)
    raw_label: str = Field(min_length=1)
    full_wording: str = Field(min_length=1)
    source_value_text: str = Field(min_length=1)
    numeric_value: Decimal
    period_kind: Literal["instant", "duration"]
    period_start: datetime | None = None
    period_end: datetime
    fiscal_year: int = Field(ge=1, le=9999)
    fiscal_period: Literal["FY", "H1", "H2", "Q1", "Q2", "Q3", "Q4"]
    accounting_basis: Literal["us_gaap", "ifrs", "local_gaap", "management", "non_gaap"]
    consolidation_scope: Literal[
        "consolidated", "parent_only", "subsidiary", "segment", "security_specific"
    ]
    dimensions: tuple[IrSourceDimension, ...] = ()
    unit_key: Unit
    source_unit_scale: KpiUnitScale
    source_unit_text: str = Field(min_length=1)
    currency: Currency | None = None
    supersedes_observation_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator(
        "source_quote", "raw_label", "full_wording", "source_value_text", "source_unit_text"
    )
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source coordinate must not be blank")
        return value

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if (self.period_kind == "duration") != (self.period_start is not None):
            raise ValueError("source duration requires period_start; instant forbids it")
        if self.period_start is not None and self.period_start >= self.period_end:
            raise ValueError("source duration start must precede its end")
        if any(value.tzinfo is None for value in (self.period_end, self.period_start) if value):
            raise ValueError("source periods require aware timestamps")
        raw_numeric = parse_source_numeric(self.source_value_text)
        if not raw_numeric.is_finite() or not self.numeric_value.is_finite():
            raise ValueError("source numeric values must be finite")
        validate_admitted_unit_scale(self.unit_key, self.source_unit_scale)
        if (
            normalize_source_numeric(
                raw_numeric, unit=self.unit_key, unit_scale=self.source_unit_scale
            )
            != self.numeric_value
        ):
            raise ValueError("numeric value must equal the source token after its reviewed scale")
        if not source_numeric_token_occurs(
            self.source_value_text, self.source_quote, row_label=self.raw_label
        ):
            raise ValueError("source numeric token is absent from its quote")
        if self.full_wording not in self.source_quote:
            raise ValueError("full management wording must occur in the source quote")
        if self.consolidation_scope == "segment" and not self.dimensions:
            raise ValueError("segment fact requires a source-qualified dimension")
        if self.unit_key in {Unit.ACTUAL, Unit.THOUSANDS, Unit.MILLIONS, Unit.BILLIONS}:
            if self.currency is None:
                raise ValueError("monetary source row requires currency")
        elif self.currency is not None:
            raise ValueError("non-monetary source row cannot carry currency")
        if len({(item.axis_namespace, item.axis_name) for item in self.dimensions}) != len(
            self.dimensions
        ):
            raise ValueError("source dimensions must have unique axes")
        return self


class IrReportedPublicationRequest(BaseModel):
    """One exact document/version and bounded candidate population."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_version_id: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject_binding_revision_id: str = Field(min_length=1, max_length=128)
    reporting_entity_id: str = Field(min_length=1, max_length=128)
    scope_security_id: str | None = Field(default=None, min_length=1, max_length=128)
    concept_namespace: str = Field(min_length=1)
    extractor_name: str = Field(min_length=1, max_length=128)
    extractor_code_version: str = Field(min_length=1, max_length=255)
    extractor_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_population_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_node_id: str = Field(min_length=1, max_length=128)
    extraction_started_at: datetime
    extraction_completed_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    candidates: tuple[IrReportedCandidate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid_population(self) -> Self:
        clocks = (
            self.extraction_started_at,
            self.extraction_completed_at,
            self.knowledge_at,
            self.recorded_at,
        )
        if any(value.tzinfo is None for value in clocks):
            raise ValueError("publication clocks must be timezone-aware")
        if not (
            self.extraction_started_at
            <= self.extraction_completed_at
            <= self.knowledge_at
            <= self.recorded_at
        ):
            raise ValueError("publication clocks are inconsistent")
        if len({item.source_row_key for item in self.candidates}) != len(self.candidates):
            raise ValueError("source row keys must be unique within one population")
        if any(item.period_end > self.knowledge_at for item in self.candidates):
            raise ValueError("source period cannot follow knowledge time")
        return self


class IrRawExchange(BaseModel):
    """One retained governed response, including the exact raw bytes as text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_response: str = Field(min_length=1)
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    transport: str = Field(min_length=1)
    llm_call_id: int = Field(gt=0)

    @property
    def exchange_sha256(self) -> str:
        """Bind a governed call, not merely its potentially repeated response body."""
        return _sha(
            [
                self.llm_call_id,
                self.prompt_sha256,
                self.response_sha256,
                self.model,
                self.provider,
                self.transport,
            ]
        )

    @model_validator(mode="after")
    def _exact_response(self) -> Self:
        if hashlib.sha256(self.raw_response.encode()).hexdigest() != self.response_sha256:
            raise ValueError("IR raw response hash does not match")
        return self


class IrRawRow(BaseModel):
    """One unreviewed row from a retained response and exact native PDF page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int = Field(gt=0)
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exchange_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_row_ordinal: int = Field(ge=0)
    raw_row: dict[str, object]
    native_source_quote: str = Field(min_length=1)
    native_char_start: int = Field(ge=0)
    native_char_end: int = Field(gt=0)


class IrOCRContextRegion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_representation: Literal["governed_ocr"] = "governed_ocr"
    page_number: int = Field(gt=0)
    exact_quote: str = Field(min_length=1)
    field_names: tuple[str, ...]
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    words: tuple[OCRWord, ...] = Field(min_length=1)


class IrNativeContextRegion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_representation: Literal["native_pdf_text"] = "native_pdf_text"
    page_number: int = Field(gt=0)
    exact_quote: str = Field(min_length=1)
    field_names: tuple[str, ...]
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)


class IrOCRRawRow(BaseModel):
    """Candidate-only source region on an exact retained OCR raster."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_representation: Literal["governed_ocr"] = "governed_ocr"
    page_number: int = Field(gt=0)
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exchange_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_row_ordinal: int = Field(ge=0)
    raw_row: dict[str, object]
    ocr_source_quote: str = Field(min_length=1)
    ocr_char_start: int = Field(ge=0)
    ocr_char_end: int = Field(gt=0)
    words: tuple[OCRWord, ...] = Field(min_length=1)
    ocr_page_manifest: dict[str, object]
    context_regions: tuple[IrOCRContextRegion | IrNativeContextRegion, ...] = ()


class IrOCRRetentionSelection(BaseModel):
    """Exact as-known OCR evidence coordinates for this candidate population."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_version_id: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_seal_id: str = Field(min_length=1, max_length=128)
    extraction_run_id: str = Field(min_length=1, max_length=128)
    page_result_ids: tuple[str, ...] = Field(min_length=1)
    cutoff_at: datetime

    @field_validator("cutoff_at")
    @classmethod
    def _aware_cutoff(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("IR OCR candidate cutoff must include a timezone")
        return value


class IrCandidateRetentionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_version_id: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extractor_name: str = Field(min_length=1, max_length=128)
    extractor_code_version: str = Field(min_length=1, max_length=255)
    extractor_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extraction_started_at: datetime
    extraction_completed_at: datetime
    responses: tuple[IrRawExchange, ...] = Field(min_length=1)
    rows: tuple[IrRawRow | IrOCRRawRow, ...]

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if any(
            value.tzinfo is None
            for value in (self.extraction_started_at, self.extraction_completed_at)
        ):
            raise ValueError("IR extraction clocks require timezones")
        if self.extraction_completed_at < self.extraction_started_at:
            raise ValueError("IR extraction completion precedes start")
        keys = [
            (row.page_number, row.exchange_sha256, row.response_row_ordinal) for row in self.rows
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("IR source row coordinates repeat")
        return self


class IrCandidatePopulation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_version_id: str
    source_sha256: str
    extraction_run_id: str
    extractor_config_sha256: str
    candidate_population_sha256: str
    candidate_node_ids: tuple[str, ...]
    retained_node_count: int
    exact_replay: bool


class IrSourceWitness(BaseModel):
    """A reviewed field interpretation tied to visible source text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_name: Literal[
        "source_definition",
        "period_kind",
        "period_start",
        "period_end",
        "fiscal_year",
        "fiscal_period",
        "unit_key",
        "source_unit_scale",
        "currency",
        "accounting_basis",
        "consolidation_scope",
        "dimensions",
    ]
    page_number: int = Field(gt=0)
    exact_quote: str = Field(min_length=1)


class IrReviewedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_node_id: str = Field(min_length=1)
    disposition: Literal["accepted", "rejected"]
    fact: IrReportedCandidate | None = None
    witnesses: tuple[IrSourceWitness, ...] = ()
    reason_code: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _disposition(self) -> Self:
        if self.disposition == "accepted":
            if self.fact is None or self.reason_code is not None:
                raise ValueError("accepted IR candidate requires exact reviewed coordinates")
            required = {
                "source_definition",
                "period_kind",
                "period_end",
                "fiscal_year",
                "fiscal_period",
                "unit_key",
                "source_unit_scale",
                "accounting_basis",
                "consolidation_scope",
            }
            if self.fact.period_start is not None:
                required.add("period_start")
            if self.fact.currency is not None:
                required.add("currency")
            if self.fact.dimensions:
                required.add("dimensions")
            if not required.issubset({witness.field_name for witness in self.witnesses}):
                raise ValueError("accepted IR candidate lacks field-specific source witnesses")
        elif self.fact is not None or self.witnesses or self.reason_code is None:
            raise ValueError("rejected IR candidate requires only a reason code")
        return self


class IrSourceContextReview(BaseModel):
    """A sealed manual review of the exact retained candidate population."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["ir-pdf-source-context-review.v1"] = "ir-pdf-source-context-review.v1"
    document_version_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extraction_run_id: str = Field(min_length=1)
    extractor_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_population_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_identity: str = Field(min_length=1)
    review_method: Literal["manual_source_context_review"]
    reviewed_at: datetime
    review_evidence: str = Field(min_length=1)
    entries: tuple[IrReviewedCandidate, ...] = Field(min_length=1)
    review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _seal(self) -> Self:
        if self.reviewed_at.tzinfo is None:
            raise ValueError("IR review time requires a timezone")
        if len({item.candidate_node_id for item in self.entries}) != len(self.entries):
            raise ValueError("IR review repeats a candidate")
        payload = self.model_dump(mode="json", exclude={"review_sha256"})
        if self.review_sha256 != _sha(payload):
            raise ValueError("IR review payload SHA-256 does not match")
        return self


def _source_document(
    conn: sqlite3.Connection,
    document_version_id: str,
    source_sha256: str,
    *,
    historical_revalidation: bool = False,
) -> None:
    row = conn.execute(
        "SELECT version.blob_sha256,version.document_type,source.source_kind,blob.media_type "
        "FROM evidence_document_versions version "
        "JOIN evidence_source_observations source ON source.observation_id=version.observation_id "
        "JOIN evidence_content_blobs blob ON blob.sha256=version.blob_sha256 "
        "WHERE version.document_version_id=?",
        (document_version_id,),
    ).fetchone()
    allowed_types = (
        {"ir_supplement", "ir_press_release"} if historical_revalidation else {"ir_supplement"}
    )
    if (
        row is None
        or row[0] != source_sha256
        or row[1] not in allowed_types
        or tuple(row[2:]) != ("ir_doc", "application/pdf")
    ):
        raise ValueError("IR PDF document version, bytes, or source type is not exact")


def _retained_graph(
    request: IrCandidateRetentionRequest,
) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    responses = tuple(
        (
            response.exchange_sha256,
            response.response_sha256,
            response.model,
            response.provider,
            response.transport,
            response.prompt_sha256,
        )
        for response in request.responses
    )
    rows = tuple(row.model_dump(mode="json") for row in request.rows)
    output_sha = _sha([responses, rows])
    run_id = "ir-pdf-extraction:" + _sha(
        [
            request.document_version_id,
            request.source_sha256,
            request.extractor_name,
            request.extractor_code_version,
            request.extractor_config_sha256,
            output_sha,
        ]
    )
    nodes = tuple(
        (
            "ir-pdf-candidate:"
            + _sha([run_id, row.page_number, row.exchange_sha256, row.response_row_ordinal]),
            _json(
                {
                    "schema_version": (
                        "ir-pdf-unreviewed-ocr-candidate.v1"
                        if isinstance(row, IrOCRRawRow)
                        else "ir-pdf-unreviewed-candidate.v3"
                    ),
                    "row": row.model_dump(mode="json"),
                    "response_node_id": "ir-pdf-response:" + _sha([run_id, row.exchange_sha256]),
                    "recurrence": "unknown",
                    "promotion_status": "pending_review",
                }
            ),
        )
        for row in request.rows
    )
    return run_id, output_sha, nodes


def _validate_native_span(row: IrRawRow, page_text: str) -> None:
    quote = row.raw_row.get("source_excerpt")
    if not isinstance(quote, str) or not quote:
        raise ValueError("IR candidate quote is absent from native PDF page")
    span = locate_char_span(quote, page_text, whitespace_only=True)
    if (
        span != (row.native_char_start, row.native_char_end)
        or page_text[row.native_char_start : row.native_char_end] != row.native_source_quote
    ):
        raise ValueError("IR native quote span is missing, ambiguous, or changed")


def _exact_ocr_words(
    projection: GovernedOCRPageProjection, quote: str, start: int, end: int
) -> tuple[OCRWord, ...]:
    if not quote or projection.text[start:end] != quote:
        raise ValueError("IR OCR quote is missing, ambiguous, or changed")
    words = OCRPageLayout.model_validate_json(projection.layout_json).words
    selected = tuple(word for word in words if word.char_start < end and word.char_end > start)
    if (
        not selected
        or selected[0].char_start != start
        or selected[-1].char_end != end
        or any(right.ordinal != left.ordinal + 1 for left, right in pairwise(selected))
    ):
        raise ValueError("IR OCR quote is not an exact word-bounded region")
    return selected


def _validate_ocr_span(
    row: IrOCRRawRow,
    pages: dict[int, GovernedOCRPageProjection],
    native_pages: tuple[str, ...],
    selection: IrOCRRetentionSelection,
) -> None:
    projection = pages.get(row.page_number)
    if projection is None:
        raise ValueError("IR OCR candidate page is absent from selected projection")
    quote = row.raw_row.get("source_excerpt")
    if quote != row.ocr_source_quote or row.words != _exact_ocr_words(
        projection, row.ocr_source_quote, row.ocr_char_start, row.ocr_char_end
    ):
        raise ValueError("IR OCR candidate word region differs from its retained layout")
    layout = OCRPageLayout.model_validate_json(projection.layout_json)
    manifest = row.ocr_page_manifest
    expected = {
        "source_representation": "governed_ocr",
        "document_version_id": projection.document_version_id,
        "source_sha256": projection.source_sha256,
        "evidence_seal_id": projection.evidence_seal_id,
        "extraction_run_id": projection.extraction_run_id,
        "page_result_id": projection.page_result_id,
        "page_node_id": projection.page_node_id,
        "layout_node_id": projection.layout_node_id,
        "layout_sha256": projection.layout_sha256,
        "text_sha256": projection.text_sha256,
        "engine_config_sha256": projection.engine_config_sha256,
        "tsv_sha256": layout.tsv_sha256,
        "render_sha256": layout.render_sha256,
        "coordinate_space": layout.version,
        "cutoff_at": selection.cutoff_at.isoformat(),
    }
    if manifest != expected:
        raise ValueError("IR OCR candidate manifest differs from selected projection")
    raw_witnesses = row.raw_row.get("context_witnesses", [])
    if not isinstance(raw_witnesses, list):
        raise ValueError("IR OCR candidate context witness population differs")
    witness_list = cast("list[object]", raw_witnesses)
    if len(witness_list) != len(row.context_regions):
        raise ValueError("IR OCR candidate context witness population differs")
    for raw, region in zip(witness_list, row.context_regions, strict=True):
        if not isinstance(raw, dict):
            raise ValueError("IR OCR candidate context witness differs from raw response")
        raw_object = cast("dict[str, object]", raw)
        raw_fields = raw_object.get("fields")
        if (
            raw_object.get("page") != region.page_number
            or raw_object.get("quote") != region.exact_quote
            or not isinstance(raw_fields, list)
            or tuple(cast("list[object]", raw_fields)) != region.field_names
        ):
            raise ValueError("IR OCR candidate context witness differs from raw response")
        if isinstance(region, IrOCRContextRegion):
            context_page = pages.get(region.page_number)
            if context_page is None or region.words != _exact_ocr_words(
                context_page, region.exact_quote, region.char_start, region.char_end
            ):
                raise ValueError("IR OCR candidate context region differs from retained layout")
        elif (
            region.page_number in pages
            or region.page_number > len(native_pages)
            or locate_char_span(
                region.exact_quote, native_pages[region.page_number - 1], whitespace_only=True
            )
            != (region.char_start, region.char_end)
        ):
            raise ValueError("IR OCR candidate native context region differs from source PDF")


def _candidate_locator(document_version_id: str, row: IrRawRow | IrOCRRawRow) -> EvidenceLocator:
    if isinstance(row, IrOCRRawRow):
        return EvidenceLocator(source_ref=document_version_id, page_number=row.page_number)
    return EvidenceLocator(
        source_ref=document_version_id,
        page_number=row.page_number,
        char_start=row.native_char_start,
        char_end=row.native_char_end,
    )


def _selected_ocr_candidate_pages(
    conn: sqlite3.Connection,
    request: IrCandidateRetentionRequest,
    native_page_count: int,
    *,
    repo_root: Path | None,
    selection: IrOCRRetentionSelection | None,
) -> tuple[GovernedOCRPageProjection, ...]:
    if selection is None:
        if any(isinstance(row, IrOCRRawRow) for row in request.rows):
            raise ValueError("IR OCR candidate requires exact governed selection")
        return ()
    if (
        repo_root is None
        or selection.document_version_id != request.document_version_id
        or selection.source_sha256 != request.source_sha256
        or len(set(selection.page_result_ids)) != len(selection.page_result_ids)
    ):
        raise ValueError("IR OCR selection differs from candidate source or page population")
    pages = resolve_governed_ocr_pages(
        conn,
        repo_root=repo_root,
        document_version_id=selection.document_version_id,
        source_sha256=selection.source_sha256,
        evidence_seal_id=selection.evidence_seal_id,
        extraction_run_id=selection.extraction_run_id,
        page_result_ids=selection.page_result_ids,
        cutoff_at=selection.cutoff_at,
    )
    if (
        not pages
        or tuple(page.page_result_id for page in pages) != selection.page_result_ids
        or len({page.page_number for page in pages}) != len(pages)
        or any(
            page.page_number > native_page_count
            or page.document_version_id != request.document_version_id
            or page.source_sha256 != request.source_sha256
            or page.evidence_seal_id != selection.evidence_seal_id
            or page.extraction_run_id != selection.extraction_run_id
            for page in pages
        )
    ):
        raise ValueError("IR OCR resolved pages differ from candidate source or selection")
    return pages


def retain_ir_pdf_candidates(
    conn: sqlite3.Connection,
    request: IrCandidateRetentionRequest,
    *,
    raw_pdf_bytes: bytes,
    page_projection: Callable[[bytes], tuple[str, ...]] | None = None,
    precommit_validation: Callable[[], None] | None = None,
    caller_transaction: bool = False,
    require_existing: bool = False,
    historical_revalidation: bool = False,
    ocr_repo_root: Path | None = None,
    ocr_selection: IrOCRRetentionSelection | None = None,
) -> IrCandidatePopulation:
    """Retain exact unreviewed PDF candidates; never create financial facts."""
    if conn.in_transaction != caller_transaction:
        raise ValueError("IR candidate retention transaction ownership differs")
    request = IrCandidateRetentionRequest.model_validate(request.model_dump(mode="python"))
    if ocr_selection is not None:
        ocr_selection = IrOCRRetentionSelection.model_validate(
            ocr_selection.model_dump(mode="python")
        )
    if hashlib.sha256(raw_pdf_bytes).hexdigest() != request.source_sha256:
        raise ValueError("IR PDF raw source hash changed")
    pages = (page_projection or project_pdf_page_texts)(raw_pdf_bytes)
    ocr_pages = _selected_ocr_candidate_pages(
        conn, request, len(pages), repo_root=ocr_repo_root, selection=ocr_selection
    )
    ocr_by_page = {page.page_number: page for page in ocr_pages}
    if len(ocr_by_page) != len(ocr_pages):
        raise ValueError("IR OCR selected pages repeat")
    responses = {item.exchange_sha256: item for item in request.responses}
    if len(responses) != len(request.responses):
        raise ValueError("IR governed responses repeat")
    for row in request.rows:
        response = responses.get(row.exchange_sha256)
        if response is None or row.page_number > len(pages):
            raise ValueError("IR candidate response or source page is missing")
        if response.response_sha256 != row.response_sha256:
            raise ValueError("IR candidate response body hash differs from exact exchange")
        parsed: object = parse_json_payload(response.raw_response, expect="array")
        if not isinstance(parsed, list):
            raise ValueError("IR candidate is absent from governed response")
        parsed_rows = cast("list[object]", parsed)
        if row.response_row_ordinal >= len(parsed_rows):
            raise ValueError("IR candidate is absent from governed response")
        if _json(parsed_rows[row.response_row_ordinal]) != _json(row.raw_row):
            raise ValueError("IR candidate differs from its governed response")
        if isinstance(row, IrOCRRawRow):
            if ocr_selection is None:
                raise ValueError("IR OCR candidate selection is unavailable")
            _validate_ocr_span(row, ocr_by_page, pages, ocr_selection)
        else:
            if row.page_number in ocr_by_page:
                raise ValueError("IR native candidate masks a selected OCR page")
            _validate_native_span(row, pages[row.page_number - 1])
    run_id, output_sha, nodes = _retained_graph(request)
    if not caller_transaction:
        conn.execute("BEGIN")
    try:
        _source_document(
            conn,
            request.document_version_id,
            request.source_sha256,
            historical_revalidation=historical_revalidation,
        )
        run = conn.execute(
            "SELECT document_version_id,input_sha256,extractor_name,extractor_code_version,"
            "extractor_config_sha256,output_sha256,started_at,completed_at,outcome "
            "FROM evidence_extraction_runs WHERE extraction_run_id=?",
            (run_id,),
        ).fetchone()
        if run is not None:
            expected = (
                request.document_version_id,
                request.source_sha256,
                request.extractor_name,
                request.extractor_code_version,
                request.extractor_config_sha256,
                output_sha,
                "succeeded",
            )
            if (*tuple(run[:6]), run[8]) != expected:
                raise ValueError("IR retained extraction replay differs from existing run")
            for response in request.responses:
                response_node_id = "ir-pdf-response:" + _sha([run_id, response.exchange_sha256])
                prior_response = conn.execute(
                    "SELECT text FROM evidence_nodes WHERE node_id=?", (response_node_id,)
                ).fetchone()
                if prior_response is None:
                    raise ValueError("IR retained governed response is missing")
                payload = json.loads(str(prior_response[0]))
                original = payload.get("response")
                if (
                    not isinstance(original, dict)
                    or payload.get("schema_version") != "ir-pdf-governed-response.v2"
                ):
                    raise ValueError("IR retained governed response changed")
                for field_name in (
                    "llm_call_id",
                    "raw_response",
                    "response_sha256",
                    "prompt_sha256",
                    "model",
                    "provider",
                    "transport",
                ):
                    if original.get(field_name) != getattr(response, field_name):
                        raise ValueError("IR retained governed response changed")
            for row, (node_id, text_value) in zip(request.rows, nodes, strict=True):
                prior = conn.execute(
                    "SELECT text,locator_json FROM evidence_nodes WHERE node_id=?", (node_id,)
                ).fetchone()
                locator = _candidate_locator(request.document_version_id, row)
                if prior is None or prior[0] != text_value or prior[1] != locator.canonical_json:
                    raise ValueError("IR retained candidate replay differs from existing evidence")
            if precommit_validation is not None:
                precommit_validation()
            if (
                _selected_ocr_candidate_pages(
                    conn, request, len(pages), repo_root=ocr_repo_root, selection=ocr_selection
                )
                != ocr_pages
            ):
                raise ValueError("IR OCR candidate replay projection changed")
            if not caller_transaction:
                conn.rollback()
            return IrCandidatePopulation(
                document_version_id=request.document_version_id,
                source_sha256=request.source_sha256,
                extraction_run_id=run_id,
                extractor_config_sha256=request.extractor_config_sha256,
                candidate_population_sha256=output_sha,
                candidate_node_ids=tuple(node_id for node_id, _ in nodes),
                retained_node_count=len(responses) + len(nodes),
                exact_replay=True,
            )
        if require_existing:
            raise ValueError("IR retained candidate population is unavailable")
        ledger = EvidenceLedger(conn)
        ledger.persist(
            ExtractionRun(
                extraction_run_id=run_id,
                idempotency_key=run_id,
                document_version_id=request.document_version_id,
                input_sha256=request.source_sha256,
                extractor_name=request.extractor_name,
                extractor_code_version=request.extractor_code_version,
                extractor_config_sha256=request.extractor_config_sha256,
                output_sha256=output_sha,
                started_at=request.extraction_started_at,
                completed_at=request.extraction_completed_at,
                outcome="succeeded",
            )
        )
        for response in request.responses:
            node_id = "ir-pdf-response:" + _sha([run_id, response.exchange_sha256])
            ledger.persist(
                EvidenceNode(
                    node_id=node_id,
                    evidence_key=node_id,
                    revision=1,
                    extraction_run_id=run_id,
                    node_kind="claim",
                    text=_json(
                        {
                            "schema_version": "ir-pdf-governed-response.v2",
                            "response": response.model_dump(mode="json"),
                        }
                    ),
                    recorded_at=request.extraction_completed_at,
                )
            )
        for row, (node_id, text_value) in zip(request.rows, nodes, strict=True):
            ledger.persist(
                EvidenceNode(
                    node_id=node_id,
                    evidence_key=node_id,
                    revision=1,
                    extraction_run_id=run_id,
                    parent_node_id="ir-pdf-response:" + _sha([run_id, row.exchange_sha256]),
                    node_kind="claim",
                    text=text_value,
                    locator=_candidate_locator(request.document_version_id, row),
                    recorded_at=request.extraction_completed_at,
                )
            )
        if precommit_validation is not None:
            precommit_validation()
        if (
            _selected_ocr_candidate_pages(
                conn, request, len(pages), repo_root=ocr_repo_root, selection=ocr_selection
            )
            != ocr_pages
        ):
            raise ValueError("IR OCR candidate projection changed before commit")
        for row in request.rows:
            if isinstance(row, IrOCRRawRow):
                if ocr_selection is None:
                    raise ValueError("IR OCR candidate selection is unavailable")
                _validate_ocr_span(row, ocr_by_page, pages, ocr_selection)
        if not caller_transaction:
            conn.commit()
        return IrCandidatePopulation(
            document_version_id=request.document_version_id,
            source_sha256=request.source_sha256,
            extraction_run_id=run_id,
            extractor_config_sha256=request.extractor_config_sha256,
            candidate_population_sha256=output_sha,
            candidate_node_ids=tuple(node_id for node_id, _ in nodes),
            retained_node_count=len(responses) + len(nodes),
            exact_replay=False,
        )
    except Exception:
        if not caller_transaction:
            conn.rollback()
        raise


def _review_candidate_rows(
    conn: sqlite3.Connection, population: IrCandidatePopulation
) -> dict[str, IrRawRow]:
    found: dict[str, IrRawRow] = {}
    for node_id in population.candidate_node_ids:
        row = conn.execute(
            "SELECT extraction_run_id,text,parent_node_id,locator_json FROM evidence_nodes WHERE node_id=?",
            (node_id,),
        ).fetchone()
        if row is None or row[0] != population.extraction_run_id:
            raise ValueError("IR reviewed candidate node is absent from exact extraction")
        payload = json.loads(str(row[1]))
        if payload.get("schema_version") != "ir-pdf-unreviewed-candidate.v3":
            raise ValueError("IR review candidate node is not a retained raw candidate")
        raw = IrRawRow.model_validate(payload["row"])
        locator = EvidenceLocator(
            source_ref=population.document_version_id,
            page_number=raw.page_number,
            char_start=raw.native_char_start,
            char_end=raw.native_char_end,
        )
        if row[3] != locator.canonical_json:
            raise ValueError("IR candidate native locator changed")
        expected_node_id = "ir-pdf-candidate:" + _sha(
            [
                population.extraction_run_id,
                raw.page_number,
                raw.exchange_sha256,
                raw.response_row_ordinal,
            ]
        )
        if node_id != expected_node_id:
            raise ValueError("IR candidate node identity changed")
        expected_parent = "ir-pdf-response:" + _sha(
            [population.extraction_run_id, raw.exchange_sha256]
        )
        if row[2] != expected_parent or payload.get("response_node_id") != expected_parent:
            raise ValueError("IR candidate raw response binding changed")
        response_row = conn.execute(
            "SELECT text FROM evidence_nodes WHERE node_id=? AND extraction_run_id=?",
            (expected_parent, population.extraction_run_id),
        ).fetchone()
        if response_row is None:
            raise ValueError("IR candidate raw response is absent")
        response_payload = json.loads(str(response_row[0]))
        if response_payload.get("schema_version") != "ir-pdf-governed-response.v2":
            raise ValueError("IR candidate raw response type changed")
        response = IrRawExchange.model_validate(response_payload["response"])
        if (
            response.response_sha256 != raw.response_sha256
            or response.exchange_sha256 != raw.exchange_sha256
        ):
            raise ValueError("IR candidate raw response exchange changed")
        parsed: object = parse_json_payload(response.raw_response, expect="array")
        if not isinstance(parsed, list):
            raise ValueError("IR candidate raw response is not an array")
        source_rows = cast("list[object]", parsed)
        if raw.response_row_ordinal >= len(source_rows) or _json(
            source_rows[raw.response_row_ordinal]
        ) != _json(raw.raw_row):
            raise ValueError("IR candidate no longer matches its raw response")
        found[node_id] = raw
    return found


def _validate_review(
    conn: sqlite3.Connection,
    review: IrSourceContextReview,
    population: IrCandidatePopulation,
    raw_pdf_bytes: bytes,
) -> None:
    if (
        review.document_version_id != population.document_version_id
        or review.source_sha256 != population.source_sha256
        or review.extraction_run_id != population.extraction_run_id
        or review.extractor_config_sha256 != population.extractor_config_sha256
        or review.candidate_population_sha256 != population.candidate_population_sha256
    ):
        raise ValueError("IR review does not bind the exact retained population")
    if hashlib.sha256(raw_pdf_bytes).hexdigest() != population.source_sha256:
        raise ValueError("IR review raw source hash changed")
    run_row = conn.execute(
        "SELECT completed_at FROM evidence_extraction_runs WHERE extraction_run_id=?",
        (population.extraction_run_id,),
    ).fetchone()
    if run_row is None or review.reviewed_at < _time(run_row[0]):
        raise ValueError("IR review precedes its retained source extraction")
    pages = project_pdf_page_texts(raw_pdf_bytes)
    raw_rows = _review_candidate_rows(conn, population)
    all_candidates: set[str] = set()
    for node_id, text_value in conn.execute(
        "SELECT node_id,text FROM evidence_nodes WHERE extraction_run_id=?",
        (population.extraction_run_id,),
    ):
        payload = json.loads(str(text_value))
        if payload.get("schema_version") == "ir-pdf-unreviewed-candidate.v3":
            all_candidates.add(str(node_id))
    if all_candidates != set(raw_rows):
        raise ValueError("IR review population omits retained candidates")
    if {item.candidate_node_id for item in review.entries} != set(raw_rows):
        raise ValueError("IR review must accept or reject every retained candidate")
    for entry in review.entries:
        raw = raw_rows[entry.candidate_node_id]
        if entry.disposition == "rejected":
            continue
        fact = entry.fact
        if fact is None:
            raise ValueError("accepted IR review lacks a fact")
        source = raw.raw_row
        if (
            source.get("period_role")
            not in {
                "current",
                "prior_period_comparator",
                "prior_year_comparator",
            }
            or source.get("range_bound") is not None
        ):
            raise ValueError(
                "IR guidance or unresolved forecast cannot publish as a reported actual"
            )
        if raw.page_number > len(pages):
            raise ValueError("IR candidate source page is missing")
        _validate_native_span(raw, pages[raw.page_number - 1])
        original_excerpt = raw.native_source_quote
        original_label = source.get("source_row_label") or source.get("label")
        if (
            fact.source_row_key != entry.candidate_node_id
            or fact.page_number != raw.page_number
            or original_excerpt not in fact.source_quote
            or fact.raw_label != original_label
            or fact.source_value_text != source.get("source_value_text")
            or fact.page_number > len(pages)
        ):
            raise ValueError("IR reviewed value does not match its retained raw candidate")
        if fact.source_quote not in pages[fact.page_number - 1]:
            raise ValueError("IR reviewed value quote is absent from source page")
        for witness in entry.witnesses:
            if (
                witness.page_number > len(pages)
                or witness.exact_quote not in pages[witness.page_number - 1]
            ):
                raise ValueError("IR reviewed context witness is absent from source page")
        if not any(
            witness.field_name == "source_unit_scale"
            and fact.source_unit_text in witness.exact_quote
            for witness in entry.witnesses
        ):
            raise ValueError("IR reviewed source unit wording lacks an exact scale witness")


def persist_ir_source_context_review(
    conn: sqlite3.Connection,
    review: IrSourceContextReview,
    population: IrCandidatePopulation,
    *,
    raw_pdf_bytes: bytes,
) -> str:
    """Record an explicit manual source-context review in the existing ledger."""
    if conn.in_transaction:
        raise ValueError("IR review requires an idle connection")
    review = IrSourceContextReview.model_validate(review.model_dump(mode="python"))
    conn.execute("BEGIN")
    try:
        _source_document(conn, population.document_version_id, population.source_sha256)
        _validate_review(conn, review, population, raw_pdf_bytes)
        existing_seal = conn.execute(
            "SELECT 1 FROM fact_extraction_run_completeness_seals_v2 WHERE extraction_run_id=?",
            (population.extraction_run_id,),
        ).fetchone()
        if existing_seal is not None:
            raise ValueError("IR extraction is already sealed; review cannot change")
        node_id = "ir-pdf-review:" + review.review_sha256
        prior = conn.execute(
            "SELECT text,extraction_run_id FROM evidence_nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        if prior is not None:
            if (
                prior[0] != _json(review.model_dump(mode="json"))
                or prior[1] != population.extraction_run_id
            ):
                raise ValueError("IR immutable source review replay differs")
            conn.rollback()
            return node_id
        ledger = EvidenceLedger(conn)
        ledger.persist(
            EvidenceNode(
                node_id=node_id,
                evidence_key=node_id,
                revision=1,
                extraction_run_id=population.extraction_run_id,
                node_kind="claim",
                text=_json(review.model_dump(mode="json")),
                recorded_at=review.reviewed_at,
            )
        )
        conn.commit()
        return node_id
    except Exception:
        conn.rollback()
        raise


def find_exact_ir_source_review(
    conn: sqlite3.Connection, population: IrCandidatePopulation, *, raw_pdf_bytes: bytes
) -> tuple[str, IrSourceContextReview] | None:
    """Find one pre-existing exact review; automatic capture never creates one."""
    matches: list[tuple[str, IrSourceContextReview]] = []
    for node_id, text_value in conn.execute(
        "SELECT node_id,text FROM evidence_nodes WHERE extraction_run_id=? AND node_kind='claim'",
        (population.extraction_run_id,),
    ):
        payload = json.loads(str(text_value))
        if payload.get("schema_version") != "ir-pdf-source-context-review.v1":
            continue
        review = IrSourceContextReview.model_validate(payload)
        if str(node_id) != "ir-pdf-review:" + review.review_sha256:
            raise ValueError("IR source review node identity changed")
        _validate_review(conn, review, population, raw_pdf_bytes)
        matches.append((str(node_id), review))
    if len(matches) > 1:
        raise ValueError("IR source review is ambiguous")
    return matches[0] if matches else None


def _document_subject(conn: sqlite3.Connection, request: IrReportedPublicationRequest) -> None:
    row = conn.execute(
        "SELECT version.issuer_id,version.blob_sha256,version.document_type,"
        "source.source_kind,blob.media_type "
        "FROM evidence_document_versions version "
        "JOIN evidence_source_observations source ON source.observation_id=version.observation_id "
        "JOIN evidence_content_blobs blob ON blob.sha256=version.blob_sha256 "
        "WHERE version.document_version_id=?",
        (request.document_version_id,),
    ).fetchone()
    if row is None or (
        row[1] != request.source_sha256
        or row[2] != "ir_supplement"
        or row[3] != "ir_doc"
        or row[4] != "application/pdf"
    ):
        raise ValueError("IR PDF document version, bytes, or source type is not exact")
    subject = ReportingEntityRegistry(conn).canonicalize_recorded_subject(
        str(row[0]), knowledge_at=request.knowledge_at
    )
    if (
        subject.material_dissent
        or subject.binding_revision_id != request.subject_binding_revision_id
        or subject.reporting_entity_id != request.reporting_entity_id
        or subject.security_id != request.scope_security_id
    ):
        raise ValueError("IR PDF recorded subject binding is not exact")


def _source_pages(request: IrReportedPublicationRequest, raw_bytes: bytes) -> tuple[str, ...]:
    if hashlib.sha256(raw_bytes).hexdigest() != request.source_sha256:
        raise ValueError("IR PDF raw source hash changed")
    pages = project_pdf_page_texts(raw_bytes)
    for item in request.candidates:
        if item.page_number > len(pages):
            raise ValueError("IR PDF source page is missing")
        page = pages[item.page_number - 1]
        if item.source_quote not in page or item.raw_label not in page:
            raise ValueError("IR PDF quote or raw label is not present on its source page")
        if any(dimension.member_label not in page for dimension in item.dimensions):
            raise ValueError("IR PDF dimension member is not present on its source page")
    return pages


def _dimension(
    item: IrSourceDimension, *, fact_cell_id: str, recorded_at: datetime
) -> FactDimensionV2:
    member = {"source_label": item.member_label}
    identifier = "ir-pdf-dimension:" + _sha(
        [fact_cell_id, item.axis_namespace, item.axis_name, member]
    )
    return FactDimensionV2(
        dimension_id=identifier,
        idempotency_key=identifier,
        axis_namespace=item.axis_namespace,
        axis_name=item.axis_name,
        member_kind="typed",
        typed_member_value=CanonicalJSONObject.model_validate(member),
        recorded_at=recorded_at,
    )


def _reported_fact(
    conn: sqlite3.Connection,
    request: IrReportedPublicationRequest,
    item: IrReportedCandidate,
    *,
    candidate_node_id: str,
    entry_sha256: str,
) -> ReportedSourceFact:
    dimensions = tuple(
        _dimension(value, fact_cell_id="pending", recorded_at=request.recorded_at)
        for value in item.dimensions
    )
    cell_payload = {
        "fact_cell_id": "pending",
        "idempotency_key": "pending",
        "reporting_entity_id": request.reporting_entity_id,
        "scope_security_id": request.scope_security_id,
        "concept_namespace": request.concept_namespace,
        "concept_name": item.raw_label,
        "taxonomy_name": "issuer-ir-pdf-source",
        "taxonomy_version": _RECIPE,
        "accounting_basis": item.accounting_basis,
        "consolidation_scope": item.consolidation_scope,
        "period_kind": item.period_kind,
        "period_start": item.period_start,
        "period_end": item.period_end,
        "fiscal_year": item.fiscal_year,
        "fiscal_period": item.fiscal_period,
        "dimensions": dimensions,
        "unit_key": item.unit_key.value,
        "currency": None if item.currency is None else item.currency.value,
        "effective_at": item.period_end,
        "knowledge_at": request.knowledge_at,
        "recorded_at": request.recorded_at,
    }
    candidate_cell = FactCellV2.model_validate(cell_payload)
    cell_id = "ir-pdf-cell:" + str(candidate_cell.semantic_key_sha256)
    cell_payload["dimensions"] = tuple(
        _dimension(value, fact_cell_id=cell_id, recorded_at=request.recorded_at)
        for value in item.dimensions
    )
    cell = FactCellV2.model_validate(
        {**cell_payload, "fact_cell_id": cell_id, "idempotency_key": cell_id}
    )
    if item.supersedes_observation_id is not None:
        prior = conn.execute(
            "SELECT fact_cell_id,observation_kind,recorded_at "
            "FROM fact_observations_v2 WHERE observation_id=?",
            (item.supersedes_observation_id,),
        ).fetchone()
        if prior is None or (
            prior[0] != cell_id or prior[1] != "reported" or _time(prior[2]) > request.knowledge_at
        ):
            raise ValueError("IR PDF correction parent is absent or has a different source cell")
    observation_id = "ir-pdf-observation:" + _sha(
        [request.document_version_id, item.source_row_key, entry_sha256]
    )
    locator = CanonicalJSONObject(
        root={
            "document_version_id": request.document_version_id,
            "source_sha256": request.source_sha256,
            "page_number": item.page_number,
            "source_quote": item.source_quote,
            "raw_label": item.raw_label,
            "full_wording": item.full_wording,
            "source_row_key": item.source_row_key,
            "source_unit_scale": item.source_unit_scale.value,
            "source_unit_text": item.source_unit_text,
            "raw_numeric_value": str(parse_source_numeric(item.source_value_text)),
            "candidate_node_id": candidate_node_id,
            "review_node_id": request.review_node_id,
        }
    )
    observation = ReportedFactObservationV2(
        observation_id=observation_id,
        idempotency_key=observation_id,
        fact_cell_id=cell_id,
        observation_kind="reported",
        value_kind="numeric",
        numeric_value=str(item.numeric_value),
        raw_lexical_value=item.source_value_text,
        method_name=request.extractor_name,
        method_version=request.extractor_code_version,
        method_config_sha256=request.extractor_config_sha256,
        revision_kind="correction" if item.supersedes_observation_id else "initial",
        supersedes_observation_id=item.supersedes_observation_id,
        effective_at=item.period_end,
        knowledge_at=request.knowledge_at,
        recorded_at=request.recorded_at,
        document_version_id=request.document_version_id,
        evidence_node_id=request.review_node_id,
        source_locator=locator,
        source_entry_sha256=entry_sha256,
        subject_binding_revision_id=request.subject_binding_revision_id,
        source_taxonomy_version=_RECIPE,
        source_unit_id=item.unit_key.value,
    )
    return ReportedSourceFact(cell=cell, observation=observation)


def publish_ir_reported_facts(
    conn: sqlite3.Connection,
    request: IrReportedPublicationRequest,
    *,
    raw_pdf_bytes: bytes,
    precommit_validation: Callable[[], None] | None = None,
) -> PublicationReceipt:
    """Publish only rows from one pre-existing, exact manual source review."""
    if conn.in_transaction:
        raise ValueError("IR PDF publication requires an idle connection")
    request = IrReportedPublicationRequest.model_validate(request.model_dump(mode="python"))
    _source_pages(request, raw_pdf_bytes)
    conn.execute("BEGIN")
    try:
        _document_subject(conn, request)
        review_row = conn.execute(
            "SELECT extraction_run_id,text,recorded_at FROM evidence_nodes WHERE node_id=?",
            (request.review_node_id,),
        ).fetchone()
        if review_row is None:
            raise ValueError("IR source-context review is absent")
        review = IrSourceContextReview.model_validate_json(str(review_row[1]))
        if request.review_node_id != "ir-pdf-review:" + review.review_sha256:
            raise ValueError("IR source-context review node identity changed")
        accepted = tuple(entry for entry in review.entries if entry.disposition == "accepted")
        if tuple(entry.fact for entry in accepted) != request.candidates:
            raise ValueError("IR publication differs from reviewed source coordinates")
        run_id = review.extraction_run_id
        run = conn.execute(
            "SELECT document_version_id,input_sha256,extractor_name,extractor_code_version,"
            "extractor_config_sha256,output_sha256,outcome,completed_at "
            "FROM evidence_extraction_runs WHERE extraction_run_id=?",
            (run_id,),
        ).fetchone()
        if run is None or tuple(run[:7]) != (
            request.document_version_id,
            request.source_sha256,
            request.extractor_name,
            request.extractor_code_version,
            request.extractor_config_sha256,
            request.candidate_population_sha256,
            "succeeded",
        ):
            raise ValueError("IR review does not match exact retained source extraction")
        if _time(run[7]) > review.reviewed_at or review.reviewed_at > request.knowledge_at:
            raise ValueError("IR source review is outside publication knowledge time")
        raw_candidate_ids = tuple(entry.candidate_node_id for entry in review.entries)
        population = IrCandidatePopulation(
            document_version_id=request.document_version_id,
            source_sha256=request.source_sha256,
            extraction_run_id=run_id,
            extractor_config_sha256=request.extractor_config_sha256,
            candidate_population_sha256=request.candidate_population_sha256,
            candidate_node_ids=raw_candidate_ids,
            retained_node_count=0,
            exact_replay=True,
        )
        _validate_review(conn, review, population, raw_pdf_bytes)
        facts: list[ReportedSourceFact] = []
        for entry in accepted:
            item = entry.fact
            if item is None:
                raise ValueError("IR accepted review lacks source coordinates")
            candidate_row = conn.execute(
                "SELECT text FROM evidence_nodes WHERE node_id=?", (entry.candidate_node_id,)
            ).fetchone()
            if candidate_row is None:
                raise ValueError("IR reviewed candidate disappeared")
            raw_candidate = IrRawRow.model_validate(json.loads(str(candidate_row[0]))["row"])
            facts.append(
                _reported_fact(
                    conn,
                    request,
                    item,
                    candidate_node_id=entry.candidate_node_id,
                    entry_sha256=_sha(raw_candidate.raw_row),
                )
            )
        if not facts:
            raise ValueError("IR review has no accepted source facts to publish")
        seal_id = "ir-pdf-extraction-seal:" + _sha([run_id, review.review_sha256])
        publication_id = "ir-pdf-source-publication:" + _sha(
            [run_id, review.review_sha256, [fact.observation.observation_id for fact in facts]]
        )
        prior = conn.execute(
            "SELECT 1 FROM source_fact_publications WHERE publication_id=?", (publication_id,)
        ).fetchone()
        if prior is not None:
            verified = verify_source_fact_publication(
                conn, publication_id=publication_id, cutoff=request.recorded_at
            )
            expected_observations = tuple(fact.observation.observation_id for fact in facts)
            members = tuple(
                member.record_id
                for member in verified.members
                if member.record_kind == "fact_observation"
            )
            if set(members) != set(expected_observations):
                raise ValueError("IR publication replay observation population changed")
            for observation_id in expected_observations:
                locator_row = conn.execute(
                    "SELECT source_locator_json FROM fact_observations_v2 WHERE observation_id=?",
                    (observation_id,),
                ).fetchone()
                if (
                    locator_row is None
                    or json.loads(str(locator_row[0])).get("review_node_id")
                    != request.review_node_id
                ):
                    raise ValueError("IR publication replay review binding changed")
            event = publication_event_for_publication(conn, publication_id=publication_id)
            conn.rollback()
            return PublicationReceipt(
                publication_id=publication_id,
                idempotency_key=publication_id,
                cell_ids=tuple(dict.fromkeys(fact.cell.fact_cell_id for fact in facts)),
                observation_ids=expected_observations,
                relation_ids=(),
                derivation_seal_ids=(),
                extraction_seal_ids=(seal_id,),
                resolution_revision_ids=(),
                publication_payload_sha256=verified.publication_payload_sha256,
                publication_seal_id=verified.publication_seal_id,
                publication_sequence=event.publication_sequence,
                publication_event_sha256=event.event_sha256,
                created_record_ids=(),
                exact_replay=True,
            )
        node_count = conn.execute(
            "SELECT COUNT(*) FROM evidence_nodes WHERE extraction_run_id=?", (run_id,)
        ).fetchone()[0]
        receipt = SourceFactRepository(conn).publish(
            SourceFactPublication(
                publication_id=publication_id,
                idempotency_key=publication_id,
                created_at=request.recorded_at,
                recorded_at=request.recorded_at,
                reported_facts=tuple(facts),
                extraction_seals=(
                    ExtractionRunCompletenessSealV2(
                        extraction_seal_id=seal_id,
                        idempotency_key=seal_id,
                        extraction_run_id=run_id,
                        expected_node_count=int(node_count),
                        completeness_policy_name="reviewed_bounded_ir_pdf_candidates_not_document_recall",
                        completeness_policy_version=_RECIPE,
                        completeness_policy_sha256=_sha(
                            [
                                request.document_version_id,
                                request.candidate_population_sha256,
                                review.review_sha256,
                                len(review.entries),
                            ]
                        ),
                        knowledge_at=request.knowledge_at,
                        recorded_at=request.recorded_at,
                    ),
                ),
            )
        )
        if precommit_validation is not None:
            precommit_validation()
        conn.commit()
        return receipt
    except Exception:
        conn.rollback()
        raise
