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
from typing import Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

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
from pipeline.kpi_definition_revisions import (
    IssuerKpiDefinitionRevision,
    KpiDefinitionComparabilityRevision,
    current_kpi_definition_comparability_revision,
    current_kpi_definition_revision,
    kpi_definition_revision_by_id,
    validate_kpi_definition_comparability_candidate,
    validate_kpi_definition_revision_candidate,
)
from pipeline.kpi_persistence import (
    KpiExtractionManifest,
    KpiValue,
    normalize_source_excerpt,
    persist_manifest,
)
from pipeline.kpi_semantic_review import (
    KpiEvidenceLocatorCoordinates,
    fact_locator_from_evidence_coordinates,
)
from pipeline.kpi_semantics import (
    KpiSemanticContext,
    KpiSemanticStatus,
    current_kpi_semantic_context,
    normalize_source_numeric,
    parse_source_numeric,
)
from pipeline.kpi_source_review import (
    SourceReviewedKpiCaptureResult,
    insert_source_reviewed_kpi_capture,
)
from pipeline.segment_junction_writer import write_segment_facts_junction
from provenance.evidence_ledger import EvidenceLocator

MAX_EXTRACTED_AT_FUTURE_SKEW = timedelta(minutes=5)
MAX_REVIEW_KNOWLEDGE_AT_FUTURE_SKEW = timedelta(minutes=5)
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
        if self.locator.locator_version < 2:
            raise ValueError("issuer manifest values require locator version 2 or newer")
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


class ReviewedKpiDefinitionCapture(BaseModel):
    """One sealed reviewer decision for an exact KPI fact and definition root."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_identity: str = Field(min_length=1)
    expected_kpi_definition_id: int = Field(gt=0)
    expected_definition_head_id: str | None = Field(min_length=1, max_length=128)
    expected_definition_revision: int = Field(ge=0)
    evidence_document_version_id: str = Field(min_length=1, max_length=128)
    evidence_node_id: str = Field(min_length=1, max_length=128)
    evidence_locator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fact_locator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: str = Field(min_length=1, max_length=128)
    knowledge_at: datetime
    context: KpiSemanticContext
    definition_revision: IssuerKpiDefinitionRevision
    comparability_revisions: tuple[KpiDefinitionComparabilityRevision, ...] = ()

    @model_validator(mode="after")
    def _complete_reviewed_capture(self) -> Self:
        if self.knowledge_at.tzinfo is None:
            raise ValueError("reviewed capture knowledge_at must be timezone-aware")
        if self.context.status is not KpiSemanticStatus.ADMITTED:
            raise ValueError("reviewed KPI definition capture must be admitted")
        definition = self.definition_revision
        if definition.kpi_definition_id != self.expected_kpi_definition_id:
            raise ValueError("reviewed definition root does not match expected KPI definition")
        if (self.expected_definition_head_id is None) != (self.expected_definition_revision == 0):
            raise ValueError("definition head and revision-zero expectations conflict")
        if (
            definition.revision != self.expected_definition_revision + 1
            or definition.supersedes_definition_revision_id != self.expected_definition_head_id
        ):
            raise ValueError("reviewed definition does not extend the expected exact head")
        if (
            definition.source_document_version_id != self.evidence_document_version_id
            or definition.source_evidence_node_id != self.evidence_node_id
            or definition.source_locator_sha256 != self.evidence_locator_sha256
        ):
            raise ValueError("reviewed definition evidence does not match the capture")
        if definition.reviewed_by != self.reviewer:
            raise ValueError("definition reviewer does not match reviewed capture")
        if (
            definition.knowledge_at > self.knowledge_at
            or definition.recorded_at > self.knowledge_at
        ):
            raise ValueError("reviewed fact capture cannot predate its definition")
        captured_id = definition.kpi_definition_revision_id
        for relation in self.comparability_revisions:
            if captured_id not in {
                relation.predecessor_definition_revision_id,
                relation.successor_definition_revision_id,
            }:
                raise ValueError("comparability decision must touch the captured definition")
            if (
                relation.source_document_version_id != self.evidence_document_version_id
                or relation.source_evidence_node_id != self.evidence_node_id
                or relation.source_locator_sha256 != self.evidence_locator_sha256
            ):
                raise ValueError("comparability evidence does not match the reviewed capture")
            if relation.reviewed_by != self.reviewer:
                raise ValueError("comparability reviewer does not match reviewed capture")
            if (
                relation.knowledge_at > self.knowledge_at
                or relation.recorded_at > self.knowledge_at
            ):
                raise ValueError("comparability revision is not bound to the reviewed capture")
        return self


class _IssuerFactManifestBase(BaseModel):
    """Shared document and population contract for issuer-fact manifests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

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
    def _bind_population(self) -> Self:
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


