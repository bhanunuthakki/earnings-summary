"""Exact-byte verification of an issuer's official IR homepage.

An official homepage is an authority locator, not proof that every publisher
archive was exhausted.  This module records that narrower fact without
creating a source-inventory seal or pretending a bounded crawl is complete.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from urllib.parse import parse_qsl, urlparse

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.evidence_links import BlobLocationObservation, EvidenceLinkLedger
from provenance.issuer_registry import (
    AuthoritySurfaceRevision,
    IssuerRegistry,
    UnresolvedIssuerIdentityError,
)

_COLLECTOR = "ir-home-authority@1"
_CREDENTIAL_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth_token",
        "password",
        "secret",
        "token",
    }
)


class IRHomeAuthorityError(RuntimeError):
    """The publisher homepage could not be verified under the closed contract."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _https_url(value: str) -> str:
    parsed = urlparse(value)
    query_keys = {key.strip().lower().replace("-", "_") for key, _value in parse_qsl(parsed.query)}
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or query_keys & _CREDENTIAL_QUERY_KEYS
    ):
        raise ValueError("IR authority URL must be uncredentialed HTTPS")
    return value


class IRHomeAuthorityRequest(_ClosedModel):
    issuer_id: str = Field(min_length=1, max_length=128)
    ticker: str = Field(min_length=1, max_length=16)
    requested_url: str = Field(min_length=1)
    final_url: str = Field(min_length=1)
    raw_body: bytes = Field(min_length=1)
    media_type: Literal["text/html", "application/xhtml+xml"]
    required_marker_groups: tuple[tuple[str, ...], ...] = Field(min_length=1)
    verification_method: str = Field(min_length=1, max_length=128)
    blob_root: Path
    apply: bool = False
    recorded_at: datetime

    _requested_url = field_validator("requested_url")(_https_url)
    _final_url = field_validator("final_url")(_https_url)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("ticker must be non-empty")
        return normalized

    @field_validator("recorded_at")
    @classmethod
    def _recorded_at(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def _marker_contract(self) -> Self:
        if any(
            not group or any(not marker.strip() for marker in group)
            for group in self.required_marker_groups
        ):
            raise ValueError("marker groups require non-empty alternatives")
        return self


class IRHomeAuthorityResult(_ClosedModel):
    mode: Literal["dry_run", "apply"]
    issuer_id: str
    ticker: str
    source_url: str
    raw_sha256: str
    source_observation_id: str | None
    records_created: int = Field(ge=0)


def verify_ir_home_authority(
    conn: sqlite3.Connection,
    *,
    request: IRHomeAuthorityRequest,
) -> IRHomeAuthorityResult:
    """Validate publisher identity markers and optionally persist the homepage."""

    request = IRHomeAuthorityRequest.model_validate(request.model_dump())
    _validate_identity(conn, request)
    _validate_markers(request)
    digest = hashlib.sha256(request.raw_body).hexdigest()
    config_sha = _config_sha(request)
    observation_id = _record_id(
        "ir-home-observation",
        request.final_url,
        digest,
        config_sha,
    )
    if not request.apply:
        return _result(request, "dry_run", digest, None, 0)

    with conn:
        created, evidence_at = _capture_source(
            conn,
            request=request,
            digest=digest,
            config_sha=config_sha,
            observation_id=observation_id,
        )
        created += _persist_surface(
            conn,
            request=request,
            observation_id=observation_id,
            recorded_at=evidence_at,
        )
    return _result(request, "apply", digest, observation_id, created)


def _validate_identity(
    conn: sqlite3.Connection,
    request: IRHomeAuthorityRequest,
) -> None:
    try:
        canonical = IssuerRegistry(conn).canonicalize_recorded_issuer(
            f"legacy-ticker:{request.ticker}",
            knowledge_at=request.recorded_at,
        )
    except UnresolvedIssuerIdentityError:
        raise IRHomeAuthorityError("ticker has no canonical issuer binding") from None
    if canonical.issuer_id != request.issuer_id:
        raise IRHomeAuthorityError("requested issuer does not match the ticker's canonical issuer")


def _validate_markers(request: IRHomeAuthorityRequest) -> None:
    visible_text = " ".join(
        BeautifulSoup(request.raw_body, "html.parser").get_text(" ", strip=True).split()
    )
    folded = visible_text.casefold()
    missing_groups = tuple(
        group
        for group in request.required_marker_groups
        if not any(marker.casefold() in folded for marker in group)
    )
    if missing_groups:
        raise IRHomeAuthorityError(f"publisher identity marker contract failed: {missing_groups}")


def _capture_source(
    conn: sqlite3.Connection,
    *,
    request: IRHomeAuthorityRequest,
    digest: str,
    config_sha: str,
    observation_id: str,
) -> tuple[int, datetime]:
    existing_observation = conn.execute(
        "SELECT observed_at FROM evidence_source_observations WHERE observation_id = ?",
        (observation_id,),
    ).fetchone()
    evidence_at = (
        request.recorded_at
        if existing_observation is None
        else _parse_datetime(existing_observation[0])
    )
    target = request.blob_root / digest[:2] / digest
    _store_exact_bytes(target, request.raw_body, digest)
    storage_uri = target.resolve().as_uri()
    created = 0
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
                    byte_size=len(request.raw_body),
                    media_type=request.media_type,
                    storage_uri=storage_uri,
                    recorded_at=evidence_at,
                )
            )
            .created
        )
    elif int(existing_blob[0]) != len(request.raw_body):
        raise IRHomeAuthorityError("existing evidence blob metadata conflicts")
    location_id = _record_id("ir-home-location", digest, storage_uri)
    existing_location = conn.execute(
        "SELECT 1 FROM evidence_blob_location_observations WHERE location_observation_id = ?",
        (location_id,),
    ).fetchone()
    if existing_location is None:
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
                    verified_at=evidence_at,
                    verified_byte_size=len(request.raw_body),
                    verified_sha256=digest,
                    recorded_at=evidence_at,
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
                    source_kind="ir_publisher_home_authority",
                    source_url=request.final_url,
                    blob_sha256=digest,
                    source_published_at=None,
                    filing_at=None,
                    accepted_at=None,
                    observed_at=evidence_at,
                    retrieved_at=evidence_at,
                    retrieval_config_sha256=config_sha,
                    collector_code_version=_COLLECTOR,
                )
            )
            .created
        )
    return created, evidence_at


