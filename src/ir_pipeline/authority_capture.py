"""Bounded raw capture for publisher-defined IR authority surfaces.

The caller declares the publisher surfaces and traversal terminal state; this
module does not infer completeness from a generic crawl.  Apply mode binds the
declared evidence to immutable raw bytes, a retrieval observation, and (only
for an exhausted surface) a verified issuer-authority revision.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal, Protocol, Self, cast
from urllib.parse import parse_qsl, urldefrag, urljoin, urlparse
from xml.etree import ElementTree

import requests
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ir_pipeline.authority import (
    AuthorityBasis,
    IRAuthorityEvidence,
    PublisherSurfaceEvidence,
    SurfaceKind,
    SurfaceOutcome,
    TerminalCondition,
    TraversalKind,
    authority_is_complete,
)
from log_redact import redact
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.evidence_links import BlobLocationObservation, EvidenceLinkLedger
from provenance.issuer_registry import (
    AuthoritySurfaceRevision,
    IssuerRegistry,
    UnresolvedIssuerIdentityError,
)
from provenance.issuer_registry import (
    SurfaceKind as RegistrySurfaceKind,
)

_COLLECTOR = "ir-authority-surface-capture@1"
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_CREDENTIAL_QUERY_KEYS = frozenset(
    {
        "apikey",
        "api_key",
        "access_token",
        "auth_token",
        "password",
        "secret",
        "token",
    }
)
_REGISTRY_KIND: dict[SurfaceKind, RegistrySurfaceKind] = {
    "primary_landing": "ir_home",
    "archive": "ir_archive",
    "publisher_api": "other",
    "pagination": "ir_archive",
    "load_more": "ir_archive",
    "event_feed": "ir_events",
    "cross_host_file_endpoint": "other",
}


class IRAuthorityCaptureError(RuntimeError):
    """The publisher-authority capture contract could not be satisfied."""


class IRAuthorityCaptureIdentityError(IRAuthorityCaptureError):
    """The requested ticker is not canonically bound to the requested issuer."""


class _SurfaceFetchError(IRAuthorityCaptureError):
    def __init__(self, reason_code: str, message: object) -> None:
        super().__init__(redact(message))
        self.reason_code = reason_code


class _ResponseLike(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def iter_content(self, chunk_size: int) -> Iterator[bytes]: ...

    def close(self) -> None: ...


class SessionLike(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: tuple[int, int],
        stream: bool,
        allow_redirects: bool,
    ) -> _ResponseLike: ...


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _uncredentialed_https(value: str) -> str:
    parsed = urlparse(value)
    query_keys = {key.strip().lower().replace("-", "_") for key, _value in parse_qsl(parsed.query)}
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or query_keys & _CREDENTIAL_QUERY_KEYS
    ):
        raise ValueError("publisher URL must be uncredentialed HTTPS")
    return value


class IRAuthorityCaptureSpec(_ClosedModel):
    """One publisher surface plus its externally verified traversal outcome."""

    surface_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_.:-]*$",
    )
    surface_kind: SurfaceKind
    source_url: str = Field(min_length=1)
    traversal_kind: TraversalKind
    outcome: SurfaceOutcome
    required: bool = True
    terminal_condition: TerminalCondition | None = None
    observed_document_urls: tuple[str, ...] = ()
    verification_method: str = Field(min_length=1, max_length=128)
    revision: int = Field(gt=0)
    supersedes_surface_revision_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )

    _source_url = field_validator("source_url")(_uncredentialed_https)

    @field_validator("observed_document_urls")
    @classmethod
    def _document_urls(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("observed publisher document URLs must be unique")
        return tuple(_uncredentialed_https(url) for url in value)

    @model_validator(mode="after")
    def _closed_surface_contract(self) -> Self:
        if (self.revision == 1) != (self.supersedes_surface_revision_id is None):
            raise ValueError("authority surface revision chain is incomplete")
        # Reuse the canonical traversal/terminal-state validator.
        PublisherSurfaceEvidence(
            surface_key=self.surface_key,
            surface_kind=self.surface_kind,
            source_url=self.source_url,
            source_observation_id="validation",
            raw_sha256="0" * 64,
            traversal_kind=self.traversal_kind,
            outcome=self.outcome,
            required=self.required,
            terminal_condition=self.terminal_condition,
            observed_document_urls=self.observed_document_urls,
        )
        if self.required and self.outcome == "exhausted" and not self.observed_document_urls:
            raise ValueError("required exhausted authority surface needs an observed document")
        return self


class IRAuthorityCaptureRequest(_ClosedModel):
    """Strict JSON request for one issuer's declared publisher authority."""

    issuer_id: str = Field(min_length=1, max_length=128)
    ticker: str = Field(min_length=1, max_length=16)
    authority_basis: AuthorityBasis
    asserted_at: datetime
    user_agent: str = Field(min_length=8, max_length=512)
    connect_timeout_seconds: int = Field(default=10, ge=1, le=120)
    read_timeout_seconds: int = Field(default=60, ge=1, le=600)
    max_surface_bytes: int = Field(default=25_000_000, ge=1, le=250_000_000)
    max_redirects: int = Field(default=5, ge=0, le=10)
    surfaces: tuple[IRAuthorityCaptureSpec, ...] = Field(min_length=1)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("ticker must be non-empty")
        return normalized

    @model_validator(mode="after")
    def _authority_shape(self) -> Self:
        keys = [surface.surface_key for surface in self.surfaces]
        if len(keys) != len(set(keys)):
            raise ValueError("publisher authority surface keys must be unique")
        # Reuse the canonical authority-basis validator with synthetic hashes.
        IRAuthorityEvidence(
            authority_basis=self.authority_basis,
            asserted_at=self.asserted_at,
            surfaces=tuple(
                PublisherSurfaceEvidence(
                    surface_key=surface.surface_key,
                    surface_kind=surface.surface_kind,
                    source_url=surface.source_url,
                    source_observation_id=f"validation:{surface.surface_key}",
                    raw_sha256="0" * 64,
                    traversal_kind=surface.traversal_kind,
                    outcome=surface.outcome,
                    required=surface.required,
                    terminal_condition=surface.terminal_condition,
                    observed_document_urls=surface.observed_document_urls,
                )
                for surface in self.surfaces
            ),
        )
        return self


