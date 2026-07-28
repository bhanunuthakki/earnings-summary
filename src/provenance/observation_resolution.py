"""Typed, append-only persistence for reported observations and their resolution.

No reader is switched by this module.  ``ObservationResolutionLedger`` only
records an evidence-anchored observation or an explicit, revisioned decision
over the complete candidate set available at a knowledge cutoff.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_FiscalPeriodType = Literal["annual", "quarter", "year_to_date", "instant", "other"]
_ObservationStatus = Literal["reported", "derived"]
_ResolverKind = Literal["deterministic_policy", "manual", "imported"]


class _ResolutionRecord(BaseModel):
    """Closed, immutable records accepted by the persistence boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ObservationDimension(_ResolutionRecord):
    """One typed axis of a financial, KPI, or segment observation."""

    key: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=256)


def _canonical_decimal(value: object) -> str:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("numeric_value must be a finite decimal") from exc
    if not decimal.is_finite():
        raise ValueError("numeric_value must be a finite decimal")
    normalized = format(decimal, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return "0" if normalized in {"", "-0"} else normalized


class ReportedObservation(_ResolutionRecord):
    """An immutable reported or derived value anchored to one evidence node."""

    observation_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    issuer_id: str = Field(min_length=1, max_length=128)
    ticker: str | None = Field(default=None, min_length=1, max_length=16)
    concept_key: str = Field(min_length=1, max_length=256)
    period_start: datetime
    period_end: datetime
    fiscal_period_type: _FiscalPeriodType
    dimensions: tuple[ObservationDimension, ...]
    numeric_value: str | None = None
    text_value: str | None = Field(default=None, min_length=1)
    currency: str | None = Field(default=None, min_length=1, max_length=16)
    unit: str | None = Field(default=None, min_length=1, max_length=64)
    scale: int | None = Field(default=None, ge=0, le=18)
    observation_status: _ObservationStatus
    evidence_node_id: str = Field(min_length=1, max_length=128)
    available_at: datetime
    recorded_at: datetime
    method: str = Field(min_length=1, max_length=128)
    method_version: str = Field(min_length=1, max_length=128)
    confidence: float = Field(ge=0, le=1)
    legacy_table: str | None = Field(default=None, min_length=1, max_length=128)
    legacy_row_id: int | None = Field(default=None, ge=1)

    _numeric_value = field_validator("numeric_value")(_canonical_decimal)

    @field_validator("dimensions")
    @classmethod
    def _dimensions_have_unique_keys(
        cls, value: tuple[ObservationDimension, ...]
    ) -> tuple[ObservationDimension, ...]:
        keys = [dimension.key for dimension in value]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate dimension keys are not allowed")
        return tuple(sorted(value, key=lambda dimension: (dimension.key, dimension.value)))

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, value: str | None) -> str | None:
        return None if value is None else value.upper()

    @model_validator(mode="after")
    def _validate_value_and_clocks(self) -> Self:
        has_numeric = self.numeric_value is not None
        has_text = self.text_value is not None
        if has_numeric == has_text:
            raise ValueError("exactly one of numeric_value or text_value is required")
        if has_numeric and self.unit is None:
            raise ValueError("numeric observations require a unit")
        if not has_numeric and any(
            value is not None for value in (self.currency, self.unit, self.scale)
        ):
            raise ValueError("narrative observations cannot carry currency, unit, or scale")
        if self.period_end < self.period_start:
            raise ValueError("period_end must not precede period_start")
        if self.recorded_at < self.available_at:
            raise ValueError("recorded_at must not precede available_at")
        if (self.legacy_table is None) != (self.legacy_row_id is None):
            raise ValueError("legacy_table and legacy_row_id must be supplied together")
        return self

    @property
    def dimensions_json(self) -> str:
        """Stable JSON representation for the closed dimension grammar."""
        return json.dumps(
            [dimension.model_dump(mode="json") for dimension in self.dimensions],
            sort_keys=True,
            separators=(",", ":"),
        )


