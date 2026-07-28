"""Robots-aware, bounded verification of curated publisher IR homepages.

The registry contains review candidates, not trusted authority.  This module
performs the network boundary once, returns an explicit outcome for every
candidate, and delegates exact-byte persistence to ``home_authority`` only
after the fetch and identity contracts pass.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import parse_qsl, urljoin, urlparse

import requests
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ir_pipeline.discover.generic import robots_policy
from ir_pipeline.home_authority import (
    IRHomeAuthorityError,
    IRHomeAuthorityRequest,
    verify_ir_home_authority,
)
from ir_pipeline.home_authority_registry import IRHomeAuthorityCandidate
from log_redact import redact
from provenance.issuer_registry import IssuerRegistry, UnresolvedIssuerIdentityError

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
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
_CHUNK_BYTES = 64 * 1024

IRHomeOutcome = Literal[
    "verified",
    "skipped_existing",
    "skipped_duplicate_issuer",
    "failed",
]


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

    def close(self) -> None: ...


RobotsResolver = Callable[[str], tuple[Callable[[str], bool], float]]
Sleeper = Callable[[float], None]
SessionFactory = Callable[[], SessionLike]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class IRHomeBatchRequest(_ClosedModel):
    candidates: tuple[IRHomeAuthorityCandidate, ...] = Field(min_length=1)
    blob_root: Path
    apply: bool = False
    recorded_at: datetime
    user_agent: str = Field(min_length=8, max_length=512)
    connect_timeout_seconds: int = Field(default=10, ge=1, le=120)
    read_timeout_seconds: int = Field(default=60, ge=1, le=600)
    max_body_bytes: int = Field(default=10_000_000, ge=1, le=100_000_000)
    max_redirects: int = Field(default=5, ge=0, le=10)
    max_workers: int = Field(default=1, ge=1, le=8)
    refresh_existing: bool = False

    @field_validator("recorded_at")
    @classmethod
    def _recorded_at(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class IRHomeBatchItem(_ClosedModel):
    ticker: str
    issuer_id: str | None
    requested_url: str
    final_url: str | None
    outcome: IRHomeOutcome
    reason_code: str = Field(min_length=1, max_length=128)
    reason_detail: str | None = None
    records_created: int = Field(ge=0)


class IRHomeBatchResult(_ClosedModel):
    mode: Literal["dry_run", "apply"]
    items: tuple[IRHomeBatchItem, ...]


class _CandidateFetchError(RuntimeError):
    def __init__(self, reason_code: str, detail: object) -> None:
        super().__init__(redact(detail))
        self.reason_code = reason_code


class _FetchedHome(_ClosedModel):
    final_url: str
    raw_body: bytes
    media_type: Literal["text/html", "application/xhtml+xml"]


@dataclass(frozen=True, slots=True)
class _PendingCandidate:
    index: int
    candidate: IRHomeAuthorityCandidate
    issuer_id: str


@dataclass(frozen=True, slots=True)
class _FetchAttempt:
    fetched: _FetchedHome | None
    reason_code: str | None = None
    reason_detail: str | None = None


def verify_ir_home_candidates(
    conn: sqlite3.Connection,
    *,
    request: IRHomeBatchRequest,
    session: SessionLike | None = None,
    session_factory: SessionFactory | None = None,
    robots_resolver: RobotsResolver = robots_policy,
    sleeper: Sleeper = time.sleep,
) -> IRHomeBatchResult:
    """Verify a candidate queue with one explicit result per input candidate."""

    request = IRHomeBatchRequest.model_validate(request.model_dump())
    if request.max_workers > 1 and session_factory is None:
        raise ValueError("parallel IR-home verification requires a session factory")
    if session is None and session_factory is None:
        raise ValueError("IR-home verification requires a session or session factory")
    seen_issuers: set[str] = set()
    items_by_index: dict[int, IRHomeBatchItem] = {}
    pending: list[_PendingCandidate] = []
    for index, candidate in enumerate(request.candidates):
        issuer_id = _canonical_issuer(
            conn,
            ticker=candidate.ticker,
            knowledge_at=request.recorded_at,
        )
        if issuer_id is None:
            items_by_index[index] = _item(
                candidate,
                None,
                "failed",
                "canonical_identity_missing",
            )
            continue
        if issuer_id in seen_issuers:
            items_by_index[index] = _item(
                candidate,
                issuer_id,
                "skipped_duplicate_issuer",
                "duplicate_canonical_issuer",
            )
            continue
        seen_issuers.add(issuer_id)
        if not request.refresh_existing and _has_verified_home(conn, issuer_id):
            items_by_index[index] = _item(
                candidate,
                issuer_id,
                "skipped_existing",
                "already_verified",
            )
            continue
        pending.append(
            _PendingCandidate(
                index=index,
                candidate=candidate,
                issuer_id=issuer_id,
            )
        )

    attempts = _fetch_pending(
        tuple(pending),
        request=request,
        session=session,
        session_factory=session_factory,
        robots_resolver=robots_resolver,
        sleeper=sleeper,
    )
    for target, attempt in zip(pending, attempts, strict=True):
        if attempt.fetched is None:
            if attempt.reason_code is None:
                raise AssertionError("failed fetch attempt omitted its reason code")
            items_by_index[target.index] = _item(
                target.candidate,
                target.issuer_id,
                "failed",
                attempt.reason_code,
                detail=attempt.reason_detail,
            )
            continue
        fetched = attempt.fetched
        try:
            verification = verify_ir_home_authority(
                conn,
                request=IRHomeAuthorityRequest(
                    issuer_id=target.issuer_id,
                    ticker=target.candidate.ticker,
                    requested_url=target.candidate.requested_url,
                    final_url=fetched.final_url,
                    raw_body=fetched.raw_body,
                    media_type=fetched.media_type,
                    required_marker_groups=target.candidate.required_marker_groups,
                    verification_method=target.candidate.verification_method,
                    blob_root=request.blob_root,
                    apply=request.apply,
                    recorded_at=request.recorded_at,
                ),
            )
        except IRHomeAuthorityError as exc:
            items_by_index[target.index] = _item(
                target.candidate,
                target.issuer_id,
                "failed",
                "authority_validation_failed",
                detail=str(exc),
            )
            continue
        items_by_index[target.index] = _item(
            target.candidate,
            target.issuer_id,
            "verified",
            "publisher_identity_markers_verified",
            final_url=fetched.final_url,
            records_created=verification.records_created,
        )
    return IRHomeBatchResult(
        mode="apply" if request.apply else "dry_run",
        items=tuple(items_by_index[index] for index in range(len(request.candidates))),
    )


def _fetch_pending(
    pending: tuple[_PendingCandidate, ...],
    *,
    request: IRHomeBatchRequest,
    session: SessionLike | None,
    session_factory: SessionFactory | None,
    robots_resolver: RobotsResolver,
    sleeper: Sleeper,
) -> tuple[_FetchAttempt, ...]:
    if not pending:
        return ()
    if request.max_workers == 1:
        owned_session = session is None
        active_session = session if session is not None else _new_session(session_factory)
        try:
            return tuple(
                _attempt_fetch(
                    target.candidate.requested_url,
                    request=request,
                    session=active_session,
                    robots_resolver=robots_resolver,
                    sleeper=sleeper,
                )
                for target in pending
            )
        finally:
            if owned_session:
                active_session.close()
    if session_factory is None:
        raise AssertionError("parallel fetch session factory was not validated")

    def fetch_one(target: _PendingCandidate) -> _FetchAttempt:
        active_session = session_factory()
        try:
            return _attempt_fetch(
                target.candidate.requested_url,
                request=request,
                session=active_session,
                robots_resolver=robots_resolver,
                sleeper=sleeper,
            )
        finally:
            active_session.close()

    with ThreadPoolExecutor(
        max_workers=min(request.max_workers, len(pending)),
        thread_name_prefix="ir-home-fetch",
    ) as executor:
        return tuple(executor.map(fetch_one, pending))


def _new_session(session_factory: SessionFactory | None) -> SessionLike:
    if session_factory is None:
        raise ValueError("IR-home verification requires a session or session factory")
    return session_factory()


def _attempt_fetch(
    requested_url: str,
    *,
    request: IRHomeBatchRequest,
    session: SessionLike,
    robots_resolver: RobotsResolver,
    sleeper: Sleeper,
) -> _FetchAttempt:
    try:
        return _FetchAttempt(
            fetched=_fetch_home(
                requested_url,
                request=request,
                session=session,
                robots_resolver=robots_resolver,
                sleeper=sleeper,
            )
        )
    except _CandidateFetchError as exc:
        return _FetchAttempt(
            fetched=None,
            reason_code=exc.reason_code,
            reason_detail=str(exc),
        )
    except (requests.RequestException, OSError, TimeoutError) as exc:
        return _FetchAttempt(
            fetched=None,
            reason_code="network_error",
            reason_detail=type(exc).__name__,
        )


def _fetch_home(
    requested_url: str,
    *,
    request: IRHomeBatchRequest,
    session: SessionLike,
    robots_resolver: RobotsResolver,
    sleeper: Sleeper,
) -> _FetchedHome:
    current_url = _uncredentialed_https(requested_url)
    for redirect_count in range(request.max_redirects + 1):
        allows, crawl_delay = robots_resolver(current_url)
        if not allows(current_url):
            raise _CandidateFetchError("robots_denied", "robots.txt denied publisher URL")
        if crawl_delay > 0:
            sleeper(crawl_delay)
        response = session.get(
            current_url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": request.user_agent,
            },
            timeout=(
                request.connect_timeout_seconds,
                request.read_timeout_seconds,
            ),
            stream=True,
            allow_redirects=False,
        )
        try:
            if response.status_code in _REDIRECT_CODES:
                location = _header(response.headers, "Location")
                if location is None:
                    raise _CandidateFetchError(
                        "redirect_location_missing",
                        "publisher redirect omitted Location",
                    )
                if redirect_count >= request.max_redirects:
                    raise _CandidateFetchError(
                        "redirect_limit_exhausted",
                        "publisher redirect limit exhausted",
                    )
                current_url = _uncredentialed_https(urljoin(current_url, location))
                continue
            if not 200 <= response.status_code < 300:
                raise _CandidateFetchError(
                    "http_status_rejected",
                    f"publisher returned HTTP {response.status_code}",
                )
            media_type = _html_media_type(response.headers)
            body = _bounded_body(response, request.max_body_bytes)
            return _FetchedHome(
                final_url=current_url,
                raw_body=body,
                media_type=media_type,
            )
        finally:
            response.close()
    raise AssertionError("bounded redirect loop did not terminate")


def _canonical_issuer(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    knowledge_at: datetime,
) -> str | None:
    try:
        return (
            IssuerRegistry(conn)
            .canonicalize_recorded_issuer(
                f"legacy-ticker:{ticker}",
                knowledge_at=knowledge_at,
            )
            .issuer_id
        )
    except UnresolvedIssuerIdentityError:
        return None


def _has_verified_home(conn: sqlite3.Connection, issuer_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM v_issuer_authority_surfaces_current "
        "WHERE issuer_id = ? AND surface_kind = 'ir_home' AND status = 'verified' "
        "LIMIT 1",
        (issuer_id,),
    ).fetchone()
    return row is not None


def _bounded_body(response: _ResponseLike, maximum: int) -> bytes:
    content_length = _header(response.headers, "Content-Length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = -1
        if declared > maximum:
            raise _CandidateFetchError(
                "body_limit_exceeded",
                "publisher body exceeded declared byte limit",
            )
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
        total += len(chunk)
        if total > maximum:
            raise _CandidateFetchError(
                "body_limit_exceeded",
                "publisher body exceeded streamed byte limit",
            )
        chunks.append(chunk)
    body = b"".join(chunks)
    if not body:
        raise _CandidateFetchError("empty_body", "publisher returned an empty body")
    return body


def _html_media_type(
    headers: Mapping[str, str],
) -> Literal["text/html", "application/xhtml+xml"]:
    raw = _header(headers, "Content-Type")
    if raw is None:
        raise _CandidateFetchError(
            "content_type_missing",
            "publisher omitted Content-Type",
        )
    media_type = raw.split(";", 1)[0].strip().lower()
    if media_type == "text/html":
        return "text/html"
    if media_type == "application/xhtml+xml":
        return "application/xhtml+xml"
    raise _CandidateFetchError(
        "content_type_rejected",
        f"publisher returned non-HTML media type {media_type}",
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    folded = name.casefold()
    for key, value in headers.items():
        if key.casefold() == folded:
            return value
    return None


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
        raise _CandidateFetchError(
            "unsafe_url",
            "publisher URL must be uncredentialed HTTPS",
        )
    return value


def _item(
    candidate: IRHomeAuthorityCandidate,
    issuer_id: str | None,
    outcome: IRHomeOutcome,
    reason_code: str,
    *,
    detail: str | None = None,
    final_url: str | None = None,
    records_created: int = 0,
) -> IRHomeBatchItem:
    return IRHomeBatchItem(
        ticker=candidate.ticker,
        issuer_id=issuer_id,
        requested_url=candidate.requested_url,
        final_url=final_url,
        outcome=outcome,
        reason_code=reason_code,
        reason_detail=detail,
        records_created=records_created,
    )