class IssuerFactManifest(_IssuerFactManifestBase):
    """Legacy issuer-fact manifest; KPI semantic bindings are explicitly null."""

    schema_version: Literal["issuer_fact_manifest.v1"] = "issuer_fact_manifest.v1"


class IssuerFactManifestV2(_IssuerFactManifestBase):
    """Issuer-fact manifest with complete reviewed definition capture for every KPI."""

    schema_version: Literal["issuer_fact_manifest.v2"] = "issuer_fact_manifest.v2"
    reviewed_by: str = Field(min_length=1, max_length=128)
    reviewed_capture_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_kpi_definition_captures: tuple[ReviewedKpiDefinitionCapture, ...]

    @property
    def reviewed_capture_set_payload(self) -> dict[str, object]:
        return {
            "schema_version": "reviewed_kpi_definition_captures.v1",
            "ticker": self.ticker,
            "source_doc_id": self.source_doc_id,
            "source_doc_sha256": self.source_doc_sha256,
            "period_end": self.period_end.isoformat(),
            "fiscal_period_type": self.fiscal_period_type.value,
            "extracted_at": self.extracted_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "reviewed_by": self.reviewed_by,
            "captures": [
                capture.model_dump(mode="json") for capture in self.reviewed_kpi_definition_captures
            ],
        }

    @property
    def computed_reviewed_capture_set_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.reviewed_capture_set_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

    @model_validator(mode="after")
    def _bind_reviewed_captures(self) -> Self:
        kpi_values = {
            value.expected().identity_key: value
            for value in self.values
            if value.kind is IssuerManifestFactKind.KPI
        }
        captures = {
            capture.fact_identity: capture for capture in self.reviewed_kpi_definition_captures
        }
        if len(captures) != len(self.reviewed_kpi_definition_captures):
            raise ValueError("reviewed KPI capture fact identities must be unique")
        if set(captures) != set(kpi_values):
            raise ValueError("v2 requires exactly one reviewed definition capture for every KPI")
        if any(capture.reviewer != self.reviewed_by for capture in captures.values()):
            raise ValueError("v2 reviewed KPI captures must share the manifest reviewer")
        if self.reviewed_capture_set_sha256 != self.computed_reviewed_capture_set_sha256:
            raise ValueError("reviewed capture set SHA-256 does not match the sealed input")
        root_ids = [capture.expected_kpi_definition_id for capture in captures.values()]
        definitions = [capture.definition_revision for capture in captures.values()]
        relations = [
            relation
            for capture in captures.values()
            for relation in capture.comparability_revisions
        ]
        duplicate_checks: tuple[tuple[list[object], str], ...] = (
            (list(root_ids), "expected KPI definition roots"),
            (
                [definition.kpi_definition_revision_id for definition in definitions],
                "definition revision identities",
            ),
            (
                [definition.idempotency_key for definition in definitions],
                "definition idempotency keys",
            ),
            (
                [definition.commitment_sha256 for definition in definitions],
                "definition commitments",
            ),
            (
                [relation.comparability_revision_id for relation in relations],
                "comparability revision identities",
            ),
            (
                [relation.idempotency_key for relation in relations],
                "comparability idempotency keys",
            ),
            ([relation.commitment_sha256 for relation in relations], "comparability commitments"),
            (
                [
                    frozenset(
                        (
                            relation.predecessor_definition_revision_id,
                            relation.successor_definition_revision_id,
                        )
                    )
                    for relation in relations
                ],
                "comparability pairs",
            ),
        )
        for values, label in duplicate_checks:
            if len(values) != len(set(values)):
                raise ValueError(f"v2 repeats {label}")
        for identity, value in kpi_values.items():
            capture = captures[identity]
            fact_locator_json = value.locator.to_json()
            if (
                fact_locator_json is None
                or hashlib.sha256(fact_locator_json.encode("utf-8")).hexdigest()
                != capture.fact_locator_sha256
            ):
                raise ValueError("reviewed capture fact locator hash mismatch")
            if (
                capture.context.metric_name_as_reported
                != capture.definition_revision.reported_label
            ):
                raise ValueError("reviewed context label does not match the definition wording")
            if capture.context.reported_period_end != self.period_end:
                raise ValueError("reviewed context period does not match the manifest")
            definition = capture.definition_revision
            if definition.status.value != "admitted" or definition.lifecycle.value != "active":
                raise ValueError("v2 KPI capture requires an admitted active definition revision")
            if definition.effective_at.date() > self.period_end:
                raise ValueError("reviewed definition is not effective for the manifest period")
            if definition.unit_key != value.unit:
                raise ValueError("reviewed definition unit does not match the KPI value")
            expected_currency = None if value.currency is None else value.currency.value
            actual_currency = None if definition.currency is None else definition.currency.value
            if expected_currency != actual_currency:
                raise ValueError("reviewed definition currency does not match the KPI value")
        return self