class IRAuthorityCaptureItem(_ClosedModel):
    surface_key: str
    outcome: Literal["fetched", "failed"]
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    raw_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    source_observation_id: str | None = Field(default=None, min_length=1, max_length=128)
    final_url: str | None = None
    byte_size: int | None = Field(default=None, ge=0)
    records_created: int = Field(default=0, ge=0)
    records_replayed: int = Field(default=0, ge=0)


class IRAuthorityCaptureResult(_ClosedModel):
    mode: Literal["dry_run", "apply"]
    issuer_id: str
    ticker: str
    complete: bool
    authority_evidence: IRAuthorityEvidence | None
    fetched: int = Field(ge=0)
    failed: int = Field(ge=0)
    records_created: int = Field(ge=0)
    records_replayed: int = Field(ge=0)
    items: tuple[IRAuthorityCaptureItem, ...]


class _FetchedSurface(_ClosedModel):
    spec: IRAuthorityCaptureSpec
    body: bytes
    raw_sha256: str
    byte_size: int
    media_type: str
    final_url: str
    source_observation_id: str
    retrieval_config_sha256: str


def capture_ir_authority_surfaces(
    conn: sqlite3.Connection,
    request: IRAuthorityCaptureRequest,
    *,
    blob_root: Path,
    apply: bool,
    session: SessionLike,
) -> IRAuthorityCaptureResult:
    """Fetch and optionally persist one complete, hash-bound authority request."""

    request = IRAuthorityCaptureRequest.model_validate(request.model_dump())
    _validate_identity(conn, request)
    config_sha = _config_sha(request)
    fetched: list[_FetchedSurface] = []
    result_items: list[IRAuthorityCaptureItem] = []
    for spec in request.surfaces:
        try:
            body, media_type, final_url = _fetch_surface(session, request, spec)
            _validate_claimed_documents(
                spec,
                body=body,
                media_type=media_type,
                final_url=final_url,
            )
        except _SurfaceFetchError as exc:
            result_items.append(
                IRAuthorityCaptureItem(
                    surface_key=spec.surface_key,
                    outcome="failed",
                    reason_code=exc.reason_code,
                )
            )
            continue
        digest = hashlib.sha256(body).hexdigest()
        observation_id = "ir-authority-observation:" + _stable_digest(
            request.issuer_id,
            spec.surface_key,
            str(spec.revision),
            spec.source_url,
            digest,
            config_sha,
        )
        material = _FetchedSurface(
            spec=spec,
            body=body,
            raw_sha256=digest,
            byte_size=len(body),
            media_type=media_type,
            final_url=final_url,
            source_observation_id=observation_id,
            retrieval_config_sha256=config_sha,
        )
        fetched.append(material)
        result_items.append(
            IRAuthorityCaptureItem(
                surface_key=spec.surface_key,
                outcome="fetched",
                reason_code="surface_fetched",
                raw_sha256=digest,
                source_observation_id=observation_id,
                final_url=final_url,
                byte_size=len(body),
            )
        )

    created = 0
    replayed = 0
    if apply and fetched:
        item_counts = _persist_surfaces(conn, request, fetched, blob_root=blob_root)
        counted_items: list[IRAuthorityCaptureItem] = []
        for item in result_items:
            counts = item_counts.get(item.surface_key, (0, 0))
            counted_items.append(
                item.model_copy(
                    update={
                        "records_created": counts[0],
                        "records_replayed": counts[1],
                    }
                )
            )
            created += counts[0]
            replayed += counts[1]
        result_items = counted_items

    authority = (
        _authority_evidence(request, fetched) if len(fetched) == len(request.surfaces) else None
    )
    complete = False
    if authority is not None:
        required_urls = tuple(
            dict.fromkeys(
                url
                for surface in authority.surfaces
                if surface.required
                for url in surface.observed_document_urls
            )
        )
        complete = authority_is_complete(authority, discovered_urls=required_urls)
    return IRAuthorityCaptureResult(
        mode="apply" if apply else "dry_run",
        issuer_id=request.issuer_id,
        ticker=request.ticker,
        complete=complete,
        authority_evidence=authority,
        fetched=len(fetched),
        failed=len(request.surfaces) - len(fetched),
        records_created=created,
        records_replayed=replayed,
        items=tuple(result_items),
    )


