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
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from compute.kpi_resolver import normalize_kpi_name
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
from pipeline.kpi_persistence import (
    KpiExtractionManifest,
    KpiValue,
    normalize_source_excerpt,
    persist_manifest,
)
from pipeline.segment_junction_writer import write_segment_facts_junction

MAX_EXTRACTED_AT_FUTURE_SKEW = timedelta(minutes=5)
_APPLY_SAVEPOINT = "apply_issuer_fact_manifest"


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
    segment_dim_type: SegmentDimType | None = None
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
            segment_dim_type=(
                self.segment_dim_type.value if self.segment_dim_type is not None else None
            ),
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
        if self.extracted_at.tzinfo is None or self.extracted_at.utcoffset() != timedelta(0):
            raise ValueError("manifest extracted_at must be timezone-aware UTC")
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
    def canonical_json(self) -> str:
        payload = self.model_dump(mode="json")
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


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
        "SELECT id, ticker, source_type, period_end, sha256, fetched_at "
        "FROM documents WHERE id = ?",
        (manifest.source_doc_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"source document {manifest.source_doc_id} does not exist")
    if str(row["ticker"]).upper() != manifest.ticker.upper():
        raise ValueError("manifest ticker does not match source document")
    if str(row["source_type"]) != SourceType.IR_DOC.value:
        raise ValueError("issuer manifest source document must be an issuer IR document")
    document_period = _parse_datetime(row["period_end"], field="period_end").date()
    if document_period != manifest.period_end:
        raise ValueError("manifest period does not match source document period")
    if str(row["sha256"]).lower() != manifest.source_doc_sha256:
        raise ValueError("manifest source document SHA-256 does not match SQLite")
    fetched_at = _parse_datetime(row["fetched_at"], field="fetched_at")
    if manifest.extracted_at.astimezone(UTC) < fetched_at:
        raise ValueError("manifest extracted_at cannot predate source document fetched_at")
    if manifest.extracted_at.astimezone(UTC) > datetime.now(UTC) + MAX_EXTRACTED_AT_FUTURE_SKEW:
        raise ValueError("manifest extracted_at exceeds the allowed future clock skew")
    return row


def _parse_datetime(raw: object, *, field: str) -> datetime:
    if isinstance(raw, datetime):
        parsed = raw
    else:
        text = str(raw).strip()
        if not text:
            raise ValueError(f"source document {field} is missing")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"source document {field} is not ISO-like") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _canonical_stored_locator(raw: object, *, fact_name: str) -> str:
    if raw is None or not str(raw).strip():
        raise ValueError(f"same-document fact {fact_name!r} has no persisted locator")
    try:
        locator = FactLocator.from_json(str(raw))
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"same-document fact {fact_name!r} has invalid persisted locator provenance"
        ) from exc
    if locator is None or locator.effective_kind() is None:
        raise ValueError(f"same-document fact {fact_name!r} has no canonical locator provenance")
    canonical = locator.to_json()
    if canonical is None:
        raise ValueError(f"same-document fact {fact_name!r} has empty locator provenance")
    return canonical


def _assert_kpi_replays_compatible(conn: sqlite3.Connection, manifest: IssuerFactManifest) -> None:
    rows = conn.execute(
        "SELECT kd.name, kf.value, kf.unit, kf.currency, "
        "kf.locator, kf.source_excerpt FROM kpi_facts kf "
        "JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
        "WHERE kf.ticker = ? AND kf.source_doc_id = ? "
        "AND date(kf.period_end) = ? AND kf.fiscal_period_type = ?",
        (
            manifest.ticker.upper(),
            manifest.source_doc_id,
            manifest.period_end.isoformat(),
            manifest.fiscal_period_type.value,
        ),
    ).fetchall()
    for value in manifest.values:
        if value.kind is not IssuerManifestFactKind.KPI:
            continue
        matching = [
            row
            for row in rows
            if normalize_kpi_name(str(row["name"])) == normalize_kpi_name(value.canonical_name)
        ]
        if len(matching) > 1:
            raise ValueError(
                f"same-document KPI {value.canonical_name!r} has duplicate existing captures"
            )
        for row in matching:
            stored_currency = None if row["currency"] is None else str(row["currency"])
            incoming_currency = value.currency.value if value.currency is not None else None
            currency_conflict = stored_currency is not None and stored_currency != incoming_currency
            try:
                value_conflict = Decimal(str(row["value"])) != value.value
            except Exception as exc:
                raise ValueError(
                    f"existing KPI {value.canonical_name!r} has a non-numeric stored value"
                ) from exc
            locator_conflict = (
                _canonical_stored_locator(row["locator"], fact_name=value.canonical_name)
                != value.locator.to_json()
            )
            excerpt_conflict = normalize_source_excerpt(
                None if row["source_excerpt"] is None else str(row["source_excerpt"])
            ) != normalize_source_excerpt(value.source_excerpt)
            if (
                value_conflict
                or str(row["unit"]) != value.unit.value
                or currency_conflict
                or locator_conflict
                or excerpt_conflict
            ):
                raise ValueError(
                    f"same-document KPI {value.canonical_name!r} conflicts with existing value or provenance"
                )


