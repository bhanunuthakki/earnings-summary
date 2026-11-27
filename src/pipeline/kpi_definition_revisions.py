"""Append-only issuer KPI definition and comparability lineage.

This module versions an issuer's own reported KPI meaning. It is deliberately
separate from the source-independent Canonical Metric ontology: a reviewed
issuer definition may later bind to that ontology, but it cannot create that
binding or infer continuity from a label.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.facts import Currency, FiscalPeriodType, Unit
from pipeline.kpi_comparability_bridges import (
    AdditiveBridgeDirection as AdditiveBridgeDirection,
)
from pipeline.kpi_comparability_bridges import (
    AdditiveCompositeBridge as AdditiveCompositeBridge,
)
from pipeline.kpi_comparability_bridges import (
    CumulativeDiffInput as CumulativeDiffInput,
)
from pipeline.kpi_comparability_bridges import (
    DerivedCumulativeDiffObservation as DerivedCumulativeDiffObservation,
)
from pipeline.kpi_comparability_bridges import (
    KpiComparabilityBridgeSpec as KpiComparabilityBridgeSpec,
)
from pipeline.kpi_comparability_bridges import (
    KpiPeriodCadenceKind as KpiPeriodCadenceKind,
)
from pipeline.kpi_comparability_bridges import (
    OverlapTolerancePolicy as OverlapTolerancePolicy,
)
from pipeline.kpi_comparability_bridges import (
    OverlapVerificationError as OverlapVerificationError,
)
from pipeline.kpi_comparability_bridges import (
    OverlapWitness as OverlapWitness,
)
from pipeline.kpi_comparability_bridges import (
    RecastComparativeBridge as RecastComparativeBridge,
)
from pipeline.kpi_comparability_bridges import (
    RenameOverlapBridge as RenameOverlapBridge,
)
from pipeline.kpi_comparability_bridges import (
    SplitScaleAppliesTo as SplitScaleAppliesTo,
)
from pipeline.kpi_comparability_bridges import (
    SplitScaleBridge as SplitScaleBridge,
)
from pipeline.kpi_comparability_bridges import (
    build_overlap_witness as build_overlap_witness,
)
from pipeline.kpi_comparability_bridges import (
    classify_period_cadence as classify_period_cadence,
)
from pipeline.kpi_comparability_bridges import (
    cumulative_diff as cumulative_diff,
)
from pipeline.kpi_comparability_bridges import (
    derive_standalone_from_cumulative as derive_standalone_from_cumulative,
)
from pipeline.kpi_comparability_bridges import (
    evaluate_overlap_witness as evaluate_overlap_witness,
)
from pipeline.kpi_comparability_bridges import (
    parse_bridge_spec_json as parse_bridge_spec_json,
)
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiSemanticContext,
    KpiSemanticStatus,
    KpiUnitScale,
    validate_admitted_unit_scale,
)
from provenance.reporting_entity_registry import ReportingEntityRegistry
from runtime.deterministic import canonical_json_ascii_default_str as _canonical_json
from runtime.deterministic import sha256_text as _sha256
from runtime.deterministic import table_or_view_exists as _table_exists


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
    bridge_spec: KpiComparabilityBridgeSpec | None = None
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
        if isinstance(self.bridge_spec, RenameOverlapBridge):
            if (
                self.relation_kind is not KpiDefinitionRelationKind.RENAMED
                or self.disposition is not KpiDefinitionComparabilityDisposition.CONTINUOUS
            ):
                raise ValueError("rename_overlap bridge requires relation=renamed and continuous")
        elif isinstance(self.bridge_spec, SplitScaleBridge):
            if (
                self.relation_kind is not KpiDefinitionRelationKind.RESTATED
                or self.disposition
                is not KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK
            ):
                raise ValueError(
                    "scale_ratio bridge requires relation=restated and comparable_with_break"
                )
        elif isinstance(self.bridge_spec, AdditiveCompositeBridge):
            if self.disposition is not KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK:
                raise ValueError("sum_of_revisions bridge requires comparable_with_break")
            if self.bridge_spec.direction == "combine":
                if self.relation_kind is not KpiDefinitionRelationKind.COMBINED:
                    raise ValueError("combine bridge requires relation_kind=combined")
                if (
                    self.predecessor_definition_revision_id
                    not in self.bridge_spec.member_definition_revision_ids
                    or self.successor_definition_revision_id
                    != self.bridge_spec.composite_definition_revision_id
                ):
                    raise ValueError(
                        "combine edge must link one member predecessor to the composite successor"
                    )
            else:
                if self.relation_kind is not KpiDefinitionRelationKind.SPLIT:
                    raise ValueError("split bridge requires relation_kind=split")
                if (
                    self.predecessor_definition_revision_id
                    != self.bridge_spec.composite_definition_revision_id
                    or self.successor_definition_revision_id
                    not in self.bridge_spec.member_definition_revision_ids
                ):
                    raise ValueError(
                        "split edge must link the composite predecessor to one member successor"
                    )
        elif isinstance(self.bridge_spec, RecastComparativeBridge) and (
            self.relation_kind is not KpiDefinitionRelationKind.RECAST
            or self.disposition is not KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK
        ):
            raise ValueError(
                "recast_comparative_only bridge requires relation=recast and comparable_with_break"
            )
        return self

    @property
    def source_locator_json(self) -> str:
        return _canonical_json(self.source_locator)

    @property
    def source_locator_sha256(self) -> str:
        return _sha256(self.source_locator_json)

    @property
    def bridge_spec_json(self) -> str | None:
        if self.bridge_spec is None:
            return None
        return _canonical_json(self.bridge_spec.model_dump(mode="json"))

    @property
    def commitment_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
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
        if self.bridge_spec is not None:
            payload["bridge_spec"] = self.bridge_spec.model_dump(mode="json")
        return payload

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
_COMPARABILITY_COLUMNS_WITH_BRIDGE = f"{_COMPARABILITY_COLUMNS},bridge_spec_json"


def _has_bridge_spec_column(conn: sqlite3.Connection) -> bool:
    return any(
        str(row[1]) == "bridge_spec_json"
        for row in conn.execute("PRAGMA table_info(kpi_definition_comparability_revisions)")
    )


def _comparability_select_columns(conn: sqlite3.Connection) -> str:
    return (
        _COMPARABILITY_COLUMNS_WITH_BRIDGE
        if _has_bridge_spec_column(conn)
        else _COMPARABILITY_COLUMNS
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
    raw_bridge = row.get("bridge_spec_json")
    bridge_spec = None if raw_bridge is None else parse_bridge_spec_json(str(raw_bridge))
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
        bridge_spec=bridge_spec,
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
    if entity is None:
        raise ValueError("definition evidence and reporting entity issuer must agree")
    evidence_issuer = str(evidence["issuer_id"])
    if evidence_issuer != str(entity[0]):
        try:
            subject = ReportingEntityRegistry(conn).canonicalize_recorded_subject(
                evidence_issuer, knowledge_at=knowledge_at.astimezone(UTC)
            )
        except LookupError as exc:
            raise ValueError("definition evidence and reporting entity issuer must agree") from exc
        if (
            subject.issuer_id != str(entity[0])
            or subject.reporting_entity_id != reporting_entity_id
            or subject.material_dissent
        ):
            raise ValueError("definition evidence and reporting entity issuer must agree")
        binding = conn.execute(
            "SELECT recorded_at FROM recorded_subject_binding_revisions "
            "WHERE binding_revision_id=?",
            (subject.binding_revision_id,),
        ).fetchone()
        if binding is None or _db_datetime(binding[0]) > recorded_at:
            raise ValueError("definition recording predates its subject binding")
        evidence_issuer = subject.issuer_id
    if scope_security_id is not None:
        security = conn.execute(
            "SELECT issuer_id FROM securities WHERE security_id=?", (scope_security_id,)
        ).fetchone()
        if security is None or str(security[0]) != evidence_issuer:
            raise ValueError("definition scope security must belong to the evidence issuer")
    retrieved_at = _db_datetime(evidence["retrieved_at"])
    document_recorded_at = _db_datetime(evidence["document_recorded_at"])
    node_recorded_at = _db_datetime(evidence["node_recorded_at"])
    if retrieved_at > knowledge_at:
        raise ValueError("definition knowledge predates source retrieval")
    if document_recorded_at > recorded_at or node_recorded_at > recorded_at:
        raise ValueError("definition recording predates its evidence")
    return evidence_issuer


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
    if isinstance(revision.bridge_spec, SplitScaleBridge) and (
        predecessor.period_kind != successor.period_kind
        or predecessor.accounting_basis != successor.accounting_basis
        or predecessor.consolidation_scope != successor.consolidation_scope
        or predecessor.dimensions != successor.dimensions
        or predecessor.currency_disposition != successor.currency_disposition
        or predecessor.currency != successor.currency
    ):
        raise ValueError(
            "scale_ratio bridge requires matching period, basis, scope, dimensions, and currency"
        )
    if isinstance(revision.bridge_spec, AdditiveCompositeBridge):
        composite = definition_by_id(revision.bridge_spec.composite_definition_revision_id)
        if composite is None:
            raise ValueError("composite bridge requires an existing composite definition revision")
        for member_id in revision.bridge_spec.member_definition_revision_ids:
            member = definition_by_id(member_id)
            if member is None:
                raise ValueError(f"composite bridge member revision is missing: {member_id}")
            member_issuer = _validate_source_evidence(
                conn,
                reporting_entity_id=member.reporting_entity_id,
                scope_security_id=member.scope_security_id,
                source_document_version_id=member.source_document_version_id,
                source_evidence_node_id=member.source_evidence_node_id,
                source_locator_json=member.source_locator_json,
                source_locator_sha256=member.source_locator_sha256,
                knowledge_at=member.knowledge_at,
                recorded_at=member.recorded_at,
            )
            if member_issuer != relation_issuer:
                raise ValueError("composite bridge members cannot cross issuer boundaries")
            if (
                member.unit_family != composite.unit_family
                or member.unit_key != composite.unit_key
                or member.unit_scale != composite.unit_scale
                or member.currency_disposition != composite.currency_disposition
                or member.currency != composite.currency
                or member.period_kind != composite.period_kind
                or member.stock_flow_behavior != composite.stock_flow_behavior
                or member.accounting_basis != composite.accounting_basis
            ):
                raise ValueError(
                    "composite bridge members must match composite unit, currency, period, and basis"
                )
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

    has_bridge_col = _has_bridge_spec_column(conn)
    if revision.bridge_spec is not None and not has_bridge_col:
        raise ValueError(
            "bridge_spec requires bridge_spec_json column (migration 0074_kpi_comparability_bridge_spec)"
        )
    columns = _COMPARABILITY_COLUMNS_WITH_BRIDGE if has_bridge_col else _COMPARABILITY_COLUMNS
    with _savepoint(conn, "persist_kpi_definition_comparability"):
        cursor = conn.execute(
            f"SELECT {columns} FROM kpi_definition_comparability_revisions "  # nosec B608
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
        base_values: tuple[object, ...] = (
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
        values = (*base_values, revision.bridge_spec_json) if has_bridge_col else base_values
        conn.execute(
            f"INSERT INTO kpi_definition_comparability_revisions ({columns}) "  # nosec B608
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
    columns = _comparability_select_columns(conn)
    cursor = conn.execute(
        f"SELECT {columns} FROM kpi_definition_comparability_revisions relation "  # nosec B608
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
    columns = _comparability_select_columns(conn)
    cursor = conn.execute(
        f"SELECT {columns} FROM kpi_definition_comparability_revisions "  # nosec B608
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


_FORWARD_REQUIRED_TABLES = (
    "kpi_definitions",
    "kpi_definition_revisions",
    "kpi_definition_comparability_revisions",
    "evidence_document_versions",
    "evidence_source_observations",
    "evidence_extraction_runs",
    "evidence_nodes",
    "reporting_entities",
)


class ForwardKpiRevisionBinding(BaseModel):
    """Typed result of binding a forward-admitted KPI fact to a definition revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kpi_definition_revision_id: str = Field(min_length=1, max_length=128)
    knowledge_at: datetime
    created_definition: bool = False
    created_comparability: bool = False
    comparability_disposition: KpiDefinitionComparabilityDisposition | None = None


