"""Typed, append-only semantic context for legacy KPI facts.

The reported observation and its semantic qualification have different
lifecycles. A value/period/source correction supersedes ``kpi_facts``; a later
source review of otherwise-correct metadata appends a semantic-context revision
against the unchanged fact row.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.facts import Unit


class KpiPeriodRole(StrEnum):
    CURRENT = "current"
    PRIOR_PERIOD_COMPARATOR = "prior_period_comparator"
    PRIOR_YEAR_COMPARATOR = "prior_year_comparator"
    GUIDANCE = "guidance"
    UNKNOWN = "unknown"


class KpiPublicationLane(StrEnum):
    CURRENT_ACTUAL = "current_actual"
    COMPARATOR = "comparator"
    GUIDANCE_TARGET = "guidance_target"
    MANAGEMENT_EXPLANATION = "management_explanation"
    ANALYST_QUESTION = "analyst_question"
    UNCLASSIFIED = "unclassified"


class KpiAccountingBasis(StrEnum):
    GAAP = "gaap"
    NON_GAAP = "non_gaap"
    MANAGEMENT = "management"
    UNKNOWN = "unknown"


class KpiConsolidationScope(StrEnum):
    CONSOLIDATED = "consolidated"
    GEOGRAPHY = "geography"
    SEGMENT = "segment"
    PRODUCT = "product"
    OTHER = "other"
    UNKNOWN = "unknown"


class KpiUnitScale(StrEnum):
    NONE = "none"
    THOUSANDS = "thousands"
    MILLIONS = "millions"
    BILLIONS = "billions"
    UNKNOWN = "unknown"


class KpiSemanticStatus(StrEnum):
    # The stored compatibility value ``admitted`` means source-qualified.
    # Publication lane independently controls where the observation may render.
    ADMITTED = "admitted"
    QUARANTINED = "quarantined"
    LEGACY_UNKNOWN = "legacy_unknown"


_ADMITTED_UNIT_SCALES_BY_PERSISTED_UNIT: Mapping[Unit, frozenset[KpiUnitScale]] = {
    Unit.ACTUAL: frozenset({KpiUnitScale.NONE}),
    Unit.THOUSANDS: frozenset({KpiUnitScale.THOUSANDS}),
    Unit.MILLIONS: frozenset({KpiUnitScale.MILLIONS}),
    Unit.BILLIONS: frozenset({KpiUnitScale.BILLIONS}),
    Unit.PERCENT: frozenset({KpiUnitScale.NONE}),
    Unit.RATIO: frozenset({KpiUnitScale.NONE}),
    Unit.BPS: frozenset({KpiUnitScale.NONE}),
    Unit.COUNT: frozenset(
        {
            KpiUnitScale.NONE,
            KpiUnitScale.THOUSANDS,
            KpiUnitScale.MILLIONS,
            KpiUnitScale.BILLIONS,
        }
    ),
}

_COUNT_SOURCE_SCALE_MULTIPLIERS: Mapping[KpiUnitScale, int] = {
    KpiUnitScale.NONE: 1,
    KpiUnitScale.THOUSANDS: 1_000,
    KpiUnitScale.MILLIONS: 1_000_000,
    KpiUnitScale.BILLIONS: 1_000_000_000,
}


def validate_admitted_unit_scale(unit: Unit, unit_scale: KpiUnitScale) -> None:
    """Reject an admitted semantic scale incompatible with the stored unit."""

    if unit_scale not in _ADMITTED_UNIT_SCALES_BY_PERSISTED_UNIT[unit]:
        raise ValueError("persisted fact unit must match semantic unit scale")


def normalize_source_numeric(value: Decimal, *, unit: Unit, unit_scale: KpiUnitScale) -> Decimal:
    """Normalize a source-presented number to the persisted fact convention."""

    validate_admitted_unit_scale(unit, unit_scale)
    if unit is not Unit.COUNT:
        return value
    return value * _COUNT_SOURCE_SCALE_MULTIPLIERS[unit_scale]


def parse_source_numeric(value: str) -> Decimal:
    """Parse one exact source-number token without applying presentation scale."""

    stripped = value.strip().replace(",", "")
    if stripped.endswith("%"):
        stripped = stripped[:-1].strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        stripped = "-" + stripped[1:-1]
    for prefix in ("$", "€", "£"):
        stripped = stripped.removeprefix(prefix).strip()
    try:
        return Decimal(stripped)
    except InvalidOperation as exc:
        raise ValueError("source value text is not numeric") from exc


_LANE_BY_ROLE: Mapping[str, KpiPublicationLane] = {
    KpiPeriodRole.CURRENT.value: KpiPublicationLane.CURRENT_ACTUAL,
    KpiPeriodRole.PRIOR_PERIOD_COMPARATOR.value: KpiPublicationLane.COMPARATOR,
    KpiPeriodRole.PRIOR_YEAR_COMPARATOR.value: KpiPublicationLane.COMPARATOR,
    KpiPeriodRole.GUIDANCE.value: KpiPublicationLane.GUIDANCE_TARGET,
    KpiPeriodRole.UNKNOWN.value: KpiPublicationLane.UNCLASSIFIED,
}


class KpiSemanticContext(BaseModel):
    """Source-bound meaning and publication eligibility for one KPI fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_name_as_reported: str = Field(min_length=1, max_length=256)
    reported_period_end: date | None = None
    period_role: KpiPeriodRole
    publication_lane: KpiPublicationLane
    accounting_basis: KpiAccountingBasis
    consolidation_scope: KpiConsolidationScope
    dimensions: dict[str, str] = Field(default_factory=dict)
    unit_scale: KpiUnitScale
    source_row_label: str | None = Field(default=None, max_length=512)
    source_column_header: str | None = Field(default=None, max_length=512)
    source_value_text: str | None = Field(default=None, min_length=1, max_length=80)
    status: KpiSemanticStatus
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def _derive_lane_for_legacy_callers(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        validated_data = cast("dict[str, object]", data)
        if validated_data.get("publication_lane") is not None:
            return validated_data
        role = validated_data.get("period_role", KpiPeriodRole.UNKNOWN)
        key = role.value if isinstance(role, KpiPeriodRole) else str(role)
        return {
            **validated_data,
            "publication_lane": _LANE_BY_ROLE.get(key, KpiPublicationLane.UNCLASSIFIED),
        }

    @model_validator(mode="after")
    def _qualification_contract(self) -> KpiSemanticContext:
        if self.status is KpiSemanticStatus.ADMITTED:
            if self.reported_period_end is None:
                raise ValueError("source-qualified KPI facts require reported_period_end")
            if self.reason_code is not None:
                raise ValueError("source-qualified KPI facts cannot carry a quarantine reason")
            if self.accounting_basis is KpiAccountingBasis.UNKNOWN:
                raise ValueError("source-qualified KPI facts require an accounting basis")
            if self.consolidation_scope is KpiConsolidationScope.UNKNOWN:
                raise ValueError("source-qualified KPI facts require a consolidation scope")
            if self.unit_scale is KpiUnitScale.UNKNOWN:
                raise ValueError("source-qualified KPI facts require a source unit scale")
            if self.publication_lane is KpiPublicationLane.UNCLASSIFIED:
                raise ValueError("source-qualified KPI facts require a publication lane")
            if (
                self.consolidation_scope
                in {
                    KpiConsolidationScope.GEOGRAPHY,
                    KpiConsolidationScope.SEGMENT,
                    KpiConsolidationScope.PRODUCT,
                }
                and not self.dimensions
            ):
                raise ValueError("scoped source-qualified KPI facts require dimensions")
            expected = _LANE_BY_ROLE.get(self.period_role.value)
            if (
                expected is not None
                and expected is not KpiPublicationLane.UNCLASSIFIED
                and self.publication_lane is not expected
            ):
                raise ValueError("publication lane conflicts with reported period role")
        elif self.reason_code is None:
            raise ValueError("non-qualified KPI facts require a reason_code")
        if (
            self.status is KpiSemanticStatus.LEGACY_UNKNOWN
            and self.publication_lane is not KpiPublicationLane.UNCLASSIFIED
        ):
            raise ValueError("legacy-unknown KPI facts must remain unclassified")
        return self


class KpiSemanticContextRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int = Field(gt=0)
    kpi_fact_id: int = Field(gt=0)
    revision: int = Field(gt=0)
    supersedes_context_id: int | None = Field(default=None, gt=0)
    context: KpiSemanticContext
    reviewed_by: str = Field(min_length=1, max_length=128)
    knowledge_at: datetime

    @field_validator("knowledge_at")
    @classmethod
    def _knowledge_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("semantic context knowledge_at must be timezone-aware")
        return value


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})"))