class ResolutionRevision(_ResolutionRecord):
    """One immutable resolver decision, including all candidates considered."""

    resolution_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    logical_key: str = Field(min_length=1, max_length=256)
    revision: int = Field(gt=0)
    candidate_observation_ids: tuple[str, ...] = Field(min_length=1)
    selected_observation_id: str = Field(min_length=1, max_length=128)
    resolver_kind: _ResolverKind
    policy_version: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1)
    knowledge_cutoff: datetime
    effective_at: datetime
    material_dissent: bool
    supersedes_resolution_id: str | None = Field(default=None, min_length=1, max_length=128)
    recorded_at: datetime

    @field_validator("candidate_observation_ids")
    @classmethod
    def _candidate_ids_are_unique_and_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not candidate for candidate in value):
            raise ValueError("candidate observation IDs cannot be empty")
        if len(set(value)) != len(value):
            raise ValueError("duplicate candidate observation IDs are not allowed")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def _validate_resolution(self) -> Self:
        if self.selected_observation_id not in self.candidate_observation_ids:
            raise ValueError("selected observation must be included in the complete candidate set")
        if self.revision == 1 and self.supersedes_resolution_id is not None:
            raise ValueError("first resolution revision cannot supersede another resolution")
        if self.recorded_at < self.knowledge_cutoff:
            raise ValueError("recorded_at must not precede knowledge_cutoff")
        return self


@dataclass(frozen=True, slots=True)
class PersistResult:
    """The identity and creation status from one idempotent append request."""

    record_id: str
    created: bool


def _matches_stored_values(existing: tuple[object, ...], expected: tuple[object, ...]) -> bool:
    if len(existing) != len(expected):
        return False
    for stored, supplied in zip(existing, expected, strict=True):
        if isinstance(supplied, datetime):
            try:
                stored_time = datetime.fromisoformat(str(stored)).replace(tzinfo=None)
            except ValueError:
                return False
            if stored_time != supplied.replace(tzinfo=None):
                return False
        elif isinstance(supplied, bool):
            if bool(stored) is not supplied:
                return False
        elif stored != supplied:
            return False
    return True