def _utc_from_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _resolve_forward_evidence(
    conn: sqlite3.Connection,
    *,
    source_doc_id: int,
    source_locator_json: str | None,
) -> tuple[str, str, str, dict[str, object], datetime, datetime, datetime] | None:
    """Resolve existing document/observation/node evidence without fabricating missing rows."""
    doc_row: tuple[object, ...] | None = None
    if _table_exists(conn, "v_legacy_document_evidence_bindings_current"):
        doc_row = conn.execute(
            "SELECT doc.document_version_id, doc.issuer_id, doc.recorded_at, obs.retrieved_at "
            "FROM v_legacy_document_evidence_bindings_current binding "
            "JOIN evidence_document_versions doc "
            "ON doc.document_version_id = binding.document_version_id "
            "JOIN evidence_source_observations obs "
            "ON obs.observation_id = doc.observation_id "
            "WHERE binding.legacy_document_id = ? LIMIT 1",
            (source_doc_id,),
        ).fetchone()
    if doc_row is None:
        doc_columns = {
            str(col[1])
            for col in conn.execute("PRAGMA table_info(evidence_document_versions)").fetchall()
        }
        if "legacy_document_id" in doc_columns:
            doc_row = conn.execute(
                "SELECT doc.document_version_id, doc.issuer_id, doc.recorded_at, obs.retrieved_at "
                "FROM evidence_document_versions doc "
                "JOIN evidence_source_observations obs "
                "ON obs.observation_id = doc.observation_id "
                "WHERE doc.legacy_document_id = ? "
                "ORDER BY doc.version_sequence DESC LIMIT 1",
                (source_doc_id,),
            ).fetchone()
    if doc_row is None:
        return None
    document_version_id = str(doc_row[0])
    raw_issuer_id = str(doc_row[1])
    doc_recorded_at = _db_datetime(doc_row[2])
    retrieved_at = _db_datetime(doc_row[3])
    node_rows = conn.execute(
        "SELECT node.node_id, node.recorded_at, node.locator_json "
        "FROM evidence_nodes node "
        "JOIN evidence_extraction_runs run "
        "ON run.extraction_run_id = node.extraction_run_id "
        "WHERE run.document_version_id = ? AND run.outcome = 'succeeded' "
        "ORDER BY datetime(node.recorded_at) DESC, node.node_id DESC",
        (document_version_id,),
    ).fetchall()
    preferred_locator_canonical: str | None = None
    if source_locator_json:
        try:
            loaded_pref = json.loads(source_locator_json)
        except json.JSONDecodeError:
            loaded_pref = None
        if isinstance(loaded_pref, dict) and loaded_pref:
            preferred_locator_canonical = _canonical_json(cast(dict[str, object], loaded_pref))
    fallback_node: tuple[str, datetime, dict[str, object]] | None = None
    for raw_node in node_rows:
        locator_raw = raw_node[2]
        if locator_raw is None:
            continue
        try:
            loaded_locator = json.loads(str(locator_raw))
        except json.JSONDecodeError:
            continue
        if not isinstance(loaded_locator, dict) or not loaded_locator:
            continue
        locator_dict: dict[str, object] = {
            str(k): v for k, v in cast("dict[object, object]", loaded_locator).items()
        }
        node_tuple = (str(raw_node[0]), _db_datetime(raw_node[1]), locator_dict)
        if (
            preferred_locator_canonical is not None
            and _canonical_json(locator_dict) == preferred_locator_canonical
        ):
            fallback_node = node_tuple
            break
        if fallback_node is None:
            fallback_node = node_tuple
    if fallback_node is None:
        return None
    node_id, node_recorded_at, locator_dict = fallback_node
    return (
        document_version_id,
        raw_issuer_id,
        node_id,
        locator_dict,
        retrieved_at,
        doc_recorded_at,
        node_recorded_at,
    )


