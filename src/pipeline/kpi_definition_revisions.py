"""Append-only issuer KPI definition and comparability lineage.

This module versions an issuer's own reported KPI meaning. It is deliberately
separate from the source-independent Canonical Metric ontology: a reviewed
issuer definition may later bind to that ontology, but it cannot create that
binding or infer continuity from a label.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.facts import Currency, Unit
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiUnitScale,
    validate_admitted_unit_scale,
)


class KpiDefinitionStatus(StrEnum):
    ADMITTED = "admitted"
    QUARANTINED = "quarantined"


class KpiDefinitionLifecycle(StrEnum):
    ACTIVE = "active"
    DISCONTINUED = "discontinued"


class KpiDefinitionTextStatus(StrEnum):
    VERBATIM = "verbatim"
    NOT_STATED = "not_stated"


class KpiDefinitionPeriodKind(StrEnum):
    INSTANT = "instant"
    DURATION = "duration"
    UNKNOWN = "unknown"


class KpiStockFlowBehavior(StrEnum):
    STOCK = "stock"
    FLOW = "flow"
    RATIO = "ratio"
    OTHER = "other"
    UNKNOWN = "unknown"


class KpiUnitFamily(StrEnum):
    CURRENCY = "currency"
    COUNT = "count"
    PERCENTAGE = "percentage"
    RATIO = "ratio"
    BASIS_POINTS = "basis_points"
    UNKNOWN = "unknown"


class KpiCurrencyDisposition(StrEnum):
    EXPLICIT = "explicit"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class KpiDefinitionRelationKind(StrEnum):
    SAME_DEFINITION = "same_definition"
    RENAMED = "renamed"
    REDEFINED = "redefined"
    RECAST = "recast"
    RESTATED = "restated"
    SPLIT = "split"
    COMBINED = "combined"


class KpiDefinitionComparabilityDisposition(StrEnum):
    CONTINUOUS = "continuous"
    COMPARABLE_WITH_BREAK = "comparable_with_break"
    NOT_COMPARABLE = "not_comparable"


KpiDefinitionUnitKey = Unit | Literal["unknown"]

_MONETARY_UNITS = {Unit.ACTUAL, Unit.THOUSANDS, Unit.MILLIONS, Unit.BILLIONS}
_UNIT_FAMILY_BY_UNIT: dict[Unit, KpiUnitFamily] = {
    Unit.ACTUAL: KpiUnitFamily.CURRENCY,
    Unit.THOUSANDS: KpiUnitFamily.CURRENCY,
    Unit.MILLIONS: KpiUnitFamily.CURRENCY,
    Unit.BILLIONS: KpiUnitFamily.CURRENCY,
    Unit.COUNT: KpiUnitFamily.COUNT,
    Unit.PERCENT: KpiUnitFamily.PERCENTAGE,
    Unit.RATIO: KpiUnitFamily.RATIO,
    Unit.BPS: KpiUnitFamily.BASIS_POINTS,
}


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _db_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _row_dict(cursor: sqlite3.Cursor, row: object) -> dict[str, object]:
    if isinstance(row, sqlite3.Row):
        return cast("dict[str, object]", dict(row))
    if not isinstance(row, tuple) or cursor.description is None:
        raise TypeError("KPI definition query returned an invalid row")
    description = cast("tuple[tuple[object, ...], ...]", cursor.description)
    values = cast("tuple[object, ...]", row)
    return dict(zip((str(column[0]) for column in description), values, strict=True))


@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str):
    conn.execute(f"SAVEPOINT {name}")  # nosec B608 -- fixed internal identifier
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")  # nosec B608
        conn.execute(f"RELEASE SAVEPOINT {name}")  # nosec B608
        raise
    conn.execute(f"RELEASE SAVEPOINT {name}")  # nosec B608


class IssuerKpiDefinitionRevision(BaseModel):
    """One source-evidenced version of an issuer-reported KPI definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kpi_definition_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    kpi_definition_id: int = Field(gt=0)
    reporting_entity_id: str = Field(min_length=1, max_length=128)
    scope_security_id: str | None = Field(default=None, min_length=1, max_length=128)
    revision: int = Field(gt=0)
    supersedes_definition_revision_id: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    status: KpiDefinitionStatus
    lifecycle: KpiDefinitionLifecycle
    reported_label: str = Field(min_length=1, max_length=512)
    reported_definition_text: str | None = Field(default=None, min_length=1)
    definition_text_status: KpiDefinitionTextStatus
    period_kind: KpiDefinitionPeriodKind
    stock_flow_behavior: KpiStockFlowBehavior
    unit_family: KpiUnitFamily
    unit_key: KpiDefinitionUnitKey
    unit_scale: KpiUnitScale
    currency_disposition: KpiCurrencyDisposition
    currency: Currency | None = None
    accounting_basis: KpiAccountingBasis
    consolidation_scope: KpiConsolidationScope
    dimensions: dict[str, str] = Field(default_factory=dict)
    source_document_version_id: str = Field(min_length=1, max_length=128)
    source_evidence_node_id: str = Field(min_length=1, max_length=128)
    source_locator: dict[str, object] = Field(min_length=1)
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)
    reviewed_by: str = Field(min_length=1, max_length=128)
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime

    @field_validator("reported_label")
    @classmethod
    def _reported_label_is_exact_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reported_label must contain issuer wording")
        return value

    @field_validator("effective_at", "knowledge_at", "recorded_at")
    @classmethod
    def _clock_is_aware(cls, value: datetime, info: object) -> datetime:
        field_name = str(getattr(info, "field_name", "definition clock"))
        return _aware(value, field_name)

    @model_validator(mode="after")
    def _definition_contract(self) -> Self:
        if (self.revision == 1) != (self.supersedes_definition_revision_id is None):
            raise ValueError("revision 1 has no predecessor; later revisions require one")
        if self.knowledge_at > self.recorded_at:
            raise ValueError("definition knowledge_at cannot follow recorded_at")
        if self.definition_text_status is KpiDefinitionTextStatus.VERBATIM:
            if self.reported_definition_text is None:
                raise ValueError("verbatim definition text is required")
        elif self.reported_definition_text is not None:
            raise ValueError("not_stated definition text must be null")
        if self.status is KpiDefinitionStatus.ADMITTED:
            if self.reason_code is not None:
                raise ValueError("admitted definitions cannot carry a quarantine reason")
            if self.period_kind is KpiDefinitionPeriodKind.UNKNOWN:
                raise ValueError("admitted definitions require period kind")
            if self.stock_flow_behavior is KpiStockFlowBehavior.UNKNOWN:
                raise ValueError("admitted definitions require stock/flow behavior")
            if self.unit_family is KpiUnitFamily.UNKNOWN or self.unit_key == "unknown":
                raise ValueError("admitted definitions require exact unit semantics")
            if self.unit_scale is KpiUnitScale.UNKNOWN:
                raise ValueError("admitted definitions require unit scale")
            if self.accounting_basis is KpiAccountingBasis.UNKNOWN:
                raise ValueError("admitted definitions require accounting basis")
            if self.consolidation_scope is KpiConsolidationScope.UNKNOWN:
                raise ValueError("admitted definitions require consolidation scope")
        elif self.reason_code is None:
            raise ValueError("quarantined definitions require a reason_code")
        if (
            self.stock_flow_behavior is KpiStockFlowBehavior.STOCK
            and self.period_kind is not KpiDefinitionPeriodKind.INSTANT
        ):
            raise ValueError("stock definitions require instant periods")
        if (
            self.stock_flow_behavior is KpiStockFlowBehavior.FLOW
            and self.period_kind is not KpiDefinitionPeriodKind.DURATION
        ):
            raise ValueError("flow definitions require duration periods")
        if (
            self.consolidation_scope
            in {
                KpiConsolidationScope.GEOGRAPHY,
                KpiConsolidationScope.SEGMENT,
                KpiConsolidationScope.PRODUCT,
            }
            and not self.dimensions
        ):
            raise ValueError("scoped definitions require dimensions")
        if self.unit_key != "unknown":
            expected_family = _UNIT_FAMILY_BY_UNIT[self.unit_key]
            if self.unit_family not in {expected_family, KpiUnitFamily.UNKNOWN}:
                raise ValueError("unit_family must match the existing Unit authority")
            if (
                self.status is KpiDefinitionStatus.ADMITTED
                and self.unit_family is not expected_family
            ):
                raise ValueError("admitted unit_family must match the existing Unit authority")
            if self.unit_scale is not KpiUnitScale.UNKNOWN:
                validate_admitted_unit_scale(self.unit_key, self.unit_scale)
            monetary = self.unit_key in _MONETARY_UNITS
            if (
                monetary
                and self.status is KpiDefinitionStatus.ADMITTED
                and (
                    self.currency_disposition is not KpiCurrencyDisposition.EXPLICIT
                    or self.currency is None
                )
            ):
                raise ValueError("admitted monetary definitions require explicit currency")
            if not monetary and (
                self.currency_disposition is not KpiCurrencyDisposition.NOT_APPLICABLE
                or self.currency is not None
            ):
                raise ValueError("non-currency definitions require not_applicable currency")
        if self.currency_disposition is KpiCurrencyDisposition.EXPLICIT and self.currency is None:
            raise ValueError("explicit currency disposition requires currency")
        if (
            self.currency_disposition is not KpiCurrencyDisposition.EXPLICIT
            and self.currency is not None
        ):
            raise ValueError("currency is allowed only with explicit disposition")
        if (
            self.status is KpiDefinitionStatus.ADMITTED
            and self.currency_disposition is KpiCurrencyDisposition.UNKNOWN
        ):
            raise ValueError("admitted definitions cannot have unknown currency applicability")
        return self

    @property
    def source_locator_json(self) -> str:
        return _canonical_json(self.source_locator)

    @property
    def source_locator_sha256(self) -> str:
        return _sha256(self.source_locator_json)

    @property
    def commitment_payload(self) -> dict[str, object]:
        return {
            "accounting_basis": self.accounting_basis.value,
            "consolidation_scope": self.consolidation_scope.value,
            "currency": None if self.currency is None else self.currency.value,
            "currency_disposition": self.currency_disposition.value,
            "definition_text_status": self.definition_text_status.value,
            "dimensions": self.dimensions,
            "effective_at": _utc_text(self.effective_at),
            "idempotency_key": self.idempotency_key,
            "knowledge_at": _utc_text(self.knowledge_at),
            "kpi_definition_id": self.kpi_definition_id,
            "kpi_definition_revision_id": self.kpi_definition_revision_id,
            "lifecycle": self.lifecycle.value,
            "period_kind": self.period_kind.value,
            "reason_code": self.reason_code,
            "recorded_at": _utc_text(self.recorded_at),
            "reported_definition_text": self.reported_definition_text,
            "reported_label": self.reported_label,
            "reporting_entity_id": self.reporting_entity_id,
            "reviewed_by": self.reviewed_by,
            "revision": self.revision,
            "scope_security_id": self.scope_security_id,
            "source_document_version_id": self.source_document_version_id,
            "source_evidence_node_id": self.source_evidence_node_id,
            "source_locator": self.source_locator,
            "status": self.status.value,
            "stock_flow_behavior": self.stock_flow_behavior.value,
            "supersedes_definition_revision_id": self.supersedes_definition_revision_id,
            "unit_family": self.unit_family.value,
            "unit_key": str(self.unit_key),
            "unit_scale": self.unit_scale.value,
        }

    @property
    def commitment_json(self) -> str:
        return _canonical_json(self.commitment_payload)

    @property
    def commitment_sha256(self) -> str:
        return _sha256(self.commitment_json)


