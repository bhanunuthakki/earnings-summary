"""Closed contracts for publisher-universe evidence and URL authorization.

A bounded crawl can prove that its own frontier stopped; it cannot prove that
the publisher exposed every reporting surface.  An authority claim therefore
names the publisher surfaces that define the universe, binds each one to an
immutable source observation, and records how pagination or load-more traversal
reached a terminal state.
"""

from __future__ import annotations

import posixpath
import re
from datetime import datetime
from typing import Literal, Self
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AuthorityBasis = Literal[
    "publisher_api",
    "publisher_archive",
    "publisher_document_feed",
]
SurfaceKind = Literal[
    "primary_landing",
    "archive",
    "publisher_api",
    "pagination",
    "load_more",
    "event_feed",
    "cross_host_file_endpoint",
]
TraversalKind = Literal[
    "single_response",
    "pagination",
    "cursor",
    "load_more",
]
SurfaceOutcome = Literal["observed", "exhausted", "failed", "robots_denied"]
TerminalCondition = Literal[
    "single_response_declared_complete",
    "publisher_total_reconciled",
    "next_link_absent",
    "empty_page",
    "cursor_absent",
    "load_more_absent",
]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublisherEndpointRule(_ClosedModel):
    """Explicit HTTPS host/path authorization for publisher-owned file endpoints."""

    host: str = Field(min_length=1, max_length=253)
    path_prefix: str = Field(default="/", min_length=1)

    @field_validator("host")
    @classmethod
    def _normalize_host(cls, value: str) -> str:
        normalized = value.strip().lower().rstrip(".")
        if not normalized or "/" in normalized or ":" in normalized or "@" in normalized:
            raise ValueError("publisher endpoint host must be a bare DNS name")
        return normalized

    @field_validator("path_prefix")
    @classmethod
    def _path_prefix(cls, value: str) -> str:
        if not value.startswith("/") or "\\" in value:
            raise ValueError("publisher endpoint path_prefix must be an absolute URL path")
        return value

    def allows(self, url: str) -> bool:
        parsed = urlparse(url)
        decoded_path = unquote(parsed.path)
        normalized_path = posixpath.normpath(decoded_path)
        normalized_prefix = posixpath.normpath(self.path_prefix)
        path_allowed = normalized_path == normalized_prefix or normalized_path.startswith(
            normalized_prefix.rstrip("/") + "/"
        )
        return (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.hostname.lower().rstrip(".") == self.host
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and "\\" not in decoded_path
            and path_allowed
        )


class PublisherSurfaceEvidence(_ClosedModel):
    """One raw-evidence-backed surface needed to enumerate publisher documents."""

    surface_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_.:-]*$",
    )
    surface_kind: SurfaceKind
    source_url: str = Field(min_length=1)
    source_observation_id: str = Field(min_length=1, max_length=128)
    raw_sha256: str
    traversal_kind: TraversalKind
    outcome: SurfaceOutcome
    required: bool = True
    terminal_condition: TerminalCondition | None = None
    observed_document_urls: tuple[str, ...] = ()

    @field_validator("raw_sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        normalized = value.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("raw_sha256 must be a lowercase SHA-256 digest")
        return normalized

    @field_validator("source_url")
    @classmethod
    def _uncredentialed_https(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("publisher surface URL must be uncredentialed HTTPS")
        return value

    @field_validator("observed_document_urls")
    @classmethod
    def _unique_documents(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("observed publisher document URLs must be unique")
        for url in value:
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname is None
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("observed publisher document URL must be uncredentialed HTTPS")
        return value

    @model_validator(mode="after")
    def _terminal_state(self) -> Self:
        if self.outcome == "exhausted" and self.terminal_condition is None:
            raise ValueError("exhausted publisher surface requires a terminal condition")
        if self.outcome != "exhausted" and self.terminal_condition is not None:
            raise ValueError("only an exhausted publisher surface may claim a terminal condition")
        allowed: dict[TraversalKind, frozenset[TerminalCondition]] = {
            "single_response": frozenset(
                {
                    "single_response_declared_complete",
                    "publisher_total_reconciled",
                }
            ),
            "pagination": frozenset(
                {
                    "publisher_total_reconciled",
                    "next_link_absent",
                    "empty_page",
                }
            ),
            "cursor": frozenset(
                {
                    "publisher_total_reconciled",
                    "cursor_absent",
                }
            ),
            "load_more": frozenset(
                {
                    "publisher_total_reconciled",
                    "load_more_absent",
                }
            ),
        }
        if (
            self.terminal_condition is not None
            and self.terminal_condition not in allowed[self.traversal_kind]
        ):
            raise ValueError("publisher surface terminal condition does not match traversal kind")
        return self


class IRAuthorityEvidence(_ClosedModel):
    """Publisher-defined reporting universe, independent of generic crawling."""

    authority_basis: AuthorityBasis
    asserted_at: datetime
    surfaces: tuple[PublisherSurfaceEvidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_surfaces(self) -> Self:
        keys = [surface.surface_key for surface in self.surfaces]
        if len(keys) != len(set(keys)):
            raise ValueError("publisher authority surface keys must be unique")
        if not any(surface.required for surface in self.surfaces):
            raise ValueError("publisher authority evidence requires a required surface")
        if self.authority_basis == "publisher_api":
            required_kind: SurfaceKind = "publisher_api"
        elif self.authority_basis == "publisher_archive":
            required_kind = "archive"
        else:
            required_kind = "event_feed"
        if not any(
            surface.required and surface.surface_kind == required_kind for surface in self.surfaces
        ):
            raise ValueError(f"{self.authority_basis} requires a required {required_kind} surface")
        return self


def authority_is_complete(
    authority: IRAuthorityEvidence,
    *,
    discovered_urls: tuple[str, ...],
) -> bool:
    """Return true only for exhausted required surfaces covering the exact URL set."""

    authority = IRAuthorityEvidence.model_validate(authority.model_dump())
    if any(surface.required and surface.outcome != "exhausted" for surface in authority.surfaces):
        return False
    observed = {
        url
        for surface in authority.surfaces
        if surface.required
        for url in surface.observed_document_urls
    }
    return bool(observed) and observed == set(discovered_urls)