def _validate_identity(
    conn: sqlite3.Connection,
    request: IRAuthorityCaptureRequest,
) -> None:
    try:
        canonical = IssuerRegistry(conn).canonicalize_recorded_issuer(
            f"legacy-ticker:{request.ticker}",
            knowledge_at=request.asserted_at,
        )
    except UnresolvedIssuerIdentityError:
        raise IRAuthorityCaptureIdentityError("ticker has no canonical issuer binding") from None
    if canonical.issuer_id != request.issuer_id:
        raise IRAuthorityCaptureIdentityError(
            "ticker and requested issuer resolve to different canonical issuers"
        )


def _fetch_surface(
    session: SessionLike,
    request: IRAuthorityCaptureRequest,
    spec: IRAuthorityCaptureSpec,
) -> tuple[bytes, str, str]:
    current_url = spec.source_url
    original_host = urlparse(current_url).hostname
    redirects = 0
    while True:
        try:
            response = session.get(
                current_url,
                headers={
                    "User-Agent": request.user_agent,
                    "Accept": "text/html,application/json,application/xml,text/xml,*/*;q=0.1",
                },
                timeout=(
                    request.connect_timeout_seconds,
                    request.read_timeout_seconds,
                ),
                stream=True,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise _SurfaceFetchError("network_error", exc) from None
        try:
            if response.status_code in _REDIRECT_CODES:
                location = response.headers.get("Location")
                if not location:
                    raise _SurfaceFetchError(
                        "http_status",
                        f"redirect status {response.status_code} omitted Location",
                    )
                if redirects >= request.max_redirects:
                    raise _SurfaceFetchError(
                        "redirect_limit",
                        "publisher surface exceeded configured redirect limit",
                    )
                candidate = urljoin(current_url, location)
                try:
                    _uncredentialed_https(candidate)
                except ValueError:
                    raise _SurfaceFetchError(
                        "credentialed_url",
                        "publisher redirect is not uncredentialed HTTPS",
                    ) from None
                if urlparse(candidate).hostname != original_host:
                    raise _SurfaceFetchError(
                        "cross_host_redirect",
                        "publisher authority redirect changed host",
                    )
                current_url = candidate
                redirects += 1
                continue
            if response.status_code < 200 or response.status_code >= 300:
                raise _SurfaceFetchError(
                    "http_status",
                    f"publisher surface returned HTTP {response.status_code}",
                )
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    raise _SurfaceFetchError(
                        "invalid_content_length",
                        "publisher surface returned invalid Content-Length",
                    ) from None
                if declared_size > request.max_surface_bytes:
                    raise _SurfaceFetchError(
                        "surface_too_large",
                        "publisher surface exceeds configured byte budget",
                    )
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > request.max_surface_bytes:
                    raise _SurfaceFetchError(
                        "surface_too_large",
                        "publisher surface exceeds configured byte budget",
                    )
                chunks.append(chunk)
            media_type = (
                response.headers.get("Content-Type", "application/octet-stream")
                .split(";", 1)[0]
                .strip()
                or "application/octet-stream"
            )
            return b"".join(chunks), media_type, current_url
        finally:
            response.close()


class _HTMLReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del tag
        for key, value in attrs:
            if value is not None and key.casefold() in {
                "href",
                "src",
                "data-href",
                "data-url",
                "data-download-url",
            }:
                self.references.append(value)


def _validate_claimed_documents(
    spec: IRAuthorityCaptureSpec,
    *,
    body: bytes,
    media_type: str,
    final_url: str,
) -> None:
    if not spec.observed_document_urls:
        return
    references = _surface_references(
        body,
        media_type=media_type,
        base_url=final_url,
    )
    claimed = {_canonical_reference(url, final_url) for url in spec.observed_document_urls}
    if not claimed.issubset(references):
        raise _SurfaceFetchError(
            "claimed_document_not_in_surface",
            "publisher surface bytes do not contain every claimed document URL",
        )


def _surface_references(
    body: bytes,
    *,
    media_type: str,
    base_url: str,
) -> set[str]:
    decoded = body.decode("utf-8", errors="replace")
    raw_references: list[str] = []
    normalized_media_type = media_type.casefold()
    if "html" in normalized_media_type:
        parser = _HTMLReferenceParser()
        parser.feed(decoded)
        raw_references.extend(parser.references)
    elif "json" in normalized_media_type:
        try:
            payload: object = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise _SurfaceFetchError(
                "surface_contract_invalid",
                "publisher JSON authority surface is malformed",
            ) from exc
        raw_references.extend(_json_strings(payload))
    elif "xml" in normalized_media_type:
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise _SurfaceFetchError(
                "surface_contract_invalid",
                "publisher XML authority surface is malformed",
            ) from exc
        for element in root.iter():
            raw_references.extend(str(value) for value in element.attrib.values())
            if element.text:
                raw_references.append(element.text.strip())
    else:
        raw_references.extend(decoded.split())
    references: set[str] = set()
    for reference in raw_references:
        try:
            references.add(_canonical_reference(reference, base_url))
        except ValueError:
            continue
    return references


def _json_strings(payload: object) -> list[str]:
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, list):
        return [item for value in cast(list[object], payload) for item in _json_strings(value)]
    if isinstance(payload, dict):
        return [
            item
            for value in cast(dict[str, object], payload).values()
            for item in _json_strings(value)
        ]
    return []