IssuerFactManifestAny: TypeAlias = IssuerFactManifest | IssuerFactManifestV2
_ISSUER_FACT_MANIFEST_ADAPTER: TypeAdapter[IssuerFactManifestAny] = TypeAdapter(
    IssuerFactManifest | IssuerFactManifestV2
)


def parse_issuer_fact_manifest(value: object) -> IssuerFactManifestAny:
    """Parse exactly one supported manifest version without upgrading legacy bytes."""

    return _ISSUER_FACT_MANIFEST_ADAPTER.validate_python(value)


def validate_issuer_fact_manifest_knowledge_time(
    manifest: IssuerFactManifestAny,
    *,
    now: datetime,
) -> None:
    """Reject future reviewed authority before it becomes a persistence cutoff."""

    if now.tzinfo is None:
        raise ValueError("issuer manifest validation clock must be timezone-aware")
    if not isinstance(manifest, IssuerFactManifestV2):
        return
    latest_allowed = now.astimezone(UTC) + MAX_REVIEW_KNOWLEDGE_AT_FUTURE_SKEW
    if any(
        capture.knowledge_at.astimezone(UTC) > latest_allowed
        for capture in manifest.reviewed_kpi_definition_captures
    ):
        raise ValueError("review knowledge_at is from the future")


class IssuerManifestApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    applied: bool
    manifest_sha256: str
    kpi_inserted: int = 0
    kpi_skipped_existing: int = 0
    segment_periods_inserted: int = 0
    segment_dimensions_inserted: int = 0
    coverage_receipts_created: int = 0
    semantic_contexts_inserted: int = 0
    definition_revisions_inserted: int = 0
    comparability_revisions_inserted: int = 0
    definition_revision_ids: tuple[str, ...] = ()
    missing_count: int = 0
    rejected_count: int = 0
    receipt: IssuerDocumentCoverageReceipt | None = None


def _source_document(conn: sqlite3.Connection, manifest: IssuerFactManifestAny) -> sqlite3.Row:
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


_ISSUER_MANIFEST_KPI_REPLAY_SQL = (
    "SELECT kf.id, kf.kpi_definition_id, kd.name, kf.value, kf.unit, kf.currency, "
    "kf.locator, kf.source_excerpt FROM kpi_facts kf "
    "JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
    "WHERE kf.ticker = ? AND kf.source_doc_id = ? "
    "AND date(kf.period_end) = ? AND kf.fiscal_period_type = ?"
)


def _capture_by_identity(
    manifest: IssuerFactManifestV2,
) -> dict[str, ReviewedKpiDefinitionCapture]:
    return {capture.fact_identity: capture for capture in manifest.reviewed_kpi_definition_captures}


