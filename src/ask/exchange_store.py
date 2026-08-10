"""Durable boundary for one Ask request from user turn through final artifacts.

The existing :mod:`ask.store` remains the general session/turn CRUD layer.
This module owns only the stronger orchestration invariants needed by an
idempotent client request: immutable historical context, one pending exchange
per session, payload-hash replay protection, and revision compare-and-swap.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Self, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from clock import now_iso
from research.proposal_approval import AskProposalRefV1
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_FACT_REF_RE = re.compile(r"(?:kpi|fin):[A-Za-z0-9._:/-]{1,508}\Z")
_SOURCE_REF_RE = re.compile(r"source:[A-Za-z0-9._:/-]{1,505}\Z")
_MAX_TURN_CHARS = 100_000
_MAX_ARTIFACT_JSON_BYTES = 100_000
_MAX_RESEARCH_CONTEXT_BYTES = 32_000
_ArtifactLink = Annotated[str, Field(min_length=1, max_length=512)]
CanonicalJson: TypeAlias = (
    bool | int | float | str | list["CanonicalJson"] | dict[str, "CanonicalJson"] | None
)

CoverageRoleAtCreation = Literal[
    "portfolio",
    "evaluation",
    "watchlist",
    "index_member",
    "none",
    "legacy_etf",
    "unknown",
]
LifecycleAtCreation = Literal["active", "archived", "unknown"]
ContextCategory = Literal["general", "research", "governed_fact", "thesis", "kpi"]
ExchangeStatus = Literal["pending", "completed", "failed"]
BeginDisposition = Literal["started", "pending", "replayed"]

_LOGGER = logging.getLogger(__name__)


class SessionContextV1(BaseModel):
    """The immutable company/research coordinates captured at session creation."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["session_context.v1"] = "session_context.v1"
    company_ticker: str | None = Field(default=None, min_length=1, max_length=24)
    coverage_role_at_creation: CoverageRoleAtCreation = "unknown"
    lifecycle_at_creation: LifecycleAtCreation = "unknown"
    category: ContextCategory = "general"
    thesis_ref: str | None = Field(default=None, min_length=1, max_length=256)
    thesis_version: str | None = Field(default=None, min_length=1, max_length=64)
    report_date: date | None = None
    origin_key: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("company_ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("company_ticker")
    @classmethod
    def _validate_ticker(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[A-Z0-9][A-Z0-9.-]*", value) is None:
            raise ValueError("company_ticker must be a normalized ticker symbol")
        return value


class ProposalErrorV1(BaseModel):
    """Safe bounded proposal-registration failure persisted with an answer."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["proposal_error.v1"] = "proposal_error.v1"
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1, max_length=256)


class ExchangeArtifactsV1(BaseModel):
    """Bounded durable links and UI handoff metadata produced by one answer."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["exchange_artifacts.v1"] = "exchange_artifacts.v1"
    route: str | None = Field(default=None, min_length=1, max_length=128)
    view_spec: dict[str, JsonValue] | None = None
    proposal_ref: AskProposalRefV1 | str | None = None
    proposal_error: ProposalErrorV1 | None = None

    @field_validator("proposal_ref")
    @classmethod
    def _bound_legacy_proposal_ref(
        cls, value: AskProposalRefV1 | str | None
    ) -> AskProposalRefV1 | str | None:
        if isinstance(value, str) and not 1 <= len(value.strip()) <= 256:
            raise ValueError("legacy proposal_ref exceeds the 256-character limit")
        return value.strip() if isinstance(value, str) else value

    source_links: list[_ArtifactLink] = Field(default_factory=list, max_length=100)
    fact_links: list[_ArtifactLink] = Field(default_factory=list, max_length=100)

    @field_validator("view_spec")
    @classmethod
    def _bound_view_spec(cls, value: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
        if value is not None and len(_canonical_json(value).encode("utf-8")) > 64_000:
            raise ValueError("view_spec exceeds the 64000-byte limit")
        return value


class SessionExchangeArtifactV1(BaseModel):
    """One completed exchange's typed artifact payload for session hydration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["session_exchange_artifact.v1"] = "session_exchange_artifact.v1"
    exchange_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    assistant_turn_id: int = Field(gt=0)
    session_revision: int = Field(ge=0)
    completed_at: str = Field(min_length=1, max_length=64)
    artifacts: ExchangeArtifactsV1


class ResearchContextV1(BaseModel):
    """Bounded per-request research coordinates, separate from session history."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["research_context.v1"] = "research_context.v1"
    screen_id: str | None = Field(default=None, min_length=1, max_length=128)
    fact_ref: str | None = Field(default=None, min_length=1, max_length=512)
    source_ref: str | None = Field(default=None, min_length=1, max_length=512)
    capability_id: str | None = Field(default=None, min_length=1, max_length=128)
    card_key: str | None = Field(default=None, min_length=1, max_length=128)
    source_ids: list[_ArtifactLink] = Field(default_factory=list, max_length=100)

    @field_validator("fact_ref")
    @classmethod
    def _validate_fact_ref(cls, value: str | None) -> str | None:
        if value is not None and _FACT_REF_RE.fullmatch(value) is None:
            raise ValueError("fact_ref must use a canonical kpi: or fin: form")
        return value

    @field_validator("source_ref")
    @classmethod
    def _validate_source_ref(cls, value: str | None) -> str | None:
        if value is not None and _SOURCE_REF_RE.fullmatch(value) is None:
            raise ValueError("source_ref must use the canonical source:<id> form")
        return value

    @field_validator("source_ids")
    @classmethod
    def _validate_source_ids(cls, value: list[str]) -> list[str]:
        if any(_SOURCE_REF_RE.fullmatch(item) is None for item in value):
            raise ValueError("source_ids entries must use the canonical source:<id> form")
        if len(set(value)) != len(value):
            raise ValueError("source_ids entries must be unique")
        return value

    @model_validator(mode="after")
    def _validate_total_size(self) -> Self:
        if (
            len(_canonical_json(self.model_dump(mode="json")).encode("utf-8"))
            > _MAX_RESEARCH_CONTEXT_BYTES
        ):
            raise ValueError(
                f"research_context exceeds the {_MAX_RESEARCH_CONTEXT_BYTES}-byte limit"
            )
        return self


@dataclass(frozen=True, slots=True)
class SessionContextRecord:
    session_id: str
    context: SessionContextV1
    context_sha256: str
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AskExchange:
    request_id: str
    session_id: str
    payload_sha256: str
    status: ExchangeStatus
    user_turn_id: int
    assistant_turn_id: int | None
    artifacts: ExchangeArtifactsV1 | None
    error_code: str | None
    session_revision: int
    created_at: str
    updated_at: str
    completed_at: str | None
    failed_at: str | None


@dataclass(frozen=True, slots=True)
class BeginExchangeResult:
    disposition: BeginDisposition
    exchange: AskExchange
    session_revision: int


class ExchangeStoreError(RuntimeError):
    """Base class for durable Ask orchestration failures."""


class SessionContextConflictError(ExchangeStoreError):
    """A session already owns a different historical context snapshot."""


class ExchangeConflictError(ExchangeStoreError):
    """A request identity was reused for a different immutable payload."""


class PendingExchangeError(ExchangeStoreError):
    """Another client request currently owns the session's pending slot."""


class RevisionConflictError(ExchangeStoreError):
    """The caller's session revision is stale."""


class ExchangeStateError(ExchangeStoreError):
    """The requested state transition is not legal from the durable state."""


class StoredExchangeDataError(ExchangeStoreError):
    """Persisted JSON or its commitment is malformed and cannot be trusted."""


def hash_request_payload(payload: object) -> str:
    """Return a stable SHA-256 for a JSON-compatible client request payload."""

    validated = _validated_json(payload)
    return _sha256(_canonical_json(validated))


def put_session_context(
    session_id: str,
    context: SessionContextV1,
    *,
    db_path: Path,
) -> SessionContextRecord:
    """Create a session's immutable context, or replay the identical snapshot."""

    _validate_identifier("session_id", session_id)
    payload_json = _model_json(context)
    payload_sha256 = _sha256(payload_json)
    timestamp = now_iso()
    connection = _open(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "INSERT OR IGNORE INTO ask_session_contexts "
            "(session_id,schema_version,context_json,context_sha256,revision,created_at,updated_at) "
            "VALUES (?,?,?,?,0,?,?)",
            (
                session_id,
                context.schema_version,
                payload_json,
                payload_sha256,
                timestamp,
                timestamp,
            ),
        )
        row = _select_context(connection, session_id)
        if row is None:
            raise ExchangeStoreError("session context insert did not produce a row")
        record = _context_from_row(row)
        if cursor.rowcount == 0 and (
            record.context_sha256 != payload_sha256 or record.context != context
        ):
            raise SessionContextConflictError(
                f"Ask session {session_id!r} already has a different historical context"
            )
        connection.commit()
        return record
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_session_context(session_id: str, *, db_path: Path) -> SessionContextRecord | None:
    """Load and validate the historical context plus current CAS revision."""

    connection = _open(db_path)
    try:
        row = _select_context(connection, session_id)
    finally:
        connection.close()
    return None if row is None else _context_from_row(row)


def begin_exchange(
    *,
    session_id: str,
    request_id: str,
    payload_sha256: str,
    user_text: str,
    expected_revision: int,
    db_path: Path,
) -> BeginExchangeResult:
    """Claim a request and append its one authoritative user turn atomically."""

    _validate_identifier("session_id", session_id)
    _validate_request_id(request_id)
    _validate_sha256("payload_sha256", payload_sha256)
    _validate_turn_text("user_text", user_text)
    _validate_revision(expected_revision)
    connection = _open(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing_row = _select_exchange(connection, request_id)
        if existing_row is not None:
            existing = _exchange_from_row(existing_row)
            if existing.session_id != session_id or existing.payload_sha256 != payload_sha256:
                raise ExchangeConflictError(
                    f"request_id {request_id!r} was already used with a different payload hash"
                )
            current_revision = _current_revision(connection, session_id)
            if existing.status == "completed":
                connection.commit()
                return BeginExchangeResult("replayed", existing, current_revision)
            if existing.status == "pending":
                connection.commit()
                return BeginExchangeResult("pending", existing, current_revision)
            raise ExchangeStateError(
                f"request_id {request_id!r} is failed and cannot be started again"
            )

        current_revision = _current_revision(connection, session_id)
        if current_revision != expected_revision:
            raise RevisionConflictError(
                f"Ask session revision mismatch: expected {expected_revision}, "
                f"found {current_revision}"
            )
        pending = connection.execute(
            "SELECT request_id FROM ask_exchanges WHERE session_id=? AND status='pending' LIMIT 1",
            (session_id,),
        ).fetchone()
        if pending is not None:
            raise PendingExchangeError(
                f"Ask session {session_id!r} already has pending request {pending['request_id']!r}"
            )

        timestamp = now_iso()
        turn_cursor = connection.execute(
            "INSERT INTO ask_turns(session_id,role,text,citations_json,model,created_at) "
            "VALUES (?,'user',?,NULL,NULL,?)",
            (session_id, user_text, timestamp),
        )
        if turn_cursor.lastrowid is None:
            raise ExchangeStoreError("Ask user turn insert did not return a row identity")
        next_revision = expected_revision + 1
        _advance_revision(
            connection,
            session_id=session_id,
            expected_revision=expected_revision,
            next_revision=next_revision,
            timestamp=timestamp,
        )
        connection.execute(
            "INSERT INTO ask_exchanges "
            "(request_id,session_id,payload_sha256,status,user_turn_id,session_revision,"
            "created_at,updated_at) VALUES (?,?,?,'pending',?,?,?,?)",
            (
                request_id,
                session_id,
                payload_sha256,
                int(turn_cursor.lastrowid),
                next_revision,
                timestamp,
                timestamp,
            ),
        )
        _touch_session(connection, session_id, timestamp)
        created_row = _select_exchange(connection, request_id)
        if created_row is None:
            raise ExchangeStoreError("Ask exchange insert did not produce a row")
        created = _exchange_from_row(created_row)
        connection.commit()
        return BeginExchangeResult("started", created, next_revision)
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        if "ask_exchanges.session_id" in str(exc):
            raise PendingExchangeError(
                f"Ask session {session_id!r} already has a pending request"
            ) from exc
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def complete_exchange(
    *,
    request_id: str,
    assistant_text: str,
    artifacts: ExchangeArtifactsV1,
    expected_revision: int,
    db_path: Path,
    citations: Sequence[object] | None = None,
    model: str | None = None,
) -> AskExchange:
    """Atomically persist the answer turn and artifacts, then mark completion."""

    _validate_request_id(request_id)
    _validate_turn_text("assistant_text", assistant_text)
    _validate_revision(expected_revision)
    if model is not None and not 1 <= len(model) <= 128:
        raise ValueError("model must contain between 1 and 128 characters")
    citations_json = _citations_json(citations)
    connection = _open(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _select_exchange(connection, request_id)
        if row is None:
            raise ExchangeStateError(f"unknown Ask request_id {request_id!r}")
        exchange = _exchange_from_row(row)
        if exchange.status == "completed":
            connection.commit()
            return exchange
        if exchange.status != "pending":
            raise ExchangeStateError(
                f"Ask request {request_id!r} cannot complete from {exchange.status!r}"
            )
        _require_transition_revision(connection, exchange, expected_revision)

        timestamp = now_iso()
        if isinstance(artifacts.proposal_ref, AskProposalRefV1):
            from research.proposal_approval import activate_ask_proposal

            active_ref = activate_ask_proposal(
                artifacts.proposal_ref,
                exchange_request_id=request_id,
                connection=connection,
                timestamp=timestamp,
            )
            artifacts = artifacts.model_copy(update={"proposal_ref": active_ref})
        artifacts_json = _model_json(artifacts)
        if len(artifacts_json.encode("utf-8")) > _MAX_ARTIFACT_JSON_BYTES:
            raise ValueError(f"artifacts exceed the {_MAX_ARTIFACT_JSON_BYTES}-byte limit")
        artifacts_sha256 = _sha256(artifacts_json)
        assistant_cursor = connection.execute(
            "INSERT INTO ask_turns(session_id,role,text,citations_json,model,created_at) "
            "VALUES (?,'assistant',?,?,?,?)",
            (exchange.session_id, assistant_text, citations_json, model, timestamp),
        )
        if assistant_cursor.lastrowid is None:
            raise ExchangeStoreError("Ask assistant turn insert did not return a row identity")
        connection.execute(
            "INSERT INTO ask_exchange_artifacts "
            "(request_id,schema_version,artifacts_json,artifacts_sha256,created_at) "
            "VALUES (?,?,?,?,?)",
            (
                request_id,
                artifacts.schema_version,
                artifacts_json,
                artifacts_sha256,
                timestamp,
            ),
        )
        next_revision = expected_revision + 1
        updated = connection.execute(
            "UPDATE ask_exchanges SET status='completed',assistant_turn_id=?,"
            "session_revision=?,updated_at=?,completed_at=? "
            "WHERE request_id=? AND status='pending' AND session_revision=?",
            (
                int(assistant_cursor.lastrowid),
                next_revision,
                timestamp,
                timestamp,
                request_id,
                expected_revision,
            ),
        )
        if updated.rowcount != 1:
            raise RevisionConflictError("Ask exchange advanced before completion")
        _advance_revision(
            connection,
            session_id=exchange.session_id,
            expected_revision=expected_revision,
            next_revision=next_revision,
            timestamp=timestamp,
        )
        _touch_session(connection, exchange.session_id, timestamp)
        completed_row = _select_exchange(connection, request_id)
        if completed_row is None:
            raise ExchangeStoreError("completed Ask exchange disappeared")
        completed = _exchange_from_row(completed_row)
        connection.commit()
        return completed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def fail_exchange(
    *,
    request_id: str,
    error_code: str,
    expected_revision: int,
    db_path: Path,
) -> AskExchange:
    """Move one pending request to a durable failed state and release its slot."""

    _validate_request_id(request_id)
    if _ERROR_CODE_RE.fullmatch(error_code) is None:
        raise ValueError("error_code must be lower snake_case and at most 128 characters")
    _validate_revision(expected_revision)
    connection = _open(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _select_exchange(connection, request_id)
        if row is None:
            raise ExchangeStateError(f"unknown Ask request_id {request_id!r}")
        exchange = _exchange_from_row(row)
        if exchange.status == "failed":
            connection.commit()
            return exchange
        if exchange.status != "pending":
            raise ExchangeStateError(
                f"Ask request {request_id!r} cannot fail from {exchange.status!r}"
            )
        _require_transition_revision(connection, exchange, expected_revision)

        timestamp = now_iso()
        from research.proposal_approval import invalidate_exchange_proposals

        invalidate_exchange_proposals(
            request_id,
            connection=connection,
            timestamp=timestamp,
            reason=error_code,
        )
        next_revision = expected_revision + 1
        updated = connection.execute(
            "UPDATE ask_exchanges SET status='failed',error_code=?,session_revision=?,"
            "updated_at=?,failed_at=? "
            "WHERE request_id=? AND status='pending' AND session_revision=?",
            (
                error_code,
                next_revision,
                timestamp,
                timestamp,
                request_id,
                expected_revision,
            ),
        )
        if updated.rowcount != 1:
            raise RevisionConflictError("Ask exchange advanced before failure recording")
        _advance_revision(
            connection,
            session_id=exchange.session_id,
            expected_revision=expected_revision,
            next_revision=next_revision,
            timestamp=timestamp,
        )
        _touch_session(connection, exchange.session_id, timestamp)
        failed_row = _select_exchange(connection, request_id)
        if failed_row is None:
            raise ExchangeStoreError("failed Ask exchange disappeared")
        failed = _exchange_from_row(failed_row)
        connection.commit()
        return failed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_exchange(request_id: str, *, db_path: Path) -> AskExchange | None:
    """Load one exchange and validate any persisted artifact boundary."""

    connection = _open(db_path)
    try:
        row = _select_exchange(connection, request_id)
    finally:
        connection.close()
    return None if row is None else _exchange_from_row(row)


def list_session_exchange_artifacts(
    session_id: str, *, db_path: Path, limit: int = 200
) -> list[SessionExchangeArtifactV1]:
    """Load validated completed artifacts for one session in completion order."""

    _validate_identifier("session_id", session_id)
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    connection = _open(db_path)
    try:
        rows = connection.execute(
            "SELECT exchange.*,artifact.schema_version AS artifact_schema_version,"
            "artifact.artifacts_json,artifact.artifacts_sha256 "
            "FROM ask_exchanges exchange JOIN ask_exchange_artifacts artifact "
            "ON artifact.request_id=exchange.request_id "
            "WHERE exchange.session_id=? AND exchange.status='completed' "
            "ORDER BY exchange.completed_at ASC, exchange.request_id ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    finally:
        connection.close()
    result: list[SessionExchangeArtifactV1] = []
    for row in rows:
        exchange = _exchange_from_row(row)
        if (
            exchange.artifacts is None
            or exchange.assistant_turn_id is None
            or exchange.completed_at is None
        ):
            raise StoredExchangeDataError("completed exchange hydration data is incomplete")
        result.append(
            SessionExchangeArtifactV1(
                exchange_id=exchange.request_id,
                request_id=exchange.request_id,
                assistant_turn_id=exchange.assistant_turn_id,
                session_revision=exchange.session_revision,
                completed_at=exchange.completed_at,
                artifacts=exchange.artifacts,
            )
        )
    return result


def orchestrate_exchange_events(
    events: Iterator[dict[str, object]],
    *,
    exchange: AskExchange,
    db_path: Path,
) -> Iterator[dict[str, object]]:
    """Persist one externally owned engine stream before releasing its final.

    Engine progress remains streaming, but its terminal frame is held until all
    trailing grounding/citation/proposal events have been consumed and the
    assistant turn plus typed artifacts commit. A provider or engine error moves
    the pending exchange to ``failed`` and exposes only a stable retry message.
    """

    if exchange.status != "pending":
        raise ExchangeStateError("only a pending Ask exchange can consume engine events")
    held_final: dict[str, object] | None = None
    citations: list[object] | None = None
    route: str | None = None
    view_spec: dict[str, JsonValue] | None = None
    proposal_ref: AskProposalRefV1 | str | None = None
    proposal_error: ProposalErrorV1 | None = None
    source_links: list[str] = []
    fact_links: list[str] = []
    try:
        for event in events:
            kind = event.get("type")
            if kind == "final":
                if held_final is not None:
                    raise ExchangeStateError("Ask engine emitted more than one final event")
                held_final = dict(event)
                maybe_route = event.get("route")
                route = str(maybe_route) if isinstance(maybe_route, str) and maybe_route else None
                continue
            if kind == "error":
                _fail_pending_exchange(exchange, db_path=db_path)
                yield {"type": "error", "error": "ask failed; retry the request"}
                return
            if kind == "fragment":
                maybe_spec = event.get("spec")
                if isinstance(maybe_spec, dict):
                    view_spec = cast("dict[str, JsonValue]", maybe_spec)
            elif kind == "citations":
                maybe_items = event.get("items")
                if isinstance(maybe_items, list):
                    citations = cast("list[object]", maybe_items)
                    source_links, fact_links = _artifact_links(citations)
            elif kind in {"diff_proposal", "proposal_ref"}:
                proposal_ref = _proposal_reference(event)
                # The governed reference remains inactive until completion commits;
                # emit only the transactionally activated persisted form below.
                if isinstance(proposal_ref, AskProposalRefV1):
                    continue
            elif kind == "proposal_error":
                raw_code = event.get("code")
                raw_message = event.get("message", event.get("error"))
                proposal_error = ProposalErrorV1(
                    code=str(raw_code or "registration_failed"),
                    message=str(raw_message or "proposal could not be registered"),
                )
            yield event

        if held_final is None:
            raise ExchangeStateError("Ask engine ended without a final event")
        final_text = held_final.get("text")
        if not isinstance(final_text, str) or not final_text.strip():
            raise ExchangeStateError("Ask engine final text is empty")
        artifacts = ExchangeArtifactsV1(
            route=route,
            view_spec=view_spec,
            proposal_ref=proposal_ref,
            proposal_error=proposal_error,
            source_links=source_links,
            fact_links=fact_links,
        )
        completed = complete_exchange(
            request_id=exchange.request_id,
            assistant_text=final_text,
            artifacts=artifacts,
            expected_revision=exchange.session_revision,
            citations=citations,
            db_path=db_path,
        )
        if completed.artifacts is not None and isinstance(
            completed.artifacts.proposal_ref, AskProposalRefV1
        ):
            yield {
                "type": "proposal_ref",
                "proposal_ref": _proposal_ref_payload(completed.artifacts.proposal_ref),
            }
        terminal = dict(held_final)
        terminal["session_revision"] = completed.session_revision
        yield terminal
    except Exception as exc:
        _fail_pending_exchange(exchange, db_path=db_path)
        _LOGGER.error("durable Ask exchange failed: %s", type(exc).__name__)
        yield {"type": "error", "error": "ask failed; retry the request"}


def replay_exchange_events(
    exchange: AskExchange,
    *,
    db_path: Path,
    session_revision: int | None = None,
) -> Iterator[dict[str, object]]:
    """Reconstruct UI-sufficient events for one completed request without work."""

    if exchange.status != "completed" or exchange.assistant_turn_id is None:
        raise ExchangeStateError("only a completed Ask exchange can be replayed")
    connection = _open(db_path)
    try:
        row = connection.execute(
            "SELECT role,text,citations_json FROM ask_turns WHERE id=? AND session_id=?",
            (exchange.assistant_turn_id, exchange.session_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None or str(row["role"]) != "assistant":
        raise StoredExchangeDataError("completed Ask exchange assistant turn is missing")
    citations = _decode_citations(row["citations_json"])
    artifacts = exchange.artifacts
    if artifacts is None:
        raise StoredExchangeDataError("completed Ask exchange artifacts are missing")
    yield {
        "type": "artifacts",
        "route": artifacts.route,
        "view_spec": artifacts.view_spec,
        "proposal_ref": _proposal_ref_payload(artifacts.proposal_ref),
        "proposal_error": (
            artifacts.proposal_error.model_dump(mode="json")
            if artifacts.proposal_error is not None
            else None
        ),
        "source_links": list(artifacts.source_links),
        "fact_links": list(artifacts.fact_links),
        "replay": True,
    }
    if citations:
        yield {"type": "citations", "items": citations, "replay": True}
    if artifacts.proposal_ref is not None:
        yield {
            "type": "proposal_ref",
            "proposal_ref": _proposal_ref_payload(artifacts.proposal_ref),
            "replay": True,
        }
    if artifacts.proposal_error is not None:
        yield {
            "type": "proposal_error",
            "code": artifacts.proposal_error.code,
            "message": artifacts.proposal_error.message,
            "error": artifacts.proposal_error.message,
            "replay": True,
        }
    yield {
        "type": "final",
        "text": str(row["text"]),
        "route": artifacts.route,
        "session_revision": (
            session_revision if session_revision is not None else exchange.session_revision
        ),
        "replay": True,
    }


def _open(db_path: Path) -> sqlite3.Connection:
    connection = connect_sqlite(
        db_path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _select_context(connection: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM ask_session_contexts WHERE session_id=?",
        (session_id,),
    ).fetchone()


def _select_exchange(connection: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT exchange.*,artifact.schema_version AS artifact_schema_version,"
        "artifact.artifacts_json,artifact.artifacts_sha256 "
        "FROM ask_exchanges exchange LEFT JOIN ask_exchange_artifacts artifact "
        "ON artifact.request_id=exchange.request_id WHERE exchange.request_id=?",
        (request_id,),
    ).fetchone()


def _context_from_row(row: sqlite3.Row) -> SessionContextRecord:
    raw_json = str(row["context_json"])
    committed_sha256 = str(row["context_sha256"])
    if _sha256(raw_json) != committed_sha256:
        raise StoredExchangeDataError("stored Ask session context hash does not match its JSON")
    try:
        context = SessionContextV1.model_validate_json(raw_json)
    except ValidationError as exc:
        raise StoredExchangeDataError("stored Ask session context is invalid") from exc
    if context.schema_version != str(row["schema_version"]):
        raise StoredExchangeDataError("stored Ask session context schema version is inconsistent")
    return SessionContextRecord(
        session_id=str(row["session_id"]),
        context=context,
        context_sha256=committed_sha256,
        revision=int(row["revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _exchange_from_row(row: sqlite3.Row) -> AskExchange:
    raw_status = str(row["status"])
    if raw_status not in {"pending", "completed", "failed"}:
        raise StoredExchangeDataError(f"stored Ask exchange has invalid status {raw_status!r}")
    status = cast("ExchangeStatus", raw_status)
    artifacts: ExchangeArtifactsV1 | None = None
    raw_artifacts = row["artifacts_json"]
    if raw_artifacts is not None:
        artifacts_json = str(raw_artifacts)
        committed_sha256 = str(row["artifacts_sha256"])
        if _sha256(artifacts_json) != committed_sha256:
            raise StoredExchangeDataError("stored Ask exchange artifact hash is invalid")
        try:
            artifacts = ExchangeArtifactsV1.model_validate_json(artifacts_json)
        except ValidationError as exc:
            raise StoredExchangeDataError("stored Ask exchange artifacts are invalid") from exc
        if artifacts.schema_version != str(row["artifact_schema_version"]):
            raise StoredExchangeDataError("stored Ask artifact schema version is inconsistent")
    if status == "completed" and artifacts is None:
        raise StoredExchangeDataError("completed Ask exchange is missing artifacts")
    if status != "completed" and artifacts is not None:
        raise StoredExchangeDataError("non-completed Ask exchange unexpectedly has artifacts")
    raw_assistant_turn_id = row["assistant_turn_id"]
    raw_error_code = row["error_code"]
    raw_completed_at = row["completed_at"]
    raw_failed_at = row["failed_at"]
    return AskExchange(
        request_id=str(row["request_id"]),
        session_id=str(row["session_id"]),
        payload_sha256=str(row["payload_sha256"]),
        status=status,
        user_turn_id=int(row["user_turn_id"]),
        assistant_turn_id=(
            int(raw_assistant_turn_id) if raw_assistant_turn_id is not None else None
        ),
        artifacts=artifacts,
        error_code=str(raw_error_code) if raw_error_code is not None else None,
        session_revision=int(row["session_revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        completed_at=str(raw_completed_at) if raw_completed_at is not None else None,
        failed_at=str(raw_failed_at) if raw_failed_at is not None else None,
    )


def _current_revision(connection: sqlite3.Connection, session_id: str) -> int:
    row = connection.execute(
        "SELECT revision FROM ask_session_contexts WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise ExchangeStateError(f"Ask session {session_id!r} has no versioned historical context")
    return int(row["revision"])


def _require_transition_revision(
    connection: sqlite3.Connection,
    exchange: AskExchange,
    expected_revision: int,
) -> None:
    current_revision = _current_revision(connection, exchange.session_id)
    if current_revision != expected_revision or exchange.session_revision != expected_revision:
        raise RevisionConflictError(
            f"Ask session revision mismatch: expected {expected_revision}, "
            f"found session={current_revision}, exchange={exchange.session_revision}"
        )


def _advance_revision(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    expected_revision: int,
    next_revision: int,
    timestamp: str,
) -> None:
    updated = connection.execute(
        "UPDATE ask_session_contexts SET revision=?,updated_at=? WHERE session_id=? AND revision=?",
        (next_revision, timestamp, session_id, expected_revision),
    )
    if updated.rowcount != 1:
        raise RevisionConflictError(f"Ask session revision advanced beyond {expected_revision}")


def _touch_session(connection: sqlite3.Connection, session_id: str, timestamp: str) -> None:
    updated = connection.execute(
        "UPDATE ask_sessions SET updated_at=? WHERE id=?",
        (timestamp, session_id),
    )
    if updated.rowcount != 1:
        raise ExchangeStateError(f"unknown Ask session {session_id!r}")


def _citations_json(citations: Sequence[object] | None) -> str | None:
    if citations is None:
        return None
    validated = _validated_json(list(citations))
    return _canonical_json(validated)


def _decode_citations(raw: object) -> list[object]:
    if raw is None:
        return []
    try:
        parsed = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise StoredExchangeDataError("stored Ask citations are invalid JSON") from exc
    validated = _validated_json(parsed)
    if not isinstance(validated, list):
        raise StoredExchangeDataError("stored Ask citations must be a JSON array")
    return cast("list[object]", validated)


def _artifact_links(citations: Sequence[object]) -> tuple[list[str], list[str]]:
    sources: list[str] = []
    facts: list[str] = []
    for raw in citations:
        if not isinstance(raw, dict):
            continue
        item = cast("dict[str, object]", raw)
        source = _bounded_artifact_link(item.get("href"))
        if source is None:
            source = _bounded_artifact_link(item.get("source_ref"))
        if source is not None and source not in sources and len(sources) < 100:
            sources.append(source)
        fact = _bounded_artifact_link(item.get("fact_ref"))
        if fact is None and item.get("candidate_kind") == "fact":
            fact = _bounded_artifact_link(item.get("candidate_id"))
        if fact is not None and fact not in facts and len(facts) < 100:
            facts.append(fact)
    return sources, facts


def _bounded_artifact_link(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if 1 <= len(normalized) <= 512 else None


def _proposal_reference(event: dict[str, object]) -> AskProposalRefV1 | str | None:
    raw_direct = event.get("proposal_ref")
    if isinstance(raw_direct, dict):
        try:
            return AskProposalRefV1.model_validate(raw_direct)
        except ValueError:
            return None
    direct = _bounded_artifact_link(event.get("proposal_ref"))
    if direct is not None:
        return direct[:256]
    raw_diff = event.get("diff")
    if not isinstance(raw_diff, dict):
        return None
    nested = _bounded_artifact_link(cast("dict[str, object]", raw_diff).get("proposal_ref"))
    return nested[:256] if nested is not None else None


def _proposal_ref_payload(value: AskProposalRefV1 | str | None) -> dict[str, object] | str | None:
    return value.model_dump(mode="json") if isinstance(value, AskProposalRefV1) else value


def _fail_pending_exchange(exchange: AskExchange, *, db_path: Path) -> None:
    try:
        current = get_exchange(exchange.request_id, db_path=db_path)
        if current is not None and current.status == "pending":
            fail_exchange(
                request_id=current.request_id,
                error_code="engine_failed",
                expected_revision=current.session_revision,
                db_path=db_path,
            )
    except Exception:
        _LOGGER.error("failed to record durable Ask exchange failure")


def _validated_json(value: object) -> CanonicalJson:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, list):
        raw_items = cast("list[object]", value)
        return [_validated_json(item) for item in raw_items]
    if isinstance(value, dict):
        raw_mapping = cast("dict[object, object]", value)
        validated: dict[str, CanonicalJson] = {}
        for key, item in raw_mapping.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            validated[key] = _validated_json(item)
        return validated
    raise ValueError(f"value of type {type(value).__name__} is not JSON-compatible")


def _model_json(model: BaseModel) -> str:
    return _canonical_json(model.model_dump(mode="json"))


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_identifier(name: str, value: str) -> None:
    if not 1 <= len(value) <= 128:
        raise ValueError(f"{name} must contain between 1 and 128 characters")


def _validate_request_id(request_id: str) -> None:
    if _REQUEST_ID_RE.fullmatch(request_id) is None:
        raise ValueError("request_id contains unsupported characters or exceeds 128 characters")


def _validate_sha256(name: str, value: str) -> None:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase hexadecimal SHA-256")


def _validate_turn_text(name: str, value: str) -> None:
    if not value.strip() or len(value) > _MAX_TURN_CHARS:
        raise ValueError(f"{name} must be non-empty and at most {_MAX_TURN_CHARS} characters")


def _validate_revision(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("expected_revision must be a non-negative integer")


__all__ = [
    "AskExchange",
    "BeginExchangeResult",
    "ExchangeArtifactsV1",
    "ExchangeConflictError",
    "ExchangeStateError",
    "ExchangeStoreError",
    "PendingExchangeError",
    "ResearchContextV1",
    "RevisionConflictError",
    "SessionContextConflictError",
    "SessionContextRecord",
    "SessionContextV1",
    "StoredExchangeDataError",
    "begin_exchange",
    "complete_exchange",
    "fail_exchange",
    "get_exchange",
    "get_session_context",
    "hash_request_payload",
    "orchestrate_exchange_events",
    "put_session_context",
    "replay_exchange_events",
]
