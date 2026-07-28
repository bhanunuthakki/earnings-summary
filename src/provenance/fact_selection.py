"""Typed, append-only selection decisions for legacy fact rows.

The ledger is intentionally separate from legacy fact writers and readers.
It preserves suspect rows for audit and lets a later read projection decide
whether they should participate in an investor-facing calculation.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_LENGTH = 64
_TARGET_TABLES = frozenset({"kpi_facts"})
SelectionState: TypeAlias = Literal["included", "excluded"]
DecisionKind: TypeAlias = Literal["deterministic", "manual", "imported"]


class _SelectionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FactSelectionDecision(_SelectionRecord):
    """One immutable included/excluded decision for a legacy fact row."""

    decision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    target_table: str = Field(min_length=1, max_length=128)
    target_row_id: int = Field(gt=0)
    revision: int = Field(gt=0)
    selection_state: SelectionState
    reason_code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    reason_details: tuple[tuple[str, str], ...] = Field(min_length=1)
    decision_kind: DecisionKind
    policy_name: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)
    policy_config_sha256: str = Field(min_length=_SHA256_LENGTH, max_length=_SHA256_LENGTH)
    evidence_node_id: str | None = Field(default=None, min_length=1, max_length=128)
    validation_issue_id: int | None = Field(default=None, gt=0)
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    supersedes_decision_id: str | None = Field(default=None, min_length=1, max_length=128)
    material_dissent: bool

    @field_validator("target_table")
    @classmethod
    def _target_is_allowlisted(cls, value: str) -> str:
        if value not in _TARGET_TABLES:
            raise ValueError(
                f"target table must be allowlisted: {', '.join(sorted(_TARGET_TABLES))}"
            )
        return value

    @field_validator("policy_config_sha256")
    @classmethod
    def _valid_config_hash(cls, value: str) -> str:
        normalized = value.lower()
        if len(normalized) != _SHA256_LENGTH or any(
            char not in "0123456789abcdef" for char in normalized
        ):
            raise ValueError("policy_config_sha256 must be a SHA-256 hex digest")
        return normalized

    @field_validator("reason_details")
    @classmethod
    def _canonical_reason_details(
        cls, value: tuple[tuple[str, str], ...]
    ) -> tuple[tuple[str, str], ...]:
        if not value:
            raise ValueError("reason_details must not be empty")
        keys = [detail[0] for detail in value]
        if any(not key or not detail for key, detail in value):
            raise ValueError("reason details require non-empty keys and values")
        if len(set(keys)) != len(keys):
            raise ValueError("reason detail keys must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def _validate_revision_and_clocks(self) -> Self:
        if self.revision == 1 and self.supersedes_decision_id is not None:
            raise ValueError("first fact selection revision cannot supersede another decision")
        if self.knowledge_at < self.effective_at:
            raise ValueError("knowledge_at must not precede effective_at")
        if self.recorded_at < self.knowledge_at:
            raise ValueError("recorded_at must not precede knowledge_at")
        return self

    @property
    def reason_details_json(self) -> str:
        return json.dumps(dict(self.reason_details), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class PersistResult:
    decision_id: str
    created: bool


def _matches_stored_values(existing: tuple[object, ...], expected: tuple[object, ...]) -> bool:
    if len(existing) != len(expected):
        return False
    for stored, supplied in zip(existing, expected, strict=True):
        if isinstance(supplied, datetime):
            try:
                parsed = datetime.fromisoformat(str(stored)).replace(tzinfo=None)
            except ValueError:
                return False
            if parsed != supplied.replace(tzinfo=None):
                return False
        elif isinstance(supplied, bool):
            if bool(stored) is not supplied:
                return False
        elif stored != supplied:
            return False
    return True


class FactSelectionLedger:
    """Single typed persistence boundary for immutable legacy-fact decisions."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def persist(self, decision: FactSelectionDecision) -> PersistResult:
        """Append a decision or accept only an exact idempotent replay."""
        self._validate_references(decision)
        columns, values = self._values(decision)
        existing = self._conn.execute(
            "SELECT decision_id FROM fact_selection_decisions WHERE idempotency_key = ?",
            (decision.idempotency_key,),
        ).fetchone()
        if existing is not None:
            return self._verify_replay(decision, columns, values)
        collision = self._conn.execute(
            "SELECT 1 FROM fact_selection_decisions WHERE decision_id = ?", (decision.decision_id,)
        ).fetchone()
        if collision is not None:
            raise ValueError(
                f"fact selection decision {decision.decision_id!r} conflicts with existing data"
            )
        self._validate_supersedes(decision)
        placeholders = ", ".join("?" for _ in columns)
        self._conn.execute(
            f"INSERT INTO fact_selection_decisions ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        return PersistResult(decision_id=decision.decision_id, created=True)

    def _validate_references(self, decision: FactSelectionDecision) -> None:
        if self._table_exists(decision.target_table):
            target = self._conn.execute(
                f"SELECT 1 FROM {decision.target_table} WHERE id = ?", (decision.target_row_id,)
            ).fetchone()
            if target is None:
                raise ValueError(
                    f"legacy {decision.target_table}.id {decision.target_row_id} does not exist"
                )
        if decision.evidence_node_id is not None and self._table_exists("evidence_nodes"):
            evidence = self._conn.execute(
                "SELECT 1 FROM evidence_nodes WHERE node_id = ?", (decision.evidence_node_id,)
            ).fetchone()
            if evidence is None:
                raise ValueError(f"evidence node {decision.evidence_node_id} does not exist")
        if decision.validation_issue_id is not None and self._table_exists("validation_issues"):
            issue = self._conn.execute(
                "SELECT 1 FROM validation_issues WHERE id = ?", (decision.validation_issue_id,)
            ).fetchone()
            if issue is None:
                raise ValueError(f"validation issue {decision.validation_issue_id} does not exist")

    def _validate_supersedes(self, decision: FactSelectionDecision) -> None:
        if decision.revision == 1:
            return
        assert decision.supersedes_decision_id is not None
        parent = self._conn.execute(
            "SELECT target_table, target_row_id, revision FROM fact_selection_decisions "
            "WHERE decision_id = ?",
            (decision.supersedes_decision_id,),
        ).fetchone()
        if parent is None:
            raise ValueError("superseded fact selection decision does not exist")
        if str(parent[0]) != decision.target_table or int(parent[1]) != decision.target_row_id:
            raise ValueError("supersedes decision must target the same target table and row")
        if int(parent[2]) != decision.revision - 1:
            raise ValueError("supersedes decision must be the immediately prior revision")

    def _verify_replay(
        self,
        decision: FactSelectionDecision,
        columns: tuple[str, ...],
        values: tuple[object, ...],
    ) -> PersistResult:
        existing = self._conn.execute(
            f"SELECT {', '.join(columns)} FROM fact_selection_decisions WHERE idempotency_key = ?",
            (decision.idempotency_key,),
        ).fetchone()
        if existing is None or not _matches_stored_values(tuple(existing), values):
            raise ValueError(
                f"immutable fact selection identity {decision.idempotency_key!r} conflicts with existing data"
            )
        return PersistResult(decision_id=decision.decision_id, created=False)

    def _table_exists(self, table_name: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def _values(
        decision: FactSelectionDecision,
    ) -> tuple[tuple[str, ...], tuple[object, ...]]:
        return (
            (
                "decision_id",
                "idempotency_key",
                "target_table",
                "target_row_id",
                "revision",
                "selection_state",
                "reason_code",
                "reason_details_json",
                "decision_kind",
                "policy_name",
                "policy_version",
                "policy_config_sha256",
                "evidence_node_id",
                "validation_issue_id",
                "effective_at",
                "knowledge_at",
                "recorded_at",
                "supersedes_decision_id",
                "material_dissent",
            ),
            (
                decision.decision_id,
                decision.idempotency_key,
                decision.target_table,
                decision.target_row_id,
                decision.revision,
                decision.selection_state,
                decision.reason_code,
                decision.reason_details_json,
                decision.decision_kind,
                decision.policy_name,
                decision.policy_version,
                decision.policy_config_sha256,
                decision.evidence_node_id,
                decision.validation_issue_id,
                decision.effective_at,
                decision.knowledge_at,
                decision.recorded_at,
                decision.supersedes_decision_id,
                decision.material_dissent,
            ),
        )
