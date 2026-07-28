"""Typed append-only registry below legal-issuer identity.

Legal issuers, regulator reporting units, securities, and venue listings are
different things.  This ledger keeps fund series and foreign reporting units
from collapsing merely because they share a registrant, depositary, or ticker.
It also replaces coarse source booleans with effective-dated document duties.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ReportingEntityKind: TypeAlias = Literal[
    "legal_registrant",
    "fund_series",
    "foreign_reporting_entity",
    "other",
]
ReportingIdentifierType: TypeAlias = Literal[
    "sec_cik",
    "sec_series_id",
    "sedar_profile",
    "edinet_code",
    "lei",
]
SecurityIdentifierType: TypeAlias = Literal[
    "sec_class_contract_id",
    "isin",
    "cusip",
    "figi",
    "otc_security_id",
]
IdentifierAuthority: TypeAlias = Literal[
    "issuer_publisher",
    "sec_registry",
    "exchange_registry",
    "regulator",
    "manual",
    "imported",
]
ResolutionOutcome: TypeAlias = Literal["selected", "unresolved", "rejected"]
DecisionKind: TypeAlias = Literal["deterministic", "manual", "imported"]
RelationshipKind: TypeAlias = Literal[
    "reports_through",
    "depositary_receipt_for",
    "share_class_of",
]
AuthorityKind: TypeAlias = Literal[
    "sec_edgar",
    "sedar_plus",
    "edinet",
    "issuer_publisher",
]
DocumentFamily: TypeAlias = Literal[
    "operating_company_periodic",
    "investment_company_periodic",
    "continuous_disclosure",
    "annual_securities_report",
    "issuer_financial_statements",
    "issuer_presentations",
    "issuer_earnings_materials",
]
ObligationState: TypeAlias = Literal["required", "optional", "not_applicable"]
SubjectBindingOutcome: TypeAlias = Literal["selected", "unresolved", "retired"]
CompletenessRule: TypeAlias = Literal[
    "regulator_inventory",
    "publisher_surface_exhaustion",
    "manual_exception",
]


def _timeline(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validate_clocks(
    effective_at: datetime,
    knowledge_at: datetime,
    recorded_at: datetime,
) -> None:
    if _timeline(knowledge_at) < _timeline(effective_at):
        raise ValueError("knowledge_at must not precede effective_at")
    if _timeline(recorded_at) < _timeline(knowledge_at):
        raise ValueError("recorded_at must not precede knowledge_at")


def _sha256(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return normalized


def _reason_json(value: tuple[tuple[str, str], ...]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Reasoned(_Record):
    reason_code: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    reason_details: tuple[tuple[str, str], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid_reason(self) -> Self:
        keys = [key for key, _ in self.reason_details]
        if len(keys) != len(set(keys)) or any(
            not key or not value for key, value in self.reason_details
        ):
            raise ValueError("reason details require unique non-empty pairs")
        return self


class ReportingEntity(_Record):
    reporting_entity_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    issuer_id: str = Field(min_length=1, max_length=128)
    reporting_entity_kind: ReportingEntityKind
    display_name: str = Field(min_length=1)
    created_at: datetime


class ReportingEntityIdentifierAssertion(_Record):
    assertion_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    reporting_entity_id: str = Field(min_length=1, max_length=128)
    identifier_type: ReportingIdentifierType
    identifier_value: str = Field(min_length=1)
    normalized_value: str = Field(min_length=1)
    authority: IdentifierAuthority
    source_observation_id: str | None = Field(default=None, min_length=1, max_length=128)
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime

    @model_validator(mode="after")
    def _valid_assertion(self) -> Self:
        if self.authority != "manual" and self.source_observation_id is None:
            raise ValueError("non-manual identifier assertion requires source evidence")
        expected = normalize_reporting_identifier(self.identifier_type, self.identifier_value)
        if self.normalized_value != expected:
            raise ValueError("normalized reporting identifier does not match value")
        _validate_clocks(self.effective_at, self.knowledge_at, self.recorded_at)
        return self

    @property
    def resolution_key(self) -> str:
        return f"{self.identifier_type}:{self.normalized_value}"


class SecurityIdentifierAssertion(_Record):
    assertion_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    security_id: str = Field(min_length=1, max_length=128)
    identifier_type: SecurityIdentifierType
    identifier_value: str = Field(min_length=1)
    normalized_value: str = Field(min_length=1)
    authority: IdentifierAuthority
    source_observation_id: str | None = Field(default=None, min_length=1, max_length=128)
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime

    @model_validator(mode="after")
    def _valid_assertion(self) -> Self:
        if self.authority != "manual" and self.source_observation_id is None:
            raise ValueError("non-manual security assertion requires source evidence")
        expected = normalize_security_identifier(self.identifier_type, self.identifier_value)
        if self.normalized_value != expected:
            raise ValueError("normalized security identifier does not match value")
        _validate_clocks(self.effective_at, self.knowledge_at, self.recorded_at)
        return self

    @property
    def resolution_key(self) -> str:
        return f"{self.identifier_type}:{self.normalized_value}"


class _IdentifierResolution(_Reasoned):
    resolution_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    resolution_key: str = Field(min_length=1, max_length=512)
    revision: int = Field(gt=0)
    outcome: ResolutionOutcome
    selected_assertion_id: str | None = Field(default=None, min_length=1, max_length=128)
    candidate_digest_sha256: str
    policy_name: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    policy_config_sha256: str
    material_dissent: bool
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    supersedes_resolution_id: str | None = Field(default=None, min_length=1, max_length=128)

    _candidate_sha = field_validator("candidate_digest_sha256")(_sha256)
    _policy_sha = field_validator("policy_config_sha256")(_sha256)

    @model_validator(mode="after")
    def _valid_resolution(self) -> Self:
        if (self.outcome == "selected") != (self.selected_assertion_id is not None):
            raise ValueError("only selected resolution may select an assertion")
        if (self.revision == 1) != (self.supersedes_resolution_id is None):
            raise ValueError("identifier resolution revision chain is incomplete")
        _validate_clocks(self.effective_at, self.knowledge_at, self.recorded_at)
        return self


class ReportingEntityIdentifierResolution(_IdentifierResolution):
    pass


class SecurityIdentifierResolution(_IdentifierResolution):
    pass


class SecurityReportingEntityRevision(_Reasoned):
    relationship_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    relationship_key: str = Field(min_length=1, max_length=256)
    revision: int = Field(gt=0)
    security_id: str = Field(min_length=1, max_length=128)
    reporting_entity_id: str = Field(min_length=1, max_length=128)
    relationship_kind: RelationshipKind
    decision_kind: DecisionKind
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    supersedes_relationship_revision_id: str | None = Field(
        default=None, min_length=1, max_length=128
    )

    @model_validator(mode="after")
    def _valid_revision(self) -> Self:
        if (self.revision == 1) != (self.supersedes_relationship_revision_id is None):
            raise ValueError("security reporting relationship chain is incomplete")
        _validate_clocks(self.effective_at, self.knowledge_at, self.recorded_at)
        return self


class SourceObligationRevision(_Reasoned):
    obligation_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    obligation_key: str = Field(min_length=1, max_length=256)
    revision: int = Field(gt=0)
    issuer_id: str = Field(min_length=1, max_length=128)
    reporting_entity_id: str | None = Field(default=None, min_length=1, max_length=128)
    authority_kind: AuthorityKind
    document_family: DocumentFamily
    obligation_state: ObligationState
    completeness_rule: CompletenessRule
    active_from: datetime
    active_to: datetime | None
    decision_kind: DecisionKind
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    supersedes_obligation_revision_id: str | None = Field(
        default=None, min_length=1, max_length=128
    )

    @model_validator(mode="after")
    def _valid_revision(self) -> Self:
        if self.active_to is not None and _timeline(self.active_to) <= _timeline(self.active_from):
            raise ValueError("source obligation active window is empty")
        if (self.revision == 1) != (self.supersedes_obligation_revision_id is None):
            raise ValueError("source obligation revision chain is incomplete")
        _validate_clocks(self.effective_at, self.knowledge_at, self.recorded_at)
        return self


class EvidenceSubjectBindingRevision(_Reasoned):
    binding_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    recorded_issuer_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(gt=0)
    issuer_id: str | None = Field(default=None, min_length=1, max_length=128)
    reporting_entity_id: str | None = Field(default=None, min_length=1, max_length=128)
    security_id: str | None = Field(default=None, min_length=1, max_length=128)
    outcome: SubjectBindingOutcome
    decision_kind: DecisionKind
    material_dissent: bool
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    supersedes_binding_revision_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _valid_revision(self) -> Self:
        if self.outcome == "selected":
            if self.issuer_id is None:
                raise ValueError("selected subject binding requires a canonical issuer")
        elif any(
            value is not None
            for value in (
                self.issuer_id,
                self.reporting_entity_id,
                self.security_id,
            )
        ):
            raise ValueError("unresolved or retired subject binding cannot select targets")
        if (self.revision == 1) != (self.supersedes_binding_revision_id is None):
            raise ValueError("subject binding revision chain is incomplete")
        _validate_clocks(self.effective_at, self.knowledge_at, self.recorded_at)
        return self


RegistryRecord: TypeAlias = (
    ReportingEntity
    | ReportingEntityIdentifierAssertion
    | ReportingEntityIdentifierResolution
    | SecurityIdentifierAssertion
    | SecurityIdentifierResolution
    | SecurityReportingEntityRevision
    | SourceObligationRevision
    | EvidenceSubjectBindingRevision
)


class ResolvedReportingEntity(_Record):
    issuer_id: str
    reporting_entity_id: str
    reporting_entity_kind: ReportingEntityKind
    display_name: str
    identifier_type: ReportingIdentifierType
    normalized_identifier: str
    material_dissent: bool


class ResolvedSecurityIdentifier(_Record):
    issuer_id: str
    security_id: str
    identifier_type: SecurityIdentifierType
    normalized_identifier: str
    material_dissent: bool


class CanonicalEvidenceSubject(_Record):
    recorded_issuer_id: str
    issuer_id: str
    reporting_entity_id: str | None = None
    security_id: str | None = None
    material_dissent: bool


@dataclass(frozen=True, slots=True)
class PersistResult:
    record_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class _InsertSpec:
    table: str
    id_column: str
    columns: tuple[str, ...]
    values: tuple[object, ...]


def normalize_reporting_identifier(
    identifier_type: ReportingIdentifierType,
    value: str,
) -> str:
    normalized = value.strip().upper()
    if identifier_type == "sec_cik":
        if not normalized.isdigit() or len(normalized) > 10:
            raise ValueError("SEC CIK must contain one to ten digits")
        return normalized.zfill(10)
    if identifier_type == "sec_series_id" and (
        len(normalized) != 10 or normalized[0] != "S" or not normalized[1:].isdigit()
    ):
        raise ValueError("SEC series ID must be S followed by nine digits")
    if identifier_type == "edinet_code" and (
        len(normalized) != 6 or normalized[0] != "E" or not normalized[1:].isdigit()
    ):
        raise ValueError("EDINET code must be E followed by five digits")
    if identifier_type == "lei" and len(normalized) != 20:
        raise ValueError("LEI must contain 20 characters")
    return normalized


def normalize_security_identifier(
    identifier_type: SecurityIdentifierType,
    value: str,
) -> str:
    normalized = value.strip().upper()
    expected_lengths = {
        "sec_class_contract_id": 10,
        "isin": 12,
        "cusip": 9,
        "figi": 12,
    }
    expected = expected_lengths.get(identifier_type)
    if expected is not None and len(normalized) != expected:
        raise ValueError(f"{identifier_type} must contain {expected} characters")
    if identifier_type == "sec_class_contract_id" and (
        normalized[0] != "C" or not normalized[1:].isdigit()
    ):
        raise ValueError("SEC class/contract ID must be C followed by nine digits")
    return normalized


def _candidate_digest(
    assertions: tuple[ReportingEntityIdentifierAssertion | SecurityIdentifierAssertion, ...],
) -> str:
    payload = [
        {
            "assertion_id": assertion.assertion_id,
            "owner_id": (
                assertion.reporting_entity_id
                if isinstance(assertion, ReportingEntityIdentifierAssertion)
                else assertion.security_id
            ),
            "resolution_key": assertion.resolution_key,
            "authority": assertion.authority,
            "source_observation_id": assertion.source_observation_id,
            "effective_at": _timeline(assertion.effective_at).isoformat(),
            "knowledge_at": _timeline(assertion.knowledge_at).isoformat(),
        }
        for assertion in sorted(assertions, key=lambda item: item.assertion_id)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def reporting_identifier_candidate_digest(
    assertions: tuple[ReportingEntityIdentifierAssertion, ...],
) -> str:
    return _candidate_digest(assertions)


def security_identifier_candidate_digest(
    assertions: tuple[SecurityIdentifierAssertion, ...],
) -> str:
    return _candidate_digest(assertions)


class ReportingEntityRegistry:
    """Single typed write and lookup boundary for reporting-unit identity."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def persist(self, record: RegistryRecord) -> PersistResult:
        validated = type(record).model_validate(record.model_dump())
        spec = _insert_spec(validated)
        cursor = self._conn.execute(
            f"INSERT OR IGNORE INTO {spec.table} ({','.join(spec.columns)}) "
            f"VALUES ({','.join('?' for _ in spec.columns)})",
            spec.values,
        )
        record_id = str(getattr(validated, spec.id_column))
        if cursor.rowcount == 1:
            return PersistResult(record_id=record_id, created=True)
        existing = self._conn.execute(
            f"SELECT {','.join(spec.columns)} FROM {spec.table} WHERE idempotency_key = ?",
            (str(validated.idempotency_key),),
        ).fetchone()
        if existing is None:
            raise ValueError(f"{spec.table} identity already exists with different idempotency key")
        if not _matches_stored(tuple(existing), spec.values):
            raise ValueError(f"{spec.table} idempotency key replay changed immutable values")
        return PersistResult(record_id=record_id, created=False)

    def resolve_reporting_identifier(
        self,
        identifier_type: ReportingIdentifierType,
        identifier_value: str,
        *,
        knowledge_at: datetime,
    ) -> ResolvedReportingEntity:
        normalized = normalize_reporting_identifier(identifier_type, identifier_value)
        row = self._conn.execute(
            """
            SELECT entity.issuer_id, entity.reporting_entity_id,
                   entity.reporting_entity_kind, entity.display_name,
                   assertion.identifier_type, assertion.normalized_value,
                   resolution.material_dissent
            FROM reporting_entity_identifier_resolution_outcomes AS resolution
            JOIN reporting_entity_identifier_assertions AS assertion
              ON assertion.assertion_id = resolution.selected_assertion_id
            JOIN reporting_entities AS entity
              ON entity.reporting_entity_id = assertion.reporting_entity_id
            WHERE resolution.resolution_key = ?
              AND resolution.outcome = 'selected'
              AND resolution.knowledge_at <= ?
              AND assertion.knowledge_at <= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM reporting_entity_identifier_resolution_outcomes AS newer
                  WHERE newer.resolution_key = resolution.resolution_key
                    AND newer.knowledge_at <= ?
                    AND newer.revision > resolution.revision
              )
            """,
            (
                f"{identifier_type}:{normalized}",
                knowledge_at,
                knowledge_at,
                knowledge_at,
            ),
        ).fetchone()
        if row is None:
            raise LookupError("reporting identifier has no selected resolution")
        return ResolvedReportingEntity.model_validate(
            {
                "issuer_id": str(row[0]),
                "reporting_entity_id": str(row[1]),
                "reporting_entity_kind": str(row[2]),
                "display_name": str(row[3]),
                "identifier_type": str(row[4]),
                "normalized_identifier": str(row[5]),
                "material_dissent": bool(row[6]),
            }
        )

    def resolve_security_identifier(
        self,
        identifier_type: SecurityIdentifierType,
        identifier_value: str,
        *,
        knowledge_at: datetime,
    ) -> ResolvedSecurityIdentifier:
        normalized = normalize_security_identifier(identifier_type, identifier_value)
        row = self._conn.execute(
            """
            SELECT security.issuer_id, security.security_id,
                   assertion.identifier_type, assertion.normalized_value,
                   resolution.material_dissent
            FROM security_identifier_resolution_outcomes AS resolution
            JOIN security_identifier_assertions AS assertion
              ON assertion.assertion_id = resolution.selected_assertion_id
            JOIN securities AS security
              ON security.security_id = assertion.security_id
            WHERE resolution.resolution_key = ?
              AND resolution.outcome = 'selected'
              AND resolution.knowledge_at <= ?
              AND assertion.knowledge_at <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM security_identifier_resolution_outcomes AS newer
                  WHERE newer.resolution_key = resolution.resolution_key
                    AND newer.knowledge_at <= ?
                    AND newer.revision > resolution.revision
              )
            """,
            (
                f"{identifier_type}:{normalized}",
                knowledge_at,
                knowledge_at,
                knowledge_at,
            ),
        ).fetchone()
        if row is None:
            raise LookupError("security identifier has no selected resolution")
        return ResolvedSecurityIdentifier.model_validate(
            {
                "issuer_id": str(row[0]),
                "security_id": str(row[1]),
                "identifier_type": str(row[2]),
                "normalized_identifier": str(row[3]),
                "material_dissent": bool(row[4]),
            }
        )

    def source_obligations(
        self,
        *,
        issuer_id: str,
        knowledge_at: datetime,
        active_at: datetime | None = None,
    ) -> tuple[SourceObligationRevision, ...]:
        active_clock = knowledge_at if active_at is None else active_at
        rows = self._conn.execute(
            """
            SELECT obligation_revision_id, idempotency_key, obligation_key,
                   revision, issuer_id, reporting_entity_id, authority_kind,
                   document_family, obligation_state, completeness_rule,
                   active_from, active_to, decision_kind, reason_code,
                   reason_details_json, effective_at, knowledge_at, recorded_at,
                   supersedes_obligation_revision_id
            FROM source_obligation_revisions AS obligation
            WHERE obligation.issuer_id = ?
              AND obligation.knowledge_at <= ?
              AND obligation.active_from <= ?
              AND (obligation.active_to IS NULL OR obligation.active_to > ?)
              AND NOT EXISTS (
                  SELECT 1 FROM source_obligation_revisions AS newer
                  WHERE newer.obligation_key = obligation.obligation_key
                    AND newer.knowledge_at <= ?
                    AND newer.revision > obligation.revision
              )
            ORDER BY obligation.obligation_key
            """,
            (
                issuer_id,
                knowledge_at,
                active_clock,
                active_clock,
                knowledge_at,
            ),
        ).fetchall()
        return tuple(
            SourceObligationRevision.model_validate(
                {
                    "obligation_revision_id": str(row[0]),
                    "idempotency_key": str(row[1]),
                    "obligation_key": str(row[2]),
                    "revision": int(row[3]),
                    "issuer_id": str(row[4]),
                    "reporting_entity_id": None if row[5] is None else str(row[5]),
                    "authority_kind": str(row[6]),
                    "document_family": str(row[7]),
                    "obligation_state": str(row[8]),
                    "completeness_rule": str(row[9]),
                    "active_from": _parse_datetime(row[10]),
                    "active_to": None if row[11] is None else _parse_datetime(row[11]),
                    "decision_kind": str(row[12]),
                    "reason_code": str(row[13]),
                    "reason_details": tuple(
                        sorted(
                            (
                                str(key),
                                str(value),
                            )
                            for key, value in json.loads(str(row[14])).items()
                        )
                    ),
                    "effective_at": _parse_datetime(row[15]),
                    "knowledge_at": _parse_datetime(row[16]),
                    "recorded_at": _parse_datetime(row[17]),
                    "supersedes_obligation_revision_id": (
                        None if row[18] is None else str(row[18])
                    ),
                }
            )
            for row in rows
        )

    def canonicalize_recorded_subject(
        self,
        recorded_issuer_id: str,
        *,
        knowledge_at: datetime,
    ) -> CanonicalEvidenceSubject:
        row = self._conn.execute(
            """
            SELECT recorded_issuer_id, issuer_id, reporting_entity_id,
                   security_id, material_dissent
            FROM recorded_subject_binding_revisions AS binding
            WHERE binding.recorded_issuer_id = ?
              AND binding.outcome = 'selected'
              AND binding.knowledge_at <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM recorded_subject_binding_revisions AS newer
                  WHERE newer.recorded_issuer_id = binding.recorded_issuer_id
                    AND newer.knowledge_at <= ?
                    AND newer.revision > binding.revision
              )
            """,
            (recorded_issuer_id, knowledge_at, knowledge_at),
        ).fetchone()
        if row is None:
            raise LookupError("recorded evidence subject has no selected resolution")
        return CanonicalEvidenceSubject.model_validate(
            {
                "recorded_issuer_id": str(row[0]),
                "issuer_id": str(row[1]),
                "reporting_entity_id": None if row[2] is None else str(row[2]),
                "security_id": None if row[3] is None else str(row[3]),
                "material_dissent": bool(row[4]),
            }
        )