def _resolve_forward_reporting_entity(
    conn: sqlite3.Connection,
    *,
    raw_issuer_id: str,
    knowledge_at: datetime,
    recorded_at: datetime,
) -> str | None:
    row = conn.execute(
        "SELECT reporting_entity_id FROM reporting_entities WHERE issuer_id = ? LIMIT 1",
        (raw_issuer_id,),
    ).fetchone()
    if row is not None:
        return str(row[0])
    if not _table_exists(conn, "recorded_subject_binding_revisions"):
        return None
    try:
        subject = ReportingEntityRegistry(conn).canonicalize_recorded_subject(
            raw_issuer_id, knowledge_at=knowledge_at.astimezone(UTC)
        )
    except LookupError:
        return None
    if subject.material_dissent:
        return None
    binding_row = conn.execute(
        "SELECT recorded_at FROM recorded_subject_binding_revisions WHERE binding_revision_id = ?",
        (subject.binding_revision_id,),
    ).fetchone()
    if binding_row is None or _db_datetime(binding_row[0]) > recorded_at:
        return None
    return subject.reporting_entity_id


def _forward_period_and_behavior(
    unit: Unit,
    *,
    current: IssuerKpiDefinitionRevision | None,
) -> tuple[KpiDefinitionPeriodKind, KpiStockFlowBehavior]:
    if unit in {Unit.PERCENT, Unit.RATIO, Unit.BPS}:
        return (KpiDefinitionPeriodKind.DURATION, KpiStockFlowBehavior.RATIO)
    if current is not None and current.unit_key is unit:
        return (current.period_kind, current.stock_flow_behavior)
    return (KpiDefinitionPeriodKind.DURATION, KpiStockFlowBehavior.FLOW)