def _context_payload(context: KpiSemanticContext) -> tuple[object, ...]:
    return (
        context.metric_name_as_reported,
        context.reported_period_end.isoformat() if context.reported_period_end else None,
        context.period_role.value,
        context.publication_lane.value,
        context.accounting_basis.value,
        context.consolidation_scope.value,
        json.dumps(context.dimensions, sort_keys=True, separators=(",", ":")),
        context.unit_scale.value,
        context.source_row_label,
        context.source_column_header,
        context.status.value,
        context.reason_code,
        context.source_value_text,
    )


def _context_from_row(row: Mapping[str, object], *, has_lane: bool) -> KpiSemanticContext:
    role = KpiPeriodRole(str(row["period_role"]))
    return KpiSemanticContext(
        metric_name_as_reported=str(row["metric_name_as_reported"]),
        reported_period_end=(
            date.fromisoformat(str(row["reported_period_end"]))
            if row["reported_period_end"] is not None
            else None
        ),
        period_role=role,
        publication_lane=(
            KpiPublicationLane(str(row["publication_lane"]))
            if has_lane
            else _LANE_BY_ROLE[role.value]
        ),
        accounting_basis=KpiAccountingBasis(str(row["accounting_basis"])),
        consolidation_scope=KpiConsolidationScope(str(row["consolidation_scope"])),
        dimensions=json.loads(str(row["dimensions_json"])),
        unit_scale=KpiUnitScale(str(row["unit_scale"])),
        source_row_label=(
            None if row["source_row_label"] is None else str(row["source_row_label"])
        ),
        source_column_header=(
            None if row["source_column_header"] is None else str(row["source_column_header"])
        ),
        source_value_text=(
            None
            if "source_value_text" not in row or row["source_value_text"] is None
            else str(row["source_value_text"])
        ),
        status=KpiSemanticStatus(str(row["status"])),
        reason_code=None if row["reason_code"] is None else str(row["reason_code"]),
    )


