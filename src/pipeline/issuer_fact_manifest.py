"""Typed, atomic application of issuer KPI and segment fact manifests.

The extractor owns the manifest; this module owns the narrow boundary that
binds it to one immutable source document and one reporting period.  Dry-run
is the default.  Applying a manifest uses the existing KPI and segment
persistence APIs, reconciles every expected item, and appends the resulting
coverage receipt before committing one SQLite transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.documents import SourceType
from models.facts import (
    Currency,
    FactLocator,
    FiscalPeriodType,
    SegmentDimension,
    SegmentDimType,
    Unit,
)
from models.kpis import DefinitionOrigin
from pipeline.issuer_document_coverage import (
    ExpectedIssuerFact,
    ExtractorFactPopulationFrame,
    IssuerDocumentCoverageReceipt,
    IssuerFactKind,
    persist_document_coverage_receipt,
    reconcile_extractor_fact_population,
)
from pipeline.kpi_persistence import KpiExtractionManifest, KpiValue, persist_manifest
from pipeline.segment_junction_writer import write_segment_facts_junction


class IssuerManifestFactKind(StrEnum):
    KPI = "kpi"
    SEGMENT = "segment"


class IssuerFactValue(BaseModel):
    """One exact issuer-reported value with a renderable source locator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1, max_length=16)
    kind: IssuerManifestFactKind
    canonical_name: str = Field(min_length=1, max_length=200)
    period_end: date
    fiscal_period_type: FiscalPeriodType
    unit: Unit
    currency: Currency | None = None
    value: Decimal
    locator: FactLocator
    source_excerpt: str | None = Field(default=None, max_length=2000)
    segment_dim_type: str | None = None
    segment_name: str | None = None
    metric: str | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> IssuerFactValue:
        if self.locator.effective_kind() is None:
            raise ValueError("issuer manifest values require a renderable locator")
        monetary = {Unit.ACTUAL, Unit.THOUSANDS, Unit.MILLIONS, Unit.BILLIONS}
        if self.unit in monetary and self.currency is None:
            raise ValueError("monetary issuer manifest values require currency")
        if self.kind is IssuerManifestFactKind.SEGMENT and not all(
            (self.segment_dim_type, self.segment_name, self.metric)
        ):
            raise ValueError("segment values require dim type, segment name, and metric")
        if self.kind is IssuerManifestFactKind.KPI and any(
            value is not None for value in (self.segment_dim_type, self.segment_name, self.metric)
        ):
            raise ValueError("KPI values cannot carry segment identity fields")
        return self

    def expected(self) -> ExpectedIssuerFact:
        return ExpectedIssuerFact(
            ticker=self.ticker,
            kind=(
                IssuerFactKind.KPI
                if self.kind is IssuerManifestFactKind.KPI
                else IssuerFactKind.SEGMENT
            ),
            canonical_name=self.canonical_name,
            period_end=self.period_end,
            fiscal_period_type=self.fiscal_period_type.value,
            unit=self.unit,
            currency=self.currency,
            segment_dim_type=self.segment_dim_type,
            segment_name=self.segment_name,
            metric=self.metric,
        )