def _assert_reviewed_source_binding(
    conn: sqlite3.Connection,
    manifest: IssuerFactManifestV2,
    value: IssuerFactValue,
    capture: ReviewedKpiDefinitionCapture,
) -> None:
    evidence = conn.execute(
        "SELECT version.document_version_id,version.blob_sha256,version.ticker,"
        "node.node_id,node.node_kind,node.text,node.locator_json,node.locator_sha256,run.outcome "
        "FROM evidence_document_versions version "
        "JOIN evidence_extraction_runs run ON run.document_version_id=version.document_version_id "
        "JOIN evidence_nodes node ON node.extraction_run_id=run.extraction_run_id "
        "WHERE version.legacy_document_id=? AND version.document_version_id=? AND node.node_id=?",
        (
            manifest.source_doc_id,
            capture.evidence_document_version_id,
            capture.evidence_node_id,
        ),
    ).fetchall()
    if len(evidence) != 1:
        raise ValueError("reviewed KPI evidence node must belong to the exact source document")
    row = evidence[0]
    if (
        str(row["blob_sha256"]) != manifest.source_doc_sha256
        or str(row["ticker"]).upper() != manifest.ticker.upper()
        or str(row["outcome"]) != "succeeded"
    ):
        raise ValueError("reviewed KPI evidence does not match the source identity")
    if str(row["node_kind"]) not in {
        "section",
        "passage",
        "table",
        "table_row",
        "table_cell",
        "pdf_page",
    }:
        raise ValueError("reviewed KPI evidence node must be substantive")
    if row["locator_json"] is None or row["locator_sha256"] is None:
        raise ValueError("reviewed KPI evidence node requires an exact locator")
    evidence_locator = EvidenceLocator.model_validate_json(str(row["locator_json"]))
    if (
        evidence_locator.canonical_json != str(row["locator_json"])
        or evidence_locator.canonical_sha256 != str(row["locator_sha256"])
        or evidence_locator.canonical_sha256 != capture.evidence_locator_sha256
    ):
        raise ValueError("reviewed KPI evidence locator hash mismatch")
    excerpt = normalize_source_excerpt(value.source_excerpt)
    if excerpt is None or excerpt not in str(row["text"]):
        raise ValueError("reviewed KPI source excerpt must occur in its evidence node")
    expected_fact_locator = fact_locator_from_evidence_coordinates(
        KpiEvidenceLocatorCoordinates.from_evidence_locator(evidence_locator),
        verbatim_snippet=excerpt,
    ).model_copy(update={"locator_version": value.locator.locator_version})
    if value.locator != expected_fact_locator:
        raise ValueError("reviewed KPI fact locator does not derive from its evidence node")
    source_value_text = capture.context.source_value_text
    if source_value_text is None or source_value_text not in excerpt:
        raise ValueError("reviewed KPI capture requires an exact source value token")
    if (
        normalize_source_numeric(
            parse_source_numeric(source_value_text),
            unit=value.unit,
            unit_scale=capture.context.unit_scale,
        )
        != value.value
    ):
        raise ValueError("reviewed KPI source value and semantic scale do not match the fact")


def _assert_reviewed_capture_against_sqlite(
    conn: sqlite3.Connection,
    manifest: IssuerFactManifestV2,
    value: IssuerFactValue,
    capture: ReviewedKpiDefinitionCapture,
) -> None:
    _assert_reviewed_source_binding(conn, manifest, value, capture)
    definition_root = conn.execute(
        "SELECT ticker,name FROM kpi_definitions WHERE id=?",
        (capture.expected_kpi_definition_id,),
    ).fetchone()
    if definition_root is None or (
        str(definition_root["ticker"]).upper() != manifest.ticker.upper()
        or str(definition_root["name"]) != value.canonical_name
    ):
        raise ValueError("reviewed KPI expected root does not match the exact fact identity")
    definition = capture.definition_revision
    expected_currency = None if value.currency is None else value.currency.value
    if (
        definition.unit_key != value.unit
        or (None if definition.currency is None else definition.currency.value) != expected_currency
        or definition.reported_label != capture.context.metric_name_as_reported
        or definition.accounting_basis != capture.context.accounting_basis
        or definition.consolidation_scope != capture.context.consolidation_scope
        or definition.dimensions != capture.context.dimensions
        or definition.unit_scale != capture.context.unit_scale
    ):
        raise ValueError("reviewed KPI definition, context, and fact semantic axes disagree")
    replay = kpi_definition_revision_by_id(
        conn,
        kpi_definition_revision_id=definition.kpi_definition_revision_id,
    )
    current = current_kpi_definition_revision(
        conn, kpi_definition_id=capture.expected_kpi_definition_id
    )
    if replay is not None:
        if replay.commitment_sha256 != definition.commitment_sha256:
            raise ValueError("reviewed KPI definition replay conflicts with persisted content")
        if (
            current is None
            or current.kpi_definition_revision_id != definition.kpi_definition_revision_id
        ):
            raise ValueError("reviewed KPI definition is no longer the exact current head")
        for relation in capture.comparability_revisions:
            persisted = current_kpi_definition_comparability_revision(
                conn,
                predecessor_definition_revision_id=relation.predecessor_definition_revision_id,
                successor_definition_revision_id=relation.successor_definition_revision_id,
            )
            if (
                persisted is None
                or persisted.comparability_revision_id != relation.comparability_revision_id
                or persisted.commitment_sha256 != relation.commitment_sha256
            ):
                raise ValueError("reviewed KPI comparability replay conflicts with durable state")
        return
    actual_head = None if current is None else current.kpi_definition_revision_id
    actual_revision = 0 if current is None else current.revision
    if (actual_head, actual_revision) != (
        capture.expected_definition_head_id,
        capture.expected_definition_revision,
    ):
        raise ValueError("KPI definition revision head changed after source review")
    validate_kpi_definition_revision_candidate(
        conn,
        definition,
        expected_definition_head_id=capture.expected_definition_head_id,
        expected_definition_revision=capture.expected_definition_revision,
    )
    for relation in capture.comparability_revisions:
        validate_kpi_definition_comparability_candidate(
            conn,
            relation,
            proposed_definition=definition,
        )