def _canonical_reference(reference: str, base_url: str) -> str:
    candidate, _fragment = urldefrag(urljoin(base_url, reference.strip()))
    return _uncredentialed_https(candidate)


def _persist_surfaces(
    conn: sqlite3.Connection,
    request: IRAuthorityCaptureRequest,
    surfaces: list[_FetchedSurface],
    *,
    blob_root: Path,
) -> dict[str, tuple[int, int]]:
    counts: dict[str, tuple[int, int]] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        for surface in surfaces:
            created = 0
            replayed = 0
            storage_uri = _ensure_blob(
                conn,
                blob_root=blob_root,
                digest=surface.raw_sha256,
                body=surface.body,
            )
            ledger = EvidenceLedger(conn)
            existing_blob = conn.execute(
                "SELECT byte_size, media_type, storage_uri, recorded_at "
                "FROM evidence_content_blobs WHERE sha256 = ?",
                (surface.raw_sha256,),
            ).fetchone()
            blob = (
                ContentBlob(
                    sha256=surface.raw_sha256,
                    byte_size=surface.byte_size,
                    media_type=surface.media_type,
                    storage_uri=storage_uri,
                    recorded_at=request.asserted_at,
                )
                if existing_blob is None
                else ContentBlob(
                    sha256=surface.raw_sha256,
                    byte_size=int(existing_blob[0]),
                    media_type=str(existing_blob[1]),
                    storage_uri=str(existing_blob[2]),
                    recorded_at=datetime.fromisoformat(str(existing_blob[3])),
                )
            )
            created, replayed = _account(
                ledger.persist(blob).created,
                created,
                replayed,
            )
            location_id = "ir-authority-location:" + _stable_digest(
                surface.raw_sha256,
                storage_uri,
            )
            created, replayed = _account(
                EvidenceLinkLedger(conn)
                .persist_location(
                    BlobLocationObservation(
                        location_observation_id=location_id,
                        idempotency_key=location_id,
                        blob_sha256=surface.raw_sha256,
                        storage_uri=storage_uri,
                        location_kind="local",
                        availability_state="present",
                        location_sequence=1,
                        verified_at=request.asserted_at,
                        verified_byte_size=surface.byte_size,
                        verified_sha256=surface.raw_sha256,
                        recorded_at=request.asserted_at,
                    )
                )
                .created,
                created,
                replayed,
            )
            observation = SourceObservation(
                observation_id=surface.source_observation_id,
                idempotency_key=surface.source_observation_id,
                source_kind="ir_publisher_authority",
                source_url=surface.spec.source_url,
                blob_sha256=surface.raw_sha256,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=request.asserted_at,
                retrieved_at=request.asserted_at,
                retrieval_config_sha256=surface.retrieval_config_sha256,
                collector_code_version=_COLLECTOR,
            )
            created, replayed = _account(
                ledger.persist(observation).created,
                created,
                replayed,
            )
            if surface.spec.outcome == "exhausted":
                identity = _stable_digest(
                    request.issuer_id,
                    surface.spec.surface_key,
                    str(surface.spec.revision),
                    surface.spec.source_url,
                    surface.source_observation_id,
                )
                revision = AuthoritySurfaceRevision(
                    surface_revision_id=f"ir-authority-surface:{identity}",
                    idempotency_key=f"ir-authority-surface:{identity}",
                    issuer_id=request.issuer_id,
                    surface_key=surface.spec.surface_key,
                    revision=surface.spec.revision,
                    surface_kind=_REGISTRY_KIND[surface.spec.surface_kind],
                    source_url=surface.spec.source_url,
                    status="verified",
                    authority_level="publisher",
                    source_observation_id=surface.source_observation_id,
                    verification_method=surface.spec.verification_method,
                    effective_at=request.asserted_at,
                    knowledge_at=request.asserted_at,
                    recorded_at=request.asserted_at,
                    supersedes_surface_revision_id=(surface.spec.supersedes_surface_revision_id),
                )
                created, replayed = _account(
                    IssuerRegistry(conn).persist(revision).created,
                    created,
                    replayed,
                )
            counts[surface.spec.surface_key] = (created, replayed)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return counts


