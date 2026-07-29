"""Typed deep module for exact source identity and canonical metric governance."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Disposition = Literal[
    "exact",
    "equivalent",
    "derived",
    "ambiguous",
    "not_applicable",
    "quarantined",
]
DimensionDisposition = Literal["exact", "equivalent", "ambiguous", "not_applicable", "quarantined"]
Lifecycle = Literal["active", "deprecated", "retired"]
PeriodKind = Literal["instant", "duration"]
ValueKind = Literal["numeric", "text", "nil"]


def canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _sql_sha256(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _time(value: datetime) -> str:
    return _utc(value).isoformat()


def _db_time(value: datetime) -> str:
    """Match SQLAlchemy's SQLite DateTime representation."""
    return _utc(value).replace(tzinfo=None).isoformat(sep=" ")


def _same_instant(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return _utc(datetime.fromisoformat(str(left))) == _utc(datetime.fromisoformat(str(right)))
    except ValueError:
        return False


def _parse_db_time(value: object) -> datetime:
    return _utc(datetime.fromisoformat(str(value)))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _validate_digest(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("must be a lowercase SHA-256 digest")
    return value


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Bitemporal(_Frozen):
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime

    @model_validator(mode="after")
    def _ordered_clocks(self) -> Self:
        if not (_utc(self.effective_at) <= _utc(self.knowledge_at) <= _utc(self.recorded_at)):
            raise ValueError("effective_at <= knowledge_at <= recorded_at clocks are required")
        return self


class CanonicalMetric(_Bitemporal):
    metric_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    canonical_name: str = Field(min_length=1, max_length=256)

    @property
    def commitment_payload(self) -> dict[str, object]:
        return {"canonical_name": self.canonical_name, "metric_id": self.metric_id}


class CanonicalAxis(_Bitemporal):
    axis_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    canonical_name: str = Field(min_length=1, max_length=256)

    @property
    def commitment_payload(self) -> dict[str, object]:
        return {"axis_id": self.axis_id, "canonical_name": self.canonical_name}


class CanonicalMember(_Bitemporal):
    member_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    axis_id: str = Field(min_length=1, max_length=128)
    canonical_name: str = Field(min_length=1, max_length=256)

    @property
    def commitment_payload(self) -> dict[str, object]:
        return {
            "axis_id": self.axis_id,
            "canonical_name": self.canonical_name,
            "member_id": self.member_id,
        }


class SourceTaxonomyComponent(_Bitemporal):
    component_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    component_kind: Literal["concept", "axis", "member"]
    taxonomy_namespace: str = Field(min_length=1)
    local_name: str = Field(min_length=1)
    taxonomy_name: str = Field(min_length=1)
    taxonomy_version: str = Field(min_length=1)
    reporting_entity_id: str | None = None
    is_extension: bool
    data_type: str | None = None
    period_type: PeriodKind | None = None
    balance: Literal["debit", "credit"] | None = None
    is_abstract: bool | None = None
    standard_label: str | None = None
    definition_text: str | None = None
    references: tuple[str, ...] = ()
    evidence_locator: dict[str, object]

    @model_validator(mode="after")
    def _component_shape(self) -> Self:
        if self.is_extension and self.reporting_entity_id is None:
            raise ValueError("extension components require a reporting entity scope")
        if self.component_kind != "concept" and any(
            value is not None
            for value in (
                self.data_type,
                self.period_type,
                self.balance,
                self.is_abstract,
            )
        ):
            raise ValueError("only source concepts may carry concept metadata")
        return self

    @property
    def reporting_entity_scope_key(self) -> str:
        return self.reporting_entity_id or "__global__"

    @property
    def commitment_payload(self) -> dict[str, object]:
        return {
            "balance": self.balance,
            "component_kind": self.component_kind,
            "data_type": self.data_type,
            "definition_text": self.definition_text,
            "evidence_locator": self.evidence_locator,
            "is_abstract": self.is_abstract,
            "is_extension": self.is_extension,
            "local_name": self.local_name,
            "period_type": self.period_type,
            "references": list(self.references),
            "reporting_entity_id": self.reporting_entity_id,
            "standard_label": self.standard_label,
            "taxonomy_name": self.taxonomy_name,
            "taxonomy_namespace": self.taxonomy_namespace,
            "taxonomy_version": self.taxonomy_version,
        }


class SourceObservationTaxonomyAssertion(_Frozen):
    observation_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    extraction_run_id: str = Field(min_length=1, max_length=128)
    taxonomy_name: str = Field(min_length=1)
    taxonomy_version: str = Field(min_length=1)
    fact_cell_semantic_key_sha256: str
    anchor_payload_sha256: str
    observation_payload_sha256: str
    extraction_output_sha256: str
    raw_entry_sha256: str
    observation_set_sha256: str
    knowledge_at: datetime
    recorded_at: datetime

    @field_validator(
        "fact_cell_semantic_key_sha256",
        "anchor_payload_sha256",
        "observation_payload_sha256",
        "extraction_output_sha256",
        "raw_entry_sha256",
        "observation_set_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        return _validate_digest(value)

    @model_validator(mode="after")
    def _ordered_clocks(self) -> Self:
        if _utc(self.knowledge_at) > _utc(self.recorded_at):
            raise ValueError("knowledge_at must not follow recorded_at")
        return self

    @property
    def commitment_payload(self) -> dict[str, object]:
        return {
            "anchor_payload_sha256": self.anchor_payload_sha256,
            "extraction_output_sha256": self.extraction_output_sha256,
            "extraction_run_id": self.extraction_run_id,
            "fact_cell_semantic_key_sha256": self.fact_cell_semantic_key_sha256,
            "observation_id": self.observation_id,
            "observation_payload_sha256": self.observation_payload_sha256,
            "observation_set_sha256": self.observation_set_sha256,
            "raw_entry_sha256": self.raw_entry_sha256,
            "taxonomy_name": self.taxonomy_name,
            "taxonomy_version": self.taxonomy_version,
        }


class CanonicalMetricDefinitionRevision(_Bitemporal):
    metric_definition_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    metric_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(gt=0)
    supersedes_metric_definition_revision_id: str | None = None
    lifecycle: Lifecycle
    definition_text: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    value_kind: ValueKind
    period_kind: PeriodKind
    unit_family: str = Field(min_length=1)
    accounting_basis: str = Field(min_length=1)
    scope_constraints: dict[str, object]

    @model_validator(mode="after")
    def _revision_shape(self) -> Self:
        if (self.revision == 1) != (self.supersedes_metric_definition_revision_id is None):
            raise ValueError("revision 1 has no parent; later revisions require one")
        return self

    @property
    def commitment_payload(self) -> dict[str, object]:
        return {
            "accounting_basis": self.accounting_basis,
            "aliases": list(self.aliases),
            "definition_text": self.definition_text,
            "lifecycle": self.lifecycle,
            "metric_id": self.metric_id,
            "period_kind": self.period_kind,
            "revision": self.revision,
            "scope_constraints": self.scope_constraints,
            "supersedes_metric_definition_revision_id": (
                self.supersedes_metric_definition_revision_id
            ),
            "unit_family": self.unit_family,
            "value_kind": self.value_kind,
        }


class _PolicyRevision(_Bitemporal):
    revision: int = Field(gt=0)
    policy_name: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    policy_config_sha256: str = Field(min_length=64, max_length=64)
    evidence: dict[str, object]
    reviewer_identity: str | None = None
    audited_policy_path: str | None = None

    @field_validator("policy_config_sha256")
    @classmethod
    def _valid_policy_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @property
    def evidence_sha256(self) -> str:
        return sha256_json(self.evidence)


class SourceDimensionMappingRevision(_PolicyRevision):
    dimension_mapping_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    source_component_id: str = Field(min_length=1, max_length=128)
    supersedes_dimension_mapping_revision_id: str | None = None
    disposition: DimensionDisposition
    canonical_axis_id: str | None = None
    canonical_member_id: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.revision == 1) != (self.supersedes_dimension_mapping_revision_id is None):
            raise ValueError("revision 1 has no parent; later revisions require one")
        admitted = self.disposition in {"exact", "equivalent"}
        if admitted != (self.canonical_axis_id is not None):
            raise ValueError("only exact/equivalent dimension mappings carry canonical IDs")
        if self.canonical_member_id is not None and self.canonical_axis_id is None:
            raise ValueError("canonical members require their canonical axis")
        return self

    @property
    def commitment_payload(self) -> dict[str, object]:
        return {
            "audited_policy_path": self.audited_policy_path,
            "canonical_axis_id": self.canonical_axis_id,
            "canonical_member_id": self.canonical_member_id,
            "disposition": self.disposition,
            "evidence_sha256": self.evidence_sha256,
            "policy_config_sha256": self.policy_config_sha256,
            "policy_name": self.policy_name,
            "policy_version": self.policy_version,
            "reviewer_identity": self.reviewer_identity,
            "revision": self.revision,
            "source_component_id": self.source_component_id,
            "supersedes_dimension_mapping_revision_id": (
                self.supersedes_dimension_mapping_revision_id
            ),
        }


class MappingRevision(_PolicyRevision):
    mapping_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    source_component_id: str = Field(min_length=1, max_length=128)
    supersedes_mapping_revision_id: str | None = None
    metric_id: str | None = Field(default=None, min_length=1, max_length=128)
    disposition: Disposition
    method_name: str = Field(min_length=1)
    method_version: str = Field(min_length=1)
    constraints: dict[str, object]

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.revision == 1) != (self.supersedes_mapping_revision_id is None):
            raise ValueError("revision 1 has no parent; later revisions require one")
        carries_metric = self.disposition in {"exact", "equivalent", "derived"}
        if carries_metric != (self.metric_id is not None):
            raise ValueError("only exact, equivalent, and derived mappings carry metric_id")
        return self

    @property
    def commitment_payload(self) -> dict[str, object]:
        return {
            "audited_policy_path": self.audited_policy_path,
            "constraints": self.constraints,
            "disposition": self.disposition,
            "evidence_sha256": self.evidence_sha256,
            "method_name": self.method_name,
            "method_version": self.method_version,
            "metric_id": self.metric_id,
            "policy_config_sha256": self.policy_config_sha256,
            "policy_name": self.policy_name,
            "policy_version": self.policy_version,
            "reviewer_identity": self.reviewer_identity,
            "revision": self.revision,
            "source_component_id": self.source_component_id,
            "supersedes_mapping_revision_id": self.supersedes_mapping_revision_id,
        }


class CanonicalDimension(_Frozen):
    axis_id: str = Field(min_length=1, max_length=128)
    member_id: str = Field(min_length=1, max_length=128)


class CanonicalMetricCell(_Bitemporal):
    canonical_metric_cell_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    metric_id: str = Field(min_length=1, max_length=128)
    reporting_entity_id: str = Field(min_length=1, max_length=128)
    scope_security_id: str | None = None
    period_kind: PeriodKind
    period_start: datetime | None = None
    period_end: datetime
    dimensions: tuple[CanonicalDimension, ...] = ()
    unit_family: str = Field(min_length=1)
    accounting_basis: str = Field(min_length=1)
    consolidation_scope: str = Field(min_length=1)

    @field_validator("dimensions")
    @classmethod
    def _canonical_dimensions(
        cls, value: tuple[CanonicalDimension, ...]
    ) -> tuple[CanonicalDimension, ...]:
        axes = [dimension.axis_id for dimension in value]
        if len(axes) != len(set(axes)):
            raise ValueError("canonical dimension axes must be unique")
        return tuple(sorted(value, key=lambda item: (item.axis_id, item.member_id)))

    @model_validator(mode="after")
    def _period_shape(self) -> Self:
        if (self.period_kind == "instant") != (self.period_start is None):
            raise ValueError("instant cells have no start; duration cells require one")
        if self.period_start is not None and self.period_end < self.period_start:
            raise ValueError("period_end must not precede period_start")
        return self

    @property
    def dimension_set(self) -> list[dict[str, str]]:
        return [dimension.model_dump() for dimension in self.dimensions]

    @property
    def semantic_identity(self) -> dict[str, object]:
        return {
            "accounting_basis": self.accounting_basis,
            "canonical_dimensions": self.dimension_set,
            "consolidation_scope": self.consolidation_scope,
            "metric_id": self.metric_id,
            "period_end": _time(self.period_end),
            "period_kind": self.period_kind,
            "period_start": (None if self.period_start is None else _time(self.period_start)),
            "reporting_entity_id": self.reporting_entity_id,
            "scope_security_id": self.scope_security_id,
            "unit_family": self.unit_family,
        }


class BindingRevision(_Bitemporal):
    binding_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    fact_cell_id: str = Field(min_length=1, max_length=128)
    source_observation_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(gt=0)
    supersedes_binding_revision_id: str | None = None
    canonical_metric_cell_id: str | None = Field(default=None, min_length=1, max_length=128)
    mapping_revision_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_component_id: str | None = Field(default=None, min_length=1, max_length=128)
    binding_status: Literal["bound", "quarantined", "retired"] = "bound"
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)
    reason_details: dict[str, object] | None = None

    @model_validator(mode="after")
    def _revision_shape(self) -> Self:
        if (self.revision == 1) != (self.supersedes_binding_revision_id is None):
            raise ValueError("revision 1 has no parent; later revisions require one")
        has_coordinates = all(
            value is not None
            for value in (
                self.canonical_metric_cell_id,
                self.mapping_revision_id,
                self.source_component_id,
            )
        )
        has_no_coordinates = all(
            value is None
            for value in (
                self.canonical_metric_cell_id,
                self.mapping_revision_id,
                self.source_component_id,
            )
        )
        has_reason = self.reason_code is not None and self.reason_details is not None
        has_no_reason = self.reason_code is None and self.reason_details is None
        if self.binding_status == "bound" and not (has_coordinates and has_no_reason):
            raise ValueError("bound revisions require coordinates and no quarantine reason")
        if self.binding_status == "quarantined" and not (has_no_coordinates and has_reason):
            raise ValueError("quarantined revisions require a reason and no coordinates")
        if self.binding_status == "retired" and not (
            has_coordinates and (has_reason or has_no_reason)
        ):
            raise ValueError("retired revisions retain coordinates and an optional full reason")
        return self

    @property
    def commitment_payload(self) -> dict[str, object]:
        return {
            "binding_status": self.binding_status,
            "canonical_metric_cell_id": self.canonical_metric_cell_id,
            "fact_cell_id": self.fact_cell_id,
            "mapping_revision_id": self.mapping_revision_id,
            "reason_code": self.reason_code,
            "reason_details": self.reason_details,
            "revision": self.revision,
            "source_component_id": self.source_component_id,
            "source_observation_id": self.source_observation_id,
            "supersedes_binding_revision_id": self.supersedes_binding_revision_id,
        }