def current_kpi_semantic_context(
    conn: sqlite3.Connection, *, kpi_fact_id: int
) -> KpiSemanticContextRevision | None:
    """Return the one current semantic-context head for a fact, if present."""
    table = "kpi_fact_semantic_contexts"
    if not _table_exists(conn, table):
        return None
    columns = _columns(conn, table)
    has_revisions = {"revision", "supersedes_context_id"}.issubset(columns)
    has_lane = "publication_lane" in columns
    if has_revisions:
        cursor = conn.execute(
            "SELECT context.* FROM kpi_fact_semantic_contexts context "
            "WHERE context.kpi_fact_id=? AND NOT EXISTS ("
            "SELECT 1 FROM kpi_fact_semantic_contexts successor "
            "WHERE successor.supersedes_context_id=context.id) "
            "ORDER BY context.revision DESC LIMIT 1",
            (kpi_fact_id,),
        )
    else:
        cursor = conn.execute(
            "SELECT context.* FROM kpi_fact_semantic_contexts context "
            "WHERE context.kpi_fact_id=? LIMIT 1",
            (kpi_fact_id,),
        )
    raw_row = cursor.fetchone()
    row = (
        None
        if raw_row is None
        else (
            dict(raw_row)
            if isinstance(raw_row, sqlite3.Row)
            else {
                column[0]: value for column, value in zip(cursor.description, raw_row, strict=True)
            }
        )
    )
    if row is None:
        return None
    return KpiSemanticContextRevision(
        id=int(row["id"]),
        kpi_fact_id=int(row["kpi_fact_id"]),
        revision=int(row["revision"]) if has_revisions else 1,
        supersedes_context_id=(
            int(row["supersedes_context_id"])
            if has_revisions and row["supersedes_context_id"] is not None
            else None
        ),
        context=_context_from_row(row, has_lane=has_lane),
        reviewed_by=(str(row["reviewed_by"]) if "reviewed_by" in columns else "legacy"),
        knowledge_at=(
            datetime.fromisoformat(str(row["knowledge_at"]).replace("Z", "+00:00"))
            if "knowledge_at" in columns
            else datetime(1970, 1, 1, tzinfo=UTC)
        ),
    )