def _authority_evidence(
    request: IRAuthorityCaptureRequest,
    surfaces: list[_FetchedSurface],
) -> IRAuthorityEvidence:
    by_key = {surface.spec.surface_key: surface for surface in surfaces}
    return IRAuthorityEvidence(
        authority_basis=request.authority_basis,
        asserted_at=request.asserted_at,
        surfaces=tuple(
            PublisherSurfaceEvidence(
                surface_key=spec.surface_key,
                surface_kind=spec.surface_kind,
                source_url=spec.source_url,
                source_observation_id=by_key[spec.surface_key].source_observation_id,
                raw_sha256=by_key[spec.surface_key].raw_sha256,
                traversal_kind=spec.traversal_kind,
                outcome=spec.outcome,
                required=spec.required,
                terminal_condition=spec.terminal_condition,
                observed_document_urls=spec.observed_document_urls,
            )
            for spec in request.surfaces
        ),
    )


def _ensure_blob(
    conn: sqlite3.Connection,
    *,
    blob_root: Path,
    digest: str,
    body: bytes,
) -> str:
    existing = conn.execute(
        "SELECT storage_uri FROM evidence_content_blobs WHERE sha256 = ?",
        (digest,),
    ).fetchone()
    if existing is not None:
        return str(existing[0])
    target = blob_root / digest[:2] / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != body:
            raise IRAuthorityCaptureError(
                "durable content-addressed blob conflicts with fetched bytes"
            )
    else:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{digest[:12]}-",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
        except Exception:
            if temporary.exists():
                temporary.unlink()
            raise
    return target.resolve().as_uri()


def _config_sha(request: IRAuthorityCaptureRequest) -> str:
    payload = request.model_dump(mode="json")
    payload["collector"] = _COLLECTOR
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _stable_digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode("utf-8")).hexdigest()


def _account(
    was_created: bool,
    created: int,
    replayed: int,
) -> tuple[int, int]:
    return (created + 1, replayed) if was_created else (created, replayed + 1)


__all__ = [
    "IRAuthorityCaptureError",
    "IRAuthorityCaptureIdentityError",
    "IRAuthorityCaptureItem",
    "IRAuthorityCaptureRequest",
    "IRAuthorityCaptureResult",
    "IRAuthorityCaptureSpec",
    "SessionLike",
    "capture_ir_authority_surfaces",
]