def _assert_kpi_replays_compatible(
    conn: sqlite3.Connection, manifest: IssuerFactManifestAny
) -> None:
    if isinstance(manifest, IssuerFactManifest):
        reviewed_rows = conn.execute(
            "SELECT id FROM kpi_facts WHERE ticker=? AND source_doc_id=? "
            "AND date(period_end)=? AND fiscal_period_type=?",
            (
                manifest.ticker.upper(),
                manifest.source_doc_id,
                manifest.period_end.isoformat(),
                manifest.fiscal_period_type.value,
            ),
        ).fetchall()
        for reviewed_row in reviewed_rows:
            semantic = current_kpi_semantic_context(conn, kpi_fact_id=int(reviewed_row["id"]))
            if semantic is not None and semantic.kpi_definition_revision_id is not None:
                raise ValueError(
                    "issuer_fact_manifest.v1 cannot attest an existing reviewed definition binding"
                )
    rows = conn.execute(
        _ISSUER_MANIFEST_KPI_REPLAY_SQL,
        (
            manifest.ticker.upper(),
            manifest.source_doc_id,
            manifest.period_end.isoformat(),
            manifest.fiscal_period_type.value,
        ),
    ).fetchall()
    captures = _capture_by_identity(manifest) if isinstance(manifest, IssuerFactManifestV2) else {}
    for value in manifest.values:
        if value.kind is not IssuerManifestFactKind.KPI:
            continue
        capture = captures.get(value.expected().identity_key)
        if isinstance(manifest, IssuerFactManifestV2):
            if capture is None:
                raise ValueError("v2 KPI is missing its reviewed definition capture")
            _assert_reviewed_capture_against_sqlite(conn, manifest, value, capture)
        matching = [
            row
            for row in rows
            if (
                int(row["kpi_definition_id"]) == capture.expected_kpi_definition_id
                if capture is not None
                else normalize_kpi_name(str(row["name"]))
                == normalize_kpi_name(value.canonical_name)
            )
        ]
        if len(matching) > 1:
            raise ValueError(
                f"same-document KPI {value.canonical_name!r} has duplicate existing captures"
            )
        for row in matching:
            stored_currency = None if row["currency"] is None else str(row["currency"])
            incoming_currency = value.currency.value if value.currency is not None else None
            currency_conflict = (
                stored_currency != incoming_currency
                if capture is not None
                else stored_currency is not None and stored_currency != incoming_currency
            )
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
            fact_id = int(row["id"])
            semantic = current_kpi_semantic_context(conn, kpi_fact_id=fact_id)
            if capture is None:
                if semantic is not None and semantic.kpi_definition_revision_id is not None:
                    raise ValueError(
                        "issuer_fact_manifest.v1 cannot attest an existing reviewed definition binding"
                    )
            else:
                if (
                    semantic is None
                    or semantic.context != capture.context
                    or semantic.reviewed_by != capture.reviewer
                    or semantic.knowledge_at != capture.knowledge_at
                    or semantic.kpi_definition_revision_id
                    != capture.definition_revision.kpi_definition_revision_id
                ):
                    raise ValueError("same-document KPI reviewed semantic binding changed")