def persist_kpi_semantic_context(
    conn: sqlite3.Connection,
    *,
    kpi_fact_id: int,
    context: KpiSemanticContext,
    reviewed_by: str = "pipeline",
    knowledge_at: datetime | None = None,
) -> int | None:
    """Append a semantic-context revision, or return the idempotent current id."""
    table = "kpi_fact_semantic_contexts"
    if not _table_exists(conn, table):
        return None
    columns = _columns(conn, table)
    if context.status is KpiSemanticStatus.ADMITTED:
        fact_columns = (
            _columns(conn, "kpi_facts") if _table_exists(conn, "kpi_facts") else frozenset[str]()
        )
        if "unit" not in fact_columns:
            raise ValueError("admitted KPI semantic context requires a persisted fact unit")
        value_select = "value" if "value" in fact_columns else "NULL"
        source_excerpt_select = "source_excerpt" if "source_excerpt" in fact_columns else "NULL"
        fact_sql_template = (
            "SELECT unit,{value_select} AS value,{source_excerpt_select} AS source_excerpt "
            "FROM kpi_facts WHERE id=?"
        )
        fact_sql = fact_sql_template.format(
            value_select=value_select,
            source_excerpt_select=source_excerpt_select,
        )
        fact = conn.execute(fact_sql, (kpi_fact_id,)).fetchone()
        if fact is None:
            raise ValueError("admitted KPI semantic context requires an existing fact")
        try:
            fact_unit = fact["unit"] if isinstance(fact, sqlite3.Row) else fact[0]
            persisted_unit = Unit(str(fact_unit))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "admitted KPI semantic context has an invalid persisted fact unit"
            ) from exc
        validate_admitted_unit_scale(persisted_unit, context.unit_scale)
        if persisted_unit is Unit.COUNT:
            if (
                "value" not in fact_columns
                or "source_excerpt" not in fact_columns
                or "source_value_text" not in columns
                or context.source_value_text is None
            ):
                raise ValueError("admitted COUNT context requires retained source value text")
            persisted_value = Decimal(
                str(fact["value"] if isinstance(fact, sqlite3.Row) else fact[1])
            )
            source_excerpt = fact["source_excerpt"] if isinstance(fact, sqlite3.Row) else fact[2]
            if source_excerpt is None or context.source_value_text not in str(source_excerpt):
                raise ValueError(
                    "admitted COUNT source value must occur in the exact source excerpt"
                )
            normalized = normalize_source_numeric(
                parse_source_numeric(context.source_value_text),
                unit=persisted_unit,
                unit_scale=context.unit_scale,
            )
            if normalized != persisted_value:
                raise ValueError(
                    "admitted COUNT source value and scale do not match persisted value"
                )
    has_revisions = {"revision", "supersedes_context_id"}.issubset(columns)
    has_lane = "publication_lane" in columns
    current = current_kpi_semantic_context(conn, kpi_fact_id=kpi_fact_id)
    if current is not None and current.context == context:
        return current.id
    if not has_revisions and current is not None:
        raise ValueError("KPI fact semantic context conflicts with immutable persisted context")

    payload = list(_context_payload(context))
    source_value_text = payload.pop()
    if not has_lane:
        payload = payload[:3] + payload[4:]
    field_names = [
        "metric_name_as_reported",
        "reported_period_end",
        "period_role",
        *(["publication_lane"] if has_lane else []),
        "accounting_basis",
        "consolidation_scope",
        "dimensions_json",
        "unit_scale",
        "source_row_label",
        "source_column_header",
        "status",
        "reason_code",
    ]
    if "source_value_text" in columns:
        field_names.append("source_value_text")
        payload.append(source_value_text)
    values: list[object] = [kpi_fact_id]
    insert_fields = ["kpi_fact_id"]
    if has_revisions:
        insert_fields.extend(["revision", "supersedes_context_id"])
        values.extend(
            [
                1 if current is None else current.revision + 1,
                None if current is None else current.id,
            ]
        )
    insert_fields.extend(field_names)
    values.extend(payload)
    if "reviewed_by" in columns:
        insert_fields.append("reviewed_by")
        values.append(reviewed_by)
    if "knowledge_at" in columns:
        observed = knowledge_at or datetime.now(UTC)
        if observed.tzinfo is None:
            raise ValueError("semantic context knowledge_at must be timezone-aware")
        insert_fields.append("knowledge_at")
        values.append(observed.astimezone(UTC).isoformat().replace("+00:00", "Z"))
    placeholders = ",".join("?" for _ in values)
    cursor = conn.execute(
        f"INSERT INTO {table} ({','.join(insert_fields)}) VALUES ({placeholders})", values
    )
    if cursor.lastrowid is None:
        raise RuntimeError("semantic context insert did not return an identity")
    return int(cursor.lastrowid)


