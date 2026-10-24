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
import re
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal, Protocol, Self, cast
from urllib.parse import parse_qsl, urldefrag, urljoin, urlparse
from urllib.request import url2pathname

import requests
from defusedxml import ElementTree
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

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
from ir_pipeline.discover.generic import is_document_anchor
from ir_pipeline.discover.mz import (
    MZ_CATALOG_MAX_BYTES,
    catalog_config,
    catalog_document_urls,
    catalog_post_body,
    catalog_post_request,
    catalog_years,
)
from log_redact import redact
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.evidence_links import BlobLocationObservation, EvidenceLinkLedger
from provenance.evidence_native_candidates import resolve_local_storage_uri
from provenance.immutable_artifact import (
    ImmutableArtifactConflictError,
    read_stable_artifact,
    require_no_reparse_points,
)
from provenance.issuer_registry import (
    AuthoritySurfaceRevision,
    IssuerRegistry,
    UnresolvedIssuerIdentityError,
)
from provenance.issuer_registry import (
    SurfaceKind as RegistrySurfaceKind,
)

_COLLECTOR = "ir-authority-surface-capture@1"
_MZ_COLLECTOR = "ir-authority-surface-capture@2-mz-catalog"
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

    def post(
        self,
        url: str,
        *,
        data: bytes,
        headers: Mapping[str, str],
        timeout: tuple[int, int],
        stream: bool,
        allow_redirects: bool,
    ) -> _ResponseLike: ...


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MZCatalogPost(_ClosedModel):
    """One publisher-scoped MZ catalog request, with no arbitrary POST body."""

    results_surface_key: str = Field(max_length=128, pattern=r"^[a-z0-9][a-z0-9_.:-]*$")
    categories: tuple[str, ...] = Field(min_length=1, max_length=32)
    year: int | None = Field(default=None, ge=2000, le=2100)

    @field_validator("categories")
    @classmethod
    def _categories(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"\w{1,128}", item) is None for item in value
        ):
            raise ValueError("MZ categories must be unique publisher category names")
        return value


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
    mz_catalog_post: MZCatalogPost | None = None

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
        if (
            self.required
            and self.outcome == "exhausted"
            and not self.observed_document_urls
            and not (self.mz_catalog_post is not None and self.mz_catalog_post.year is not None)
        ):
            raise ValueError("required exhausted authority surface needs an observed document")
        if self.mz_catalog_post is not None and (
            self.surface_kind != "publisher_api"
            or (self.mz_catalog_post.year is None and self.required)
            or (self.mz_catalog_post.year is not None and self.outcome != "exhausted")
        ):
            raise ValueError("MZ catalog years must be optional and document years exhausted")
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
        mz_posts = [surface for surface in self.surfaces if surface.mz_catalog_post is not None]
        if mz_posts:
            if self.authority_basis != "publisher_api":
                raise ValueError("MZ catalog requires publisher API authority")
            years = [
                surface
                for surface in mz_posts
                if surface.mz_catalog_post and surface.mz_catalog_post.year is None
            ]
            if len(years) != 1:
                raise ValueError("MZ catalog requires one years response")
            results_key = (
                years[0].mz_catalog_post.results_surface_key if years[0].mz_catalog_post else ""
            )
            results = next(
                (surface for surface in self.surfaces if surface.surface_key == results_key), None
            )
            if results is None or results.mz_catalog_post is not None:
                raise ValueError("MZ catalog requires a raw publisher results page")
            categories = years[0].mz_catalog_post.categories if years[0].mz_catalog_post else ()
            if any(
                surface.mz_catalog_post is None
                or surface.mz_catalog_post.results_surface_key != results_key
                or surface.mz_catalog_post.categories != categories
                for surface in mz_posts
            ):
                raise ValueError("MZ catalog requests must use one results page and category scope")
            post_years = [
                surface.mz_catalog_post.year
                for surface in mz_posts
                if surface.mz_catalog_post and surface.mz_catalog_post.year is not None
            ]
            if len(post_years) != len(set(post_years)) or not post_years or len(post_years) > 30:
                raise ValueError("MZ catalog document years must be unique and bounded")
            results_index = keys.index(results_key)
            years_index = keys.index(years[0].surface_key)
            if results_index >= years_index or any(
                keys.index(surface.surface_key) <= years_index
                for surface in mz_posts
                if surface.mz_catalog_post and surface.mz_catalog_post.year is not None
            ):
                raise ValueError("MZ results, years, and document years must be ordered")
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
    request_body: bytes | None = None
    collector_code_version: str = _COLLECTOR


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
    config_bytes = _config_bytes(request)
    config_sha = hashlib.sha256(config_bytes).hexdigest()
    collector = _MZ_COLLECTOR if _has_mz_catalog(request) else _COLLECTOR
    fetched: list[_FetchedSurface] = []
    fetched_by_key: dict[str, _FetchedSurface] = {}
    mz_years: set[int] | None = None
    result_items: list[IRAuthorityCaptureItem] = []
    for spec in request.surfaces:
        try:
            post_body = _mz_request_body(spec, fetched_by_key, mz_years)
            body, media_type, final_url = _fetch_surface(
                session, request, spec, post_body=post_body
            )
            if spec.mz_catalog_post is None:
                _validate_claimed_documents(
                    spec,
                    body=body,
                    media_type=media_type,
                    final_url=final_url,
                )
            elif spec.mz_catalog_post.year is None:
                try:
                    received_years = set(catalog_years(body))
                except (ValueError, ValidationError) as exc:
                    raise _SurfaceFetchError("mz_years_invalid", exc) from None
                requested_years = {
                    item.mz_catalog_post.year
                    for item in request.surfaces
                    if item.mz_catalog_post is not None and item.mz_catalog_post.year is not None
                }
                if received_years != requested_years:
                    raise _SurfaceFetchError(
                        "mz_year_set_mismatch",
                        "declared document years differ from publisher MZ years",
                    )
                mz_years = received_years
            else:
                try:
                    urls = catalog_document_urls(body)
                except (ValueError, ValidationError) as exc:
                    raise _SurfaceFetchError("mz_documents_invalid", exc) from None
                try:
                    approved_urls = all(
                        urlparse(_uncredentialed_https(url)).hostname == "api.mziq.com"
                        and urlparse(url).path.startswith("/mzfilemanager/")
                        for url in urls
                    )
                except ValueError:
                    approved_urls = False
                if not approved_urls:
                    raise _SurfaceFetchError(
                        "mz_document_endpoint_invalid",
                        "MZ catalog named an unapproved document endpoint",
                    )
                if set(urls) != set(spec.observed_document_urls):
                    raise _SurfaceFetchError(
                        "document_set_mismatch",
                        "publisher MZ catalog differs from declared document URLs",
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
            request_body=post_body,
            collector_code_version=collector,
        )
        fetched.append(material)
        fetched_by_key[spec.surface_key] = material
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
        item_counts = _persist_surfaces(
            conn, request, fetched, blob_root=blob_root, config_bytes=config_bytes
        )
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