def _resolution_spec(
    record: ReportingEntityIdentifierResolution | SecurityIdentifierResolution,
    *,
    table: str,
) -> _InsertSpec:
    return _InsertSpec(
        table=table,
        id_column="resolution_id",
        columns=(
            "resolution_id",
            "idempotency_key",
            "resolution_key",
            "revision",
            "outcome",
            "selected_assertion_id",
            "candidate_digest_sha256",
            "policy_name",
            "policy_version",
            "policy_config_sha256",
            "reason_code",
            "reason_details_json",
            "material_dissent",
            "effective_at",
            "knowledge_at",
            "recorded_at",
            "supersedes_resolution_id",
        ),
        values=(
            record.resolution_id,
            record.idempotency_key,
            record.resolution_key,
            record.revision,
            record.outcome,
            record.selected_assertion_id,
            record.candidate_digest_sha256,
            record.policy_name,
            record.policy_version,
            record.policy_config_sha256,
            record.reason_code,
            _reason_json(record.reason_details),
            record.material_dissent,
            record.effective_at,
            record.knowledge_at,
            record.recorded_at,
            record.supersedes_resolution_id,
        ),
    )


def _insert_spec(record: RegistryRecord) -> _InsertSpec:
    if isinstance(record, ReportingEntity):
        return _InsertSpec(
            table="reporting_entities",
            id_column="reporting_entity_id",
            columns=(
                "reporting_entity_id",
                "idempotency_key",
                "issuer_id",
                "reporting_entity_kind",
                "display_name",
                "created_at",
            ),
            values=(
                record.reporting_entity_id,
                record.idempotency_key,
                record.issuer_id,
                record.reporting_entity_kind,
                record.display_name,
                record.created_at,
            ),
        )
    if isinstance(record, ReportingEntityIdentifierAssertion):
        return _InsertSpec(
            table="reporting_entity_identifier_assertions",
            id_column="assertion_id",
            columns=(
                "assertion_id",
                "idempotency_key",
                "reporting_entity_id",
                "identifier_type",
                "identifier_value",
                "normalized_value",
                "authority",
                "source_observation_id",
                "effective_at",
                "knowledge_at",
                "recorded_at",
            ),
            values=(
                record.assertion_id,
                record.idempotency_key,
                record.reporting_entity_id,
                record.identifier_type,
                record.identifier_value,
                record.normalized_value,
                record.authority,
                record.source_observation_id,
                record.effective_at,
                record.knowledge_at,
                record.recorded_at,
            ),
        )
    if isinstance(record, ReportingEntityIdentifierResolution):
        return _resolution_spec(
            record,
            table="reporting_entity_identifier_resolution_outcomes",
        )
    if isinstance(record, SecurityIdentifierAssertion):
        return _InsertSpec(
            table="security_identifier_assertions",
            id_column="assertion_id",
            columns=(
                "assertion_id",
                "idempotency_key",
                "security_id",
                "identifier_type",
                "identifier_value",
                "normalized_value",
                "authority",
                "source_observation_id",
                "effective_at",
                "knowledge_at",
                "recorded_at",
            ),
            values=(
                record.assertion_id,
                record.idempotency_key,
                record.security_id,
                record.identifier_type,
                record.identifier_value,
                record.normalized_value,
                record.authority,
                record.source_observation_id,
                record.effective_at,
                record.knowledge_at,
                record.recorded_at,
            ),
        )
    if isinstance(record, SecurityIdentifierResolution):
        return _resolution_spec(
            record,
            table="security_identifier_resolution_outcomes",
        )
    if isinstance(record, SecurityReportingEntityRevision):
        return _InsertSpec(
            table="security_reporting_entity_revisions",
            id_column="relationship_revision_id",
            columns=(
                "relationship_revision_id",
                "idempotency_key",
                "relationship_key",
                "revision",
                "security_id",
                "reporting_entity_id",
                "relationship_kind",
                "decision_kind",
                "reason_code",
                "reason_details_json",
                "effective_at",
                "knowledge_at",
                "recorded_at",
                "supersedes_relationship_revision_id",
            ),
            values=(
                record.relationship_revision_id,
                record.idempotency_key,
                record.relationship_key,
                record.revision,
                record.security_id,
                record.reporting_entity_id,
                record.relationship_kind,
                record.decision_kind,
                record.reason_code,
                _reason_json(record.reason_details),
                record.effective_at,
                record.knowledge_at,
                record.recorded_at,
                record.supersedes_relationship_revision_id,
            ),
        )
    if isinstance(record, SourceObligationRevision):
        return _InsertSpec(
            table="source_obligation_revisions",
            id_column="obligation_revision_id",
            columns=(
                "obligation_revision_id",
                "idempotency_key",
                "obligation_key",
                "revision",
                "issuer_id",
                "reporting_entity_id",
                "authority_kind",
                "document_family",
                "obligation_state",
                "completeness_rule",
                "active_from",
                "active_to",
                "decision_kind",
                "reason_code",
                "reason_details_json",
                "effective_at",
                "knowledge_at",
                "recorded_at",
                "supersedes_obligation_revision_id",
            ),
            values=(
                record.obligation_revision_id,
                record.idempotency_key,
                record.obligation_key,
                record.revision,
                record.issuer_id,
                record.reporting_entity_id,
                record.authority_kind,
                record.document_family,
                record.obligation_state,
                record.completeness_rule,
                record.active_from,
                record.active_to,
                record.decision_kind,
                record.reason_code,
                _reason_json(record.reason_details),
                record.effective_at,
                record.knowledge_at,
                record.recorded_at,
                record.supersedes_obligation_revision_id,
            ),
        )
    return _InsertSpec(
        table="recorded_subject_binding_revisions",
        id_column="binding_revision_id",
        columns=(
            "binding_revision_id",
            "idempotency_key",
            "recorded_issuer_id",
            "revision",
            "issuer_id",
            "reporting_entity_id",
            "security_id",
            "outcome",
            "decision_kind",
            "reason_code",
            "reason_details_json",
            "material_dissent",
            "effective_at",
            "knowledge_at",
            "recorded_at",
            "supersedes_binding_revision_id",
        ),
        values=(
            record.binding_revision_id,
            record.idempotency_key,
            record.recorded_issuer_id,
            record.revision,
            record.issuer_id,
            record.reporting_entity_id,
            record.security_id,
            record.outcome,
            record.decision_kind,
            record.reason_code,
            _reason_json(record.reason_details),
            record.material_dissent,
            record.effective_at,
            record.knowledge_at,
            record.recorded_at,
            record.supersedes_binding_revision_id,
        ),
    )


def _parse_datetime(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _matches_stored(stored: tuple[object, ...], supplied: tuple[object, ...]) -> bool:
    if len(stored) != len(supplied):
        return False
    for persisted, expected in zip(stored, supplied, strict=True):
        if isinstance(expected, datetime):
            try:
                persisted_time = datetime.fromisoformat(str(persisted))
            except ValueError:
                return False
            if _timeline(persisted_time) != _timeline(expected):
                return False
        elif persisted != expected:
            return False
    return True
