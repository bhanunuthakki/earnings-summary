"""Convert reviewed, offline issuer-fact inputs into one apply manifest.

This is deliberately a production *producer*, not an extraction path.  It
combines a validated legacy KPI manifest, a reviewed population frame, and an
explicit reviewed segment-value frame without reading a database, a document,
or a network source.  The result remains inert until the separate issuer-fact
apply CLI is invoked with its explicit ``--apply`` flag.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.documents import SourceType
from models.facts import FactLocator, FiscalPeriodType
from pipeline.issuer_document_coverage import ExtractorFactPopulationFrame
from pipeline.issuer_fact_manifest import (
    IssuerFactManifest,
    IssuerFactValue,
    IssuerManifestFactKind,
)
from pipeline.kpi_persistence import KpiExtractionManifest


class ReviewedSegmentValues(BaseModel):
    """Explicit, reviewer-supplied segment captures for one IR document.

    The legacy KPI wrapper does not carry a source SHA-256, so this independent
    reviewed input binds the fully composed manifest to its immutable source
    document.  It cannot carry KPI values: splitting the planes avoids silently
    treating a hand-authored segment as an extractor-produced KPI.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["issuer_segment_values.v1"] = "issuer_segment_values.v1"
    ticker: str = Field(min_length=1, max_length=16)
    source_doc_id: int = Field(gt=0)
    source_doc_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    period_end: date
    fiscal_period_type: FiscalPeriodType
    extracted_at: datetime
    values: tuple[IssuerFactValue, ...]

    @model_validator(mode="after")
    def _bind_segment_values(self) -> ReviewedSegmentValues:
        if any(value.kind is not IssuerManifestFactKind.SEGMENT for value in self.values):
            raise ValueError("reviewed segment values may only contain segment facts")
        if any(value.ticker.upper() != self.ticker.upper() for value in self.values):
            raise ValueError("segment value ticker must match the reviewed segment header")
        if any(
            value.period_end != self.period_end
            or value.fiscal_period_type is not self.fiscal_period_type
            for value in self.values
        ):
            raise ValueError("segment values must match the reviewed segment period")
        identities = [value.expected().identity_key for value in self.values]
        if len(identities) != len(set(identities)):
            raise ValueError("reviewed segment fact identities must be unique")
        return self


def _legacy_kpi_values(legacy: KpiExtractionManifest) -> tuple[IssuerFactValue, ...]:
    """Re-type legacy KPI values at the v2-only issuer-manifest boundary."""
    values: list[IssuerFactValue] = []
    for legacy_value in legacy.values:
        locator = legacy_value.locator
        if not isinstance(locator, FactLocator) or locator.locator_version < 2:
            raise ValueError("legacy KPI values require a renderable locator version 2 or newer")
        values.append(
            IssuerFactValue(
                ticker=legacy.ticker,
                kind=IssuerManifestFactKind.KPI,
                canonical_name=legacy_value.name,
                period_end=legacy.period_end.date(),
                fiscal_period_type=legacy.fiscal_period_type,
                unit=legacy_value.unit,
                currency=legacy_value.currency,
                value=legacy_value.value,
                locator=locator,
                source_excerpt=legacy_value.source_excerpt,
            )
        )
    identities = [value.expected().identity_key for value in values]
    if len(identities) != len(set(identities)):
        raise ValueError("legacy KPI fact identities must be unique")
    return tuple(values)


def _assert_header_agreement(
    legacy: KpiExtractionManifest,
    frame: ExtractorFactPopulationFrame,
    segments: ReviewedSegmentValues,
) -> None:
    """Reject any document identity or reporting-period cross-contamination."""
    if legacy.source_doc_id != frame.document_id or legacy.source_doc_id != segments.source_doc_id:
        raise ValueError(
            "legacy KPI manifest, population frame, and segment values must share a document"
        )
    tickers = {legacy.ticker.upper(), frame.ticker.upper(), segments.ticker.upper()}
    if len(tickers) != 1:
        raise ValueError(
            "legacy KPI manifest, population frame, and segment values must share a ticker"
        )
    if legacy.period_end.date() != segments.period_end:
        raise ValueError("legacy KPI manifest and segment values must share a period")
    if legacy.fiscal_period_type is not segments.fiscal_period_type:
        raise ValueError("legacy KPI manifest and segment values must share a fiscal period type")
    if any(
        expected.period_end != segments.period_end
        or expected.fiscal_period_type != segments.fiscal_period_type.value
        for expected in frame.expected
    ):
        raise ValueError("population frame expected facts must share the reviewed segment period")
    if frame.extracted_at != segments.extracted_at:
        raise ValueError("population frame and reviewed segment values must share extracted_at")


def produce_issuer_fact_manifest(
    legacy: KpiExtractionManifest,
    frame: ExtractorFactPopulationFrame,
    segments: ReviewedSegmentValues,
) -> IssuerFactManifest:
    """Build a complete, deterministic issuer-fact application manifest.

    The population frame is authoritative: captured KPI and segment identities
    plus its explicit rejection identities must form its exact expected set.
    No inference fills a gap, and no database writes occur in this function.
    """
    if legacy.primary_source is not SourceType.IR_DOC:
        raise ValueError("legacy KPI manifest primary_source must be IR_DOC")
    _assert_header_agreement(legacy, frame, segments)
    values = (*_legacy_kpi_values(legacy), *segments.values)
    captured_by_identity = {value.expected().identity_key: value for value in values}
    if len(captured_by_identity) != len(values):
        raise ValueError("captured issuer fact identities must be unique")
    expected_by_identity = {expected.identity_key: expected for expected in frame.expected}
    captured_ids = set(captured_by_identity)
    expected_ids = set(expected_by_identity)
    rejected_ids = set(frame.rejected)
    if captured_ids - expected_ids or expected_ids - (captured_ids | rejected_ids):
        raise ValueError(
            "captured facts must exactly close the expected population with rejections"
        )
    if captured_ids & rejected_ids:
        raise ValueError("captured facts cannot also be explicitly rejected")
    if rejected_ids - expected_ids:
        raise ValueError("rejected facts must refer to the expected population")

    ordered_values = tuple(captured_by_identity[key] for key in sorted(captured_by_identity))
    ordered_expected = tuple(expected_by_identity[key] for key in sorted(expected_by_identity))
    ordered_rejected = {key: frame.rejected[key] for key in sorted(frame.rejected)}
    return IssuerFactManifest(
        ticker=segments.ticker,
        source_doc_id=segments.source_doc_id,
        source_doc_sha256=segments.source_doc_sha256,
        period_end=segments.period_end,
        fiscal_period_type=segments.fiscal_period_type,
        values=ordered_values,
        expected=ordered_expected,
        rejected=ordered_rejected,
        expected_population_status=frame.expected_population_status,
        extracted_at=segments.extracted_at,
    )