def _has_mz_catalog(request: IRAuthorityCaptureRequest) -> bool:
    return any(spec.mz_catalog_post is not None for spec in request.surfaces)


def _mz_request_body(
    spec: IRAuthorityCaptureSpec,
    fetched_by_key: Mapping[str, _FetchedSurface],
    mz_years: set[int] | None,
) -> bytes | None:
    post = spec.mz_catalog_post
    if post is None:
        return None
    results = fetched_by_key.get(post.results_surface_key)
    if results is None:
        raise _SurfaceFetchError("mz_results_missing", "MZ results page was not retained")
    if post.year is not None and (mz_years is None or post.year not in mz_years):
        raise _SurfaceFetchError("mz_years_unverified", "MZ document year is not verified")
    try:
        base, issuer_id, categories = catalog_config(results.body.decode("utf-8"))
        parsed_base = urlparse(_uncredentialed_https(base))
        if (
            parsed_base.hostname != "api.mziq.com"
            or parsed_base.port is not None
            or parsed_base.path != "/mzfilemanager"
            or parsed_base.query
            or parsed_base.fragment
            or re.fullmatch(r"[A-Za-z0-9-]{1,128}", issuer_id) is None
        ):
            raise ValueError("MZ results page named an unapproved catalog endpoint")
        if tuple(categories) != post.categories:
            raise ValueError("MZ categories differ from the retained results page")
        url, body = catalog_post_request(base, issuer_id, post.categories, year=post.year)
        if url != spec.source_url:
            raise ValueError("MZ endpoint differs from the retained results page")
    except (UnicodeError, ValueError) as exc:
        raise _SurfaceFetchError("mz_catalog_config_mismatch", exc) from None
    return body


