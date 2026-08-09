"""Evidence-backed bootstrap for non-SEC issuer and security identities.

Foreign listings and depositary receipts cannot safely be resolved by treating
the recorded ticker as a legal-issuer identifier.  This boundary imports an
owner-reviewed, closed identity bundle while preserving the exact publisher or
regulator bytes that support every assertion.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.evidence_links import BlobLocationObservation, EvidenceLinkLedger
from provenance.issuer_registry import (
    AuthorityLevel,
    AuthoritySurfaceRevision,
    IdentifierAssertion,
    IdentifierAuthority,
    IdentifierResolution,
    IdentifierType,
    IssuerEntity,
    IssuerProfileRevision,
    IssuerRegistry,
    LegacyIssuerBindingRevision,
    ListingAssertion,
    ListingResolution,
    ReportingScopeRevision,
    Security,
    SecurityKind,
    SurfaceKind,
    identifier_candidate_digest,
    listing_candidate_digest,
    normalize_identifier,
)
from provenance.reporting_entity_registry import (
    AuthorityKind,
    CompletenessRule,
    DocumentFamily,
    EvidenceSubjectBindingRevision,
    ObligationState,
    RelationshipKind,
    ReportingEntity,
    ReportingEntityIdentifierAssertion,
    ReportingEntityIdentifierResolution,
    ReportingEntityKind,
    ReportingEntityRegistry,
    ReportingIdentifierType,
    SecurityIdentifierAssertion,
    SecurityIdentifierResolution,
    SecurityIdentifierType,
    SecurityReportingEntityRevision,
    SourceObligationRevision,
    normalize_reporting_identifier,
    normalize_security_identifier,
    reporting_identifier_candidate_digest,
    security_identifier_candidate_digest,
)

_COLLECTOR_VERSION = "foreign-issuer-bootstrap@1"
_POLICY_NAME = "analyst_reviewed_authority_bundle"
_POLICY_VERSION = "1"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ForeignIssuerSource(_ClosedModel):
    source_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_.:-]*$",
    )
    source_kind: str = Field(min_length=1, max_length=64)
    source_url: str = Field(min_length=1)
    media_type: str = Field(min_length=1, max_length=255)
    raw_body: bytes = Field(min_length=1)


class IssuerIdentifierClaim(_ClosedModel):
    identifier_type: IdentifierType
    identifier_value: str = Field(min_length=1)
    authority: IdentifierAuthority
    source_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _not_sec(self) -> Self:
        if self.identifier_type == "sec_cik":
            raise ValueError("foreign identity bundles cannot assert an SEC CIK")
        normalize_identifier(self.identifier_type, self.identifier_value)
        return self


class ReportingIdentifierClaim(_ClosedModel):
    identifier_type: ReportingIdentifierType
    identifier_value: str = Field(min_length=1)
    authority: IdentifierAuthority
    source_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _not_sec(self) -> Self:
        if self.identifier_type in {"sec_cik", "sec_series_id"}:
            raise ValueError("foreign identity bundles cannot assert SEC reporting IDs")
        normalize_reporting_identifier(self.identifier_type, self.identifier_value)
        return self


class SecurityIdentifierClaim(_ClosedModel):
    identifier_type: SecurityIdentifierType
    identifier_value: str = Field(min_length=1)
    authority: IdentifierAuthority
    source_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _normalized(self) -> Self:
        normalize_security_identifier(self.identifier_type, self.identifier_value)
        return self


class ListingClaim(_ClosedModel):
    market_mic: str = Field(min_length=4, max_length=8)
    ticker: str = Field(min_length=1, max_length=32)
    currency: str = Field(min_length=3, max_length=3)
    authority: IdentifierAuthority
    source_key: str = Field(min_length=1, max_length=128)

    @field_validator("market_mic", "currency")
    @classmethod
    def _upper_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, value: str) -> str:
        return value.strip().upper()


class SecurityClaim(_ClosedModel):
    security_id: str = Field(min_length=1, max_length=128)
    security_kind: SecurityKind
    share_class: str | None = Field(default=None, min_length=1)
    relationship_kind: RelationshipKind
    source_key: str = Field(min_length=1, max_length=128)
    identifiers: tuple[SecurityIdentifierClaim, ...] = Field(min_length=1)
    listings: tuple[ListingClaim, ...] = ()


class AuthoritySurfaceClaim(_ClosedModel):
    surface_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_.:-]*$",
    )
    surface_kind: SurfaceKind
    source_url: str = Field(min_length=1)
    authority_level: AuthorityLevel
    source_key: str = Field(min_length=1, max_length=128)


class SourceObligationClaim(_ClosedModel):
    authority_kind: AuthorityKind
    document_family: DocumentFamily
    obligation_state: ObligationState
    completeness_rule: CompletenessRule
    source_key: str = Field(min_length=1, max_length=128)


class ForeignIssuerBootstrapRequest(_ClosedModel):
    ticker: str = Field(min_length=1, max_length=32)
    issuer_id: str = Field(min_length=1, max_length=128)
    legal_name: str = Field(min_length=1)
    domicile_country: str = Field(min_length=2, max_length=2)
    filing_regime: str = Field(min_length=1, max_length=32)
    profile_source_key: str = Field(min_length=1, max_length=128)
    reporting_entity_id: str = Field(min_length=1, max_length=128)
    reporting_entity_kind: ReportingEntityKind
    reporting_entity_display_name: str = Field(min_length=1)
    sources: tuple[ForeignIssuerSource, ...] = Field(min_length=1)
    issuer_identifiers: tuple[IssuerIdentifierClaim, ...] = ()
    reporting_identifiers: tuple[ReportingIdentifierClaim, ...] = ()
    securities: tuple[SecurityClaim, ...] = Field(min_length=1)
    subject_security_id: str = Field(min_length=1, max_length=128)
    authority_surfaces: tuple[AuthoritySurfaceClaim, ...] = ()
    obligations: tuple[SourceObligationClaim, ...] = ()
    inclusion_state: Literal["core", "monitored", "discovery", "excluded"]
    require_sec: bool
    require_ir: bool
    require_earnings: bool
    blob_root: Path
    apply: bool = False
    recorded_at: datetime

    @field_validator("ticker", "domicile_country")
    @classmethod
    def _upper_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("recorded_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def _closed_bundle(self) -> Self:
        if self.issuer_id.startswith("legacy-ticker:"):
            raise ValueError("canonical issuer ID cannot be a recorded ticker")
        source_keys = [source.source_key for source in self.sources]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("source keys must be unique")
        security_ids = [security.security_id for security in self.securities]
        if len(security_ids) != len(set(security_ids)):
            raise ValueError("security IDs must be unique")
        if self.subject_security_id not in set(security_ids):
            raise ValueError("subject security must belong to the bundle issuer")
        referenced = {
            self.profile_source_key,
            *(claim.source_key for claim in self.issuer_identifiers),
            *(claim.source_key for claim in self.reporting_identifiers),
            *(security.source_key for security in self.securities),
            *(claim.source_key for security in self.securities for claim in security.identifiers),
            *(listing.source_key for security in self.securities for listing in security.listings),
            *(surface.source_key for surface in self.authority_surfaces),
            *(obligation.source_key for obligation in self.obligations),
        }
        missing = referenced - set(source_keys)
        if missing:
            raise ValueError(f"bundle references unknown source keys: {sorted(missing)}")
        return self


class SourceCapture(_ClosedModel):
    source_key: str
    source_url: str
    sha256: str
    observation_id: str | None


class ForeignIssuerBootstrapResult(_ClosedModel):
    mode: Literal["dry_run", "apply"]
    ticker: str
    canonical_issuer_id: str
    reporting_entity_id: str
    subject_security_id: str
    sources: tuple[SourceCapture, ...]
    records_created: int = Field(ge=0)


def bootstrap_foreign_issuer(
    conn: sqlite3.Connection,
    *,
    request: ForeignIssuerBootstrapRequest,
) -> ForeignIssuerBootstrapResult:
    """Validate and optionally persist one closed foreign-issuer identity bundle."""

    request = ForeignIssuerBootstrapRequest.model_validate(request.model_dump())
    source_captures = tuple(
        SourceCapture(
            source_key=source.source_key,
            source_url=source.source_url,
            sha256=hashlib.sha256(source.raw_body).hexdigest(),
            observation_id=None,
        )
        for source in request.sources
    )
    if not request.apply:
        return _result(request, "dry_run", source_captures, 0)

    with conn:
        observations, captures, created = _capture_sources(conn, request)
        issuer_registry = IssuerRegistry(conn)
        reporting_registry = ReportingEntityRegistry(conn)
        created += _persist_issuer(
            conn,
            issuer_registry,
            request=request,
            observations=observations,
        )
        created += _persist_reporting_entity(
            conn,
            reporting_registry,
            request=request,
            observations=observations,
        )
        created += _persist_securities(
            conn,
            issuer_registry,
            reporting_registry,
            request=request,
            observations=observations,
        )
        created += _persist_surfaces(
            conn,
            issuer_registry,
            request=request,
            observations=observations,
        )
        created += _persist_scope_and_obligations(
            conn,
            issuer_registry,
            reporting_registry,
            request=request,
            observations=observations,
        )
        created += _persist_legacy_bindings(
            conn,
            issuer_registry,
            reporting_registry,
            request=request,
            observations=observations,
        )
    return _result(request, "apply", captures, created)


def _result(
    request: ForeignIssuerBootstrapRequest,
    mode: Literal["dry_run", "apply"],
    captures: tuple[SourceCapture, ...],
    created: int,
) -> ForeignIssuerBootstrapResult:
    return ForeignIssuerBootstrapResult(
        mode=mode,
        ticker=request.ticker,
        canonical_issuer_id=request.issuer_id,
        reporting_entity_id=request.reporting_entity_id,
        subject_security_id=request.subject_security_id,
        sources=captures,
        records_created=created,
    )


def _capture_sources(
    conn: sqlite3.Connection,
    request: ForeignIssuerBootstrapRequest,
) -> tuple[dict[str, str], tuple[SourceCapture, ...], int]:
    observations: dict[str, str] = {}
    captures: list[SourceCapture] = []
    created = 0
    for source in request.sources:
        digest = hashlib.sha256(source.raw_body).hexdigest()
        config_sha = _digest(
            "retrieval-config",
            json.dumps(
                {
                    "accept": source.media_type,
                    "source_url": source.source_url,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        observation_id = _record_id(
            "source-observation",
            source.source_url,
            digest,
            config_sha,
        )
        existing_observation = conn.execute(
            "SELECT observed_at FROM evidence_source_observations WHERE idempotency_key = ?",
            (observation_id,),
        ).fetchone()
        observed_at = (
            request.recorded_at
            if existing_observation is None
            else _parse_datetime(existing_observation[0])
        )
        path = request.blob_root / digest[:2] / digest
        _store_exact_bytes(path, source.raw_body, digest)
        storage_uri = path.resolve().as_uri()
        existing_blob = conn.execute(
            "SELECT byte_size FROM evidence_content_blobs WHERE sha256 = ?",
            (digest,),
        ).fetchone()
        if existing_blob is None:
            created += int(
                EvidenceLedger(conn)
                .persist(
                    ContentBlob(
                        sha256=digest,
                        byte_size=len(source.raw_body),
                        media_type=source.media_type,
                        storage_uri=storage_uri,
                        recorded_at=observed_at,
                    )
                )
                .created
            )
        elif int(existing_blob[0]) != len(source.raw_body):
            raise ValueError("existing evidence blob metadata conflicts")
        location_id = _record_id("blob-location", digest, storage_uri)
        created += int(
            EvidenceLinkLedger(conn)
            .persist_location(
                BlobLocationObservation(
                    location_observation_id=location_id,
                    idempotency_key=location_id,
                    blob_sha256=digest,
                    storage_uri=storage_uri,
                    location_kind="local",
                    availability_state="present",
                    location_sequence=1,
                    verified_at=observed_at,
                    verified_byte_size=len(source.raw_body),
                    verified_sha256=digest,
                    recorded_at=observed_at,
                )
            )
            .created
        )
        if existing_observation is None:
            created += int(
                EvidenceLedger(conn)
                .persist(
                    SourceObservation(
                        observation_id=observation_id,
                        idempotency_key=observation_id,
                        source_kind=source.source_kind,
                        source_url=source.source_url,
                        blob_sha256=digest,
                        source_published_at=None,
                        filing_at=None,
                        accepted_at=None,
                        observed_at=observed_at,
                        retrieved_at=observed_at,
                        retrieval_config_sha256=config_sha,
                        collector_code_version=_COLLECTOR_VERSION,
                    )
                )
                .created
            )
        observations[source.source_key] = observation_id
        captures.append(
            SourceCapture(
                source_key=source.source_key,
                source_url=source.source_url,
                sha256=digest,
                observation_id=observation_id,
            )
        )
    return observations, tuple(captures), created


def _persist_issuer(
    conn: sqlite3.Connection,
    registry: IssuerRegistry,
    *,
    request: ForeignIssuerBootstrapRequest,
    observations: dict[str, str],
) -> int:
    existing_entity = conn.execute(
        "SELECT entity_kind FROM issuer_entities WHERE issuer_id = ?",
        (request.issuer_id,),
    ).fetchone()
    if existing_entity is not None and str(existing_entity[0]) != "operating_company":
        raise ValueError("foreign issuer ID conflicts with existing entity kind")
    created = 0
    if existing_entity is None:
        created += int(
            registry.persist(
                IssuerEntity(
                    issuer_id=request.issuer_id,
                    idempotency_key=f"issuer-entity:{request.issuer_id}",
                    entity_kind="operating_company",
                    created_at=request.recorded_at,
                )
            ).created
        )
    source_observation_id = observations[request.profile_source_key]
    current = conn.execute(
        "SELECT profile_revision_id, revision, legal_name, domicile_country, "
        "filing_regime, status FROM issuer_profile_revisions "
        "WHERE issuer_id = ? ORDER BY revision DESC LIMIT 1",
        (request.issuer_id,),
    ).fetchone()
    semantics = (
        request.legal_name,
        request.domicile_country,
        request.filing_regime,
        "active",
    )
    if current is None or tuple(str(value) for value in current[2:]) != semantics:
        revision = 1 if current is None else int(current[1]) + 1
        record_id = _record_id(
            "issuer-profile",
            request.issuer_id,
            source_observation_id,
            str(revision),
        )
        created += int(
            registry.persist(
                IssuerProfileRevision(
                    profile_revision_id=record_id,
                    idempotency_key=record_id,
                    issuer_id=request.issuer_id,
                    revision=revision,
                    legal_name=request.legal_name,
                    domicile_country=request.domicile_country,
                    filing_regime=request.filing_regime,
                    fiscal_year_end=None,
                    status="active",
                    decision_kind="imported",
                    reason_code="analyst_reviewed_foreign_identity_bundle",
                    reason_details=(("source_observation_id", source_observation_id),),
                    effective_at=request.recorded_at,
                    knowledge_at=request.recorded_at,
                    recorded_at=request.recorded_at,
                    supersedes_profile_revision_id=(None if current is None else str(current[0])),
                )
            ).created
        )
    for claim in request.issuer_identifiers:
        normalized_value = normalize_identifier(
            claim.identifier_type,
            claim.identifier_value,
        )
        existing_assertion = conn.execute(
            "SELECT assertion_id FROM issuer_identifier_assertions "
            "WHERE issuer_id = ? AND identifier_type = ? "
            "AND normalized_value = ? AND authority = ? "
            "ORDER BY recorded_at LIMIT 1",
            (
                request.issuer_id,
                claim.identifier_type,
                normalized_value,
                claim.authority,
            ),
        ).fetchone()
        if existing_assertion is not None:
            continue
        assertion = IdentifierAssertion(
            assertion_id=_record_id(
                "issuer-identifier-assertion",
                request.issuer_id,
                claim.identifier_type,
                claim.identifier_value,
                observations[claim.source_key],
            ),
            idempotency_key=_record_id(
                "issuer-identifier-assertion",
                request.issuer_id,
                claim.identifier_type,
                claim.identifier_value,
                observations[claim.source_key],
            ),
            issuer_id=request.issuer_id,
            identifier_type=claim.identifier_type,
            identifier_value=claim.identifier_value,
            normalized_value=normalized_value,
            authority=claim.authority,
            source_observation_id=observations[claim.source_key],
            effective_at=request.recorded_at,
            knowledge_at=request.recorded_at,
            recorded_at=request.recorded_at,
        )
        created += int(registry.persist(assertion).created)
        created += _resolve_issuer_identifier(conn, registry, assertion, request.recorded_at)
    return created


def _persist_reporting_entity(
    conn: sqlite3.Connection,
    registry: ReportingEntityRegistry,
    *,
    request: ForeignIssuerBootstrapRequest,
    observations: dict[str, str],
) -> int:
    existing_entity = conn.execute(
        "SELECT issuer_id, reporting_entity_kind, display_name "
        "FROM reporting_entities WHERE reporting_entity_id = ?",
        (request.reporting_entity_id,),
    ).fetchone()
    expected_entity = (
        request.issuer_id,
        request.reporting_entity_kind,
        request.reporting_entity_display_name,
    )
    if (
        existing_entity is not None
        and tuple(str(value) for value in existing_entity) != expected_entity
    ):
        raise ValueError("reporting entity ID conflicts with immutable identity")
    created = 0
    if existing_entity is None:
        created += int(
            registry.persist(
                ReportingEntity(
                    reporting_entity_id=request.reporting_entity_id,
                    idempotency_key=request.reporting_entity_id,
                    issuer_id=request.issuer_id,
                    reporting_entity_kind=request.reporting_entity_kind,
                    display_name=request.reporting_entity_display_name,
                    created_at=request.recorded_at,
                )
            ).created
        )
    for claim in request.reporting_identifiers:
        observation_id = observations[claim.source_key]
        normalized_value = normalize_reporting_identifier(
            claim.identifier_type,
            claim.identifier_value,
        )
        existing_assertion = conn.execute(
            "SELECT assertion_id FROM reporting_entity_identifier_assertions "
            "WHERE reporting_entity_id = ? AND identifier_type = ? "
            "AND normalized_value = ? AND authority = ? "
            "ORDER BY recorded_at LIMIT 1",
            (
                request.reporting_entity_id,
                claim.identifier_type,
                normalized_value,
                claim.authority,
            ),
        ).fetchone()
        if existing_assertion is not None:
            continue
        assertion_id = _record_id(
            "reporting-identifier-assertion",
            request.reporting_entity_id,
            claim.identifier_type,
            claim.identifier_value,
            observation_id,
        )
        assertion = ReportingEntityIdentifierAssertion(
            assertion_id=assertion_id,
            idempotency_key=assertion_id,
            reporting_entity_id=request.reporting_entity_id,
            identifier_type=claim.identifier_type,
            identifier_value=claim.identifier_value,
            normalized_value=normalized_value,
            authority=claim.authority,
            source_observation_id=observation_id,
            effective_at=request.recorded_at,
            knowledge_at=request.recorded_at,
            recorded_at=request.recorded_at,
        )
        created += int(registry.persist(assertion).created)
        created += _resolve_reporting_identifier(
            conn,
            registry,
            assertion,
            request.recorded_at,
        )
    return created


def _persist_securities(
    conn: sqlite3.Connection,
    issuer_registry: IssuerRegistry,
    reporting_registry: ReportingEntityRegistry,
    *,
    request: ForeignIssuerBootstrapRequest,
    observations: dict[str, str],
) -> int:
    created = 0
    for security in request.securities:
        existing_security = conn.execute(
            "SELECT issuer_id, security_kind, share_class FROM securities WHERE security_id = ?",
            (security.security_id,),
        ).fetchone()
        expected_security = (
            request.issuer_id,
            security.security_kind,
            security.share_class,
        )
        if (
            existing_security is not None
            and (
                str(existing_security[0]),
                str(existing_security[1]),
                None if existing_security[2] is None else str(existing_security[2]),
            )
            != expected_security
        ):
            raise ValueError("security ID conflicts with immutable identity")
        if existing_security is None:
            created += int(
                issuer_registry.persist(
                    Security(
                        security_id=security.security_id,
                        idempotency_key=security.security_id,
                        issuer_id=request.issuer_id,
                        security_kind=security.security_kind,
                        share_class=security.share_class,
                        created_at=request.recorded_at,
                    )
                ).created
            )
        for claim in security.identifiers:
            observation_id = observations[claim.source_key]
            normalized_value = normalize_security_identifier(
                claim.identifier_type,
                claim.identifier_value,
            )
            existing_assertion = conn.execute(
                "SELECT assertion_id FROM security_identifier_assertions "
                "WHERE security_id = ? AND identifier_type = ? "
                "AND normalized_value = ? AND authority = ? "
                "ORDER BY recorded_at LIMIT 1",
                (
                    security.security_id,
                    claim.identifier_type,
                    normalized_value,
                    claim.authority,
                ),
            ).fetchone()
            if existing_assertion is not None:
                continue
            assertion_id = _record_id(
                "security-identifier-assertion",
                security.security_id,
                claim.identifier_type,
                claim.identifier_value,
                observation_id,
            )
            assertion = SecurityIdentifierAssertion(
                assertion_id=assertion_id,
                idempotency_key=assertion_id,
                security_id=security.security_id,
                identifier_type=claim.identifier_type,
                identifier_value=claim.identifier_value,
                normalized_value=normalized_value,
                authority=claim.authority,
                source_observation_id=observation_id,
                effective_at=request.recorded_at,
                knowledge_at=request.recorded_at,
                recorded_at=request.recorded_at,
            )
            created += int(reporting_registry.persist(assertion).created)
            created += _resolve_security_identifier(
                conn,
                reporting_registry,
                assertion,
                request.recorded_at,
            )
        for listing in security.listings:
            existing_listing = conn.execute(
                "SELECT assertion_id FROM security_listing_assertions "
                "WHERE security_id = ? AND market_mic = ? "
                "AND normalized_ticker = ? AND currency = ? "
                "AND status = 'listed' AND authority = ? "
                "ORDER BY recorded_at LIMIT 1",
                (
                    security.security_id,
                    listing.market_mic,
                    listing.ticker.upper(),
                    listing.currency,
                    listing.authority,
                ),
            ).fetchone()
            if existing_listing is not None:
                continue
            assertion_id = _record_id(
                "listing-assertion",
                security.security_id,
                listing.market_mic,
                listing.ticker,
                observations[listing.source_key],
            )
            assertion = ListingAssertion(
                assertion_id=assertion_id,
                idempotency_key=assertion_id,
                security_id=security.security_id,
                market_mic=listing.market_mic,
                ticker=listing.ticker,
                normalized_ticker=listing.ticker.upper(),
                currency=listing.currency,
                status="listed",
                authority=listing.authority,
                source_observation_id=observations[listing.source_key],
                effective_at=request.recorded_at,
                knowledge_at=request.recorded_at,
                recorded_at=request.recorded_at,
            )
            created += int(issuer_registry.persist(assertion).created)
            created += _resolve_listing(
                conn,
                issuer_registry,
                assertion,
                request.recorded_at,
            )
        created += _persist_security_relationship(
            conn,
            reporting_registry,
            request=request,
            security=security,
            observation_id=observations[security.source_key],
        )
    return created


def _persist_security_relationship(
    conn: sqlite3.Connection,
    registry: ReportingEntityRegistry,
    *,
    request: ForeignIssuerBootstrapRequest,
    security: SecurityClaim,
    observation_id: str,
) -> int:
    relationship_key = f"{security.security_id}:reporting-entity"
    current = conn.execute(
        "SELECT relationship_revision_id, revision, reporting_entity_id, "
        "relationship_kind FROM security_reporting_entity_revisions "
        "WHERE relationship_key = ? ORDER BY revision DESC LIMIT 1",
        (relationship_key,),
    ).fetchone()
    semantics = (request.reporting_entity_id, security.relationship_kind)
    if current is not None and tuple(str(value) for value in current[2:]) == semantics:
        return 0
    revision = 1 if current is None else int(current[1]) + 1
    record_id = _record_id(
        "security-reporting-relationship",
        relationship_key,
        request.reporting_entity_id,
        security.relationship_kind,
        str(revision),
    )
    return int(
        registry.persist(
            SecurityReportingEntityRevision(
                relationship_revision_id=record_id,
                idempotency_key=record_id,
                relationship_key=relationship_key,
                revision=revision,
                security_id=security.security_id,
                reporting_entity_id=request.reporting_entity_id,
                relationship_kind=security.relationship_kind,
                decision_kind="imported",
                reason_code="analyst_reviewed_foreign_identity_bundle",
                reason_details=(("source_observation_id", observation_id),),
                effective_at=request.recorded_at,
                knowledge_at=request.recorded_at,
                recorded_at=request.recorded_at,
                supersedes_relationship_revision_id=(None if current is None else str(current[0])),
            )
        ).created
    )


def _persist_surfaces(
    conn: sqlite3.Connection,
    registry: IssuerRegistry,
    *,
    request: ForeignIssuerBootstrapRequest,
    observations: dict[str, str],
) -> int:
    created = 0
    for surface in request.authority_surfaces:
        current = conn.execute(
            "SELECT surface_revision_id, revision, source_url, status, "
            "authority_level, source_observation_id "
            "FROM issuer_authority_surface_revisions "
            "WHERE issuer_id = ? AND surface_key = ? "
            "ORDER BY revision DESC LIMIT 1",
            (request.issuer_id, surface.surface_key),
        ).fetchone()
        observation_id = observations[surface.source_key]
        semantics = (surface.source_url, "verified", surface.authority_level)
        if current is not None and tuple(str(value) for value in current[2:5]) == semantics:
            continue
        revision = 1 if current is None else int(current[1]) + 1
        record_id = _record_id(
            "authority-surface",
            request.issuer_id,
            surface.surface_key,
            observation_id,
            str(revision),
        )
        created += int(
            registry.persist(
                AuthoritySurfaceRevision(
                    surface_revision_id=record_id,
                    idempotency_key=record_id,
                    issuer_id=request.issuer_id,
                    surface_key=surface.surface_key,
                    revision=revision,
                    surface_kind=surface.surface_kind,
                    source_url=surface.source_url,
                    status="verified",
                    authority_level=surface.authority_level,
                    source_observation_id=observation_id,
                    verification_method="analyst_reviewed_authority_bundle",
                    effective_at=request.recorded_at,
                    knowledge_at=request.recorded_at,
                    recorded_at=request.recorded_at,
                    supersedes_surface_revision_id=(None if current is None else str(current[0])),
                )
            ).created
        )
    return created


def _persist_scope_and_obligations(
    conn: sqlite3.Connection,
    issuer_registry: IssuerRegistry,
    reporting_registry: ReportingEntityRegistry,
    *,
    request: ForeignIssuerBootstrapRequest,
    observations: dict[str, str],
) -> int:
    current = conn.execute(
        "SELECT scope_revision_id, revision, inclusion_state, history_policy, "
        "require_sec, require_ir, require_earnings "
        "FROM issuer_reporting_scope_revisions "
        "WHERE scope_key = 'investor-research' AND issuer_id = ? "
        "ORDER BY revision DESC LIMIT 1",
        (request.issuer_id,),
    ).fetchone()
    semantics = (
        request.inclusion_state,
        "all_available",
        int(request.require_sec),
        int(request.require_ir),
        int(request.require_earnings),
    )
    created = 0
    if current is None or tuple(current[2:]) != semantics:
        revision = 1 if current is None else int(current[1]) + 1
        record_id = _record_id(
            "reporting-scope",
            request.issuer_id,
            request.inclusion_state,
            str(revision),
        )
        created += int(
            issuer_registry.persist(
                ReportingScopeRevision(
                    scope_revision_id=record_id,
                    idempotency_key=record_id,
                    scope_key="investor-research",
                    issuer_id=request.issuer_id,
                    revision=revision,
                    inclusion_state=request.inclusion_state,
                    history_policy="all_available",
                    history_start=None,
                    latest_years=None,
                    require_sec=request.require_sec,
                    require_ir=request.require_ir,
                    require_earnings=request.require_earnings,
                    decision_kind="imported",
                    reason_code="tracked_foreign_issuer_scope",
                    reason_details=(("recorded_ticker", request.ticker),),
                    effective_at=request.recorded_at,
                    knowledge_at=request.recorded_at,
                    recorded_at=request.recorded_at,
                    supersedes_scope_revision_id=(None if current is None else str(current[0])),
                )
            ).created
        )
    for obligation in request.obligations:
        obligation_key = (
            f"{request.reporting_entity_id}:"
            f"{obligation.authority_kind}:{obligation.document_family}"
        )
        current_obligation = conn.execute(
            "SELECT obligation_revision_id, revision, obligation_state, "
            "completeness_rule FROM source_obligation_revisions "
            "WHERE obligation_key = ? ORDER BY revision DESC LIMIT 1",
            (obligation_key,),
        ).fetchone()
        obligation_semantics = (
            obligation.obligation_state,
            obligation.completeness_rule,
        )
        if (
            current_obligation is not None
            and tuple(str(value) for value in current_obligation[2:]) == obligation_semantics
        ):
            continue
        revision = 1 if current_obligation is None else int(current_obligation[1]) + 1
        observation_id = observations[obligation.source_key]
        record_id = _record_id(
            "source-obligation",
            obligation_key,
            observation_id,
            str(revision),
        )
        created += int(
            reporting_registry.persist(
                SourceObligationRevision(
                    obligation_revision_id=record_id,
                    idempotency_key=record_id,
                    obligation_key=obligation_key,
                    revision=revision,
                    issuer_id=request.issuer_id,
                    reporting_entity_id=request.reporting_entity_id,
                    authority_kind=obligation.authority_kind,
                    document_family=obligation.document_family,
                    obligation_state=obligation.obligation_state,
                    completeness_rule=obligation.completeness_rule,
                    active_from=request.recorded_at,
                    active_to=None,
                    decision_kind="imported",
                    reason_code="analyst_reviewed_foreign_source_duty",
                    reason_details=(("source_observation_id", observation_id),),
                    effective_at=request.recorded_at,
                    knowledge_at=request.recorded_at,
                    recorded_at=request.recorded_at,
                    supersedes_obligation_revision_id=(
                        None if current_obligation is None else str(current_obligation[0])
                    ),
                )
            ).created
        )
    return created


def _persist_legacy_bindings(
    conn: sqlite3.Connection,
    issuer_registry: IssuerRegistry,
    reporting_registry: ReportingEntityRegistry,
    *,
    request: ForeignIssuerBootstrapRequest,
    observations: dict[str, str],
) -> int:
    recorded_issuer_id = f"legacy-ticker:{request.ticker}"
    observation_id = observations[request.profile_source_key]
    current = conn.execute(
        "SELECT binding_revision_id, revision, issuer_id, outcome "
        "FROM legacy_issuer_binding_revisions WHERE recorded_issuer_id = ? "
        "ORDER BY revision DESC LIMIT 1",
        (recorded_issuer_id,),
    ).fetchone()
    created = 0
    if current is None or (
        None if current[2] is None else str(current[2]),
        str(current[3]),
    ) != (request.issuer_id, "selected"):
        revision = 1 if current is None else int(current[1]) + 1
        record_id = _record_id(
            "legacy-binding",
            recorded_issuer_id,
            request.issuer_id,
            observation_id,
            str(revision),
        )
        created += int(
            issuer_registry.persist(
                LegacyIssuerBindingRevision(
                    binding_revision_id=record_id,
                    idempotency_key=record_id,
                    recorded_issuer_id=recorded_issuer_id,
                    revision=revision,
                    issuer_id=request.issuer_id,
                    outcome="selected",
                    decision_kind="imported",
                    reason_code="analyst_reviewed_foreign_identity_bundle",
                    reason_details=(("source_observation_id", observation_id),),
                    material_dissent=False,
                    effective_at=request.recorded_at,
                    knowledge_at=request.recorded_at,
                    recorded_at=request.recorded_at,
                    supersedes_binding_revision_id=(None if current is None else str(current[0])),
                )
            ).created
        )
    current_subject = conn.execute(
        "SELECT binding_revision_id, revision, issuer_id, reporting_entity_id, "
        "security_id, outcome FROM recorded_subject_binding_revisions "
        "WHERE recorded_issuer_id = ? ORDER BY revision DESC LIMIT 1",
        (recorded_issuer_id,),
    ).fetchone()
    subject_semantics = (
        request.issuer_id,
        request.reporting_entity_id,
        request.subject_security_id,
        "selected",
    )
    if (
        current_subject is not None
        and tuple(str(value) for value in current_subject[2:]) == subject_semantics
    ):
        return created
    revision = 1 if current_subject is None else int(current_subject[1]) + 1
    subject_id = _record_id(
        "recorded-subject-binding",
        recorded_issuer_id,
        request.issuer_id,
        request.reporting_entity_id,
        request.subject_security_id,
        observation_id,
        str(revision),
    )
    created += int(
        reporting_registry.persist(
            EvidenceSubjectBindingRevision(
                binding_revision_id=subject_id,
                idempotency_key=subject_id,
                recorded_issuer_id=recorded_issuer_id,
                revision=revision,
                issuer_id=request.issuer_id,
                reporting_entity_id=request.reporting_entity_id,
                security_id=request.subject_security_id,
                outcome="selected",
                decision_kind="imported",
                reason_code="analyst_reviewed_foreign_identity_bundle",
                reason_details=(("source_observation_id", observation_id),),
                material_dissent=False,
                effective_at=request.recorded_at,
                knowledge_at=request.recorded_at,
                recorded_at=request.recorded_at,
                supersedes_binding_revision_id=(
                    None if current_subject is None else str(current_subject[0])
                ),
            )
        ).created
    )
    return created


def _resolve_issuer_identifier(
    conn: sqlite3.Connection,
    registry: IssuerRegistry,
    assertion: IdentifierAssertion,
    recorded_at: datetime,
) -> int:
    rows = conn.execute(
        "SELECT assertion_id, idempotency_key, issuer_id, identifier_value, "
        "normalized_value, authority, source_observation_id, effective_at, "
        "knowledge_at, recorded_at FROM issuer_identifier_assertions "
        "WHERE identifier_type = ? AND normalized_value = ? ORDER BY assertion_id",
        (assertion.identifier_type, assertion.normalized_value),
    ).fetchall()
    assertions = tuple(
        IdentifierAssertion(
            assertion_id=str(row[0]),
            idempotency_key=str(row[1]),
            issuer_id=str(row[2]),
            identifier_type=assertion.identifier_type,
            identifier_value=str(row[3]),
            normalized_value=str(row[4]),
            authority=cast(IdentifierAuthority, str(row[5])),
            source_observation_id=None if row[6] is None else str(row[6]),
            effective_at=_parse_datetime(row[7]),
            knowledge_at=_parse_datetime(row[8]),
            recorded_at=_parse_datetime(row[9]),
        )
        for row in rows
    )
    if {item.issuer_id for item in assertions} != {assertion.issuer_id}:
        raise ValueError("foreign issuer identifier has competing canonical owners")
    digest = identifier_candidate_digest(assertions)
    return _persist_resolution(
        conn,
        registry,
        assertion=assertion,
        candidate_digest=digest,
        recorded_at=recorded_at,
    )


def _persist_resolution(
    conn: sqlite3.Connection,
    registry: IssuerRegistry,
    *,
    assertion: IdentifierAssertion,
    candidate_digest: str,
    recorded_at: datetime,
) -> int:
    current = conn.execute(
        "SELECT resolution_id, revision, candidate_digest_sha256, "
        "selected_assertion_id FROM issuer_identifier_resolution_outcomes "
        "WHERE resolution_key = ? ORDER BY revision DESC LIMIT 1",
        (assertion.resolution_key,),
    ).fetchone()
    if current is not None and (
        str(current[2]),
        str(current[3]),
    ) == (candidate_digest, assertion.assertion_id):
        return 0
    revision = 1 if current is None else int(current[1]) + 1
    record_id = _record_id(
        "issuer-identifier-resolution",
        assertion.resolution_key,
        candidate_digest,
        str(revision),
    )
    return int(
        registry.persist(
            IdentifierResolution(
                resolution_id=record_id,
                idempotency_key=record_id,
                resolution_key=assertion.resolution_key,
                revision=revision,
                outcome="selected",
                selected_assertion_id=assertion.assertion_id,
                candidate_digest_sha256=candidate_digest,
                policy_name=_POLICY_NAME,
                policy_version=_POLICY_VERSION,
                policy_config_sha256=_policy_sha("issuer_identifier"),
                reason_code="analyst_reviewed_authority_identifier",
                reason_details=(("selected_assertion_id", assertion.assertion_id),),
                material_dissent=False,
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_resolution_id=None if current is None else str(current[0]),
            )
        ).created
    )


def _resolve_reporting_identifier(
    conn: sqlite3.Connection,
    registry: ReportingEntityRegistry,
    assertion: ReportingEntityIdentifierAssertion,
    recorded_at: datetime,
) -> int:
    rows = conn.execute(
        "SELECT assertion_id, reporting_entity_id FROM "
        "reporting_entity_identifier_assertions "
        "WHERE identifier_type = ? AND normalized_value = ?",
        (assertion.identifier_type, assertion.normalized_value),
    ).fetchall()
    if {str(row[1]) for row in rows} != {assertion.reporting_entity_id}:
        raise ValueError("reporting identifier has competing canonical owners")
    digest = reporting_identifier_candidate_digest((assertion,))
    current = conn.execute(
        "SELECT resolution_id, revision, candidate_digest_sha256, "
        "selected_assertion_id FROM "
        "reporting_entity_identifier_resolution_outcomes "
        "WHERE resolution_key = ? ORDER BY revision DESC LIMIT 1",
        (assertion.resolution_key,),
    ).fetchone()
    if current is not None and (
        str(current[2]),
        str(current[3]),
    ) == (digest, assertion.assertion_id):
        return 0
    if len(rows) != 1:
        raise ValueError("reporting identifier candidate set changed")
    revision = 1 if current is None else int(current[1]) + 1
    record_id = _record_id(
        "reporting-identifier-resolution",
        assertion.resolution_key,
        digest,
        str(revision),
    )
    return int(
        registry.persist(
            ReportingEntityIdentifierResolution(
                resolution_id=record_id,
                idempotency_key=record_id,
                resolution_key=assertion.resolution_key,
                revision=revision,
                outcome="selected",
                selected_assertion_id=assertion.assertion_id,
                candidate_digest_sha256=digest,
                policy_name=_POLICY_NAME,
                policy_version=_POLICY_VERSION,
                policy_config_sha256=_policy_sha("reporting_identifier"),
                reason_code="analyst_reviewed_authority_identifier",
                reason_details=(("selected_assertion_id", assertion.assertion_id),),
                material_dissent=False,
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_resolution_id=None if current is None else str(current[0]),
            )
        ).created
    )


def _resolve_security_identifier(
    conn: sqlite3.Connection,
    registry: ReportingEntityRegistry,
    assertion: SecurityIdentifierAssertion,
    recorded_at: datetime,
) -> int:
    rows = conn.execute(
        "SELECT assertion_id, security_id FROM security_identifier_assertions "
        "WHERE identifier_type = ? AND normalized_value = ?",
        (assertion.identifier_type, assertion.normalized_value),
    ).fetchall()
    if {str(row[1]) for row in rows} != {assertion.security_id}:
        raise ValueError("security identifier has competing canonical owners")
    digest = security_identifier_candidate_digest((assertion,))
    current = conn.execute(
        "SELECT resolution_id, revision, candidate_digest_sha256, "
        "selected_assertion_id FROM security_identifier_resolution_outcomes "
        "WHERE resolution_key = ? ORDER BY revision DESC LIMIT 1",
        (assertion.resolution_key,),
    ).fetchone()
    if current is not None and (
        str(current[2]),
        str(current[3]),
    ) == (digest, assertion.assertion_id):
        return 0
    if len(rows) != 1:
        raise ValueError("security identifier candidate set changed")
    revision = 1 if current is None else int(current[1]) + 1
    record_id = _record_id(
        "security-identifier-resolution",
        assertion.resolution_key,
        digest,
        str(revision),
    )
    return int(
        registry.persist(
            SecurityIdentifierResolution(
                resolution_id=record_id,
                idempotency_key=record_id,
                resolution_key=assertion.resolution_key,
                revision=revision,
                outcome="selected",
                selected_assertion_id=assertion.assertion_id,
                candidate_digest_sha256=digest,
                policy_name=_POLICY_NAME,
                policy_version=_POLICY_VERSION,
                policy_config_sha256=_policy_sha("security_identifier"),
                reason_code="analyst_reviewed_authority_identifier",
                reason_details=(("selected_assertion_id", assertion.assertion_id),),
                material_dissent=False,
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_resolution_id=None if current is None else str(current[0]),
            )
        ).created
    )


def _resolve_listing(
    conn: sqlite3.Connection,
    registry: IssuerRegistry,
    assertion: ListingAssertion,
    recorded_at: datetime,
) -> int:
    rows = conn.execute(
        "SELECT assertion_id, security_id FROM security_listing_assertions "
        "WHERE market_mic = ? AND normalized_ticker = ?",
        (assertion.market_mic, assertion.normalized_ticker),
    ).fetchall()
    if {str(row[1]) for row in rows} != {assertion.security_id}:
        raise ValueError("listing has competing canonical security owners")
    digest = listing_candidate_digest((assertion,))
    current = conn.execute(
        "SELECT resolution_id, revision, candidate_digest_sha256, "
        "selected_assertion_id FROM security_listing_resolution_outcomes "
        "WHERE resolution_key = ? ORDER BY revision DESC LIMIT 1",
        (assertion.resolution_key,),
    ).fetchone()
    if current is not None and (
        str(current[2]),
        str(current[3]),
    ) == (digest, assertion.assertion_id):
        return 0
    if len(rows) != 1:
        raise ValueError("listing candidate set changed")
    revision = 1 if current is None else int(current[1]) + 1
    record_id = _record_id(
        "listing-resolution",
        assertion.resolution_key,
        digest,
        str(revision),
    )
    return int(
        registry.persist(
            ListingResolution(
                resolution_id=record_id,
                idempotency_key=record_id,
                resolution_key=assertion.resolution_key,
                revision=revision,
                outcome="selected",
                selected_assertion_id=assertion.assertion_id,
                candidate_digest_sha256=digest,
                policy_name=_POLICY_NAME,
                policy_version=_POLICY_VERSION,
                policy_config_sha256=_policy_sha("security_listing"),
                reason_code="analyst_reviewed_authority_listing",
                reason_details=(("selected_assertion_id", assertion.assertion_id),),
                material_dissent=False,
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_resolution_id=None if current is None else str(current[0]),
            )
        ).created
    )


def _store_exact_bytes(path: Path, raw_body: bytes, digest: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError("existing foreign-issuer evidence blob fails hash verification")
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(raw_body)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _record_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:" + hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _digest(namespace: str, payload: str) -> str:
    return hashlib.sha256(f"{namespace}\0{payload}".encode()).hexdigest()


def _policy_sha(purpose: str) -> str:
    return _digest(
        "foreign-issuer-policy",
        json.dumps(
            {
                "policy": _POLICY_NAME,
                "purpose": purpose,
                "version": _POLICY_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _parse_datetime(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
