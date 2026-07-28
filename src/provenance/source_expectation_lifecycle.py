"""Append-only lifecycle for documents an authority once said should exist."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ExpectationStatus = Literal["expected", "withdrawn_by_authority", "superseded_by_authority"]


class ExpectedDocumentLifecycle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lifecycle_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    inventory_key: str = Field(min_length=1, max_length=256)
    expected_document_key: str = Field(min_length=1, max_length=256)
    source_inventory_snapshot_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(gt=0)
    status: ExpectationStatus
    expected_document_id: str | None = Field(default=None, min_length=1, max_length=128)
    authority_observation_id: str = Field(min_length=1, max_length=128)
    reason_code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    reason_details: tuple[tuple[str, str], ...] = Field(min_length=1)
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    supersedes_lifecycle_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("reason_details")
    @classmethod
    def _details(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        if any(not key or not detail for key, detail in value):
            raise ValueError("lifecycle details require non-empty keys and values")
        keys = [key for key, _ in value]
        if len(keys) != len(set(keys)):
            raise ValueError("lifecycle detail keys must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def _lineage(self) -> Self:
        if (self.status == "expected") != (self.expected_document_id is not None):
            raise ValueError("only an expected lifecycle revision has a document anchor")
        if (self.revision == 1) != (self.supersedes_lifecycle_id is None):
            raise ValueError("lifecycle revision must supersede its exact prior revision")
        if self.knowledge_at < self.effective_at or self.recorded_at < self.knowledge_at:
            raise ValueError("lifecycle clocks are out of order")
        return self

    @property
    def reason_details_json(self) -> str:
        return json.dumps(dict(self.reason_details), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class PersistResult:
    lifecycle_id: str
    created: bool


def persist_expected_document_lifecycle(
    conn: sqlite3.Connection, record: ExpectedDocumentLifecycle
) -> PersistResult:
    if record.revision > 1:
        prior = conn.execute(
            "SELECT inventory_key, expected_document_key, revision "
            "FROM expected_document_lifecycle_revisions WHERE lifecycle_id = ?",
            (record.supersedes_lifecycle_id,),
        ).fetchone()
        if prior is None or (str(prior[0]), str(prior[1]), int(prior[2])) != (
            record.inventory_key,
            record.expected_document_key,
            record.revision - 1,
        ):
            raise ValueError("expected document lifecycle prior revision is invalid")
    columns = (
        "lifecycle_id",
        "idempotency_key",
        "inventory_key",
        "expected_document_key",
        "source_inventory_snapshot_id",
        "revision",
        "status",
        "expected_document_id",
        "authority_observation_id",
        "reason_code",
        "reason_details_json",
        "effective_at",
        "knowledge_at",
        "recorded_at",
        "supersedes_lifecycle_id",
    )
    values = (
        record.lifecycle_id,
        record.idempotency_key,
        record.inventory_key,
        record.expected_document_key,
        record.source_inventory_snapshot_id,
        record.revision,
        record.status,
        record.expected_document_id,
        record.authority_observation_id,
        record.reason_code,
        record.reason_details_json,
        record.effective_at,
        record.knowledge_at,
        record.recorded_at,
        record.supersedes_lifecycle_id,
    )
    existing = conn.execute(
        f"SELECT {', '.join(columns)} FROM expected_document_lifecycle_revisions "
        "WHERE idempotency_key = ?",
        (record.idempotency_key,),
    ).fetchone()
    if existing is not None:
        if not _same(tuple(existing), values):
            raise ValueError("immutable expected document lifecycle conflicts")
        return PersistResult(record.lifecycle_id, False)
    conn.execute(
        f"INSERT INTO expected_document_lifecycle_revisions ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        values,
    )
    return PersistResult(record.lifecycle_id, True)


def _same(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    def normalized(value: object) -> object:
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value

    return tuple(normalized(value) for value in left) == tuple(normalized(value) for value in right)