class KpiDefinitionComparabilityRevision(BaseModel):
    """One source-evidenced direct comparison between exact definition revisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    comparability_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    predecessor_definition_revision_id: str = Field(min_length=1, max_length=128)
    successor_definition_revision_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(gt=0)
    supersedes_comparability_revision_id: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    relation_kind: KpiDefinitionRelationKind
    disposition: KpiDefinitionComparabilityDisposition
    reason_code: str = Field(min_length=1, max_length=128)
    reviewed_by: str = Field(min_length=1, max_length=128)
    source_document_version_id: str = Field(min_length=1, max_length=128)
    source_evidence_node_id: str = Field(min_length=1, max_length=128)
    source_locator: dict[str, object] = Field(min_length=1)
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime

    @field_validator("effective_at", "knowledge_at", "recorded_at")
    @classmethod
    def _clock_is_aware(cls, value: datetime, info: object) -> datetime:
        field_name = str(getattr(info, "field_name", "comparability clock"))
        return _aware(value, field_name)

    @model_validator(mode="after")
    def _comparability_contract(self) -> Self:
        if self.predecessor_definition_revision_id == self.successor_definition_revision_id:
            raise ValueError("comparability requires two different definition revisions")
        if (self.revision == 1) != (self.supersedes_comparability_revision_id is None):
            raise ValueError("comparability revision 1 has no predecessor")
        if self.knowledge_at > self.recorded_at:
            raise ValueError("comparability knowledge_at cannot follow recorded_at")
        if self.disposition is KpiDefinitionComparabilityDisposition.CONTINUOUS and (
            self.relation_kind
            not in {
                KpiDefinitionRelationKind.SAME_DEFINITION,
                KpiDefinitionRelationKind.RENAMED,
            }
        ):
            raise ValueError("only same-definition or renamed relations can be continuous")
        return self

    @property
    def source_locator_json(self) -> str:
        return _canonical_json(self.source_locator)

    @property
    def source_locator_sha256(self) -> str:
        return _sha256(self.source_locator_json)

    @property
    def commitment_payload(self) -> dict[str, object]:
        return {
            "comparability_revision_id": self.comparability_revision_id,
            "disposition": self.disposition.value,
            "effective_at": _utc_text(self.effective_at),
            "idempotency_key": self.idempotency_key,
            "knowledge_at": _utc_text(self.knowledge_at),
            "predecessor_definition_revision_id": self.predecessor_definition_revision_id,
            "reason_code": self.reason_code,
            "recorded_at": _utc_text(self.recorded_at),
            "relation_kind": self.relation_kind.value,
            "reviewed_by": self.reviewed_by,
            "revision": self.revision,
            "source_document_version_id": self.source_document_version_id,
            "source_evidence_node_id": self.source_evidence_node_id,
            "source_locator": self.source_locator,
            "successor_definition_revision_id": self.successor_definition_revision_id,
            "supersedes_comparability_revision_id": self.supersedes_comparability_revision_id,
        }

    @property
    def commitment_json(self) -> str:
        return _canonical_json(self.commitment_payload)

    @property
    def commitment_sha256(self) -> str:
        return _sha256(self.commitment_json)


_DEFINITION_COLUMNS = (
    "kpi_definition_revision_id,idempotency_key,kpi_definition_id,reporting_entity_id,"
    "scope_security_id,revision,supersedes_definition_revision_id,status,lifecycle,"
    "reported_label,reported_definition_text,definition_text_status,period_kind,"
    "stock_flow_behavior,unit_family,unit_key,unit_scale,currency_disposition,currency,"
    "accounting_basis,consolidation_scope,dimensions_json,source_document_version_id,"
    "source_evidence_node_id,source_locator_json,source_locator_sha256,reason_code,reviewed_by,"
    "commitment_json,commitment_sha256,effective_at,knowledge_at,recorded_at"
)

_COMPARABILITY_COLUMNS = (
    "comparability_revision_id,idempotency_key,predecessor_definition_revision_id,"
    "successor_definition_revision_id,revision,supersedes_comparability_revision_id,"
    "relation_kind,disposition,reason_code,reviewed_by,source_document_version_id,"
    "source_evidence_node_id,source_locator_json,source_locator_sha256,commitment_json,"
    "commitment_sha256,effective_at,knowledge_at,recorded_at"
)


def _definition_from_row(row: dict[str, object]) -> IssuerKpiDefinitionRevision:
    raw_unit = str(row["unit_key"])
    unit_key: KpiDefinitionUnitKey = "unknown" if raw_unit == "unknown" else Unit(raw_unit)
    return IssuerKpiDefinitionRevision(
        kpi_definition_revision_id=str(row["kpi_definition_revision_id"]),
        idempotency_key=str(row["idempotency_key"]),
        kpi_definition_id=int(str(row["kpi_definition_id"])),
        reporting_entity_id=str(row["reporting_entity_id"]),
        scope_security_id=(
            None if row["scope_security_id"] is None else str(row["scope_security_id"])
        ),
        revision=int(str(row["revision"])),
        supersedes_definition_revision_id=(
            None
            if row["supersedes_definition_revision_id"] is None
            else str(row["supersedes_definition_revision_id"])
        ),
        status=KpiDefinitionStatus(str(row["status"])),
        lifecycle=KpiDefinitionLifecycle(str(row["lifecycle"])),
        reported_label=str(row["reported_label"]),
        reported_definition_text=(
            None
            if row["reported_definition_text"] is None
            else str(row["reported_definition_text"])
        ),
        definition_text_status=KpiDefinitionTextStatus(str(row["definition_text_status"])),
        period_kind=KpiDefinitionPeriodKind(str(row["period_kind"])),
        stock_flow_behavior=KpiStockFlowBehavior(str(row["stock_flow_behavior"])),
        unit_family=KpiUnitFamily(str(row["unit_family"])),
        unit_key=unit_key,
        unit_scale=KpiUnitScale(str(row["unit_scale"])),
        currency_disposition=KpiCurrencyDisposition(str(row["currency_disposition"])),
        currency=None if row["currency"] is None else Currency(str(row["currency"])),
        accounting_basis=KpiAccountingBasis(str(row["accounting_basis"])),
        consolidation_scope=KpiConsolidationScope(str(row["consolidation_scope"])),
        dimensions=cast("dict[str, str]", json.loads(str(row["dimensions_json"]))),
        source_document_version_id=str(row["source_document_version_id"]),
        source_evidence_node_id=str(row["source_evidence_node_id"]),
        source_locator=cast("dict[str, object]", json.loads(str(row["source_locator_json"]))),
        reason_code=None if row["reason_code"] is None else str(row["reason_code"]),
        reviewed_by=str(row["reviewed_by"]),
        effective_at=_db_datetime(row["effective_at"]),
        knowledge_at=_db_datetime(row["knowledge_at"]),
        recorded_at=_db_datetime(row["recorded_at"]),
    )


def _comparability_from_row(row: dict[str, object]) -> KpiDefinitionComparabilityRevision:
    return KpiDefinitionComparabilityRevision(
        comparability_revision_id=str(row["comparability_revision_id"]),
        idempotency_key=str(row["idempotency_key"]),
        predecessor_definition_revision_id=str(row["predecessor_definition_revision_id"]),
        successor_definition_revision_id=str(row["successor_definition_revision_id"]),
        revision=int(str(row["revision"])),
        supersedes_comparability_revision_id=(
            None
            if row["supersedes_comparability_revision_id"] is None
            else str(row["supersedes_comparability_revision_id"])
        ),
        relation_kind=KpiDefinitionRelationKind(str(row["relation_kind"])),
        disposition=KpiDefinitionComparabilityDisposition(str(row["disposition"])),
        reason_code=str(row["reason_code"]),
        reviewed_by=str(row["reviewed_by"]),
        source_document_version_id=str(row["source_document_version_id"]),
        source_evidence_node_id=str(row["source_evidence_node_id"]),
        source_locator=cast("dict[str, object]", json.loads(str(row["source_locator_json"]))),
        effective_at=_db_datetime(row["effective_at"]),
        knowledge_at=_db_datetime(row["knowledge_at"]),
        recorded_at=_db_datetime(row["recorded_at"]),
    )


def _definition_by_id(
    conn: sqlite3.Connection, definition_revision_id: str
) -> IssuerKpiDefinitionRevision | None:
    cursor = conn.execute(
        f"SELECT {_DEFINITION_COLUMNS} FROM kpi_definition_revisions "  # nosec B608
        "WHERE kpi_definition_revision_id=?",
        (definition_revision_id,),
    )
    row = cursor.fetchone()
    return None if row is None else _definition_from_row(_row_dict(cursor, row))


def kpi_definition_revision_by_id(
    conn: sqlite3.Connection, *, kpi_definition_revision_id: str
) -> IssuerKpiDefinitionRevision | None:
    """Return one exact immutable revision identity."""

    return _definition_by_id(conn, kpi_definition_revision_id)


def _validate_source_evidence(
    conn: sqlite3.Connection,
    *,
    reporting_entity_id: str,
    scope_security_id: str | None,
    source_document_version_id: str,
    source_evidence_node_id: str,
    source_locator_json: str,
    source_locator_sha256: str,
    knowledge_at: datetime,
    recorded_at: datetime,
) -> str:
    cursor = conn.execute(
        "SELECT document.issuer_id,source.retrieved_at,document.recorded_at AS document_recorded_at,"
        "node.recorded_at AS node_recorded_at,node.locator_json,node.locator_sha256,run.outcome "
        "FROM evidence_document_versions document "
        "JOIN evidence_source_observations source ON source.observation_id=document.observation_id "
        "JOIN evidence_extraction_runs run ON run.document_version_id=document.document_version_id "
        "JOIN evidence_nodes node ON node.extraction_run_id=run.extraction_run_id "
        "WHERE document.document_version_id=? AND node.node_id=?",
        (source_document_version_id, source_evidence_node_id),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise ValueError("definition evidence node must belong to the exact document version")
    evidence = _row_dict(cursor, rows[0])
    if str(evidence["outcome"]) != "succeeded":
        raise ValueError("definition evidence requires a succeeded extraction run")
    if evidence["locator_json"] is None or evidence["locator_sha256"] is None:
        raise ValueError("definition evidence node requires an exact locator")
    try:
        evidence_locator_json = _canonical_json(json.loads(str(evidence["locator_json"])))
    except json.JSONDecodeError as exc:
        raise ValueError("definition evidence node locator is invalid") from exc
    if (
        evidence_locator_json != source_locator_json
        or str(evidence["locator_sha256"]) != source_locator_sha256
        or _sha256(evidence_locator_json) != source_locator_sha256
    ):
        raise ValueError("definition source locator must match its exact evidence node")
    entity = conn.execute(
        "SELECT issuer_id FROM reporting_entities WHERE reporting_entity_id=?",
        (reporting_entity_id,),
    ).fetchone()
    if entity is None or str(entity[0]) != str(evidence["issuer_id"]):
        raise ValueError("definition evidence and reporting entity issuer must agree")
    if scope_security_id is not None:
        security = conn.execute(
            "SELECT issuer_id FROM securities WHERE security_id=?", (scope_security_id,)
        ).fetchone()
        if security is None or str(security[0]) != str(evidence["issuer_id"]):
            raise ValueError("definition scope security must belong to the evidence issuer")
    retrieved_at = _db_datetime(evidence["retrieved_at"])
    document_recorded_at = _db_datetime(evidence["document_recorded_at"])
    node_recorded_at = _db_datetime(evidence["node_recorded_at"])
    if retrieved_at > knowledge_at:
        raise ValueError("definition knowledge predates source retrieval")
    if document_recorded_at > recorded_at or node_recorded_at > recorded_at:
        raise ValueError("definition recording predates its evidence")
    return str(evidence["issuer_id"])


def validate_kpi_definition_revision_candidate(
    conn: sqlite3.Connection,
    revision: IssuerKpiDefinitionRevision,
    *,
    expected_definition_head_id: str | None,
    expected_definition_revision: int,
) -> None:
    """Read-validate one proposed definition against the exact current lifecycle."""

    if (
        conn.execute(
            "SELECT 1 FROM kpi_definitions WHERE id=?", (revision.kpi_definition_id,)
        ).fetchone()
        is None
    ):
        raise ValueError("definition revision root is missing")
    if (
        conn.execute(
            "SELECT 1 FROM kpi_definition_revisions WHERE kpi_definition_revision_id=? ",
            (revision.kpi_definition_revision_id,),
        ).fetchone()
        is not None
    ):
        raise ValueError("definition revision identity is already persisted")
    if (
        conn.execute(
            "SELECT 1 FROM kpi_definition_revisions WHERE idempotency_key=?",
            (revision.idempotency_key,),
        ).fetchone()
        is not None
    ):
        raise ValueError("definition revision idempotency key is already persisted")
    revision_issuer = _validate_source_evidence(
        conn,
        reporting_entity_id=revision.reporting_entity_id,
        scope_security_id=revision.scope_security_id,
        source_document_version_id=revision.source_document_version_id,
        source_evidence_node_id=revision.source_evidence_node_id,
        source_locator_json=revision.source_locator_json,
        source_locator_sha256=revision.source_locator_sha256,
        knowledge_at=revision.knowledge_at,
        recorded_at=revision.recorded_at,
    )
    current = current_kpi_definition_revision(conn, kpi_definition_id=revision.kpi_definition_id)
    actual_head = None if current is None else current.kpi_definition_revision_id
    actual_revision = 0 if current is None else current.revision
    if (actual_head, actual_revision) != (
        expected_definition_head_id,
        expected_definition_revision,
    ):
        raise ValueError("KPI definition revision head changed after source review")
    if (
        revision.revision != expected_definition_revision + 1
        or revision.supersedes_definition_revision_id != expected_definition_head_id
    ):
        raise ValueError("definition revision does not extend the exact current head")
    if current is not None and (
        revision.knowledge_at < current.knowledge_at or revision.recorded_at < current.recorded_at
    ):
        raise ValueError("definition revision clocks cannot precede the current head")
    if current is not None:
        current_issuer = conn.execute(
            "SELECT issuer_id FROM reporting_entities WHERE reporting_entity_id=?",
            (current.reporting_entity_id,),
        ).fetchone()
        if current_issuer is None or str(current_issuer[0]) != revision_issuer:
            raise ValueError("definition revisions cannot cross issuer boundaries")


def validate_kpi_definition_comparability_candidate(
    conn: sqlite3.Connection,
    revision: KpiDefinitionComparabilityRevision,
    *,
    proposed_definition: IssuerKpiDefinitionRevision | None = None,
) -> None:
    """Read-validate one direct relation, optionally against one proposed definition."""

    if (
        conn.execute(
            "SELECT 1 FROM kpi_definition_comparability_revisions "
            "WHERE comparability_revision_id=?",
            (revision.comparability_revision_id,),
        ).fetchone()
        is not None
    ):
        raise ValueError("comparability revision identity is already persisted")
    if (
        conn.execute(
            "SELECT 1 FROM kpi_definition_comparability_revisions WHERE idempotency_key=?",
            (revision.idempotency_key,),
        ).fetchone()
        is not None
    ):
        raise ValueError("comparability idempotency key is already persisted")

    proposed_id = (
        None if proposed_definition is None else proposed_definition.kpi_definition_revision_id
    )
    if proposed_id is not None and proposed_id not in {
        revision.predecessor_definition_revision_id,
        revision.successor_definition_revision_id,
    }:
        raise ValueError("comparability capture must relate the captured definition")

    def definition_by_id(definition_id: str) -> IssuerKpiDefinitionRevision | None:
        if proposed_definition is not None and definition_id == proposed_id:
            return proposed_definition
        return _definition_by_id(conn, definition_id)

    predecessor = definition_by_id(revision.predecessor_definition_revision_id)
    successor = definition_by_id(revision.successor_definition_revision_id)
    if predecessor is None or successor is None:
        raise ValueError("comparability requires two definition revisions")
    predecessor_issuer = _validate_source_evidence(
        conn,
        reporting_entity_id=predecessor.reporting_entity_id,
        scope_security_id=predecessor.scope_security_id,
        source_document_version_id=predecessor.source_document_version_id,
        source_evidence_node_id=predecessor.source_evidence_node_id,
        source_locator_json=predecessor.source_locator_json,
        source_locator_sha256=predecessor.source_locator_sha256,
        knowledge_at=predecessor.knowledge_at,
        recorded_at=predecessor.recorded_at,
    )
    successor_issuer = _validate_source_evidence(
        conn,
        reporting_entity_id=successor.reporting_entity_id,
        scope_security_id=successor.scope_security_id,
        source_document_version_id=successor.source_document_version_id,
        source_evidence_node_id=successor.source_evidence_node_id,
        source_locator_json=successor.source_locator_json,
        source_locator_sha256=successor.source_locator_sha256,
        knowledge_at=successor.knowledge_at,
        recorded_at=successor.recorded_at,
    )
    relation_issuer = _validate_source_evidence(
        conn,
        reporting_entity_id=successor.reporting_entity_id,
        scope_security_id=successor.scope_security_id,
        source_document_version_id=revision.source_document_version_id,
        source_evidence_node_id=revision.source_evidence_node_id,
        source_locator_json=revision.source_locator_json,
        source_locator_sha256=revision.source_locator_sha256,
        knowledge_at=revision.knowledge_at,
        recorded_at=revision.recorded_at,
    )
    if len({predecessor_issuer, successor_issuer, relation_issuer}) != 1:
        raise ValueError("comparability cannot cross issuer boundaries")
    if max(predecessor.knowledge_at, successor.knowledge_at) > revision.knowledge_at:
        raise ValueError("comparability knowledge cannot predate either definition revision")
    if max(predecessor.recorded_at, successor.recorded_at) > revision.recorded_at:
        raise ValueError("comparability recording cannot predate either definition revision")
    if revision.disposition is KpiDefinitionComparabilityDisposition.CONTINUOUS and (
        predecessor.unit_family != successor.unit_family
        or predecessor.unit_key != successor.unit_key
        or predecessor.unit_scale != successor.unit_scale
    ):
        raise ValueError(
            "a unit or presentation-scale change requires an explicit comparability break"
        )
    if revision.disposition is KpiDefinitionComparabilityDisposition.CONTINUOUS and (
        predecessor.period_kind != successor.period_kind
        or predecessor.stock_flow_behavior != successor.stock_flow_behavior
        or predecessor.accounting_basis != successor.accounting_basis
        or predecessor.consolidation_scope != successor.consolidation_scope
        or predecessor.dimensions != successor.dimensions
    ):
        raise ValueError("a semantic-axis change requires an explicit comparability break")
    if revision.disposition is KpiDefinitionComparabilityDisposition.CONTINUOUS and (
        predecessor.currency_disposition != successor.currency_disposition
        or predecessor.currency != successor.currency
    ):
        raise ValueError("a currency change requires an explicit comparability break")
    reverse = conn.execute(
        "SELECT 1 FROM kpi_definition_comparability_revisions "
        "WHERE predecessor_definition_revision_id=? "
        "AND successor_definition_revision_id=? LIMIT 1",
        (
            revision.successor_definition_revision_id,
            revision.predecessor_definition_revision_id,
        ),
    ).fetchone()
    if reverse is not None:
        raise ValueError("reversed comparability pair already exists")
    current = current_kpi_definition_comparability_revision(
        conn,
        predecessor_definition_revision_id=revision.predecessor_definition_revision_id,
        successor_definition_revision_id=revision.successor_definition_revision_id,
    )
    if revision.revision == 1:
        if current is not None:
            raise ValueError("comparability revision 1 conflicts with an existing lifecycle")
    elif (
        current is None
        or current.comparability_revision_id != revision.supersedes_comparability_revision_id
        or current.revision + 1 != revision.revision
    ):
        raise ValueError("comparability revision does not supersede the exact current head")
    if current is not None and (
        revision.knowledge_at < current.knowledge_at or revision.recorded_at < current.recorded_at
    ):
        raise ValueError("comparability clocks cannot precede the current head")


def persist_kpi_definition_revision(
    conn: sqlite3.Connection, revision: IssuerKpiDefinitionRevision
) -> IssuerKpiDefinitionRevision:
    """Append one definition revision or return an exact idempotent replay."""

    with _savepoint(conn, "persist_kpi_definition_revision"):
        cursor = conn.execute(
            f"SELECT {_DEFINITION_COLUMNS} "  # nosec B608
            "FROM kpi_definition_revisions WHERE idempotency_key=?",
            (revision.idempotency_key,),
        )
        replay_row = cursor.fetchone()
        if replay_row is not None:
            replay = _row_dict(cursor, replay_row)
            if (
                str(replay["commitment_json"]) != revision.commitment_json
                or str(replay["commitment_sha256"]) != revision.commitment_sha256
            ):
                raise ValueError("definition idempotency key conflicts with persisted content")
            return _definition_from_row(replay)
        current = current_kpi_definition_revision(
            conn, kpi_definition_id=revision.kpi_definition_id
        )
        validate_kpi_definition_revision_candidate(
            conn,
            revision,
            expected_definition_head_id=(
                None if current is None else current.kpi_definition_revision_id
            ),
            expected_definition_revision=0 if current is None else current.revision,
        )
        values: tuple[object, ...] = (
            revision.kpi_definition_revision_id,
            revision.idempotency_key,
            revision.kpi_definition_id,
            revision.reporting_entity_id,
            revision.scope_security_id,
            revision.revision,
            revision.supersedes_definition_revision_id,
            revision.status.value,
            revision.lifecycle.value,
            revision.reported_label,
            revision.reported_definition_text,
            revision.definition_text_status.value,
            revision.period_kind.value,
            revision.stock_flow_behavior.value,
            revision.unit_family.value,
            str(revision.unit_key),
            revision.unit_scale.value,
            revision.currency_disposition.value,
            None if revision.currency is None else revision.currency.value,
            revision.accounting_basis.value,
            revision.consolidation_scope.value,
            _canonical_json(revision.dimensions),
            revision.source_document_version_id,
            revision.source_evidence_node_id,
            revision.source_locator_json,
            revision.source_locator_sha256,
            revision.reason_code,
            revision.reviewed_by,
            revision.commitment_json,
            revision.commitment_sha256,
            _utc_text(revision.effective_at),
            _utc_text(revision.knowledge_at),
            _utc_text(revision.recorded_at),
        )
        conn.execute(
            f"INSERT INTO kpi_definition_revisions ({_DEFINITION_COLUMNS}) "  # nosec B608
            f"VALUES ({','.join('?' for _ in values)})",  # nosec B608
            values,
        )
    persisted = _definition_by_id(conn, revision.kpi_definition_revision_id)
    if persisted is None or persisted.commitment_sha256 != revision.commitment_sha256:
        raise RuntimeError("definition revision did not round-trip exactly")
    return persisted


def current_kpi_definition_revision(
    conn: sqlite3.Connection, *, kpi_definition_id: int
) -> IssuerKpiDefinitionRevision | None:
    """Return the one append-chain head, independent of its effective date."""

    cursor = conn.execute(
        f"SELECT {_DEFINITION_COLUMNS} FROM kpi_definition_revisions definition "  # nosec B608
        "WHERE definition.kpi_definition_id=? AND NOT EXISTS ("
        "SELECT 1 FROM kpi_definition_revisions successor "
        "WHERE successor.supersedes_definition_revision_id="
        "definition.kpi_definition_revision_id) ORDER BY definition.revision DESC",
        (kpi_definition_id,),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise RuntimeError("KPI definition lifecycle has multiple current heads")
    return None if not rows else _definition_from_row(_row_dict(cursor, rows[0]))


def kpi_definition_revision_as_known(
    conn: sqlite3.Connection,
    *,
    kpi_definition_id: int,
    effective_at: datetime,
    known_at: datetime,
) -> IssuerKpiDefinitionRevision | None:
    """Select by effective and knowledge time without filtering lifecycle state."""

    _aware(effective_at, "effective_at")
    _aware(known_at, "known_at")
    cursor = conn.execute(
        f"SELECT {_DEFINITION_COLUMNS} FROM kpi_definition_revisions "  # nosec B608
        "WHERE kpi_definition_id=? AND datetime(effective_at)<=datetime(?) "
        "AND datetime(knowledge_at)<=datetime(?) AND datetime(recorded_at)<=datetime(?) "
        "ORDER BY datetime(effective_at) DESC,datetime(knowledge_at) DESC,revision DESC LIMIT 1",
        (
            kpi_definition_id,
            _utc_text(effective_at),
            _utc_text(known_at),
            _utc_text(known_at),
        ),
    )
    row = cursor.fetchone()
    return None if row is None else _definition_from_row(_row_dict(cursor, row))


def persist_kpi_definition_comparability_revision(
    conn: sqlite3.Connection, revision: KpiDefinitionComparabilityRevision
) -> KpiDefinitionComparabilityRevision:
    """Append an exact pairwise disposition or return an exact replay."""

    with _savepoint(conn, "persist_kpi_definition_comparability"):
        cursor = conn.execute(
            f"SELECT {_COMPARABILITY_COLUMNS} FROM kpi_definition_comparability_revisions "  # nosec B608
            "WHERE idempotency_key=?",
            (revision.idempotency_key,),
        )
        replay_row = cursor.fetchone()
        if replay_row is not None:
            replay = _row_dict(cursor, replay_row)
            if (
                str(replay["commitment_json"]) != revision.commitment_json
                or str(replay["commitment_sha256"]) != revision.commitment_sha256
            ):
                raise ValueError("comparability idempotency key conflicts with persisted content")
            return _comparability_from_row(replay)
        validate_kpi_definition_comparability_candidate(conn, revision)
        values: tuple[object, ...] = (
            revision.comparability_revision_id,
            revision.idempotency_key,
            revision.predecessor_definition_revision_id,
            revision.successor_definition_revision_id,
            revision.revision,
            revision.supersedes_comparability_revision_id,
            revision.relation_kind.value,
            revision.disposition.value,
            revision.reason_code,
            revision.reviewed_by,
            revision.source_document_version_id,
            revision.source_evidence_node_id,
            revision.source_locator_json,
            revision.source_locator_sha256,
            revision.commitment_json,
            revision.commitment_sha256,
            _utc_text(revision.effective_at),
            _utc_text(revision.knowledge_at),
            _utc_text(revision.recorded_at),
        )
        conn.execute(
            f"INSERT INTO kpi_definition_comparability_revisions ({_COMPARABILITY_COLUMNS}) "  # nosec B608
            f"VALUES ({','.join('?' for _ in values)})",  # nosec B608
            values,
        )
    persisted = current_kpi_definition_comparability_revision(
        conn,
        predecessor_definition_revision_id=revision.predecessor_definition_revision_id,
        successor_definition_revision_id=revision.successor_definition_revision_id,
    )
    if persisted is None or persisted.commitment_sha256 != revision.commitment_sha256:
        raise RuntimeError("comparability revision did not round-trip exactly")
    return persisted


def current_kpi_definition_comparability_revision(
    conn: sqlite3.Connection,
    *,
    predecessor_definition_revision_id: str,
    successor_definition_revision_id: str,
) -> KpiDefinitionComparabilityRevision | None:
    cursor = conn.execute(
        f"SELECT {_COMPARABILITY_COLUMNS} FROM kpi_definition_comparability_revisions relation "  # nosec B608
        "WHERE relation.predecessor_definition_revision_id=? "
        "AND relation.successor_definition_revision_id=? AND NOT EXISTS ("
        "SELECT 1 FROM kpi_definition_comparability_revisions successor "
        "WHERE successor.supersedes_comparability_revision_id="
        "relation.comparability_revision_id) ORDER BY relation.revision DESC",
        (predecessor_definition_revision_id, successor_definition_revision_id),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise RuntimeError("KPI comparability lifecycle has multiple current heads")
    return None if not rows else _comparability_from_row(_row_dict(cursor, rows[0]))


def kpi_definition_comparability_as_known(
    conn: sqlite3.Connection,
    *,
    first_definition_revision_id: str,
    second_definition_revision_id: str,
    effective_at: datetime,
    known_at: datetime,
) -> KpiDefinitionComparabilityRevision | None:
    """Return the current direct pair disposition at both requested cutoffs."""

    _aware(effective_at, "effective_at")
    _aware(known_at, "known_at")
    cursor = conn.execute(
        f"SELECT {_COMPARABILITY_COLUMNS} FROM kpi_definition_comparability_revisions "  # nosec B608
        "WHERE ((predecessor_definition_revision_id=? AND successor_definition_revision_id=?) "
        "OR (predecessor_definition_revision_id=? AND successor_definition_revision_id=?)) "
        "AND datetime(effective_at)<=datetime(?) AND datetime(knowledge_at)<=datetime(?) "
        "AND datetime(recorded_at)<=datetime(?) "
        "ORDER BY datetime(effective_at) DESC,datetime(knowledge_at) DESC,revision DESC LIMIT 1",
        (
            first_definition_revision_id,
            second_definition_revision_id,
            second_definition_revision_id,
            first_definition_revision_id,
            _utc_text(effective_at),
            _utc_text(known_at),
            _utc_text(known_at),
        ),
    )
    row = cursor.fetchone()
    return None if row is None else _comparability_from_row(_row_dict(cursor, row))
