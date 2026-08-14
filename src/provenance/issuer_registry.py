"""Append-only canonical issuer identity and reporting-universe boundary.

Ticker symbols, legal names, regulator identifiers, and publisher URLs are
observations, not entity identity.  This module keeps those observations
immutable, requires an explicit resolution before an identifier is canonical,
and separates the investor's research scope from broad discovery membership.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EntityKind: TypeAlias = Literal["operating_company", "fund", "partnership", "other"]
ProfileStatus: TypeAlias = Literal["active", "inactive", "merged", "dissolved"]
DecisionKind: TypeAlias = Literal["deterministic", "manual", "imported"]
IdentifierType: TypeAlias = Literal[
    "sec_cik",
    "lei",
    "isin",
    "figi",
    "sedar_profile",
    "companies_house",
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
SurfaceKind: TypeAlias = Literal[
    "sec_submissions",
    "sec_companyfacts",
    "ir_home",
    "ir_archive",
    "ir_events",
    "ir_presentations",
    "ir_financials",
    "ir_sec_filings",
    "earnings_feed",
    "other",
]
SurfaceStatus: TypeAlias = Literal["candidate", "verified", "retired", "unavailable"]
AuthorityLevel: TypeAlias = Literal["regulator", "publisher", "third_party"]
InclusionState: TypeAlias = Literal["core", "monitored", "discovery", "excluded"]
HistoryPolicy: TypeAlias = Literal["all_available", "since_date", "latest_n_years"]
SecurityKind: TypeAlias = Literal[
    "common_stock",
    "preferred_stock",
    "adr",
    "fund_share",
    "partnership_unit",
    "debt",
    "other",
]
ListingStatus: TypeAlias = Literal["listed", "delisted", "suspended"]

_SHA256_LENGTH = 64


def _timeline(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _sha256(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return normalized


def _reason_json(value: tuple[tuple[str, str], ...]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


class _RegistryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _ReasonedRecord(_RegistryRecord):
    reason_code: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    reason_details: tuple[tuple[str, str], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_reason_details(self) -> Self:
        keys = [key for key, _ in self.reason_details]
        if any(not key or not value for key, value in self.reason_details):
            raise ValueError("reason details require non-empty keys and values")
        if len(keys) != len(set(keys)):
            raise ValueError("reason detail keys must be unique")
        return self


class IssuerEntity(_RegistryRecord):
    issuer_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    entity_kind: EntityKind
    created_at: datetime


class IssuerProfileRevision(_ReasonedRecord):
    profile_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    issuer_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(gt=0)
    legal_name: str = Field(min_length=1)
    domicile_country: str | None = Field(default=None, min_length=2, max_length=2)
    filing_regime: str | None = Field(default=None, min_length=1, max_length=32)
    fiscal_year_end: str | None = Field(
        default=None,
        pattern=r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$",
    )
    status: ProfileStatus
    decision_kind: DecisionKind
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    supersedes_profile_revision_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("domicile_country")
    @classmethod
    def _country(cls, value: str | None) -> str | None:
        return None if value is None else value.upper()

    @model_validator(mode="after")
    def _validate_revision(self) -> Self:
        if (self.revision == 1) != (self.supersedes_profile_revision_id is None):
            raise ValueError("profile revision chain is incomplete")
        _validate_clocks(self.effective_at, self.knowledge_at, self.recorded_at)
        return self


class IdentifierAssertion(_RegistryRecord):
    assertion_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    issuer_id: str = Field(min_length=1, max_length=128)
    identifier_type: IdentifierType
    identifier_value: str = Field(min_length=1)
    normalized_value: str = Field(min_length=1)
    authority: IdentifierAuthority
    source_observation_id: str | None = Field(default=None, min_length=1, max_length=128)
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime

    @model_validator(mode="after")
    def _validate_assertion(self) -> Self:
        if self.authority != "manual" and self.source_observation_id is None:
            raise ValueError("non-manual identifier assertion requires source evidence")
        _validate_clocks(self.effective_at, self.knowledge_at, self.recorded_at)
        return self

    @property
    def resolution_key(self) -> str:
        return f"{self.identifier_type}:{self.normalized_value}"


class IdentifierResolution(_ReasonedRecord):
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
    def _validate_resolution(self) -> Self:
        if (self.outcome == "selected") != (self.selected_assertion_id is not None):
            raise ValueError("only selected identifier resolution may select an assertion")
        if (self.revision == 1) != (self.supersedes_resolution_id is None):
            raise ValueError("identifier resolution revision chain is incomplete")
        _validate_clocks(self.effective_at, self.knowledge_at, self.recorded_at)
        return self


class Security(_RegistryRecord):
    security_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    issuer_id: str = Field(min_length=1, max_length=128)
    security_kind: SecurityKind
    share_class: str | None = Field(default=None, min_length=1)
    created_at: datetime


class ListingAssertion(_RegistryRecord):
    assertion_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    security_id: str = Field(min_length=1, max_length=128)
    market_mic: str = Field(min_length=4, max_length=8)
    ticker: str = Field(min_length=1)
    normalized_ticker: str = Field(min_length=1)
    currency: str = Field(min_length=3, max_length=3)
    status: ListingStatus
    authority: IdentifierAuthority
    source_observation_id: str | None = Field(default=None, min_length=1, max_length=128)
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime

    @field_validator("market_mic", "currency")
    @classmethod
    def _upper_code(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _validate_listing(self) -> Self:
        if self.authority != "manual" and self.source_observation_id is None:
            raise ValueError("non-manual listing assertion requires source evidence")
        _validate_clocks(self.effective_at, self.knowledge_at, self.recorded_at)
        return self

    @property
    def resolution_key(self) -> str:
        return f"listing:{self.market_mic}:{self.normalized_ticker}"


class ListingResolution(_ReasonedRecord):
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
    def _validate_resolution(self) -> Self:
        if (self.outcome == "selected") != (self.selected_assertion_id is not None):
            raise ValueError("only selected listing resolution may select an assertion")
        if (self.revision == 1) != (self.supersedes_resolution_id is None):
            raise ValueError("listing resolution revision chain is incomplete")
        _validate_clocks(self.effective_at, self.knowledge_at, self.recorded_at)
        return self


class AuthoritySurfaceRevision(_RegistryRecord):
    surface_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    issuer_id: str = Field(min_length=1, max_length=128)
    surface_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_.:-]*$",
    )
    revision: int = Field(gt=0)
    surface_kind: SurfaceKind
    source_url: str = Field(min_length=1)
    status: SurfaceStatus
    authority_level: AuthorityLevel
    source_observation_id: str | None = Field(default=None, min_length=1, max_length=128)
    verification_method: str = Field(min_length=1, max_length=128)
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    supersedes_surface_revision_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _validate_surface(self) -> Self:
        if self.status == "verified" and self.source_observation_id is None:
            raise ValueError("verified authority surface requires source evidence")
        if (self.revision == 1) != (self.supersedes_surface_revision_id is None):
            raise ValueError("authority surface revision chain is incomplete")
        _validate_clocks(self.effective_at, self.knowledge_at, self.recorded_at)
        return self


class ReportingScopeRevision(_ReasonedRecord):
    scope_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    scope_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_.:-]*$",
    )
    issuer_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(gt=0)
    inclusion_state: InclusionState
    history_policy: HistoryPolicy
    history_start: datetime | None = None
    latest_years: int | None = Field(default=None, gt=0)
    require_sec: bool
    require_ir: bool
    require_earnings: bool
    decision_kind: DecisionKind
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    supersedes_scope_revision_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _validate_scope(self) -> Self:
        if self.history_policy == "all_available":
            valid_history = self.history_start is None and self.latest_years is None
        elif self.history_policy == "since_date":
            valid_history = self.history_start is not None and self.latest_years is None
        else:
            valid_history = self.history_start is None and self.latest_years is not None
        if not valid_history:
            raise ValueError("history policy parameters are inconsistent")
        if (self.revision == 1) != (self.supersedes_scope_revision_id is None):
            raise ValueError("reporting scope revision chain is incomplete")
        _validate_clocks(self.effective_at, self.knowledge_at, self.recorded_at)
        return self


class LegacyIssuerBindingRevision(_ReasonedRecord):
    """Temporal bridge from immutable recorded IDs to canonical issuers."""

    binding_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    recorded_issuer_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(gt=0)
    issuer_id: str | None = Field(default=None, min_length=1, max_length=128)
    outcome: Literal["selected", "unresolved", "retired"]
    decision_kind: DecisionKind
    material_dissent: bool
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    supersedes_binding_revision_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        if (self.outcome == "selected") != (self.issuer_id is not None):
            raise ValueError("only selected legacy binding may select an issuer")
        if (self.revision == 1) != (self.supersedes_binding_revision_id is None):
            raise ValueError("legacy binding revision chain is incomplete")
        _validate_clocks(self.effective_at, self.knowledge_at, self.recorded_at)
        return self


RegistryRecord: TypeAlias = (
    IssuerEntity
    | IssuerProfileRevision
    | IdentifierAssertion
    | IdentifierResolution
    | Security
    | ListingAssertion
    | ListingResolution
    | AuthoritySurfaceRevision
    | ReportingScopeRevision
    | LegacyIssuerBindingRevision
)


@dataclass(frozen=True, slots=True)
class PersistResult:
    record_id: str
    created: bool


class UnresolvedIssuerIdentityError(LookupError):
    """Raised when identity evidence exists but no canonical selection is valid."""


class CanonicalIssuer(_RegistryRecord):
    issuer_id: str
    entity_kind: EntityKind
    legal_name: str | None
    identifier_type: IdentifierType | None = None
    normalized_identifier: str | None = None
    material_dissent: bool = False


class ResolvedListing(_RegistryRecord):
    issuer_id: str
    security_id: str
    security_kind: SecurityKind
    share_class: str | None
    market_mic: str
    ticker: str
    normalized_ticker: str
    currency: str
    status: ListingStatus
    material_dissent: bool


class VerifiedAuthoritySurface(_RegistryRecord):
    surface_revision_id: str
    issuer_id: str
    surface_key: str
    surface_kind: SurfaceKind
    source_url: str
    source_observation_id: str
    verification_method: str
    authority_level: AuthorityLevel
    knowledge_at: datetime


@dataclass(frozen=True, slots=True)
class _InsertSpec:
    table: str
    id_column: str
    columns: tuple[str, ...]
    values: tuple[object, ...]


def _validate_clocks(effective_at: datetime, knowledge_at: datetime, recorded_at: datetime) -> None:
    if _timeline(knowledge_at) < _timeline(effective_at):
        raise ValueError("knowledge_at must not precede effective_at")
    if _timeline(recorded_at) < _timeline(knowledge_at):
        raise ValueError("recorded_at must not precede knowledge_at")


def identifier_candidate_digest(
    assertions: tuple[IdentifierAssertion, ...] | list[IdentifierAssertion],
) -> str:
    """Hash the complete ordered identifier candidate envelope."""

    payload = [
        {
            "assertion_id": assertion.assertion_id,
            "issuer_id": assertion.issuer_id,
            "resolution_key": assertion.resolution_key,
            "authority": assertion.authority,
            "source_observation_id": assertion.source_observation_id,
            "effective_at": _timeline(assertion.effective_at).isoformat(),
            "knowledge_at": _timeline(assertion.knowledge_at).isoformat(),
        }
        for assertion in sorted(assertions, key=lambda item: item.assertion_id)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def listing_candidate_digest(
    assertions: tuple[ListingAssertion, ...] | list[ListingAssertion],
) -> str:
    """Hash the complete ordered listing candidate envelope."""

    payload = [
        {
            "assertion_id": assertion.assertion_id,
            "security_id": assertion.security_id,
            "resolution_key": assertion.resolution_key,
            "currency": assertion.currency,
            "status": assertion.status,
            "authority": assertion.authority,
            "source_observation_id": assertion.source_observation_id,
            "effective_at": _timeline(assertion.effective_at).isoformat(),
            "knowledge_at": _timeline(assertion.knowledge_at).isoformat(),
        }
        for assertion in sorted(assertions, key=lambda item: item.assertion_id)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class IssuerRegistry:
    """Single typed write boundary for issuer and reporting-universe records."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def persist(self, record: RegistryRecord) -> PersistResult:
        validated = type(record).model_validate(record.model_dump())
        spec = _insert_spec(validated)
        placeholders = ",".join("?" for _ in spec.columns)
        sql = (
            f"INSERT OR IGNORE INTO {spec.table} ({','.join(spec.columns)}) VALUES ({placeholders})"
        )
        cursor = self._conn.execute(sql, spec.values)
        record_id = str(getattr(validated, spec.id_column))
        if cursor.rowcount == 1:
            return PersistResult(record_id=record_id, created=True)
        existing = self._conn.execute(
            f"SELECT {','.join(spec.columns)} FROM {spec.table} WHERE idempotency_key = ?",  # nosec B608 -- trusted internal SQL shape; values remain bound
            (str(validated.idempotency_key),),
        ).fetchone()
        if existing is None:
            raise ValueError(f"{spec.table} identity already exists with different idempotency key")
        if not _matches_stored(tuple(existing), spec.values):
            raise ValueError(f"{spec.table} idempotency key replay changed immutable values")
        return PersistResult(record_id=record_id, created=False)

    def resolve_identifier(
        self,
        identifier_type: IdentifierType,
        identifier_value: str,
        *,
        knowledge_at: datetime,
    ) -> CanonicalIssuer:
        """Resolve one regulator identifier at a historical knowledge cutoff."""

        normalized = normalize_identifier(identifier_type, identifier_value)
        resolution_key = f"{identifier_type}:{normalized}"
        row = self._conn.execute(
            """
            SELECT
                assertion.issuer_id,
                entity.entity_kind,
                profile.legal_name,
                resolution.material_dissent
            FROM issuer_identifier_resolution_outcomes AS resolution
            JOIN issuer_identifier_assertions AS assertion
              ON assertion.assertion_id = resolution.selected_assertion_id
            JOIN issuer_entities AS entity
              ON entity.issuer_id = assertion.issuer_id
            LEFT JOIN issuer_profile_revisions AS profile
              ON profile.issuer_id = assertion.issuer_id
             AND profile.knowledge_at <= ?
             AND NOT EXISTS (
                SELECT 1
                FROM issuer_profile_revisions AS newer_profile
                WHERE newer_profile.issuer_id = profile.issuer_id
                  AND newer_profile.knowledge_at <= ?
                  AND newer_profile.revision > profile.revision
             )
            WHERE resolution.resolution_key = ?
              AND resolution.outcome = 'selected'
              AND resolution.knowledge_at <= ?
              AND assertion.knowledge_at <= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM issuer_identifier_resolution_outcomes AS newer
                  WHERE newer.resolution_key = resolution.resolution_key
                    AND newer.knowledge_at <= ?
                    AND newer.revision > resolution.revision
              )
            """,
            (
                knowledge_at,
                knowledge_at,
                resolution_key,
                knowledge_at,
                knowledge_at,
                knowledge_at,
            ),
        ).fetchone()
        if row is None:
            raise UnresolvedIssuerIdentityError(
                f"no canonical issuer for {resolution_key!r} at knowledge cutoff"
            )
        return CanonicalIssuer.model_validate(
            {
                "issuer_id": str(row[0]),
                "entity_kind": str(row[1]),
                "legal_name": None if row[2] is None else str(row[2]),
                "identifier_type": identifier_type,
                "normalized_identifier": normalized,
                "material_dissent": bool(row[3]),
            }
        )

    def resolve_listing(
        self,
        market_mic: str,
        ticker: str,
        *,
        knowledge_at: datetime,
    ) -> ResolvedListing:
        """Resolve a tradable listing without treating its ticker as issuer identity."""

        normalized_mic = market_mic.strip().upper()
        normalized_ticker = ticker.strip().upper()
        resolution_key = f"listing:{normalized_mic}:{normalized_ticker}"
        row = self._conn.execute(
            """
            SELECT
                security.issuer_id,
                security.security_id,
                security.security_kind,
                security.share_class,
                assertion.market_mic,
                assertion.ticker,
                assertion.normalized_ticker,
                assertion.currency,
                assertion.status,
                resolution.material_dissent
            FROM security_listing_resolution_outcomes AS resolution
            JOIN security_listing_assertions AS assertion
              ON assertion.assertion_id = resolution.selected_assertion_id
            JOIN securities AS security
              ON security.security_id = assertion.security_id
            WHERE resolution.resolution_key = ?
              AND resolution.outcome = 'selected'
              AND resolution.knowledge_at <= ?
              AND assertion.knowledge_at <= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM security_listing_resolution_outcomes AS newer
                  WHERE newer.resolution_key = resolution.resolution_key
                    AND newer.knowledge_at <= ?
                    AND newer.revision > resolution.revision
              )
            """,
            (resolution_key, knowledge_at, knowledge_at, knowledge_at),
        ).fetchone()
        if row is None:
            raise UnresolvedIssuerIdentityError(
                f"no canonical security listing for {resolution_key!r}"
            )
        return ResolvedListing.model_validate(
            {
                "issuer_id": str(row[0]),
                "security_id": str(row[1]),
                "security_kind": str(row[2]),
                "share_class": None if row[3] is None else str(row[3]),
                "market_mic": str(row[4]),
                "ticker": str(row[5]),
                "normalized_ticker": str(row[6]),
                "currency": str(row[7]),
                "status": str(row[8]),
                "material_dissent": bool(row[9]),
            }
        )

    def canonicalize_recorded_issuer(
        self,
        recorded_issuer_id: str,
        *,
        knowledge_at: datetime,
    ) -> CanonicalIssuer:
        """Resolve a historical free-form issuer ID without mutating old evidence."""

        row = self._conn.execute(
            """
            SELECT
                binding.issuer_id,
                entity.entity_kind,
                profile.legal_name,
                binding.material_dissent
            FROM legacy_issuer_binding_revisions AS binding
            JOIN issuer_entities AS entity ON entity.issuer_id = binding.issuer_id
            LEFT JOIN issuer_profile_revisions AS profile
              ON profile.issuer_id = binding.issuer_id
             AND profile.knowledge_at <= ?
             AND NOT EXISTS (
                 SELECT 1
                 FROM issuer_profile_revisions AS newer_profile
                 WHERE newer_profile.issuer_id = profile.issuer_id
                   AND newer_profile.knowledge_at <= ?
                   AND newer_profile.revision > profile.revision
             )
            WHERE binding.recorded_issuer_id = ?
              AND binding.outcome = 'selected'
              AND binding.knowledge_at <= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM legacy_issuer_binding_revisions AS newer
                  WHERE newer.recorded_issuer_id = binding.recorded_issuer_id
                    AND newer.knowledge_at <= ?
                    AND newer.revision > binding.revision
              )
            """,
            (
                knowledge_at,
                knowledge_at,
                recorded_issuer_id,
                knowledge_at,
                knowledge_at,
            ),
        ).fetchone()
        if row is None:
            raise UnresolvedIssuerIdentityError(
                f"recorded issuer {recorded_issuer_id!r} has no canonical binding"
            )
        return CanonicalIssuer.model_validate(
            {
                "issuer_id": str(row[0]),
                "entity_kind": str(row[1]),
                "legal_name": None if row[2] is None else str(row[2]),
                "material_dissent": bool(row[3]),
            }
        )

    def source_authority(
        self,
        issuer_id: str,
        surface_kind: SurfaceKind,
        *,
        knowledge_at: datetime,
    ) -> tuple[VerifiedAuthoritySurface, ...]:
        """Return all verified current authority surfaces at a knowledge cutoff."""

        rows = self._conn.execute(
            """
            SELECT
                surface_revision_id,
                issuer_id,
                surface_key,
                surface_kind,
                source_url,
                source_observation_id,
                verification_method,
                authority_level,
                knowledge_at
            FROM issuer_authority_surface_revisions AS surface
            WHERE surface.issuer_id = ?
              AND surface.surface_kind = ?
              AND surface.status = 'verified'
              AND surface.knowledge_at <= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM issuer_authority_surface_revisions AS newer
                  WHERE newer.issuer_id = surface.issuer_id
                    AND newer.surface_key = surface.surface_key
                    AND newer.knowledge_at <= ?
                    AND newer.revision > surface.revision
              )
            ORDER BY surface.surface_key
            """,
            (issuer_id, surface_kind, knowledge_at, knowledge_at),
        ).fetchall()
        return tuple(
            VerifiedAuthoritySurface.model_validate(
                {
                    "surface_revision_id": str(row[0]),
                    "issuer_id": str(row[1]),
                    "surface_key": str(row[2]),
                    "surface_kind": str(row[3]),
                    "source_url": str(row[4]),
                    "source_observation_id": str(row[5]),
                    "verification_method": str(row[6]),
                    "authority_level": str(row[7]),
                    "knowledge_at": datetime.fromisoformat(str(row[8])),
                }
            )
            for row in rows
        )