class OntologySnapshot(_Frozen):
    ontology_snapshot_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    cutoff_at: datetime
    recorded_at: datetime

    @model_validator(mode="after")
    def _clocks(self) -> Self:
        if _utc(self.recorded_at) < _utc(self.cutoff_at):
            raise ValueError("snapshot recorded_at must not precede cutoff_at")
        return self


class MetricOntology:
    """Atomic persistence, point-in-time reads, binding proof, and sealing."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.create_function("fact_sha256", 1, _sql_sha256, deterministic=True)

    @contextmanager
    def _savepoint(self, name: str) -> Generator[None, None, None]:
        self._conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self._conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self._conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        self._conn.execute(f"RELEASE SAVEPOINT {name}")

    def _insert_or_verify(
        self,
        table: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        *,
        idempotency_key: str,
    ) -> None:
        serialized = tuple(
            _db_time(value) if isinstance(value, datetime) else value for value in values
        )
        existing = self._conn.execute(
            f"SELECT {','.join(columns)} FROM {table} WHERE idempotency_key=?",  # nosec B608 -- trusted internal SQL shape; values remain bound
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != serialized:
                raise ValueError(f"idempotency conflict for {table}")
            return
        placeholders = ",".join("?" for _ in columns)
        self._conn.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",  # nosec B608 -- trusted internal SQL shape; values remain bound
            serialized,
        )

    def _persist_committed(
        self,
        table: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        *,
        idempotency_key: str,
        payload: object,
    ) -> None:
        commitment = canonical_json(payload)
        self._insert_or_verify(
            table,
            (*columns, "commitment_json", "commitment_sha256"),
            (*values, commitment, _digest(commitment)),
            idempotency_key=idempotency_key,
        )

    def persist_metric(self, metric: CanonicalMetric) -> None:
        with self._savepoint("persist_canonical_metric"):
            self._persist_committed(
                "canonical_metrics",
                (
                    "metric_id",
                    "idempotency_key",
                    "canonical_name",
                    "effective_at",
                    "knowledge_at",
                    "recorded_at",
                ),
                (
                    metric.metric_id,
                    metric.idempotency_key,
                    metric.canonical_name,
                    metric.effective_at,
                    metric.knowledge_at,
                    metric.recorded_at,
                ),
                idempotency_key=metric.idempotency_key,
                payload=metric.commitment_payload,
            )

    def persist_axis(self, axis: CanonicalAxis) -> None:
        with self._savepoint("persist_canonical_axis"):
            self._persist_committed(
                "canonical_axes",
                (
                    "axis_id",
                    "idempotency_key",
                    "canonical_name",
                    "effective_at",
                    "knowledge_at",
                    "recorded_at",
                ),
                (
                    axis.axis_id,
                    axis.idempotency_key,
                    axis.canonical_name,
                    axis.effective_at,
                    axis.knowledge_at,
                    axis.recorded_at,
                ),
                idempotency_key=axis.idempotency_key,
                payload=axis.commitment_payload,
            )

    def persist_member(self, member: CanonicalMember) -> None:
        with self._savepoint("persist_canonical_member"):
            self._persist_committed(
                "canonical_members",
                (
                    "member_id",
                    "idempotency_key",
                    "axis_id",
                    "canonical_name",
                    "effective_at",
                    "knowledge_at",
                    "recorded_at",
                ),
                (
                    member.member_id,
                    member.idempotency_key,
                    member.axis_id,
                    member.canonical_name,
                    member.effective_at,
                    member.knowledge_at,
                    member.recorded_at,
                ),
                idempotency_key=member.idempotency_key,
                payload=member.commitment_payload,
            )

    def persist_source_component(self, component: SourceTaxonomyComponent) -> None:
        with self._savepoint("persist_source_component"):
            self._persist_committed(
                "source_taxonomy_components",
                (
                    "component_id",
                    "idempotency_key",
                    "component_kind",
                    "taxonomy_namespace",
                    "local_name",
                    "taxonomy_name",
                    "taxonomy_version",
                    "reporting_entity_id",
                    "reporting_entity_scope_key",
                    "is_extension",
                    "data_type",
                    "period_type",
                    "balance",
                    "is_abstract",
                    "standard_label",
                    "definition_text",
                    "references_json",
                    "evidence_locator_json",
                    "effective_at",
                    "knowledge_at",
                    "recorded_at",
                ),
                (
                    component.component_id,
                    component.idempotency_key,
                    component.component_kind,
                    component.taxonomy_namespace,
                    component.local_name,
                    component.taxonomy_name,
                    component.taxonomy_version,
                    component.reporting_entity_id,
                    component.reporting_entity_scope_key,
                    int(component.is_extension),
                    component.data_type,
                    component.period_type,
                    component.balance,
                    (None if component.is_abstract is None else int(component.is_abstract)),
                    component.standard_label,
                    component.definition_text,
                    canonical_json(list(component.references)),
                    canonical_json(component.evidence_locator),
                    component.effective_at,
                    component.knowledge_at,
                    component.recorded_at,
                ),
                idempotency_key=component.idempotency_key,
                payload=component.commitment_payload,
            )

    def persist_observation_taxonomy_assertion(
        self, assertion: SourceObservationTaxonomyAssertion
    ) -> None:
        self._prove_observation_taxonomy_assertion(assertion)
        with self._savepoint("persist_observation_taxonomy_assertion"):
            self._persist_committed(
                "source_observation_taxonomy_assertions",
                (
                    "observation_id",
                    "idempotency_key",
                    "extraction_run_id",
                    "taxonomy_name",
                    "taxonomy_version",
                    "fact_cell_semantic_key_sha256",
                    "anchor_payload_sha256",
                    "observation_payload_sha256",
                    "extraction_output_sha256",
                    "raw_entry_sha256",
                    "observation_set_sha256",
                    "knowledge_at",
                    "recorded_at",
                ),
                (
                    assertion.observation_id,
                    assertion.idempotency_key,
                    assertion.extraction_run_id,
                    assertion.taxonomy_name,
                    assertion.taxonomy_version,
                    assertion.fact_cell_semantic_key_sha256,
                    assertion.anchor_payload_sha256,
                    assertion.observation_payload_sha256,
                    assertion.extraction_output_sha256,
                    assertion.raw_entry_sha256,
                    assertion.observation_set_sha256,
                    assertion.knowledge_at,
                    assertion.recorded_at,
                ),
                idempotency_key=assertion.idempotency_key,
                payload=assertion.commitment_payload,
            )

    def _prove_observation_taxonomy_assertion(
        self, assertion: SourceObservationTaxonomyAssertion
    ) -> None:
        row = self._conn.execute(
            """
            SELECT 1
            FROM fact_observations_v2 observation
            JOIN fact_cells_v2 cell
              ON cell.fact_cell_id=observation.fact_cell_id
            JOIN fact_cell_identity_seals_v2 cell_seal
              ON cell_seal.fact_cell_id=cell.fact_cell_id
            JOIN fact_reported_observation_anchors_v2 anchor
              ON anchor.observation_id=observation.observation_id
            JOIN fact_observation_payload_commitments_v2 payload
              ON payload.observation_id=observation.observation_id
            JOIN evidence_extraction_runs run
              ON run.extraction_run_id=anchor.extraction_run_id
            JOIN fact_extraction_run_completeness_seals_v2 completeness
              ON completeness.extraction_run_id=run.extraction_run_id
            WHERE observation.observation_id=?
              AND observation.observation_kind='reported'
              AND run.extraction_run_id=?
              AND run.outcome='succeeded'
              AND cell.taxonomy_name=?
              AND anchor.source_taxonomy_version=?
              AND cell_seal.semantic_key_sha256=?
              AND anchor.anchor_payload_sha256=?
              AND payload.observation_payload_sha256=?
              AND run.output_sha256=?
              AND anchor.extraction_output_sha256=?
              AND completeness.extraction_output_sha256=?
              AND anchor.raw_entry_sha256=?
              AND observation.source_entry_sha256=?
              AND completeness.observation_set_sha256=?
              AND EXISTS (
                    SELECT 1
                    FROM json_each(completeness.observation_set_json) member
                    WHERE member.value=observation.observation_id)
              AND datetime(cell.recorded_at) <= datetime(?)
              AND datetime(cell_seal.sealed_at) <= datetime(?)
              AND datetime(observation.recorded_at) <= datetime(?)
              AND datetime(anchor.recorded_at) <= datetime(?)
              AND datetime(payload.committed_at) <= datetime(?)
              AND datetime(run.completed_at) <= datetime(?)
              AND datetime(completeness.knowledge_at) <= datetime(?)
              AND datetime(completeness.recorded_at) <= datetime(?)
            """,
            (
                assertion.observation_id,
                assertion.extraction_run_id,
                assertion.taxonomy_name,
                assertion.taxonomy_version,
                assertion.fact_cell_semantic_key_sha256,
                assertion.anchor_payload_sha256,
                assertion.observation_payload_sha256,
                assertion.extraction_output_sha256,
                assertion.extraction_output_sha256,
                assertion.extraction_output_sha256,
                assertion.raw_entry_sha256,
                assertion.raw_entry_sha256,
                assertion.observation_set_sha256,
                *(_db_time(assertion.knowledge_at) for _ in range(8)),
            ),
        ).fetchone()
        if row is None:
            raise ValueError("taxonomy assertion lacks exact committed fact evidence")

    def persist_metric_definition(self, definition: CanonicalMetricDefinitionRevision) -> None:
        with self._savepoint("persist_metric_definition"):
            self._persist_committed(
                "canonical_metric_definition_revisions",
                (
                    "metric_definition_revision_id",
                    "idempotency_key",
                    "metric_id",
                    "revision",
                    "supersedes_metric_definition_revision_id",
                    "lifecycle",
                    "definition_text",
                    "aliases_json",
                    "value_kind",
                    "period_kind",
                    "unit_family",
                    "accounting_basis",
                    "scope_constraints_json",
                    "effective_at",
                    "knowledge_at",
                    "recorded_at",
                ),
                (
                    definition.metric_definition_revision_id,
                    definition.idempotency_key,
                    definition.metric_id,
                    definition.revision,
                    definition.supersedes_metric_definition_revision_id,
                    definition.lifecycle,
                    definition.definition_text,
                    canonical_json(list(definition.aliases)),
                    definition.value_kind,
                    definition.period_kind,
                    definition.unit_family,
                    definition.accounting_basis,
                    canonical_json(definition.scope_constraints),
                    definition.effective_at,
                    definition.knowledge_at,
                    definition.recorded_at,
                ),
                idempotency_key=definition.idempotency_key,
                payload=definition.commitment_payload,
            )

    def persist_mapping(self, mapping: MappingRevision) -> None:
        source = self._conn.execute(
            "SELECT is_extension FROM source_taxonomy_components "
            "WHERE component_id=? AND component_kind='concept'",
            (mapping.source_component_id,),
        ).fetchone()
        if source is None:
            raise ValueError("mapping requires a registered source concept")
        if (
            mapping.metric_id is not None
            and self._conn.execute(
                "SELECT 1 FROM canonical_metrics WHERE metric_id=?",
                (mapping.metric_id,),
            ).fetchone()
            is None
        ):
            raise ValueError("mapping requires a registered canonical metric")
        if (
            bool(source["is_extension"])
            and mapping.disposition in {"exact", "equivalent"}
            and not (mapping.reviewer_identity or mapping.audited_policy_path)
        ):
            raise ValueError("extension mapping requires reviewer or audited policy path")
        with self._savepoint("persist_metric_mapping"):
            self._persist_committed(
                "metric_mapping_revisions",
                (
                    "mapping_revision_id",
                    "idempotency_key",
                    "source_component_id",
                    "revision",
                    "supersedes_mapping_revision_id",
                    "metric_id",
                    "disposition",
                    "policy_name",
                    "policy_version",
                    "policy_config_sha256",
                    "method_name",
                    "method_version",
                    "constraints_json",
                    "evidence_json",
                    "evidence_sha256",
                    "reviewer_identity",
                    "audited_policy_path",
                    "effective_at",
                    "knowledge_at",
                    "recorded_at",
                ),
                (
                    mapping.mapping_revision_id,
                    mapping.idempotency_key,
                    mapping.source_component_id,
                    mapping.revision,
                    mapping.supersedes_mapping_revision_id,
                    mapping.metric_id,
                    mapping.disposition,
                    mapping.policy_name,
                    mapping.policy_version,
                    mapping.policy_config_sha256,
                    mapping.method_name,
                    mapping.method_version,
                    canonical_json(mapping.constraints),
                    canonical_json(mapping.evidence),
                    mapping.evidence_sha256,
                    mapping.reviewer_identity,
                    mapping.audited_policy_path,
                    mapping.effective_at,
                    mapping.knowledge_at,
                    mapping.recorded_at,
                ),
                idempotency_key=mapping.idempotency_key,
                payload=mapping.commitment_payload,
            )

    def persist_dimension_mapping(self, mapping: SourceDimensionMappingRevision) -> None:
        source = self._conn.execute(
            "SELECT component_kind,is_extension FROM source_taxonomy_components "
            "WHERE component_id=?",
            (mapping.source_component_id,),
        ).fetchone()
        if source is None or source["component_kind"] not in {"axis", "member"}:
            raise ValueError("dimension mapping requires a source axis or member")
        if (
            bool(source["is_extension"])
            and mapping.disposition in {"exact", "equivalent"}
            and not (mapping.reviewer_identity or mapping.audited_policy_path)
        ):
            raise ValueError("extension dimension mapping requires reviewer or audited policy path")
        with self._savepoint("persist_dimension_mapping"):
            self._persist_committed(
                "source_dimension_mapping_revisions",
                (
                    "dimension_mapping_revision_id",
                    "idempotency_key",
                    "source_component_id",
                    "revision",
                    "supersedes_dimension_mapping_revision_id",
                    "disposition",
                    "canonical_axis_id",
                    "canonical_member_id",
                    "policy_name",
                    "policy_version",
                    "policy_config_sha256",
                    "evidence_json",
                    "evidence_sha256",
                    "reviewer_identity",
                    "audited_policy_path",
                    "effective_at",
                    "knowledge_at",
                    "recorded_at",
                ),
                (
                    mapping.dimension_mapping_revision_id,
                    mapping.idempotency_key,
                    mapping.source_component_id,
                    mapping.revision,
                    mapping.supersedes_dimension_mapping_revision_id,
                    mapping.disposition,
                    mapping.canonical_axis_id,
                    mapping.canonical_member_id,
                    mapping.policy_name,
                    mapping.policy_version,
                    mapping.policy_config_sha256,
                    canonical_json(mapping.evidence),
                    mapping.evidence_sha256,
                    mapping.reviewer_identity,
                    mapping.audited_policy_path,
                    mapping.effective_at,
                    mapping.knowledge_at,
                    mapping.recorded_at,
                ),
                idempotency_key=mapping.idempotency_key,
                payload=mapping.commitment_payload,
            )

    def persist_canonical_metric_cell(self, cell: CanonicalMetricCell) -> None:
        if (
            self._conn.execute(
                "SELECT 1 FROM canonical_metrics WHERE metric_id=?", (cell.metric_id,)
            ).fetchone()
            is None
        ):
            raise ValueError("canonical cell requires a registered metric")
        for dimension in cell.dimensions:
            if (
                self._conn.execute(
                    "SELECT 1 FROM canonical_members WHERE axis_id=? AND member_id=?",
                    (dimension.axis_id, dimension.member_id),
                ).fetchone()
                is None
            ):
                raise ValueError("canonical cell contains an unknown axis/member")
        dimension_json = canonical_json(cell.dimension_set)
        semantic_json = canonical_json(cell.semantic_identity)
        with self._savepoint("persist_canonical_metric_cell"):
            self._insert_or_verify(
                "canonical_metric_cells",
                (
                    "canonical_metric_cell_id",
                    "idempotency_key",
                    "metric_id",
                    "reporting_entity_id",
                    "scope_security_id",
                    "period_kind",
                    "period_start",
                    "period_end",
                    "dimension_count",
                    "unit_family",
                    "accounting_basis",
                    "consolidation_scope",
                    "effective_at",
                    "knowledge_at",
                    "recorded_at",
                ),
                (
                    cell.canonical_metric_cell_id,
                    cell.idempotency_key,
                    cell.metric_id,
                    cell.reporting_entity_id,
                    cell.scope_security_id,
                    cell.period_kind,
                    cell.period_start,
                    cell.period_end,
                    len(cell.dimensions),
                    cell.unit_family,
                    cell.accounting_basis,
                    cell.consolidation_scope,
                    cell.effective_at,
                    cell.knowledge_at,
                    cell.recorded_at,
                ),
                idempotency_key=cell.idempotency_key,
            )
            for ordinal, dimension in enumerate(cell.dimensions):
                self._conn.execute(
                    "INSERT INTO canonical_metric_cell_dimensions "
                    "(canonical_metric_cell_id,dimension_ordinal,axis_id,member_id) "
                    "VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                    (
                        cell.canonical_metric_cell_id,
                        ordinal,
                        dimension.axis_id,
                        dimension.member_id,
                    ),
                )
            self._conn.execute(
                "INSERT INTO canonical_metric_cell_seals "
                "(canonical_metric_cell_id,dimension_set_json,"
                "dimension_set_sha256,semantic_identity_json,"
                "semantic_key_sha256,sealed_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT DO NOTHING",
                (
                    cell.canonical_metric_cell_id,
                    dimension_json,
                    _digest(dimension_json),
                    semantic_json,
                    _digest(semantic_json),
                    _db_time(cell.recorded_at),
                ),
            )
            self._verify_cell_replay(cell)

    def _verify_cell_replay(self, cell: CanonicalMetricCell) -> None:
        seal = self._conn.execute(
            "SELECT dimension_set_json,semantic_identity_json "
            "FROM canonical_metric_cell_seals WHERE canonical_metric_cell_id=?",
            (cell.canonical_metric_cell_id,),
        ).fetchone()
        if seal is None or tuple(seal) != (
            canonical_json(cell.dimension_set),
            canonical_json(cell.semantic_identity),
        ):
            raise ValueError("canonical metric cell replay conflict")

    def persist_binding(self, binding: BindingRevision) -> None:
        if binding.binding_status == "bound":
            self._prove_binding_compatibility(binding)
        elif binding.binding_status == "quarantined":
            self._prove_derived_quarantine(binding)
        else:
            self._prove_observation_coordinate(binding)
        if binding.revision > 1:
            parent = self._conn.execute(
                "SELECT binding_status FROM fact_cell_canonical_binding_revisions "
                "WHERE binding_revision_id=?",
                (binding.supersedes_binding_revision_id,),
            ).fetchone()
            if parent is not None and parent["binding_status"] == "quarantined":
                raise ValueError("quarantined binding revisions are terminal")
        commitment = canonical_json(binding.commitment_payload)
        reason_details = (
            None if binding.reason_details is None else canonical_json(binding.reason_details)
        )
        with self._savepoint("persist_fact_cell_canonical_binding"):
            self._insert_or_verify(
                "fact_cell_canonical_binding_revisions",
                (
                    "binding_revision_id",
                    "idempotency_key",
                    "fact_cell_id",
                    "source_observation_id",
                    "revision",
                    "supersedes_binding_revision_id",
                    "canonical_metric_cell_id",
                    "mapping_revision_id",
                    "source_component_id",
                    "binding_status",
                    "reason_code",
                    "reason_details_json",
                    "reason_details_sha256",
                    "commitment_json",
                    "commitment_sha256",
                    "effective_at",
                    "knowledge_at",
                    "recorded_at",
                ),
                (
                    binding.binding_revision_id,
                    binding.idempotency_key,
                    binding.fact_cell_id,
                    binding.source_observation_id,
                    binding.revision,
                    binding.supersedes_binding_revision_id,
                    binding.canonical_metric_cell_id,
                    binding.mapping_revision_id,
                    binding.source_component_id,
                    binding.binding_status,
                    binding.reason_code,
                    reason_details,
                    (None if reason_details is None else _digest(reason_details)),
                    commitment,
                    _digest(commitment),
                    binding.effective_at,
                    binding.knowledge_at,
                    binding.recorded_at,
                ),
                idempotency_key=binding.idempotency_key,
            )

    def _prove_observation_coordinate(self, binding: BindingRevision) -> str:
        row = self._conn.execute(
            "SELECT observation_kind FROM fact_observations_v2 "
            "WHERE observation_id=? AND fact_cell_id=?",
            (binding.source_observation_id, binding.fact_cell_id),
        ).fetchone()
        if row is None:
            raise ValueError("binding observation does not belong to its fact cell")
        return str(row["observation_kind"])

    def _prove_derived_quarantine(self, binding: BindingRevision) -> None:
        if self._prove_observation_coordinate(binding) != "derived":
            raise ValueError("only derived observations use terminal ontology quarantine")

    def _prove_binding_compatibility(self, binding: BindingRevision) -> None:
        source_component_id = binding.source_component_id
        mapping_revision_id = binding.mapping_revision_id
        canonical_metric_cell_id = binding.canonical_metric_cell_id
        if (
            source_component_id is None
            or mapping_revision_id is None
            or canonical_metric_cell_id is None
        ):
            raise ValueError("bound revisions require complete canonical coordinates")
        row = self._conn.execute(
            """
            SELECT source_cell.concept_namespace AS source_concept_namespace,
                   source_cell.concept_name AS source_concept_name,
                   source_cell.reporting_entity_id AS source_reporting_entity_id,
                   source_cell.scope_security_id AS source_scope_security_id,
                   source_cell.period_kind AS source_period_kind,
                   source_cell.period_start AS source_period_start,
                   source_cell.period_end AS source_period_end,
                   source_cell.accounting_basis AS source_accounting_basis,
                   source_cell.consolidation_scope AS source_consolidation_scope,
                   source_cell.unit_key AS source_unit_key,
                   source_cell.currency AS source_currency,
                   observation.observation_kind AS observation_kind,
                   anchor.source_taxonomy_version AS anchor_taxonomy_version,
                   taxonomy.taxonomy_name AS assertion_taxonomy_name,
                   taxonomy.taxonomy_version AS assertion_taxonomy_version,
                   source.taxonomy_namespace AS component_taxonomy_namespace,
                   source.local_name AS component_local_name,
                   source.taxonomy_name AS component_taxonomy_name,
                   source.taxonomy_version AS component_taxonomy_version,
                   source.reporting_entity_scope_key AS component_entity_scope,
                   mapping.metric_id AS mapping_metric_id,
                   mapping.disposition AS mapping_disposition,
                   target.metric_id AS target_metric_id,
                   target.reporting_entity_id AS target_reporting_entity_id,
                   target.scope_security_id AS target_scope_security_id,
                   target.period_kind AS target_period_kind,
                   target.period_start AS target_period_start,
                   target.period_end AS target_period_end,
                   target.unit_family AS target_unit_family,
                   target.accounting_basis AS target_accounting_basis,
                   target.consolidation_scope AS target_consolidation_scope
            FROM fact_cells_v2 source_cell
            JOIN fact_observations_v2 observation
              ON observation.observation_id=?
             AND observation.fact_cell_id=source_cell.fact_cell_id
             AND observation.observation_kind='reported'
            JOIN fact_reported_observation_anchors_v2 anchor
              ON anchor.observation_id=observation.observation_id
            JOIN source_observation_taxonomy_assertions taxonomy
              ON taxonomy.observation_id=observation.observation_id
             AND taxonomy.taxonomy_version=anchor.source_taxonomy_version
            JOIN source_taxonomy_components source
              ON source.component_id=? AND source.component_kind='concept'
            JOIN metric_mapping_revisions mapping
              ON mapping.mapping_revision_id=?
             AND mapping.source_component_id=source.component_id
            JOIN canonical_metric_cells target
              ON target.canonical_metric_cell_id=?
            JOIN canonical_metric_cell_seals target_seal
              ON target_seal.canonical_metric_cell_id=target.canonical_metric_cell_id
            WHERE source_cell.fact_cell_id=?
              AND datetime(taxonomy.knowledge_at) <= datetime(?)
              AND datetime(taxonomy.recorded_at) <= datetime(?)
              AND datetime(source.effective_at) <= datetime(?)
              AND datetime(source.knowledge_at) <= datetime(?)
              AND datetime(source.recorded_at) <= datetime(?)
              AND datetime(mapping.effective_at) <= datetime(?)
              AND datetime(mapping.knowledge_at) <= datetime(?)
              AND datetime(mapping.recorded_at) <= datetime(?)
              AND datetime(source_cell.effective_at) <= datetime(?)
              AND datetime(source_cell.knowledge_at) <= datetime(?)
              AND datetime(source_cell.recorded_at) <= datetime(?)
              AND datetime(target.effective_at) <= datetime(?)
              AND datetime(target.knowledge_at) <= datetime(?)
              AND datetime(target.recorded_at) <= datetime(?)
              AND datetime(target_seal.sealed_at) <= datetime(?)
            """,
            (
                binding.source_observation_id,
                source_component_id,
                mapping_revision_id,
                canonical_metric_cell_id,
                binding.fact_cell_id,
                _db_time(binding.knowledge_at),
                _db_time(binding.recorded_at),
                _db_time(binding.effective_at),
                _db_time(binding.knowledge_at),
                _db_time(binding.recorded_at),
                _db_time(binding.effective_at),
                _db_time(binding.knowledge_at),
                _db_time(binding.recorded_at),
                _db_time(binding.effective_at),
                _db_time(binding.knowledge_at),
                _db_time(binding.recorded_at),
                _db_time(binding.effective_at),
                _db_time(binding.knowledge_at),
                _db_time(binding.recorded_at),
                _db_time(binding.recorded_at),
            ),
        ).fetchone()
        if row is None:
            raise ValueError("binding requires committed source and canonical records")
        source_unit_family = (
            "currency"
            if row["source_currency"] is not None
            else str(row["source_unit_key"]).lower()
            if str(row["source_unit_key"]).lower() in {"pure", "shares"}
            else str(row["source_unit_key"])
        )
        expected = (
            row["observation_kind"] == "reported",
            row["source_concept_namespace"] == row["component_taxonomy_namespace"],
            row["source_concept_name"] == row["component_local_name"],
            row["anchor_taxonomy_version"] == row["assertion_taxonomy_version"],
            row["anchor_taxonomy_version"] == row["component_taxonomy_version"],
            row["assertion_taxonomy_name"] == row["component_taxonomy_name"],
            row["component_entity_scope"] in {"__global__", row["source_reporting_entity_id"]},
            row["mapping_disposition"] in {"exact", "equivalent", "derived"},
            row["mapping_metric_id"] == row["target_metric_id"],
            row["source_reporting_entity_id"] == row["target_reporting_entity_id"],
            row["source_scope_security_id"] == row["target_scope_security_id"],
            row["source_period_kind"] == row["target_period_kind"],
            _same_instant(row["source_period_start"], row["target_period_start"]),
            _same_instant(row["source_period_end"], row["target_period_end"]),
            source_unit_family == row["target_unit_family"],
            row["source_accounting_basis"] == row["target_accounting_basis"],
            row["source_consolidation_scope"] == row["target_consolidation_scope"],
        )
        if not all(expected):
            raise ValueError("binding source assertion is incompatible with canonical cell")
        source_dimensions = self._resolved_source_dimensions(binding)
        target_dimensions = {
            (str(item["axis_id"]), str(item["member_id"]))
            for item in self._conn.execute(
                "SELECT axis_id,member_id "
                "FROM canonical_metric_cell_dimensions "
                "WHERE canonical_metric_cell_id=?",
                (canonical_metric_cell_id,),
            )
        }
        if source_dimensions != target_dimensions:
            raise ValueError("binding canonical dimensions do not match source fact")

    def _resolved_source_dimensions(self, binding: BindingRevision) -> set[tuple[str, str]]:
        rows = self._conn.execute(
            """
            SELECT dim.member_kind, axis.component_id AS axis_component_id,
                   member.component_id AS member_component_id
            FROM fact_dimensions_normalized_v2 dim
            JOIN fact_observations_v2 observation
              ON observation.observation_id=?
             AND observation.fact_cell_id=dim.fact_cell_id
            JOIN fact_reported_observation_anchors_v2 anchor
              ON anchor.observation_id=observation.observation_id
            LEFT JOIN source_taxonomy_components axis
              ON axis.component_kind='axis'
             AND axis.taxonomy_namespace=dim.axis_namespace
             AND axis.local_name=dim.axis_name
             AND axis.taxonomy_version=anchor.source_taxonomy_version
             AND axis.reporting_entity_scope_key IN (
                 '__global__',
                 (SELECT reporting_entity_id FROM fact_cells_v2
                  WHERE fact_cell_id=dim.fact_cell_id))
            LEFT JOIN source_taxonomy_components member
              ON member.component_kind='member'
             AND member.taxonomy_namespace=dim.explicit_member_namespace
             AND member.local_name=dim.explicit_member_name
             AND member.taxonomy_version=anchor.source_taxonomy_version
             AND member.reporting_entity_scope_key IN (
                 '__global__',
                 (SELECT reporting_entity_id FROM fact_cells_v2
                  WHERE fact_cell_id=dim.fact_cell_id))
            WHERE dim.fact_cell_id=?
            ORDER BY dim.dimension_ordinal
            """,
            (binding.source_observation_id, binding.fact_cell_id),
        ).fetchall()
        resolved: set[tuple[str, str]] = set()
        for row in rows:
            if (
                row["member_kind"] != "explicit"
                or row["axis_component_id"] is None
                or row["member_component_id"] is None
            ):
                raise ValueError("unknown or typed source dimension fails closed")
            axis = self.dimension_mapping_as_known(
                str(row["axis_component_id"]), binding.knowledge_at
            )
            member = self.dimension_mapping_as_known(
                str(row["member_component_id"]), binding.knowledge_at
            )
            if (
                axis is None
                or member is None
                or axis.disposition not in {"exact", "equivalent"}
                or member.disposition not in {"exact", "equivalent"}
                or axis.canonical_axis_id is None
                or member.canonical_axis_id != axis.canonical_axis_id
                or member.canonical_member_id is None
            ):
                raise ValueError("source dimension lacks an admitted mapping")
            resolved.add((axis.canonical_axis_id, member.canonical_member_id))
        return resolved

    def metric_definition_as_known(
        self, metric_id: str, cutoff_at: datetime
    ) -> CanonicalMetricDefinitionRevision | None:
        row = self._as_known_row(
            "canonical_metric_definition_revisions",
            "metric_id",
            metric_id,
            cutoff_at,
        )
        return None if row is None else self._definition_from_row(row)

    def mapping_as_known(
        self, source_component_id: str, cutoff_at: datetime
    ) -> MappingRevision | None:
        row = self._as_known_row(
            "metric_mapping_revisions",
            "source_component_id",
            source_component_id,
            cutoff_at,
        )
        return None if row is None else self._mapping_from_row(row)

    def dimension_mapping_as_known(
        self, source_component_id: str, cutoff_at: datetime
    ) -> SourceDimensionMappingRevision | None:
        row = self._as_known_row(
            "source_dimension_mapping_revisions",
            "source_component_id",
            source_component_id,
            cutoff_at,
        )
        return None if row is None else self._dimension_mapping_from_row(row)

    def binding_as_known(
        self, source_observation_id: str, cutoff_at: datetime
    ) -> BindingRevision | None:
        row = self._as_known_row(
            "fact_cell_canonical_binding_revisions",
            "source_observation_id",
            source_observation_id,
            cutoff_at,
        )
        return None if row is None else self._binding_from_row(row)

    def bindings_for_fact_cell_as_known(
        self, fact_cell_id: str, cutoff_at: datetime
    ) -> tuple[BindingRevision, ...]:
        cutoff = _db_time(cutoff_at)
        rows = self._conn.execute(
            """
            SELECT binding.*
            FROM fact_cell_canonical_binding_revisions binding
            WHERE binding.fact_cell_id=?
              AND datetime(binding.effective_at) <= datetime(?)
              AND datetime(binding.knowledge_at) <= datetime(?)
              AND datetime(binding.recorded_at) <= datetime(?)
              AND NOT EXISTS (
                  SELECT 1
                  FROM fact_cell_canonical_binding_revisions newer
                  WHERE newer.source_observation_id=binding.source_observation_id
                    AND newer.revision > binding.revision
                    AND datetime(newer.effective_at) <= datetime(?)
                    AND datetime(newer.knowledge_at) <= datetime(?)
                    AND datetime(newer.recorded_at) <= datetime(?)
              )
            ORDER BY binding.source_observation_id
            """,
            (fact_cell_id, cutoff, cutoff, cutoff, cutoff, cutoff, cutoff),
        ).fetchall()
        return tuple(self._binding_from_row(row) for row in rows)

    def _as_known_row(
        self, table: str, coordinate: str, value: str, cutoff_at: datetime
    ) -> sqlite3.Row | None:
        cutoff = _db_time(cutoff_at)
        return self._conn.execute(
            f"SELECT * FROM {table} WHERE {coordinate}=? "  # nosec B608 -- trusted internal SQL shape; values remain bound
            "AND datetime(effective_at) <= datetime(?) "
            "AND datetime(knowledge_at) <= datetime(?) "
            "AND datetime(recorded_at) <= datetime(?) "
            "ORDER BY revision DESC LIMIT 1",
            (value, cutoff, cutoff, cutoff),
        ).fetchone()

    def seal_snapshot(self, snapshot: OntologySnapshot) -> None:
        with self._savepoint("seal_ontology_snapshot"):
            self._insert_or_verify(
                "ontology_snapshot_headers",
                (
                    "ontology_snapshot_id",
                    "idempotency_key",
                    "cutoff_at",
                    "recorded_at",
                ),
                (
                    snapshot.ontology_snapshot_id,
                    snapshot.idempotency_key,
                    snapshot.cutoff_at,
                    snapshot.recorded_at,
                ),
                idempotency_key=snapshot.idempotency_key,
            )
            existing = self._conn.execute(
                "SELECT 1 FROM ontology_snapshot_seals WHERE ontology_snapshot_id=?",
                (snapshot.ontology_snapshot_id,),
            ).fetchone()
            if existing is not None:
                self.verify_snapshot(snapshot.ontology_snapshot_id)
                return
            members = list(self._expected_snapshot_members(snapshot))
            for ordinal, (kind, member_id, digest) in enumerate(members):
                self._conn.execute(
                    "INSERT INTO ontology_snapshot_members "
                    "(ontology_snapshot_id,member_ordinal,member_kind,"
                    "member_id,member_sha256) VALUES (?,?,?,?,?)",
                    (
                        snapshot.ontology_snapshot_id,
                        ordinal,
                        kind,
                        member_id,
                        digest,
                    ),
                )
            payload = canonical_json(
                [
                    {"id": member_id, "kind": kind, "sha256": digest}
                    for kind, member_id, digest in members
                ]
            )
            self._conn.execute(
                "INSERT INTO ontology_snapshot_seals "
                "(ontology_snapshot_id,member_count,canonical_member_set_json,"
                "member_set_sha256,sealed_at) VALUES (?,?,?,?,?)",
                (
                    snapshot.ontology_snapshot_id,
                    len(members),
                    payload,
                    _digest(payload),
                    _db_time(snapshot.recorded_at),
                ),
            )
        self.verify_snapshot(snapshot.ontology_snapshot_id)

    def _expected_snapshot_members(
        self, snapshot: OntologySnapshot
    ) -> Iterable[tuple[str, str, str]]:
        rows = self._conn.execute(
            "SELECT member_kind,member_id,member_sha256 "
            "FROM v_ontology_snapshot_expected_members "
            "WHERE ontology_snapshot_id=? ORDER BY member_kind,member_id",
            (snapshot.ontology_snapshot_id,),
        )
        return (
            (str(row["member_kind"]), str(row["member_id"]), str(row["member_sha256"]))
            for row in rows
        )

    def verify_snapshot(self, snapshot_id: str) -> None:
        seal = self._conn.execute(
            "SELECT member_count,canonical_member_set_json,member_set_sha256 "
            "FROM ontology_snapshot_seals WHERE ontology_snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if seal is None:
            raise ValueError("ontology snapshot is missing its final seal")
        members = self._conn.execute(
            "SELECT member_kind,member_id,member_sha256 "
            "FROM ontology_snapshot_members WHERE ontology_snapshot_id=? "
            "ORDER BY member_ordinal",
            (snapshot_id,),
        ).fetchall()
        payload = canonical_json(
            [
                {
                    "id": str(row["member_id"]),
                    "kind": str(row["member_kind"]),
                    "sha256": str(row["member_sha256"]),
                }
                for row in members
            ]
        )
        expected = self._conn.execute(
            "SELECT member_kind,member_id,member_sha256 "
            "FROM v_ontology_snapshot_expected_members "
            "WHERE ontology_snapshot_id=? ORDER BY member_kind,member_id",
            (snapshot_id,),
        ).fetchall()
        actual_set = {
            (str(row["member_kind"]), str(row["member_id"]), str(row["member_sha256"]))
            for row in members
        }
        expected_set = {
            (str(row["member_kind"]), str(row["member_id"]), str(row["member_sha256"]))
            for row in expected
        }
        if (
            int(seal["member_count"]) != len(members)
            or str(seal["canonical_member_set_json"]) != payload
            or str(seal["member_set_sha256"]) != _digest(payload)
            or actual_set != expected_set
        ):
            raise ValueError("ontology snapshot has missing, extra, or tampered members")

    @staticmethod
    def _definition_from_row(row: sqlite3.Row) -> CanonicalMetricDefinitionRevision:
        return CanonicalMetricDefinitionRevision(
            metric_definition_revision_id=str(row["metric_definition_revision_id"]),
            idempotency_key=str(row["idempotency_key"]),
            metric_id=str(row["metric_id"]),
            revision=int(row["revision"]),
            supersedes_metric_definition_revision_id=row[
                "supersedes_metric_definition_revision_id"
            ],
            lifecycle=cast(Lifecycle, str(row["lifecycle"])),
            definition_text=str(row["definition_text"]),
            aliases=tuple(json.loads(str(row["aliases_json"]))),
            value_kind=cast(ValueKind, str(row["value_kind"])),
            period_kind=cast(PeriodKind, str(row["period_kind"])),
            unit_family=str(row["unit_family"]),
            accounting_basis=str(row["accounting_basis"]),
            scope_constraints=json.loads(str(row["scope_constraints_json"])),
            effective_at=_parse_db_time(row["effective_at"]),
            knowledge_at=_parse_db_time(row["knowledge_at"]),
            recorded_at=_parse_db_time(row["recorded_at"]),
        )

    @staticmethod
    def _mapping_from_row(row: sqlite3.Row) -> MappingRevision:
        return MappingRevision(
            mapping_revision_id=str(row["mapping_revision_id"]),
            idempotency_key=str(row["idempotency_key"]),
            source_component_id=str(row["source_component_id"]),
            revision=int(row["revision"]),
            supersedes_mapping_revision_id=row["supersedes_mapping_revision_id"],
            metric_id=row["metric_id"],
            disposition=cast(Disposition, str(row["disposition"])),
            policy_name=str(row["policy_name"]),
            policy_version=str(row["policy_version"]),
            policy_config_sha256=str(row["policy_config_sha256"]),
            method_name=str(row["method_name"]),
            method_version=str(row["method_version"]),
            constraints=json.loads(str(row["constraints_json"])),
            evidence=json.loads(str(row["evidence_json"])),
            reviewer_identity=row["reviewer_identity"],
            audited_policy_path=row["audited_policy_path"],
            effective_at=_parse_db_time(row["effective_at"]),
            knowledge_at=_parse_db_time(row["knowledge_at"]),
            recorded_at=_parse_db_time(row["recorded_at"]),
        )

    @staticmethod
    def _dimension_mapping_from_row(
        row: sqlite3.Row,
    ) -> SourceDimensionMappingRevision:
        return SourceDimensionMappingRevision(
            dimension_mapping_revision_id=str(row["dimension_mapping_revision_id"]),
            idempotency_key=str(row["idempotency_key"]),
            source_component_id=str(row["source_component_id"]),
            revision=int(row["revision"]),
            supersedes_dimension_mapping_revision_id=row[
                "supersedes_dimension_mapping_revision_id"
            ],
            disposition=cast(DimensionDisposition, str(row["disposition"])),
            canonical_axis_id=row["canonical_axis_id"],
            canonical_member_id=row["canonical_member_id"],
            policy_name=str(row["policy_name"]),
            policy_version=str(row["policy_version"]),
            policy_config_sha256=str(row["policy_config_sha256"]),
            evidence=json.loads(str(row["evidence_json"])),
            reviewer_identity=row["reviewer_identity"],
            audited_policy_path=row["audited_policy_path"],
            effective_at=_parse_db_time(row["effective_at"]),
            knowledge_at=_parse_db_time(row["knowledge_at"]),
            recorded_at=_parse_db_time(row["recorded_at"]),
        )

    @staticmethod
    def _binding_from_row(row: sqlite3.Row) -> BindingRevision:
        return BindingRevision(
            binding_revision_id=str(row["binding_revision_id"]),
            idempotency_key=str(row["idempotency_key"]),
            fact_cell_id=str(row["fact_cell_id"]),
            source_observation_id=str(row["source_observation_id"]),
            revision=int(row["revision"]),
            supersedes_binding_revision_id=row["supersedes_binding_revision_id"],
            canonical_metric_cell_id=(
                None
                if row["canonical_metric_cell_id"] is None
                else str(row["canonical_metric_cell_id"])
            ),
            mapping_revision_id=(
                None if row["mapping_revision_id"] is None else str(row["mapping_revision_id"])
            ),
            source_component_id=(
                None if row["source_component_id"] is None else str(row["source_component_id"])
            ),
            binding_status=cast(
                Literal["bound", "quarantined", "retired"],
                str(row["binding_status"]),
            ),
            reason_code=(None if row["reason_code"] is None else str(row["reason_code"])),
            reason_details=(
                None
                if row["reason_details_json"] is None
                else json.loads(str(row["reason_details_json"]))
            ),
            effective_at=_parse_db_time(row["effective_at"]),
            knowledge_at=_parse_db_time(row["knowledge_at"]),
            recorded_at=_parse_db_time(row["recorded_at"]),
        )