class ComparativeOverlapCandidate(BaseModel):
    """Comparative prior-period value reported in a new filing for overlap verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_end: str = Field(min_length=10, max_length=10)
    successor_comparative_value: Decimal
    fiscal_period_type: FiscalPeriodType | None = None
    predecessor_value: Decimal | None = None
    source_locator: dict[str, object] | None = None


def lookup_revision_reported_value(
    conn: sqlite3.Connection,
    *,
    kpi_definition_revision_id: str,
    period_end: str | date,
    fiscal_period_type: FiscalPeriodType | None = None,
) -> Decimal | None:
    """Look up the single admitted non-superseded reported value for a revision and period."""
    if not _table_exists(conn, "kpi_facts") or not _table_exists(
        conn, "kpi_fact_semantic_contexts"
    ):
        return None
    # Local import avoids an import cycle with provenance.financial_fact_resolution.
    from provenance.financial_fact_resolution import canonical_fact_relation

    relation = canonical_fact_relation(conn, "kpi_facts")
    iso_period = period_end.isoformat() if isinstance(period_end, date) else str(period_end)[:10]
    fact_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(kpi_facts)").fetchall()}
    supersedes_filter = (
        f"AND NOT EXISTS (SELECT 1 FROM {relation.sql} succ WHERE succ.supersedes_id = f.id) "
        if relation.selection_mode != "resolved_view" and "supersedes_id" in fact_cols
        else ""
    )
    fpt_clause = "AND f.fiscal_period_type = ? " if fiscal_period_type is not None else ""
    params: list[object] = [kpi_definition_revision_id, iso_period]
    if fiscal_period_type is not None:
        params.append(fiscal_period_type.value)
    rows = conn.execute(
        f"SELECT f.value FROM {relation.sql} f "  # nosec B608 -- fixed canonical relation + predicates
        "JOIN kpi_fact_semantic_contexts ksc ON ksc.kpi_fact_id = f.id "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM kpi_fact_semantic_contexts nxt WHERE nxt.supersedes_context_id = ksc.id"
        ") "
        "WHERE ksc.kpi_definition_revision_id = ? "
        "AND ksc.status = 'admitted' "
        "AND date(f.period_end) = date(?) "
        f"{supersedes_filter}{fpt_clause}"
        "ORDER BY f.id",
        tuple(params),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise ValueError(
            f"multiple admitted facts found for {kpi_definition_revision_id} at {iso_period}; "
            "specify fiscal_period_type"
        )
    return Decimal(str(rows[0][0]))


def bind_forward_kpi_definition_revision(
    conn: sqlite3.Connection,
    *,
    kpi_definition_id: int,
    period_end: datetime,
    fiscal_period_type: FiscalPeriodType,
    unit: Unit,
    currency: Currency | None,
    source_doc_id: int | None,
    context: KpiSemanticContext,
    reviewed_by: str,
    knowledge_at: datetime | None = None,
    source_locator_json: str | None = None,
    comparative_overlap: ComparativeOverlapCandidate | None = None,
    overlap_policy: OverlapTolerancePolicy | None = None,
) -> ForwardKpiRevisionBinding | None:
    """Resolve or append an admitted KPI definition revision for forward persistence.

    Returns ``None`` when the semantic context is not admitted, source evidence or
    revision tables are absent, or chronological invariants prevent safe binding,
    so legacy unbound paths remain explicit without fabricating evidence.
    """
    del fiscal_period_type
    if context.status is not KpiSemanticStatus.ADMITTED or source_doc_id is None:
        return None
    if not all(_table_exists(conn, table) for table in _FORWARD_REQUIRED_TABLES):
        return None
    if kpi_definition_id <= 0:
        return None
    if (
        conn.execute("SELECT 1 FROM kpi_definitions WHERE id = ?", (kpi_definition_id,)).fetchone()
        is None
    ):
        return None
    effective_at = _utc_from_datetime(period_end)
    if context.reported_period_end is None or context.reported_period_end != effective_at.date():
        return None
    unit_family = _UNIT_FAMILY_BY_UNIT.get(unit)
    if unit_family is None or unit_family is KpiUnitFamily.UNKNOWN:
        return None
    if (
        context.unit_scale is KpiUnitScale.UNKNOWN
        or context.accounting_basis is KpiAccountingBasis.UNKNOWN
        or context.consolidation_scope is KpiConsolidationScope.UNKNOWN
    ):
        return None
    if (
        context.consolidation_scope
        in {
            KpiConsolidationScope.GEOGRAPHY,
            KpiConsolidationScope.SEGMENT,
            KpiConsolidationScope.PRODUCT,
        }
        and not context.dimensions
    ):
        return None
    try:
        validate_admitted_unit_scale(unit, context.unit_scale)
    except ValueError:
        return None
    if unit in _MONETARY_UNITS:
        if currency is None:
            return None
        currency_disposition = KpiCurrencyDisposition.EXPLICIT
    else:
        if currency is not None:
            return None
        currency_disposition = KpiCurrencyDisposition.NOT_APPLICABLE

    evidence = _resolve_forward_evidence(
        conn,
        source_doc_id=source_doc_id,
        source_locator_json=source_locator_json,
    )
    if evidence is None:
        return None
    (
        document_version_id,
        raw_issuer_id,
        evidence_node_id,
        locator_dict,
        observed_at,
        doc_captured_at,
        node_captured_at,
    ) = evidence
    evidence_floor = max(observed_at, doc_captured_at, node_captured_at)
    explicit_knowledge = knowledge_at is not None
    base_ts = _utc_from_datetime(knowledge_at) if knowledge_at is not None else datetime.now(UTC)
    if explicit_knowledge and base_ts < evidence_floor:
        return None
    candidate_ts = max(base_ts, evidence_floor)

    reporting_entity_id = _resolve_forward_reporting_entity(
        conn,
        raw_issuer_id=raw_issuer_id,
        knowledge_at=candidate_ts,
        recorded_at=candidate_ts,
    )
    if reporting_entity_id is None:
        return None

    current = current_kpi_definition_revision(conn, kpi_definition_id=kpi_definition_id)
    reviewer = reviewed_by.strip() or "pipeline"
    if current is not None:
        if (
            current.reporting_entity_id != reporting_entity_id
            or current.status is not KpiDefinitionStatus.ADMITTED
            or current.lifecycle is not KpiDefinitionLifecycle.ACTIVE
        ):
            return None
        if current.effective_at > effective_at:
            historical = kpi_definition_revision_as_known(
                conn,
                kpi_definition_id=kpi_definition_id,
                effective_at=effective_at,
                known_at=candidate_ts,
            )
            if (
                historical is None
                or historical.reporting_entity_id != reporting_entity_id
                or historical.status is not KpiDefinitionStatus.ADMITTED
                or historical.lifecycle is not KpiDefinitionLifecycle.ACTIVE
            ):
                return None
            historical_match = (
                historical.reported_label == context.metric_name_as_reported
                and historical.accounting_basis is context.accounting_basis
                and historical.consolidation_scope is context.consolidation_scope
                and historical.dimensions == context.dimensions
                and historical.unit_key is unit
                and historical.unit_scale is context.unit_scale
                and historical.currency_disposition is currency_disposition
                and historical.currency is currency
            )
            if not historical_match:
                return None
            historical_floor = max(historical.knowledge_at, historical.recorded_at)
            if explicit_knowledge and candidate_ts < historical_floor:
                return None
            resolved_ts = max(candidate_ts, historical_floor)
            return ForwardKpiRevisionBinding(
                kpi_definition_revision_id=historical.kpi_definition_revision_id,
                knowledge_at=resolved_ts,
                created_definition=False,
                created_comparability=False,
                comparability_disposition=None,
            )
        current_floor = max(current.knowledge_at, current.recorded_at)
        if explicit_knowledge and candidate_ts < current_floor:
            return None
        candidate_ts = max(candidate_ts, current_floor)
        exact_match = (
            current.reported_label == context.metric_name_as_reported
            and current.accounting_basis is context.accounting_basis
            and current.consolidation_scope is context.consolidation_scope
            and current.dimensions == context.dimensions
            and current.unit_key is unit
            and current.unit_scale is context.unit_scale
            and current.currency_disposition is currency_disposition
            and current.currency is currency
        )
        if exact_match:
            selected = conn.execute(
                "SELECT kpi_definition_revision_id FROM kpi_definition_revisions "
                "WHERE kpi_definition_id = ? "
                "AND datetime(effective_at) <= datetime(?) "
                "AND datetime(knowledge_at) <= datetime(?) "
                "AND datetime(recorded_at) <= datetime(?) "
                "ORDER BY datetime(effective_at) DESC, datetime(knowledge_at) DESC, revision DESC "
                "LIMIT 1",
                (
                    kpi_definition_id,
                    _utc_text(effective_at),
                    _utc_text(candidate_ts),
                    _utc_text(candidate_ts),
                ),
            ).fetchone()
            if selected is None or str(selected[0]) != current.kpi_definition_revision_id:
                return None
            return ForwardKpiRevisionBinding(
                kpi_definition_revision_id=current.kpi_definition_revision_id,
                knowledge_at=candidate_ts,
                created_definition=False,
                created_comparability=False,
                comparability_disposition=None,
            )

    period_kind, stock_flow_behavior = _forward_period_and_behavior(unit, current=current)
    revision_number = 1 if current is None else current.revision + 1
    supersedes_id = None if current is None else current.kpi_definition_revision_id
    signature = {
        "accounting_basis": context.accounting_basis.value,
        "consolidation_scope": context.consolidation_scope.value,
        "currency": None if currency is None else currency.value,
        "currency_disposition": currency_disposition.value,
        "dimensions": context.dimensions,
        "effective_at": _utc_text(effective_at),
        "kpi_definition_id": kpi_definition_id,
        "period_kind": period_kind.value,
        "reported_label": context.metric_name_as_reported,
        "reporting_entity_id": reporting_entity_id,
        "revision": revision_number,
        "stock_flow_behavior": stock_flow_behavior.value,
        "supersedes_definition_revision_id": supersedes_id,
        "unit_key": unit.value,
        "unit_scale": context.unit_scale.value,
    }
    def_digest = _sha256(_canonical_json(signature))[:24]
    new_revision = IssuerKpiDefinitionRevision(
        kpi_definition_revision_id=f"fwd-kdr-{kpi_definition_id}-r{revision_number}-{def_digest}",
        idempotency_key=f"fwd-kdr-key:{kpi_definition_id}:r{revision_number}:{def_digest}",
        kpi_definition_id=kpi_definition_id,
        reporting_entity_id=reporting_entity_id,
        revision=revision_number,
        supersedes_definition_revision_id=supersedes_id,
        status=KpiDefinitionStatus.ADMITTED,
        lifecycle=KpiDefinitionLifecycle.ACTIVE,
        reported_label=context.metric_name_as_reported,
        reported_definition_text=None,
        definition_text_status=KpiDefinitionTextStatus.NOT_STATED,
        period_kind=period_kind,
        stock_flow_behavior=stock_flow_behavior,
        unit_family=unit_family,
        unit_key=unit,
        unit_scale=context.unit_scale,
        currency_disposition=currency_disposition,
        currency=currency,
        accounting_basis=context.accounting_basis,
        consolidation_scope=context.consolidation_scope,
        dimensions=dict(context.dimensions),
        source_document_version_id=document_version_id,
        source_evidence_node_id=evidence_node_id,
        source_locator=locator_dict,
        reviewed_by=reviewer,
        effective_at=effective_at,
        knowledge_at=candidate_ts,
        recorded_at=candidate_ts,
    )

    comparability_disposition: KpiDefinitionComparabilityDisposition | None = None
    created_comparability = False
    with _savepoint(conn, "bind_forward_kpi_definition_revision"):
        persist_kpi_definition_revision(conn, new_revision)
        if current is not None:
            semantic_same = (
                current.accounting_basis is context.accounting_basis
                and current.consolidation_scope is context.consolidation_scope
                and current.dimensions == context.dimensions
                and current.unit_key is unit
                and current.unit_scale is context.unit_scale
                and current.currency_disposition is currency_disposition
                and current.currency is currency
                and current.period_kind is period_kind
                and current.stock_flow_behavior is stock_flow_behavior
            )
            bridge_spec: KpiComparabilityBridgeSpec | None = None
            if semantic_same and current.reported_label != context.metric_name_as_reported:
                if comparative_overlap is not None:
                    resolved_pred = (
                        comparative_overlap.predecessor_value
                        if comparative_overlap.predecessor_value is not None
                        else lookup_revision_reported_value(
                            conn,
                            kpi_definition_revision_id=current.kpi_definition_revision_id,
                            period_end=comparative_overlap.period_end,
                            fiscal_period_type=comparative_overlap.fiscal_period_type,
                        )
                    )
                    if resolved_pred is None:
                        relation_kind = KpiDefinitionRelationKind.RENAMED
                        comparability_disposition = (
                            KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK
                        )
                        reason_code = "forward_label_renamed_unverified"
                    else:
                        witness_locator = comparative_overlap.source_locator or locator_dict
                        ok, witness, _ = evaluate_overlap_witness(
                            period_end=comparative_overlap.period_end,
                            predecessor_value=resolved_pred,
                            successor_value=comparative_overlap.successor_comparative_value,
                            source_locator=witness_locator,
                            fiscal_period_type=comparative_overlap.fiscal_period_type,
                            policy=overlap_policy,
                        )
                        if ok and witness is not None:
                            relation_kind = KpiDefinitionRelationKind.RENAMED
                            comparability_disposition = (
                                KpiDefinitionComparabilityDisposition.CONTINUOUS
                            )
                            reason_code = "verified_rename_overlap"
                            bridge_spec = RenameOverlapBridge(overlap_witness=witness)
                        else:
                            relation_kind = KpiDefinitionRelationKind.REDEFINED
                            comparability_disposition = (
                                KpiDefinitionComparabilityDisposition.NOT_COMPARABLE
                            )
                            reason_code = "rename_overlap_mismatch"
                else:
                    relation_kind = KpiDefinitionRelationKind.RENAMED
                    comparability_disposition = (
                        KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK
                    )
                    reason_code = "forward_label_renamed"
            else:
                relation_kind = KpiDefinitionRelationKind.REDEFINED
                comparability_disposition = KpiDefinitionComparabilityDisposition.NOT_COMPARABLE
                reason_code = "forward_semantic_redefined"
            comp_digest_payload: dict[str, object] = {
                "predecessor": current.kpi_definition_revision_id,
                "successor": new_revision.kpi_definition_revision_id,
                "disposition": comparability_disposition.value,
                "relation_kind": relation_kind.value,
            }
            if bridge_spec is not None:
                comp_digest_payload["bridge_spec"] = bridge_spec.model_dump(mode="json")
            comp_digest = _sha256(_canonical_json(comp_digest_payload))[:24]
            persist_kpi_definition_comparability_revision(
                conn,
                KpiDefinitionComparabilityRevision(
                    comparability_revision_id=f"fwd-kcr-{comp_digest}",
                    idempotency_key=f"fwd-kcr-key:{comp_digest}",
                    predecessor_definition_revision_id=current.kpi_definition_revision_id,
                    successor_definition_revision_id=new_revision.kpi_definition_revision_id,
                    revision=1,
                    supersedes_comparability_revision_id=None,
                    relation_kind=relation_kind,
                    disposition=comparability_disposition,
                    reason_code=reason_code,
                    reviewed_by=reviewer,
                    source_document_version_id=document_version_id,
                    source_evidence_node_id=evidence_node_id,
                    source_locator=locator_dict,
                    bridge_spec=bridge_spec,
                    effective_at=effective_at,
                    knowledge_at=candidate_ts,
                    recorded_at=candidate_ts,
                ),
            )
            created_comparability = True

    return ForwardKpiRevisionBinding(
        kpi_definition_revision_id=new_revision.kpi_definition_revision_id,
        knowledge_at=candidate_ts,
        created_definition=True,
        created_comparability=created_comparability,
        comparability_disposition=comparability_disposition,
    )