def _persist_surface(
    conn: sqlite3.Connection,
    *,
    request: IRHomeAuthorityRequest,
    observation_id: str,
    recorded_at: datetime,
) -> int:
    current = conn.execute(
        "SELECT surface_revision_id, revision, source_url, status, "
        "authority_level FROM issuer_authority_surface_revisions "
        "WHERE issuer_id = ? AND surface_key = 'ir-home' "
        "ORDER BY revision DESC LIMIT 1",
        (request.issuer_id,),
    ).fetchone()
    semantics = (request.final_url, "verified", "publisher")
    if current is not None and tuple(str(value) for value in current[2:]) == semantics:
        return 0
    revision = 1 if current is None else int(current[1]) + 1
    record_id = _record_id(
        "ir-home-surface",
        request.issuer_id,
        request.final_url,
        observation_id,
        str(revision),
    )
    return int(
        IssuerRegistry(conn)
        .persist(
            AuthoritySurfaceRevision(
                surface_revision_id=record_id,
                idempotency_key=record_id,
                issuer_id=request.issuer_id,
                surface_key="ir-home",
                revision=revision,
                surface_kind="ir_home",
                source_url=request.final_url,
                status="verified",
                authority_level="publisher",
                source_observation_id=observation_id,
                verification_method=request.verification_method,
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_surface_revision_id=None if current is None else str(current[0]),
            )
        )
        .created
    )


def _result(
    request: IRHomeAuthorityRequest,
    mode: Literal["dry_run", "apply"],
    digest: str,
    observation_id: str | None,
    created: int,
) -> IRHomeAuthorityResult:
    return IRHomeAuthorityResult(
        mode=mode,
        issuer_id=request.issuer_id,
        ticker=request.ticker,
        source_url=request.final_url,
        raw_sha256=digest,
        source_observation_id=observation_id,
        records_created=created,
    )


def _config_sha(request: IRHomeAuthorityRequest) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "collector": _COLLECTOR,
                "requested_url": request.requested_url,
                "final_url": request.final_url,
                "media_type": request.media_type,
                "required_marker_groups": request.required_marker_groups,
                "verification_method": request.verification_method,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _store_exact_bytes(target: Path, raw_body: bytes, digest: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise IRHomeAuthorityError(
                "existing content-addressed homepage blob fails hash verification"
            )
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{digest[:12]}-",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw_body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _record_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:" + hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _parse_datetime(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