def _fetch_surface(
    session: SessionLike,
    request: IRAuthorityCaptureRequest,
    spec: IRAuthorityCaptureSpec,
    *,
    post_body: bytes | None,
) -> tuple[bytes, str, str]:
    current_url = spec.source_url
    original_host = urlparse(current_url).hostname
    byte_limit = (
        min(request.max_surface_bytes, MZ_CATALOG_MAX_BYTES)
        if post_body
        else request.max_surface_bytes
    )
    redirects = 0
    while True:
        try:
            headers = {
                "User-Agent": request.user_agent,
                "Accept": "text/html,application/json,application/xml,text/xml,*/*;q=0.1",
            }
            timeout = (request.connect_timeout_seconds, request.read_timeout_seconds)
            if post_body is None:
                response = session.get(
                    current_url,
                    headers=headers,
                    timeout=timeout,
                    stream=True,
                    allow_redirects=False,
                )
            else:
                response = session.post(
                    current_url,
                    data=post_body,
                    headers={**headers, "Content-Type": "application/json"},
                    timeout=timeout,
                    stream=True,
                    allow_redirects=False,
                )
        except requests.RequestException as exc:
            raise _SurfaceFetchError("network_error", exc) from None
        try:
            if response.status_code in _REDIRECT_CODES:
                if post_body is not None:
                    raise _SurfaceFetchError("mz_post_redirect", "MZ catalog POST redirected")
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
                if declared_size > byte_limit:
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
                if total > byte_limit:
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
        self.references: list[tuple[str, str]] = []
        self._active_links: list[tuple[str, str, list[str]]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {key.casefold(): value for key, value in attrs if value is not None}
        if tag in {"a", "button"}:
            reference = next(
                (
                    attributes[key]
                    for key in ("href", "data-download-url", "data-href", "data-url")
                    if key in attributes
                ),
                None,
            )
            if reference is not None:
                self._active_links.append((tag, reference, []))
        elif tag == "iframe" and "src" in attributes:
            self.references.append((attributes["src"], attributes.get("title", "")))
        else:
            reference = next(
                (
                    attributes[key]
                    for key in ("data-download-url", "data-href", "data-url")
                    if key in attributes
                ),
                None,
            )
            if reference is not None:
                self.references.append((reference, attributes.get("aria-label", "")))

    def handle_data(self, data: str) -> None:
        for _tag, _reference, text in self._active_links:
            text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._active_links and self._active_links[-1][0] == tag:
            _tag, reference, text = self._active_links.pop()
            self.references.append((reference, " ".join(text)))


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
    # Exhausted surfaces are closed enumerations. Any parsed HTTPS reference
    # omitted by the claim is evidence that the publisher universe is not closed.
    if spec.outcome == "exhausted" and claimed != references:
        raise _SurfaceFetchError(
            "unclaimed_document_in_surface",
            "exhausted publisher surface contains an unclaimed document URL",
        )


def _surface_references(
    body: bytes,
    *,
    media_type: str,
    base_url: str,
) -> set[str]:
    decoded = body.decode("utf-8", errors="replace")
    raw_references: list[tuple[str, str]] = []
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
        raw_references.extend(_json_document_links(payload))
    elif "xml" in normalized_media_type:
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise _SurfaceFetchError(
                "surface_contract_invalid",
                "publisher XML authority surface is malformed",
            ) from exc
        for element in root.iter():
            raw_references.extend(
                (str(value), "")
                for key, value in element.attrib.items()
                if key.casefold() in {"href", "src", "url", "link", "download_url"}
            )
            if element.tag.casefold() in {"link", "url", "loc", "enclosure"} and element.text:
                raw_references.append((element.text.strip(), ""))
    else:
        raw_references.extend((part, "") for part in decoded.split())
    references: set[str] = set()
    publisher_host = urlparse(base_url).hostname or ""
    for reference, label in raw_references:
        try:
            candidate = _canonical_reference(reference, base_url)
        except ValueError:
            continue
        if is_document_anchor(
            candidate,
            label,
            publisher_host=publisher_host,
            publisher_file_rules=(),
            candidate_link_scope="observed_binary",
        ):
            references.add(candidate)
    return references


def _json_document_links(payload: object, *, url_values: bool = False) -> list[tuple[str, str]]:
    if isinstance(payload, str):
        return [(payload, "")] if url_values else []
    if isinstance(payload, list):
        return [
            item
            for value in cast(list[object], payload)
            for item in _json_document_links(value, url_values=url_values)
        ]
    if isinstance(payload, dict):
        record = cast(dict[str, object], payload)
        label = next(
            (
                value
                for key in ("file_title", "title", "name", "label")
                if isinstance(value := record.get(key), str)
            ),
            "",
        )
        links = [
            (value, label)
            for key, value in record.items()
            if key.casefold()
            in {
                "url",
                "href",
                "link_url",
                "permalink",
                "download_url",
                "document_url",
                "file_url",
            }
            and isinstance(value, str)
        ]
        return links + [
            item
            for key, value in record.items()
            for item in _json_document_links(
                value,
                url_values=key.casefold() in {"urls", "document_urls", "links"},
            )
        ]
    return []


def _canonical_reference(reference: str, base_url: str) -> str:
    if not reference or any(character.isspace() for character in reference):
        raise ValueError("document reference is not a URL")
    candidate, _fragment = urldefrag(urljoin(base_url, reference.strip()))
    return _uncredentialed_https(candidate)


def _persist_surfaces(
    conn: sqlite3.Connection,
    request: IRAuthorityCaptureRequest,
    surfaces: list[_FetchedSurface],
    *,
    blob_root: Path,
    config_bytes: bytes,
) -> dict[str, tuple[int, int]]:
    counts: dict[str, tuple[int, int]] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _has_mz_catalog(request):
            extra_created, extra_replayed = _persist_blob(
                conn,
                blob_root=blob_root,
                body=config_bytes,
                media_type="application/json",
                recorded_at=request.asserted_at,
            )
            for body in dict.fromkeys(
                surface.request_body for surface in surfaces if surface.request_body is not None
            ):
                made, reused = _persist_blob(
                    conn,
                    blob_root=blob_root,
                    body=body,
                    media_type="application/json",
                    recorded_at=request.asserted_at,
                )
                extra_created += made
                extra_replayed += reused
        else:
            extra_created = extra_replayed = 0
        for surface in surfaces:
            created, replayed = _persist_blob(
                conn,
                blob_root=blob_root,
                body=surface.body,
                media_type=surface.media_type,
                recorded_at=request.asserted_at,
            )
            ledger = EvidenceLedger(conn)
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
                collector_code_version=surface.collector_code_version,
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
            if not counts:
                created += extra_created
                replayed += extra_replayed
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
        storage_uri = str(existing[0])
        parsed = urlparse(storage_uri)
        if (
            parsed.scheme != "file"
            or parsed.netloc not in {"", "localhost"}
            or parsed.query
            or parsed.fragment
        ):
            raise IRAuthorityCaptureError("registered blob has no verifiable local storage")
        lexical_path = Path(url2pathname(parsed.path))
        if not lexical_path.is_absolute():
            raise IRAuthorityCaptureError("registered blob has no absolute local storage")
        try:
            # Check the original URI before the resolver follows path aliases.
            require_no_reparse_points(lexical_path)
            require_no_reparse_points(blob_root)
            path = resolve_local_storage_uri(storage_uri, allowed_roots=(blob_root,))
            if path is None:
                raise IRAuthorityCaptureError("registered blob leaves the approved content root")
            snapshot, retained = read_stable_artifact(path)
        except (OSError, ImmutableArtifactConflictError):
            raise IRAuthorityCaptureError("registered blob is unavailable") from None
        if snapshot.size_bytes != len(body) or snapshot.file_sha256 != digest or retained != body:
            raise IRAuthorityCaptureError("registered blob conflicts with fetched bytes")
        return storage_uri
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


def _read_retained_blob(
    conn: sqlite3.Connection, *, blob_root: Path, digest: str, max_bytes: int
) -> bytes:
    row = conn.execute(
        "SELECT storage_uri, byte_size FROM evidence_content_blobs WHERE sha256 = ?",
        (digest,),
    ).fetchone()
    if row is None or int(row[1]) > max_bytes:
        raise IRAuthorityCaptureError("retained authority blob is absent or oversized")
    storage_uri = str(row[0])
    parsed = urlparse(storage_uri)
    if (
        parsed.scheme != "file"
        or parsed.netloc not in {"", "localhost"}
        or parsed.query
        or parsed.fragment
    ):
        raise IRAuthorityCaptureError("retained authority blob has no approved local URI")
    lexical_path = Path(url2pathname(parsed.path))
    if not lexical_path.is_absolute():
        raise IRAuthorityCaptureError("retained authority blob path is not absolute")
    try:
        require_no_reparse_points(lexical_path)
        require_no_reparse_points(blob_root)
        path = resolve_local_storage_uri(storage_uri, allowed_roots=(blob_root,))
        if path is None:
            raise IRAuthorityCaptureError("retained authority blob leaves approved root")
        snapshot, body = read_stable_artifact(path, max_bytes=max_bytes, allowed_root=blob_root)
    except (OSError, ImmutableArtifactConflictError) as exc:
        raise IRAuthorityCaptureError("retained authority blob is unavailable") from exc
    if snapshot.file_sha256 != digest or snapshot.size_bytes != int(row[1]):
        raise IRAuthorityCaptureError("retained authority blob differs from its ledger row")
    return body


def verified_mz_authority_documents(
    conn: sqlite3.Connection,
    authority: IRAuthorityEvidence,
    *,
    blob_root: Path,
    issuer_id: str,
    ticker: str,
) -> tuple[tuple[str, str], ...] | None:
    """Reconstruct an @2 MZ document set from retained request and response bytes.

    Return None when no surface uses the MZ collector; the generic crawl
    comparison remains responsible for those authority claims.
    """
    authority = IRAuthorityEvidence.model_validate(authority.model_dump())
    configs: list[str] = []
    collectors: list[str] = []
    for surface in authority.surfaces:
        row = conn.execute(
            "SELECT retrieval_config_sha256, collector_code_version "
            "FROM evidence_source_observations WHERE observation_id = ?",
            (surface.source_observation_id,),
        ).fetchone()
        if row is None:
            raise IRAuthorityCaptureError("authority observation is absent")
        configs.append(str(row[0]))
        collectors.append(str(row[1]))
    if _MZ_COLLECTOR not in collectors:
        return None
    if any(collector != _MZ_COLLECTOR for collector in collectors):
        raise IRAuthorityCaptureError("MZ authority mixes collector versions")
    config_digest = configs[0]
    config_body = _read_retained_blob(
        conn, blob_root=blob_root, digest=config_digest, max_bytes=2_000_000
    )
    try:
        config_data: object = json.loads(config_body)
        if not isinstance(config_data, dict):
            raise ValueError("MZ collector manifest is not an object")
        config = cast(dict[str, object], config_data)
        if config.pop("collector") != _MZ_COLLECTOR:
            raise ValueError("MZ collector manifest differs")
        post_data = config.pop("post_requests")
        if not isinstance(post_data, dict):
            raise ValueError("MZ POST manifest is not an object")
        posts = cast(dict[str, object], post_data)
        request = IRAuthorityCaptureRequest.model_validate(config)
        if not _has_mz_catalog(request) or _config_bytes(request) != config_body:
            raise ValueError("MZ request manifest differs from retained bytes")
        if (
            request.authority_basis != authority.authority_basis
            or request.asserted_at != authority.asserted_at
            or request.issuer_id != issuer_id
            or request.ticker != ticker
        ):
            raise ValueError("MZ authority identity differs from retained request")
        if set(posts) != {
            spec.surface_key for spec in request.surfaces if spec.mz_catalog_post is not None
        }:
            raise ValueError("MZ POST manifest is incomplete")
        by_key = {surface.surface_key: surface for surface in authority.surfaces}
        if set(by_key) != {spec.surface_key for spec in request.surfaces}:
            raise ValueError("MZ authority surfaces differ from retained request")
        bodies: dict[str, bytes] = {}
        for spec in request.surfaces:
            surface = by_key[spec.surface_key]
            observation = conn.execute(
                "SELECT source_url, blob_sha256, retrieval_config_sha256, collector_code_version, "
                "observed_at, retrieved_at "
                "FROM evidence_source_observations WHERE observation_id = ?",
                (surface.source_observation_id,),
            ).fetchone()
            if (
                observation is None
                or tuple(map(str, observation[:4]))
                != (spec.source_url, surface.raw_sha256, config_digest, _MZ_COLLECTOR)
                or any(
                    _as_utc(datetime.fromisoformat(str(clock))) != _as_utc(request.asserted_at)
                    for clock in observation[4:]
                )
                or surface.source_url != spec.source_url
                or surface.observed_document_urls != spec.observed_document_urls
                or surface.outcome != spec.outcome
                or surface.required != spec.required
                or surface.surface_kind != spec.surface_kind
                or surface.traversal_kind != spec.traversal_kind
                or surface.terminal_condition != spec.terminal_condition
                or surface.source_observation_id
                != "ir-authority-observation:"
                + _stable_digest(
                    request.issuer_id,
                    spec.surface_key,
                    str(spec.revision),
                    spec.source_url,
                    surface.raw_sha256,
                    config_digest,
                )
            ):
                raise ValueError("MZ surface differs from its retained observation")
            bodies[spec.surface_key] = _read_retained_blob(
                conn,
                blob_root=blob_root,
                digest=surface.raw_sha256,
                max_bytes=(
                    min(request.max_surface_bytes, MZ_CATALOG_MAX_BYTES)
                    if spec.mz_catalog_post is not None
                    else request.max_surface_bytes
                ),
            )
        results: dict[str, _FetchedSurface] = {}
        years: set[int] | None = None
        documents: list[tuple[str, str]] = []
        for spec in request.surfaces:
            body = bodies[spec.surface_key]
            post = spec.mz_catalog_post
            if post is None:
                results[spec.surface_key] = _FetchedSurface(
                    spec=spec,
                    body=body,
                    raw_sha256=by_key[spec.surface_key].raw_sha256,
                    byte_size=len(body),
                    media_type="text/html",
                    final_url=spec.source_url,
                    source_observation_id=by_key[spec.surface_key].source_observation_id,
                    retrieval_config_sha256=config_digest,
                )
                continue
            request_body = _mz_request_body(spec, results, years)
            if request_body is None:
                raise ValueError("MZ POST request body is absent")
            digest = hashlib.sha256(request_body).hexdigest()
            if (
                posts[spec.surface_key]
                != {"method": "POST", "content_type": "application/json", "body_sha256": digest}
                or _read_retained_blob(
                    conn, blob_root=blob_root, digest=digest, max_bytes=MZ_CATALOG_MAX_BYTES
                )
                != request_body
            ):
                raise ValueError("MZ POST request differs from retained bytes")
            if post.year is None:
                years = set(catalog_years(body))
                declared = {
                    item.mz_catalog_post.year
                    for item in request.surfaces
                    if item.mz_catalog_post is not None and item.mz_catalog_post.year is not None
                }
                if years != declared:
                    raise ValueError("MZ publisher years differ from declared years")
            else:
                urls = catalog_document_urls(body)
                if set(urls) != set(spec.observed_document_urls) or not all(
                    urlparse(_uncredentialed_https(url)).hostname == "api.mziq.com"
                    and urlparse(url).path.startswith("/mzfilemanager/")
                    for url in urls
                ):
                    raise ValueError("MZ document URLs differ from retained publisher bytes")
                documents.extend((url, spec.source_url) for url in urls)
        if not documents or len(documents) != len(set(url for url, _ in documents)):
            raise ValueError("MZ document population is empty or duplicated")
        return tuple(documents)
    except (KeyError, TypeError, ValueError, ValidationError, _SurfaceFetchError) as exc:
        raise IRAuthorityCaptureError("retained MZ authority reconstruction failed") from exc


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _persist_blob(
    conn: sqlite3.Connection,
    *,
    blob_root: Path,
    body: bytes,
    media_type: str,
    recorded_at: datetime,
) -> tuple[int, int]:
    digest = hashlib.sha256(body).hexdigest()
    storage_uri = _ensure_blob(conn, blob_root=blob_root, digest=digest, body=body)
    existing = conn.execute(
        "SELECT byte_size, media_type, storage_uri, recorded_at "
        "FROM evidence_content_blobs WHERE sha256 = ?",
        (digest,),
    ).fetchone()
    blob = (
        ContentBlob(
            sha256=digest,
            byte_size=len(body),
            media_type=media_type,
            storage_uri=storage_uri,
            recorded_at=recorded_at,
        )
        if existing is None
        else ContentBlob(
            sha256=digest,
            byte_size=int(existing[0]),
            media_type=str(existing[1]),
            storage_uri=str(existing[2]),
            recorded_at=datetime.fromisoformat(str(existing[3])),
        )
    )
    created, replayed = _account(EvidenceLedger(conn).persist(blob).created, 0, 0)
    location_id = "ir-authority-location:" + _stable_digest(digest, storage_uri)
    existing_location = conn.execute(
        "SELECT verified_at, recorded_at FROM evidence_blob_location_observations "
        "WHERE location_observation_id = ?",
        (location_id,),
    ).fetchone()
    verified_at, first_recorded_at = (
        (recorded_at, recorded_at)
        if existing_location is None
        else (
            datetime.fromisoformat(str(existing_location[0])),
            datetime.fromisoformat(str(existing_location[1])),
        )
    )
    created, replayed = _account(
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
                verified_at=verified_at,
                verified_byte_size=len(body),
                verified_sha256=digest,
                recorded_at=first_recorded_at,
            )
        )
        .created,
        created,
        replayed,
    )
    return created, replayed


def _config_bytes(request: IRAuthorityCaptureRequest) -> bytes:
    payload = request.model_dump(mode="json")
    if _has_mz_catalog(request):
        payload["collector"] = _MZ_COLLECTOR
        payload["post_requests"] = {
            spec.surface_key: {
                "method": "POST",
                "content_type": "application/json",
                "body_sha256": hashlib.sha256(
                    catalog_post_body(
                        spec.mz_catalog_post.categories,
                        year=spec.mz_catalog_post.year,
                    )
                ).hexdigest(),
            }
            for spec in request.surfaces
            if spec.mz_catalog_post is not None
        }
    else:
        # Keep the historical @1 receipt and retrieval hash byte-identical.
        for surface in payload["surfaces"]:
            surface.pop("mz_catalog_post", None)
        payload["collector"] = _COLLECTOR
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


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
    "MZCatalogPost",
    "SessionLike",
    "capture_ir_authority_surfaces",
]