class ObservationResolutionLedger:
    """Single typed persistence API for the observation-resolution foundation."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def persist_observation(self, observation: ReportedObservation) -> PersistResult:
        """Append one observation or accept an exact idempotent replay."""
        self._validate_legacy_bridge(observation)
        columns, values = self._observation_values(observation)
        return self._persist_one(
            table="reported_observations",
            columns=columns,
            values=values,
            identity_column="idempotency_key",
            identity_value=observation.idempotency_key,
            record_id=observation.observation_id,
        )

    def persist_resolution(self, resolution: ResolutionRevision) -> PersistResult:
        """Append a complete resolution candidate set and its selected member.

        Candidate rows precede the parent inside a savepoint.  Their deferred
        FK lets the database trigger prove selected membership at insertion;
        the savepoint keeps an invalid request from leaving partial candidates.
        """
        existing = self._conn.execute(
            "SELECT resolution_id FROM observation_resolution_revisions WHERE idempotency_key = ?",
            (resolution.idempotency_key,),
        ).fetchone()
        if existing is not None:
            return self._verify_resolution_replay(resolution)
        collision = self._conn.execute(
            "SELECT 1 FROM observation_resolution_revisions WHERE resolution_id = ?",
            (resolution.resolution_id,),
        ).fetchone()
        if collision is not None:
            raise ValueError(
                f"resolution identity {resolution.resolution_id!r} conflicts with existing data"
            )
        self._require_observations(resolution.candidate_observation_ids)
        self._conn.execute("SAVEPOINT persist_observation_resolution")
        try:
            for observation_id in resolution.candidate_observation_ids:
                self._conn.execute(
                    "INSERT INTO observation_resolution_candidates (resolution_id, observation_id) "
                    "VALUES (?, ?)",
                    (resolution.resolution_id, observation_id),
                )
            columns, values = self._resolution_values(resolution)
            placeholders = ", ".join("?" for _ in columns)
            self._conn.execute(
                "INSERT INTO observation_resolution_revisions "
                f"({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
        except (sqlite3.Error, ValueError):
            self._conn.execute("ROLLBACK TO SAVEPOINT persist_observation_resolution")
            self._conn.execute("RELEASE SAVEPOINT persist_observation_resolution")
            raise
        self._conn.execute("RELEASE SAVEPOINT persist_observation_resolution")
        return PersistResult(record_id=resolution.resolution_id, created=True)

    def _persist_one(
        self,
        *,
        table: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        identity_column: str,
        identity_value: str,
        record_id: str,
    ) -> PersistResult:
        placeholders = ", ".join("?" for _ in columns)
        cursor = self._conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
            values,
        )
        if cursor.rowcount == 1:
            return PersistResult(record_id=record_id, created=True)
        existing = self._conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE {identity_column} = ?",
            (identity_value,),
        ).fetchone()
        if existing is None or not _matches_stored_values(tuple(existing), values):
            raise ValueError(
                f"immutable {table} identity {identity_value!r} conflicts with existing data"
            )
        return PersistResult(record_id=record_id, created=False)

    def _verify_resolution_replay(self, resolution: ResolutionRevision) -> PersistResult:
        columns, values = self._resolution_values(resolution)
        existing = self._conn.execute(
            f"SELECT {', '.join(columns)} FROM observation_resolution_revisions "
            "WHERE idempotency_key = ?",
            (resolution.idempotency_key,),
        ).fetchone()
        candidates = self._conn.execute(
            "SELECT observation_id FROM observation_resolution_candidates "
            "WHERE resolution_id = ? ORDER BY observation_id",
            (resolution.resolution_id,),
        ).fetchall()
        stored_candidates = tuple(str(row[0]) for row in candidates)
        if (
            existing is None
            or not _matches_stored_values(tuple(existing), values)
            or stored_candidates != resolution.candidate_observation_ids
        ):
            raise ValueError(
                f"immutable resolution identity {resolution.idempotency_key!r} conflicts with existing data"
            )
        return PersistResult(record_id=resolution.resolution_id, created=False)

    def _require_observations(self, observation_ids: tuple[str, ...]) -> None:
        placeholders = ", ".join("?" for _ in observation_ids)
        rows = self._conn.execute(
            f"SELECT observation_id FROM reported_observations WHERE observation_id IN ({placeholders})",
            observation_ids,
        ).fetchall()
        found = {str(row[0]) for row in rows}
        missing = sorted(set(observation_ids) - found)
        if missing:
            raise ValueError(f"resolution candidates do not exist: {', '.join(missing)}")

    def _validate_legacy_bridge(self, observation: ReportedObservation) -> None:
        if observation.legacy_table is None:
            return
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (observation.legacy_table,),
        ).fetchone()
        if row is None:
            return
        table_name = str(row[0]).replace('"', '""')
        legacy_row = self._conn.execute(
            f'SELECT 1 FROM "{table_name}" WHERE rowid = ?', (observation.legacy_row_id,)
        ).fetchone()
        if legacy_row is None:
            raise ValueError(
                f"legacy {observation.legacy_table}.rowid {observation.legacy_row_id} does not exist"
            )

    @staticmethod
    def _observation_values(
        observation: ReportedObservation,
    ) -> tuple[tuple[str, ...], tuple[object, ...]]:
        return (
            (
                "observation_id",
                "idempotency_key",
                "issuer_id",
                "ticker",
                "concept_key",
                "period_start",
                "period_end",
                "fiscal_period_type",
                "dimensions_json",
                "numeric_value",
                "text_value",
                "currency",
                "unit",
                "scale",
                "observation_status",
                "evidence_node_id",
                "available_at",
                "recorded_at",
                "method",
                "method_version",
                "confidence",
                "legacy_table",
                "legacy_row_id",
            ),
            (
                observation.observation_id,
                observation.idempotency_key,
                observation.issuer_id,
                observation.ticker,
                observation.concept_key,
                observation.period_start,
                observation.period_end,
                observation.fiscal_period_type,
                observation.dimensions_json,
                observation.numeric_value,
                observation.text_value,
                observation.currency,
                observation.unit,
                observation.scale,
                observation.observation_status,
                observation.evidence_node_id,
                observation.available_at,
                observation.recorded_at,
                observation.method,
                observation.method_version,
                observation.confidence,
                observation.legacy_table,
                observation.legacy_row_id,
            ),
        )

    @staticmethod
    def _resolution_values(
        resolution: ResolutionRevision,
    ) -> tuple[tuple[str, ...], tuple[object, ...]]:
        return (
            (
                "resolution_id",
                "idempotency_key",
                "logical_key",
                "revision",
                "selected_observation_id",
                "resolver_kind",
                "policy_version",
                "reason",
                "knowledge_cutoff",
                "effective_at",
                "material_dissent",
                "supersedes_resolution_id",
                "recorded_at",
            ),
            (
                resolution.resolution_id,
                resolution.idempotency_key,
                resolution.logical_key,
                resolution.revision,
                resolution.selected_observation_id,
                resolution.resolver_kind,
                resolution.policy_version,
                resolution.reason,
                resolution.knowledge_cutoff,
                resolution.effective_at,
                resolution.material_dissent,
                resolution.supersedes_resolution_id,
                resolution.recorded_at,
            ),
        )