def unclassified_kpi_context(
    *, metric_name_as_reported: str, reported_period_end: date
) -> KpiSemanticContext:
    """Explicit unknown context for a producer that has not classified meaning."""
    return KpiSemanticContext(
        metric_name_as_reported=metric_name_as_reported,
        reported_period_end=reported_period_end,
        period_role=KpiPeriodRole.UNKNOWN,
        publication_lane=KpiPublicationLane.UNCLASSIFIED,
        accounting_basis=KpiAccountingBasis.UNKNOWN,
        consolidation_scope=KpiConsolidationScope.UNKNOWN,
        dimensions={},
        unit_scale=KpiUnitScale.UNKNOWN,
        status=KpiSemanticStatus.LEGACY_UNKNOWN,
        reason_code="producer_missing_semantic_context",
    )


def semantic_admission_sql(
    conn: sqlite3.Connection,
    *,
    fact_alias: str = "kf",
    context_alias: str = "ksc",
    fail_closed: bool = False,
) -> tuple[str, str]:
    """Return a schema-tolerant current-series semantic join and predicate.

    During shadow rollout, missing and explicit ``legacy_unknown`` contexts stay
    readable. Qualified non-current lanes and quarantines never enter a current
    series. A bounded consumer switches to ``fail_closed=True`` only after its
    all-status census reaches zero unknown/missing rows.
    """
    table = "kpi_fact_semantic_contexts"
    if not _table_exists(conn, table):
        return ("", "0=1" if fail_closed else "1=1")
    columns = _columns(conn, table)
    if {"revision", "supersedes_context_id"}.issubset(columns):
        join = (
            f"LEFT JOIN {table} {context_alias} ON {context_alias}.kpi_fact_id={fact_alias}.id "
            f"AND NOT EXISTS (SELECT 1 FROM {table} {context_alias}_successor "
            f"WHERE {context_alias}_successor.supersedes_context_id={context_alias}.id)"
        )
    else:
        join = f"LEFT JOIN {table} {context_alias} ON {context_alias}.kpi_fact_id={fact_alias}.id"
    current_lane = (
        f" AND {context_alias}.publication_lane='{KpiPublicationLane.CURRENT_ACTUAL.value}'"
        if "publication_lane" in columns
        else ""
    )
    qualified_current = f"({context_alias}.status='admitted'{current_lane})"
    if fail_closed:
        return join, qualified_current
    return (
        join,
        f"({context_alias}.id IS NULL OR {context_alias}.status='legacy_unknown' "
        f"OR {qualified_current})",
    )