def _assert_segment_replays_compatible(
    conn: sqlite3.Connection, manifest: IssuerFactManifest
) -> None:
    rows = conn.execute(
        "SELECT sd.dim_type, sd.dim_name, sd.metric, sd.value, sd.locator, "
        "COALESCE(sd.unit, sp.unit) AS effective_unit, sp.currency "
        "FROM segment_periods sp "
        "JOIN segment_dimensions sd ON sd.period_id = sp.id "
        "WHERE sp.ticker = ? AND sp.source_doc_id = ? "
        "AND date(sp.period_end) = ? AND sp.fiscal_period_type = ?",
        (
            manifest.ticker.upper(),
            manifest.source_doc_id,
            manifest.period_end.isoformat(),
            manifest.fiscal_period_type.value,
        ),
    ).fetchall()
    for value in manifest.values:
        if value.kind is not IssuerManifestFactKind.SEGMENT:
            continue
        matching = [
            row
            for row in rows
            if (
                str(row["dim_type"]),
                str(row["dim_name"]),
                str(row["metric"]),
            )
            == (
                value.segment_dim_type.value if value.segment_dim_type is not None else "",
                value.segment_name,
                value.metric,
            )
        ]
        if len(matching) > 1:
            raise ValueError(
                f"same-document segment {value.canonical_name!r} has duplicate existing captures"
            )
        for row in matching:
            stored_currency = None if row["currency"] is None else str(row["currency"])
            incoming_currency = value.currency.value if value.currency is not None else None
            try:
                value_conflict = Decimal(str(row["value"])) != value.value
            except Exception as exc:
                raise ValueError(
                    f"existing segment {value.canonical_name!r} has a non-numeric stored value"
                ) from exc
            locator_conflict = (
                _canonical_stored_locator(row["locator"], fact_name=value.canonical_name)
                != value.locator.to_json()
            )
            if (
                value_conflict
                or str(row["effective_unit"]) != value.unit.value
                or stored_currency != incoming_currency
                or locator_conflict
            ):
                raise ValueError(
                    f"same-document segment {value.canonical_name!r} conflicts with existing value or provenance"
                )


def _validate_replays(conn: sqlite3.Connection, manifest: IssuerFactManifest) -> None:
    _assert_kpi_replays_compatible(conn, manifest)
    _assert_segment_replays_compatible(conn, manifest)


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
        dim_type = value.segment_dim_type
        if dim_type is None:
            raise ValueError("segment value has no dimension type")
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
    # Frozen Pydantic models can still be constructed with unvalidated
    # ``model_copy(update=...)``.  Re-parse at the public boundary so dry-run
    # and apply enforce the same closed schema.
    manifest = IssuerFactManifest.model_validate(manifest.model_dump(mode="json", warnings=False))
    _source_document(conn, manifest)
    _validate_replays(conn, manifest)
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
    conn.execute(f"SAVEPOINT {_APPLY_SAVEPOINT}")
    try:
        kpi_inserted, kpi_skipped = _apply_kpis(conn, manifest)
        period_inserted, segment_inserted = _apply_segments(conn, manifest)
        receipt = reconcile_extractor_fact_population(
            conn,
            frame,
            application_manifest_json=manifest.canonical_json,
            application_manifest_sha256=manifest.manifest_sha256,
        )
        if receipt.missing_count:
            raise ValueError(
                f"issuer manifest left {receipt.missing_count} expected fact(s) missing"
            )
        receipt_results = persist_document_coverage_receipt(conn, receipt)
        conn.execute(f"RELEASE SAVEPOINT {_APPLY_SAVEPOINT}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {_APPLY_SAVEPOINT}")
        conn.execute(f"RELEASE SAVEPOINT {_APPLY_SAVEPOINT}")
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
