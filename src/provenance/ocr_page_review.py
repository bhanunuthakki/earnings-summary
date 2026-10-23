"""Explicit reviewed dispositions for exact OCR-flagged PDF pages.

The detector remains authoritative for text coverage. A review says only that
one visually inspected page has no financial content for native IR capture.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from runtime.deterministic import canonical_json


class ReviewedPdfPageRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    document_version_id: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    assessment_id: str = Field(min_length=1, max_length=128)
    page_number: int = Field(ge=1)
    native_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: Literal["no_financial_content", "requires_ocr"]
    reviewer: str = Field(min_length=1, max_length=256)
    reviewed_at: AwareDatetime
    recorded_at: AwareDatetime
    rationale: str = Field(min_length=20)
    expected_prior_review_id: str | None = None

    @model_validator(mode="after")
    def valid_review(self) -> Self:
        if not self.reviewer.strip() or len(self.rationale.strip()) < 20:
            raise ValueError("reviewer and substantive rationale are required")
        if self.reviewed_at > self.recorded_at:
            raise ValueError("review cannot be recorded before it occurred")
        if self.expected_prior_review_id is not None and not self.expected_prior_review_id.strip():
            raise ValueError("prior review identity cannot be empty")
        return self


class RetainedPdfPageReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    review_id: str
    revision: int
    document_version_id: str
    source_sha256: str
    assessment_id: str
    page_number: int
    native_text_sha256: str
    render_sha256: str
    disposition: Literal["no_financial_content", "requires_ocr"]
    reviewer: str
    reviewed_at: AwareDatetime
    recorded_at: AwareDatetime
    rationale: str
    supersedes_review_id: str | None
    commitment_sha256: str


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("review time must be timezone aware")
    return parsed.astimezone(UTC)


def _row(row: sqlite3.Row) -> RetainedPdfPageReview:
    data: dict[str, object] = dict(zip(row.keys(), tuple(row), strict=True))
    data["reviewed_at"] = _aware(str(data["reviewed_at"]))
    data["recorded_at"] = _aware(str(data["recorded_at"]))
    review = RetainedPdfPageReview.model_validate(data)
    commitment = review.model_dump(mode="json", exclude={"review_id", "commitment_sha256"})
    if _sha(commitment) != review.commitment_sha256 or review.review_id != (
        "pdf-page-review:" + review.commitment_sha256
    ):
        raise ValueError("reviewed PDF page commitment mismatch")
    return review


def current_reviewed_pdf_page(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    assessment_id: str,
    page_number: int,
) -> RetainedPdfPageReview | None:
    """Read the one current review; do not infer approval from an OCR flag."""
    rows = conn.execute(
        "SELECT * FROM reviewed_pdf_page_revisions WHERE document_version_id=? "
        "AND assessment_id=? AND page_number=? ORDER BY revision",
        (document_version_id, assessment_id, page_number),
    ).fetchall()
    if not rows:
        return None
    previous: RetainedPdfPageReview | None = None
    for raw in rows:
        review = _row(raw)
        if review.revision != (1 if previous is None else previous.revision + 1) or (
            review.supersedes_review_id != (previous.review_id if previous else None)
        ):
            raise ValueError("reviewed PDF page revision chain changed")
        previous = review
    return previous


def record_reviewed_pdf_page(
    conn: sqlite3.Connection, request: ReviewedPdfPageRevision
) -> RetainedPdfPageReview:
    """Append one explicit human decision inside the caller's transaction."""
    request = ReviewedPdfPageRevision.model_validate(request.model_dump(mode="python"))
    bound = conn.execute(
        "SELECT 1 FROM ocr_document_assessments assessment "
        "JOIN ocr_preflight_pages page ON page.assessment_id=assessment.assessment_id "
        "JOIN evidence_document_versions version "
        "ON version.document_version_id=assessment.document_version_id "
        "WHERE assessment.assessment_id=? AND assessment.document_version_id=? "
        "AND assessment.input_sha256=? AND version.blob_sha256=? "
        "AND assessment.outcome='ocr_required' AND page.page_number=? "
        "AND page.requires_ocr=1 AND page.native_text_sha256=?",
        (
            request.assessment_id,
            request.document_version_id,
            request.source_sha256,
            request.source_sha256,
            request.page_number,
            request.native_text_sha256,
        ),
    ).fetchone()
    if bound is None:
        raise ValueError("reviewed PDF page source binding unavailable")
    prior = current_reviewed_pdf_page(
        conn,
        document_version_id=request.document_version_id,
        assessment_id=request.assessment_id,
        page_number=request.page_number,
    )
    if request.expected_prior_review_id != (prior.review_id if prior else None):
        raise ValueError("reviewed PDF page prior revision changed")
    revision = 1 if prior is None else prior.revision + 1
    body = {
        **request.model_dump(mode="json", exclude={"expected_prior_review_id"}),
        "revision": revision,
        "supersedes_review_id": prior.review_id if prior else None,
    }
    commitment_sha256 = _sha(body)
    review_id = "pdf-page-review:" + commitment_sha256
    conn.execute(
        "INSERT INTO reviewed_pdf_page_revisions "
        "(review_id,document_version_id,source_sha256,assessment_id,page_number,"
        "native_text_sha256,render_sha256,revision,supersedes_review_id,disposition,"
        "reviewer,reviewed_at,recorded_at,rationale,commitment_sha256) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            review_id,
            request.document_version_id,
            request.source_sha256,
            request.assessment_id,
            request.page_number,
            request.native_text_sha256,
            request.render_sha256,
            revision,
            prior.review_id if prior else None,
            request.disposition,
            request.reviewer,
            request.reviewed_at.isoformat(),
            request.recorded_at.isoformat(),
            request.rationale,
            commitment_sha256,
        ),
    )
    result = current_reviewed_pdf_page(
        conn,
        document_version_id=request.document_version_id,
        assessment_id=request.assessment_id,
        page_number=request.page_number,
    )
    if result is None or result.review_id != review_id:
        raise ValueError("reviewed PDF page append was not retained")
    return result