def normalize_identifier(identifier_type: IdentifierType, value: str) -> str:
    """Normalize regulator identifiers without accepting ticker aliases."""

    normalized = value.strip()
    if identifier_type == "sec_cik":
        if not normalized.isdigit() or len(normalized) > 10:
            raise ValueError("SEC CIK must contain one to ten digits")
        return normalized.zfill(10)
    return normalized.upper()


def ensure_sec_cik_evidence_binding(
    conn: sqlite3.Connection,
    *,
    recorded_issuer_id: str,
    recorded_at: datetime,
) -> int:
    """Bind a deterministic ``sec-cik-*`` evidence subject when resolvable.

    Evidence capture may precede issuer-registry bootstrap, so an absent
    canonical CIK is left for the bootstrap reconciler. Once the registry has
    selected a unique SEC CIK, however, evidence persistence must not leave the
    recorded subject orphaned.
    """

    prefix = "sec-cik-"
    if not recorded_issuer_id.startswith(prefix):
        return 0
    normalized_cik = recorded_issuer_id.removeprefix(prefix)
    if len(normalized_cik) != 10 or not normalized_cik.isdigit():
        raise ValueError("SEC-CIK evidence subject must end in exactly ten digits")
    required_tables = (
        "issuer_identifier_resolution_outcomes",
        "issuer_identifier_assertions",
        "legacy_issuer_binding_revisions",
    )
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            f"AND name IN ({','.join('?' for _ in required_tables)})",  # nosec B608 -- fixed internal table list; values remain bound
            required_tables,
        ).fetchall()
    }
    if present != set(required_tables):
        return 0
    registry = IssuerRegistry(conn)
    try:
        canonical = registry.resolve_identifier(
            "sec_cik",
            normalized_cik,
            knowledge_at=recorded_at,
        )
    except UnresolvedIssuerIdentityError:
        return 0
    if canonical.material_dissent:
        raise UnresolvedIssuerIdentityError(
            f"SEC CIK {normalized_cik!r} has material identity dissent"
        )
    current = conn.execute(
        "SELECT binding_revision_id, revision, issuer_id, outcome, reason_code "
        "FROM legacy_issuer_binding_revisions WHERE recorded_issuer_id = ? "
        "ORDER BY revision DESC LIMIT 1",
        (recorded_issuer_id,),
    ).fetchone()
    semantics = (canonical.issuer_id, "selected", "unique_sec_cik_selected")
    if (
        current is not None
        and (
            None if current[2] is None else str(current[2]),
            str(current[3]),
            str(current[4]),
        )
        == semantics
    ):
        return 0
    revision = 1 if current is None else int(current[1]) + 1
    digest = hashlib.sha256(
        json.dumps(
            {
                "recorded_issuer_id": recorded_issuer_id,
                "issuer_id": canonical.issuer_id,
                "revision": revision,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    record_id = f"sec-cik-binding:{digest}"
    return int(
        registry.persist(
            LegacyIssuerBindingRevision(
                binding_revision_id=record_id,
                idempotency_key=record_id,
                recorded_issuer_id=recorded_issuer_id,
                revision=revision,
                issuer_id=canonical.issuer_id,
                outcome="selected",
                decision_kind="deterministic",
                reason_code="unique_sec_cik_selected",
                reason_details=(("normalized_cik", normalized_cik),),
                material_dissent=False,
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_binding_revision_id=None if current is None else str(current[0]),
            )
        ).created
    )


def evidence_document_relation(conn: sqlite3.Connection) -> str:
    """Return the canonical issuer projection when the 0227 cutover is present."""

    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'view' "
        "AND name = 'v_evidence_document_versions_canonical'"
    ).fetchone()
    return (
        "v_evidence_document_versions_canonical"
        if exists is not None
        else "evidence_document_versions"
    )


def _insert_spec(record: RegistryRecord) -> _InsertSpec:
    if isinstance(record, IssuerEntity):
        return _InsertSpec(
            table="issuer_entities",
            id_column="issuer_id",
            columns=("issuer_id", "idempotency_key", "entity_kind", "created_at"),
            values=(
                record.issuer_id,
                record.idempotency_key,
                record.entity_kind,
                record.created_at,
            ),
        )
    if isinstance(record, IssuerProfileRevision):
        return _InsertSpec(
            table="issuer_profile_revisions",
            id_column="profile_revision_id",
            columns=(
                "profile_revision_id",
                "idempotency_key",
                "issuer_id",
                "revision",
                "legal_name",
                "domicile_country",
                "filing_regime",
                "fiscal_year_end",
                "status",
                "decision_kind",
                "reason_code",
                "reason_details_json",
                "effective_at",
                "knowledge_at",
                "recorded_at",
                "supersedes_profile_revision_id",
            ),
            values=(
                record.profile_revision_id,
                record.idempotency_key,
                record.issuer_id,
                record.revision,
                record.legal_name,
                record.domicile_country,
                record.filing_regime,
                record.fiscal_year_end,
                record.status,
                record.decision_kind,
                record.reason_code,
                _reason_json(record.reason_details),
                record.effective_at,
                record.knowledge_at,
                record.recorded_at,
                record.supersedes_profile_revision_id,
            ),
        )
    if isinstance(record, IdentifierAssertion):
        return _InsertSpec(
            table="issuer_identifier_assertions",
            id_column="assertion_id",
            columns=(
                "assertion_id",
                "idempotency_key",
                "issuer_id",
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
                record.issuer_id,
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
    if isinstance(record, IdentifierResolution):
        return _InsertSpec(
            table="issuer_identifier_resolution_outcomes",
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
    if isinstance(record, Security):
        return _InsertSpec(
            table="securities",
            id_column="security_id",
            columns=(
                "security_id",
                "idempotency_key",
                "issuer_id",
                "security_kind",
                "share_class",
                "created_at",
            ),
            values=(
                record.security_id,
                record.idempotency_key,
                record.issuer_id,
                record.security_kind,
                record.share_class,
                record.created_at,
            ),
        )
    if isinstance(record, ListingAssertion):
        return _InsertSpec(
            table="security_listing_assertions",
            id_column="assertion_id",
            columns=(
                "assertion_id",
                "idempotency_key",
                "security_id",
                "market_mic",
                "ticker",
                "normalized_ticker",
                "currency",
                "status",
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
                record.market_mic,
                record.ticker,
                record.normalized_ticker,
                record.currency,
                record.status,
                record.authority,
                record.source_observation_id,
                record.effective_at,
                record.knowledge_at,
                record.recorded_at,
            ),
        )
    if isinstance(record, ListingResolution):
        return _InsertSpec(
            table="security_listing_resolution_outcomes",
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
    if isinstance(record, AuthoritySurfaceRevision):
        return _InsertSpec(
            table="issuer_authority_surface_revisions",
            id_column="surface_revision_id",
            columns=(
                "surface_revision_id",
                "idempotency_key",
                "issuer_id",
                "surface_key",
                "revision",
                "surface_kind",
                "source_url",
                "status",
                "authority_level",
                "source_observation_id",
                "verification_method",
                "effective_at",
                "knowledge_at",
                "recorded_at",
                "supersedes_surface_revision_id",
            ),
            values=(
                record.surface_revision_id,
                record.idempotency_key,
                record.issuer_id,
                record.surface_key,
                record.revision,
                record.surface_kind,
                record.source_url,
                record.status,
                record.authority_level,
                record.source_observation_id,
                record.verification_method,
                record.effective_at,
                record.knowledge_at,
                record.recorded_at,
                record.supersedes_surface_revision_id,
            ),
        )
    if isinstance(record, ReportingScopeRevision):
        return _InsertSpec(
            table="issuer_reporting_scope_revisions",
            id_column="scope_revision_id",
            columns=(
                "scope_revision_id",
                "idempotency_key",
                "scope_key",
                "issuer_id",
                "revision",
                "inclusion_state",
                "history_policy",
                "history_start",
                "latest_years",
                "require_sec",
                "require_ir",
                "require_earnings",
                "decision_kind",
                "reason_code",
                "reason_details_json",
                "effective_at",
                "knowledge_at",
                "recorded_at",
                "supersedes_scope_revision_id",
            ),
            values=(
                record.scope_revision_id,
                record.idempotency_key,
                record.scope_key,
                record.issuer_id,
                record.revision,
                record.inclusion_state,
                record.history_policy,
                record.history_start,
                record.latest_years,
                record.require_sec,
                record.require_ir,
                record.require_earnings,
                record.decision_kind,
                record.reason_code,
                _reason_json(record.reason_details),
                record.effective_at,
                record.knowledge_at,
                record.recorded_at,
                record.supersedes_scope_revision_id,
            ),
        )
    return _InsertSpec(
        table="legacy_issuer_binding_revisions",
        id_column="binding_revision_id",
        columns=(
            "binding_revision_id",
            "idempotency_key",
            "recorded_issuer_id",
            "revision",
            "issuer_id",
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
