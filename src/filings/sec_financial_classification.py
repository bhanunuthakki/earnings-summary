"""Reviewed SEC package subjects with exact raw-byte and heading witnesses.

This classification supplements the authoritative accession package inventory.
It does not replace acquisition, extraction, or semantic admission receipts.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from soupsieve import SelectorSyntaxError

from provenance.source_coverage_reconcile import ExpectedDocumentImport


class PackageSubjectReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    source_url: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_path: str = Field(min_length=1)
    document_type: Literal[
        "financial_statement",
        "supplement",
        "earnings_release",
        "investor_presentation",
        "investor_update",
    ]
    heading_selector: str = Field(min_length=1)
    heading_text: str = Field(min_length=20)
    period_end: date | None = None
    period_end_text: str | None = None
    rationale: str = Field(min_length=20)

    @model_validator(mode="after")
    def _period_witness(self) -> Self:
        if self.document_type not in {"investor_presentation", "investor_update"}:
            if self.period_end is None or self.period_end_text is None:
                raise ValueError("financial_subject_requires_reported_period_witness")
            normalized = self.period_end_text.replace(",", "").strip()
            try:
                parsed = datetime.strptime(normalized, "%B %d %Y").date()
            except ValueError as exc:
                raise ValueError("financial_subject_period_text_invalid") from exc
            if parsed != self.period_end or self.period_end_text not in self.heading_text:
                raise ValueError("financial_subject_period_witness_mismatch")
        elif self.period_end is not None or self.period_end_text is not None:
            raise ValueError("presentation_cannot_assert_financial_period")
        return self


class SecPackageSubjectManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["sec_package_subject_review.v1"] = "sec_package_subject_review.v1"
    cik: str = Field(pattern=r"^\d{10}$")
    ticker: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    subjects: tuple[PackageSubjectReview, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_subjects(self) -> Self:
        urls = [item.source_url for item in self.subjects]
        if len(urls) != len(set(urls)):
            raise ValueError("sec_package_subject_duplicate")
        for item in self.subjects:
            parsed = urlparse(item.source_url)
            prefix = (
                f"/Archives/edgar/data/{int(self.cik)}/{item.accession_number.replace('-', '')}/"
            )
            if (
                parsed.scheme != "https"
                or parsed.netloc != "www.sec.gov"
                or not parsed.path.startswith(prefix)
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("sec_package_subject_identity_mismatch")
            if item.period_end is not None and item.period_end > self.reviewed_at.date():
                raise ValueError("sec_package_subject_future_period")
        return self


def load_package_subject_reviews(
    path: Path, *, cik: str, ticker: str, as_of: datetime
) -> tuple[SecPackageSubjectManifest, str]:
    """Validate the exact review and every cited source before inventory writes."""
    raw = path.read_bytes()
    manifest = SecPackageSubjectManifest.model_validate_json(raw)
    if manifest.cik != cik or manifest.ticker != ticker or manifest.reviewed_at > as_of:
        raise ValueError("sec_package_subject_review_scope_or_clock_mismatch")
    for subject in manifest.subjects:
        body = (path.parent / subject.raw_path).read_bytes()
        if hashlib.sha256(body).hexdigest() != subject.source_sha256:
            raise ValueError("sec_package_subject_raw_bytes_changed")
        try:
            nodes = BeautifulSoup(body, "lxml").select(subject.heading_selector)
        except SelectorSyntaxError as exc:
            raise ValueError("sec_package_subject_heading_selector_invalid") from exc
        if len(nodes) != 1:
            raise ValueError("sec_package_subject_heading_locator_ambiguous")
        text = re.sub(r"\s+", " ", nodes[0].get_text(" ", strip=True)).strip()
        if text != subject.heading_text:
            raise ValueError("sec_package_subject_heading_changed")
    return manifest, hashlib.sha256(raw).hexdigest()


def classify_expected_package_subjects(
    documents: tuple[ExpectedDocumentImport, ...],
    manifest: SecPackageSubjectManifest,
    *,
    manifest_sha256: str,
) -> tuple[ExpectedDocumentImport, ...]:
    """Return a new inventory vector, preserving SEC identity and source duty."""
    by_url = {item.source_url: item for item in manifest.subjects}
    matched: set[str] = set()
    result: list[ExpectedDocumentImport] = []
    for document in documents:
        subject = by_url.get(document.source_url or "")
        if subject is None:
            result.append(document)
            continue
        if (
            document.source_kind != "sec_filing"
            or document.form_type not in {"6-K", "6-K/A"}
            or document.accession_number != subject.accession_number
            or document.expectation_basis != "authoritative"
            or document.absence is None
            or document.absence.coverage_status != "available"
        ):
            raise ValueError("sec_package_subject_not_authoritative_six_k_member")
        details = dict(document.absence.reason_details)
        details.update(
            subject_review_sha256=manifest_sha256,
            subject_source_sha256=subject.source_sha256,
            subject_reviewer=manifest.reviewer,
            subject_reviewed_at=manifest.reviewed_at.isoformat(),
            subject_heading_selector=subject.heading_selector,
            subject_heading_text=subject.heading_text,
            subject_rationale=subject.rationale,
        )
        if subject.period_end_text is not None:
            details["subject_period_end_text"] = subject.period_end_text
        result.append(
            document.model_copy(
                update={
                    "document_type": subject.document_type,
                    "period_end": None
                    if subject.period_end is None
                    else datetime.combine(subject.period_end, datetime.min.time(), UTC),
                    "absence": document.absence.model_copy(
                        update={"reason_details": tuple(sorted(details.items()))}
                    ),
                }
            )
        )
        matched.add(subject.source_url)
    if matched != set(by_url):
        raise ValueError("sec_package_subject_not_in_authoritative_inventory")
    return tuple(result)
