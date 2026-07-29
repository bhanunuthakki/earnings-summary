"""Immutable membership and seals for multi-response source inventories.

A source inventory can be assembled from several authoritative responses (for
example, the SEC submissions root plus every historical page).  The 0219
snapshot points at the primary observation; these records preserve the full
response set and make completeness a verifiable, content-addressed claim.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

ComponentKind: TypeAlias = Literal[
    "primary", "historical_page", "crawl_page", "event_feed", "other"
]
ComponentOutcome: TypeAlias = Literal["succeeded", "failed"]
CompletionStatus: TypeAlias = Literal["complete", "incomplete"]


class _InventoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InventoryComponent(_InventoryRecord):
    component_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=128)
    component_key: str = Field(min_length=1, max_length=256)
    component_kind: ComponentKind
    source_url: str = Field(min_length=1)
    source_observation_id: str | None = Field(default=None, min_length=1, max_length=128)
    outcome: ComponentOutcome
    required: bool
    failure_reason: str | None = Field(default=None, min_length=1, max_length=128)
    ordinal: int = Field(ge=0)
    recorded_at: datetime

    @model_validator(mode="after")
    def _validate_lineage(self) -> InventoryComponent:
        if self.outcome == "succeeded":
            if self.source_observation_id is None or self.failure_reason is not None:
                raise ValueError("successful inventory component requires an observation only")
        elif self.source_observation_id is not None or self.failure_reason is None:
            raise ValueError(
                "failed inventory component requires a failure reason and no observation"
            )
        return self


class InventorySeal(_InventoryRecord):
    snapshot_id: str = Field(min_length=1, max_length=128)
    expected_component_count: int = Field(gt=0)
    component_digest_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    completion_status: CompletionStatus
    sealed_at: datetime


class InventoryManifestLink(_InventoryRecord):
    manifest_id: str = Field(min_length=1, max_length=128)
    snapshot_id: str = Field(min_length=1, max_length=128)
    linked_at: datetime


InventoryRecord: TypeAlias = InventoryComponent | InventorySeal | InventoryManifestLink


@dataclass(frozen=True, slots=True)
class PersistResult:
    record_id: str
    created: bool


def component_digest(components: tuple[InventoryComponent, ...]) -> str:
    """Return a stable digest over the exact ordered inventory membership."""

    ordered = sorted(components, key=lambda item: (item.ordinal, item.component_key))
    payload = [item.model_dump(mode="json", exclude={"recorded_at"}) for item in ordered]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


class SourceInventorySealStore:
    """Typed append boundary for inventory components, seals, and search links."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def persist(self, record: InventoryRecord) -> PersistResult:
        if isinstance(record, InventoryComponent):
            return self._persist_component(record)
        if isinstance(record, InventorySeal):
            return self._persist_seal(record)
        return self._persist_manifest_link(record)

    def _persist_component(self, record: InventoryComponent) -> PersistResult:
        columns = (
            "component_id",
            "idempotency_key",
            "snapshot_id",
            "component_key",
            "component_kind",
            "source_url",
            "source_observation_id",
            "outcome",
            "required",
            "failure_reason",
            "ordinal",
            "recorded_at",
        )
        values = (
            record.component_id,
            record.idempotency_key,
            record.snapshot_id,
            record.component_key,
            record.component_kind,
            record.source_url,
            record.source_observation_id,
            record.outcome,
            int(record.required),
            record.failure_reason,
            record.ordinal,
            record.recorded_at,
        )
        return self._insert(
            table="source_inventory_components",
            columns=columns,
            values=values,
            identity_column="idempotency_key",
            identity_value=record.idempotency_key,
            record_id=record.component_id,
        )

    def _persist_seal(self, record: InventorySeal) -> PersistResult:
        rows = self._conn.execute(
            "SELECT component_id,idempotency_key,snapshot_id,component_key,"
            "component_kind,source_url,source_observation_id,outcome,required,"
            "failure_reason,ordinal,recorded_at "
            "FROM source_inventory_components WHERE snapshot_id = ? "
            "ORDER BY ordinal, component_key",
            (record.snapshot_id,),
        ).fetchall()
        components = tuple(
            InventoryComponent(
                component_id=str(row[0]),
                idempotency_key=str(row[1]),
                snapshot_id=str(row[2]),
                component_key=str(row[3]),
                component_kind=cast(ComponentKind, str(row[4])),
                source_url=str(row[5]),
                source_observation_id=None if row[6] is None else str(row[6]),
                outcome=cast(ComponentOutcome, str(row[7])),
                required=bool(row[8]),
                failure_reason=None if row[9] is None else str(row[9]),
                ordinal=int(row[10]),
                recorded_at=row[11],
            )
            for row in rows
        )
        if len(components) != record.expected_component_count:
            raise ValueError("inventory seal component count does not match")
        if component_digest(components) != record.component_digest_sha256:
            raise ValueError("inventory seal component digest does not match")
        complete = all(
            not component.required or component.outcome == "succeeded" for component in components
        )
        expected_status = "complete" if complete else "incomplete"
        if record.completion_status != expected_status:
            raise ValueError("inventory seal completion status does not match components")
        columns = (
            "snapshot_id",
            "expected_component_count",
            "component_digest_sha256",
            "completion_status",
            "sealed_at",
        )
        values = (
            record.snapshot_id,
            record.expected_component_count,
            record.component_digest_sha256,
            record.completion_status,
            record.sealed_at,
        )
        return self._insert(
            table="source_inventory_snapshot_seals",
            columns=columns,
            values=values,
            identity_column="snapshot_id",
            identity_value=record.snapshot_id,
            record_id=record.snapshot_id,
        )

    def _persist_manifest_link(self, record: InventoryManifestLink) -> PersistResult:
        columns = ("manifest_id", "snapshot_id", "linked_at")
        values = (record.manifest_id, record.snapshot_id, record.linked_at)
        return self._insert(
            table="search_manifest_source_inventories",
            columns=columns,
            values=values,
            identity_column="manifest_id || ':' || snapshot_id",
            identity_value=f"{record.manifest_id}:{record.snapshot_id}",
            record_id=f"{record.manifest_id}:{record.snapshot_id}",
        )

    def _insert(
        self,
        *,
        table: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        identity_column: str,
        identity_value: str,
        record_id: str,
    ) -> PersistResult:
        existing = self._conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE {identity_column} = ?",  # nosec B608 -- trusted internal SQL shape; values remain bound
            (identity_value,),
        ).fetchone()
        if existing is not None:
            if not _same(tuple(existing), values):
                raise ValueError(f"immutable {table} identity conflicts with existing data")
            return PersistResult(record_id=record_id, created=False)
        cursor = self._conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) "  # nosec B608 -- trusted internal SQL shape; values remain bound
            f"VALUES ({', '.join('?' for _ in columns)}) ON CONFLICT DO NOTHING",
            values,
        )
        if cursor.rowcount == 1:
            return PersistResult(record_id=record_id, created=True)
        existing = self._conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE {identity_column} = ?",  # nosec B608 -- trusted internal SQL shape; values remain bound
            (identity_value,),
        ).fetchone()
        if existing is None or not _same(tuple(existing), values):
            raise ValueError(f"immutable {table} identity conflicts with existing data")
        return PersistResult(record_id=record_id, created=False)


def _same(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    def normalize(value: object) -> object:
        if isinstance(value, datetime):
            return value.isoformat(sep=" ")
        return value

    return tuple(normalize(value) for value in left) == tuple(normalize(value) for value in right)