class IssuerFactManifest(BaseModel):
    """One source-document-bound KPI/segment population manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["issuer_fact_manifest.v1"] = "issuer_fact_manifest.v1"
    ticker: str = Field(min_length=1, max_length=16)
    source_doc_id: int = Field(gt=0)
    source_doc_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    period_end: date
    fiscal_period_type: FiscalPeriodType
    values: tuple[IssuerFactValue, ...] = ()
    expected: tuple[ExpectedIssuerFact, ...] = ()
    rejected: dict[str, str] = Field(default_factory=dict[str, str])
    expected_population_status: Literal["populated", "zero_expected"] = "populated"
    extracted_at: datetime

    @model_validator(mode="after")
    def _bind_population(self) -> IssuerFactManifest:
        header_ticker = self.ticker.upper()
        if any(value.ticker.upper() != header_ticker for value in self.values):
            raise ValueError("manifest value ticker must match manifest ticker")
        if any(expected.ticker.upper() != header_ticker for expected in self.expected):
            raise ValueError("manifest expected ticker must match manifest ticker")
        if any(
            value.period_end != self.period_end
            or value.fiscal_period_type != self.fiscal_period_type
            for value in self.values
        ):
            raise ValueError("manifest values must match the manifest period")
        if any(
            expected.period_end != self.period_end
            or expected.fiscal_period_type != self.fiscal_period_type.value
            for expected in self.expected
        ):
            raise ValueError("manifest expected facts must match the manifest period")
        value_expected = [value.expected() for value in self.values]
        value_ids = [item.identity_key for item in value_expected]
        expected_ids = [item.identity_key for item in self.expected]
        if len(value_ids) != len(set(value_ids)) or len(expected_ids) != len(set(expected_ids)):
            raise ValueError("manifest fact identities must be unique")
        if set(value_ids) - set(expected_ids):
            raise ValueError("manifest values must be declared in expected population")
        if set(self.rejected) - set(expected_ids):
            raise ValueError("manifest rejection keys must refer to expected facts")
        if set(value_ids) & set(self.rejected):
            raise ValueError("a fact cannot be both captured and explicitly rejected")
        if set(value_ids) | set(self.rejected) != set(expected_ids):
            raise ValueError("every expected fact must be captured or explicitly rejected")
        if self.expected_population_status == "zero_expected" and (
            self.expected or self.values or self.rejected
        ):
            raise ValueError("zero_expected manifest cannot contain facts")
        if self.expected_population_status == "populated" and not self.expected:
            raise ValueError("populated manifest requires expected facts")
        if any(not reason.strip() for reason in self.rejected.values()):
            raise ValueError("rejection reasons must be non-empty")
        return self

    @property
    def manifest_sha256(self) -> str:
        payload = self.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IssuerManifestApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    applied: bool
    manifest_sha256: str
    kpi_inserted: int = 0
    kpi_skipped_existing: int = 0
    segment_periods_inserted: int = 0
    segment_dimensions_inserted: int = 0
    coverage_receipts_created: int = 0
    missing_count: int = 0
    rejected_count: int = 0
    receipt: IssuerDocumentCoverageReceipt | None = None


def _source_document(conn: sqlite3.Connection, manifest: IssuerFactManifest) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, ticker, source_type, sha256 FROM documents WHERE id = ?",
        (manifest.source_doc_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"source document {manifest.source_doc_id} does not exist")
    if str(row["ticker"]).upper() != manifest.ticker.upper():
        raise ValueError("manifest ticker does not match source document")
    if str(row["source_type"]) != SourceType.IR_DOC.value:
        raise ValueError("issuer manifest source document must be an issuer IR document")
    if str(row["sha256"]).lower() != manifest.source_doc_sha256:
        raise ValueError("manifest source document SHA-256 does not match SQLite")
    return row


def _period_datetime(period_end: date) -> datetime:
    return datetime.combine(period_end, time.min)


def _apply_kpis(conn: sqlite3.Connection, manifest: IssuerFactManifest) -> tuple[int, int]:
    values = [
        KpiValue(
            name=value.canonical_name,
            value=value.value,
            unit=value.unit,
            currency=value.currency,
            confidence=1.0,
            source_excerpt=value.source_excerpt,
            locator=value.locator,
        )
        for value in manifest.values
        if value.kind is IssuerManifestFactKind.KPI
    ]
    if not values:
        return (0, 0)
    result = persist_manifest(
        conn,
        run_id=f"issuer-manifest:{manifest.manifest_sha256}",
        manifest=KpiExtractionManifest(
            ticker=manifest.ticker.upper(),
            period_end=_period_datetime(manifest.period_end),
            fiscal_period_type=manifest.fiscal_period_type,
            source_doc_id=manifest.source_doc_id,
            primary_source=SourceType.IR_DOC,
            extracted_by="issuer_manifest_v1",
            origin=DefinitionOrigin.ANALYST,
            values=values,
        ),
        commit=False,
    )
    return (result.inserted, result.skipped_existing)


def _apply_segments(conn: sqlite3.Connection, manifest: IssuerFactManifest) -> tuple[int, int]:
    by_dim_type: dict[SegmentDimType, tuple[Currency | None, list[SegmentDimension]]] = {}
    for value in manifest.values:
        if value.kind is not IssuerManifestFactKind.SEGMENT:
            continue
        try:
            dim_type = SegmentDimType(value.segment_dim_type or "")
        except ValueError as exc:
            raise ValueError(
                f"unsupported segment dimension type {value.segment_dim_type!r}"
            ) from exc
        if dim_type not in by_dim_type:
            by_dim_type[dim_type] = (value.currency, [])
        elif by_dim_type[dim_type][0] != value.currency:
            raise ValueError("segment values sharing a dimension must use one currency")
        by_dim_type[dim_type][1].append(
            SegmentDimension(
                dim_type=dim_type,
                dim_name=value.segment_name or value.canonical_name,
                value=value.value,
                metric=value.metric or "revenue",
                unit=value.unit,
                confidence=1.0,
                extracted_by="issuer_manifest_v1",
                locator=value.locator.to_json(),
            )
        )
    periods_inserted = 0
    dimensions_inserted = 0
    for currency, dimensions in by_dim_type.values():
        first = dimensions[0]
        period_inserted, dimension_count = write_segment_facts_junction(
            conn,
            ticker=manifest.ticker.upper(),
            period_end=_period_datetime(manifest.period_end),
            fiscal_period_type=manifest.fiscal_period_type,
            source_doc_id=manifest.source_doc_id,
            currency=currency,
            unit=first.unit or Unit.ACTUAL,
            dimensions=dimensions,
            period_method_version="issuer_manifest_v1",
        )
        periods_inserted += period_inserted
        dimensions_inserted += dimension_count
    return (periods_inserted, dimensions_inserted)


def apply_issuer_fact_manifest(
    conn: sqlite3.Connection, manifest: IssuerFactManifest, *, apply: bool = False
) -> IssuerManifestApplyResult:
    """Validate or atomically apply one issuer manifest.

    ``apply=False`` performs no writes.  ``apply=True`` rolls back all KPI,
    segment, and receipt writes when any expected fact remains missing.
    """
    _source_document(conn, manifest)
    if not apply:
        return IssuerManifestApplyResult(applied=False, manifest_sha256=manifest.manifest_sha256)

    frame = ExtractorFactPopulationFrame(
        document_id=manifest.source_doc_id,
        ticker=manifest.ticker.upper(),
        expected=manifest.expected,
        rejected=manifest.rejected,
        extracted_at=manifest.extracted_at.astimezone(UTC),
        expected_population_status=manifest.expected_population_status,
    )
    try:
        conn.execute("BEGIN")
        kpi_inserted, kpi_skipped = _apply_kpis(conn, manifest)
        period_inserted, segment_inserted = _apply_segments(conn, manifest)
        receipt = reconcile_extractor_fact_population(conn, frame)
        if receipt.missing_count:
            raise ValueError(
                f"issuer manifest left {receipt.missing_count} expected fact(s) missing"
            )
        receipt_results = persist_document_coverage_receipt(conn, receipt)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return IssuerManifestApplyResult(
        applied=True,
        manifest_sha256=manifest.manifest_sha256,
        kpi_inserted=kpi_inserted,
        kpi_skipped_existing=kpi_skipped,
        segment_periods_inserted=period_inserted,
        segment_dimensions_inserted=segment_inserted,
        coverage_receipts_created=sum(item.created for item in receipt_results),
        missing_count=receipt.missing_count,
        rejected_count=receipt.rejected_count,
        receipt=receipt,
    )