def _assert_segment_replays_compatible(
    conn: sqlite3.Connection, manifest: IssuerFactManifestAny
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


def validate_issuer_fact_manifest_against_sqlite(
    conn: sqlite3.Connection, manifest: IssuerFactManifestAny
) -> None:
    """Validate one typed manifest against its document and persisted fact rows.

    Before application, absent rows are allowed because this same seam guards
    the pending write. Once rows exist, any same-document identity must match
    its exact value, unit, currency, locator, and persisted excerpt provenance.
    Receipt persistence reuses this public check so a rehashed evidence blob
    cannot claim semantics different from the canonical SQLite rows.
    """
    manifest = parse_issuer_fact_manifest(manifest.model_dump(mode="json"))
    validate_issuer_fact_manifest_knowledge_time(manifest, now=datetime.now(UTC))
    _source_document(conn, manifest)
    _assert_kpi_replays_compatible(conn, manifest)
    _assert_segment_replays_compatible(conn, manifest)


def _period_datetime(period_end: date) -> datetime:
    return datetime.combine(period_end, time.min)


def _apply_kpis(
    conn: sqlite3.Connection, manifest: IssuerFactManifestAny
) -> tuple[int, int, int, int, int, tuple[str, ...]]:
    if isinstance(manifest, IssuerFactManifestV2):
        captures = _capture_by_identity(manifest)
        effects: list[SourceReviewedKpiCaptureResult] = []
        for value in manifest.values:
            if value.kind is not IssuerManifestFactKind.KPI:
                continue
            capture = captures[value.expected().identity_key]
            effects.append(
                insert_source_reviewed_kpi_capture(
                    conn,
                    ticker=manifest.ticker.upper(),
                    period_end=_period_datetime(manifest.period_end),
                    fiscal_period_type=manifest.fiscal_period_type,
                    source_doc_id=manifest.source_doc_id,
                    kpi_definition_id=capture.expected_kpi_definition_id,
                    expected_definition_name=value.canonical_name,
                    value=value.value,
                    unit=value.unit,
                    currency=value.currency,
                    locator=value.locator,
                    source_excerpt=value.source_excerpt,
                    reviewer=capture.reviewer,
                    knowledge_at=capture.knowledge_at,
                    context=capture.context,
                    definition_revision=capture.definition_revision,
                    comparability_revisions=capture.comparability_revisions,
                    expected_definition_head_id=capture.expected_definition_head_id,
                    expected_definition_revision=capture.expected_definition_revision,
                )
            )
        return (
            sum(effect.fact_inserted for effect in effects),
            sum(not effect.fact_inserted for effect in effects),
            sum(effect.semantic_context_inserted for effect in effects),
            sum(effect.definition_revision_inserted for effect in effects),
            sum(effect.comparability_revisions_inserted for effect in effects),
            tuple(effect.definition_revision_id for effect in effects),
        )
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
        return (0, 0, 0, 0, 0, ())
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
    return (result.inserted, result.skipped_existing, 0, 0, 0, ())


def _apply_segments(conn: sqlite3.Connection, manifest: IssuerFactManifestAny) -> tuple[int, int]:
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
    conn: sqlite3.Connection, manifest: IssuerFactManifestAny, *, apply: bool = False
) -> IssuerManifestApplyResult:
    """Validate or atomically apply one issuer manifest.

    ``apply=False`` performs no writes.  ``apply=True`` rolls back all KPI,
    segment, and receipt writes when any expected fact remains missing.
    """
    # Frozen Pydantic models can still be constructed with unvalidated
    # ``model_copy(update=...)``.  Re-parse at the public boundary so dry-run
    # and apply enforce the same closed schema.
    manifest = parse_issuer_fact_manifest(manifest.model_dump(mode="json", warnings=False))
    validate_issuer_fact_manifest_knowledge_time(manifest, now=datetime.now(UTC))
    validate_issuer_fact_manifest_against_sqlite(conn, manifest)
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
        # Re-run the complete validation inside the write transaction. The
        # dry-run/preflight observation never acts as the concurrency guard.
        validate_issuer_fact_manifest_against_sqlite(conn, manifest)
        (
            kpi_inserted,
            kpi_skipped,
            semantic_inserted,
            definitions_inserted,
            relations_inserted,
            definition_revision_ids,
        ) = _apply_kpis(conn, manifest)
        period_inserted, segment_inserted = _apply_segments(conn, manifest)
        validate_issuer_fact_manifest_against_sqlite(conn, manifest)
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
        semantic_contexts_inserted=semantic_inserted,
        definition_revisions_inserted=definitions_inserted,
        comparability_revisions_inserted=relations_inserted,
        definition_revision_ids=definition_revision_ids,
        missing_count=receipt.missing_count,
        rejected_count=receipt.rejected_count,
        receipt=receipt,
    )
