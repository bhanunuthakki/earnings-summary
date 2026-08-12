"""Durable FMP circuit breaking, recovery work, leases, and receipts.

The core is intentionally transport- and filesystem-free. Callers prove what
is already available, receive durable leases, end the database transaction,
perform external work, and then atomically record sanitized outcomes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.companies import ListType
from pipeline.source_policy import (
    ArtifactKind,
    CollectionSource,
    decision_for,
)

PROVIDER = "fmp"
SCREENING_ENDPOINT_KEYS: frozenset[str] = frozenset(
    {
        "profile",
        "peers",
        "key_metrics_ttm",
        "financial_ratios_ttm",
        "income_statement_quarterly",
        "key_metrics_quarterly",
        "balance_sheet_quarterly",
        "historical_market_cap",
    }
)


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class WorkState(StrEnum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    SATISFIED = "SATISFIED"
    TERMINAL = "TERMINAL"


class ExecutionMode(StrEnum):
    LIVE = "LIVE"
    PROBE = "PROBE"
    CORPUS = "CORPUS"
    ALTERNATIVE = "ALTERNATIVE"
    RECONCILE = "RECONCILE"
    UNAVAILABLE = "UNAVAILABLE"
    ALREADY_SATISFIED = "ALREADY_SATISFIED"
    ALREADY_APPLIED_CORPUS = "ALREADY_APPLIED_CORPUS"


class CredentialAvailability(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    INVALID = "invalid"


class OutcomeCode(StrEnum):
    LIVE_SUCCESS = "live_success"
    CORPUS_SUCCESS = "corpus_success"
    ALTERNATIVE_SUCCESS = "alternative_success"
    RECONCILED_SUCCESS = "reconciled_success"
    HTTP_UNAUTHORIZED = "http_unauthorized"
    ACCOUNT_PAYMENT_REQUIRED = "account_payment_required"
    ACCOUNT_AUTH_FORBIDDEN = "account_auth_forbidden"
    ENDPOINT_FORBIDDEN = "endpoint_forbidden"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    TRANSPORT_ERROR = "transport_error"
    CLIENT_CONTRACT_ERROR = "client_contract_error"
    ENDPOINT_EMPTY = "endpoint_empty"
    CORPUS_UNAVAILABLE = "corpus_unavailable"


class ReceiptStatus(StrEnum):
    FRESH = "FRESH"
    DEGRADED_CORPUS = "DEGRADED_CORPUS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class WorkPriority(IntEnum):
    INDEX_SCREENING = 100
    REQUESTED_EVALUATION = 200
    PORTFOLIO = 300


class AlternativeSource(StrEnum):
    SEC = "sec"


class AlternativeCoverageState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    STALE = "stale"
    DISPUTED = "disputed"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _validate_sha256(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return value


def _validate_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        raise ValueError("timestamps must use the repository naive-UTC convention")
    return value


def _validate_optional_naive_utc(value: datetime | None) -> datetime | None:
    return _validate_naive_utc(value) if value is not None else None


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


class CircuitConfig(_FrozenModel):
    transient_failure_threshold: int = Field(default=3, ge=1, le=100)
    rate_limit_threshold: int = Field(default=3, ge=1, le=100)
    retry_delay_seconds: int = Field(default=60, ge=0, le=86_400)
    probe_delay_seconds: int = Field(default=900, ge=1, le=604_800)
    auth_probe_delay_seconds: int = Field(default=21_600, ge=1, le=604_800)
    rate_limit_probe_delay_seconds: int = Field(default=900, ge=1, le=604_800)


class CorpusSnapshot(_FrozenModel):
    cache_generation_id: str = Field(min_length=1, max_length=256)
    content_sha256: str
    captured_at: datetime

    _content_hash = field_validator("content_sha256")(_validate_sha256)
    _captured_at = field_validator("captured_at")(_validate_naive_utc)


class FmpSnapshotProof(_FrozenModel):
    work_id: str
    cache_generation_id: str = Field(min_length=1, max_length=256)
    policy_sha256: str
    content_sha256: str
    captured_at: datetime

    _hashes = field_validator("work_id", "policy_sha256", "content_sha256")(_validate_sha256)
    _captured_at = field_validator("captured_at")(_validate_naive_utc)


class AlternativeResolution(_FrozenModel):
    source: AlternativeSource
    policy_sha256: str
    endpoint_key: str = Field(min_length=1, max_length=96, pattern=r"^[a-z0-9_]+$")
    period_key: str = Field(min_length=1, max_length=96)
    concept_keys: tuple[str, ...] = Field(min_length=1)
    evidence_fresh_at: datetime
    source_authorized: bool
    has_unresolved_disagreement: bool
    coverage_state: AlternativeCoverageState
    coverage_proof_sha256: str | None = None
    evidence_ids: tuple[int, ...] = Field(min_length=1)
    fact_ids: tuple[int, ...] = Field(min_length=1)

    _policy_hash = field_validator("policy_sha256")(_validate_sha256)

    @property
    def canonical_proof_sha256(self) -> str:
        return _sha256_json(
            {
                "concept_keys": self.concept_keys,
                "endpoint_key": self.endpoint_key,
                "evidence_fresh_at": _iso(self.evidence_fresh_at),
                "evidence_ids": self.evidence_ids,
                "fact_ids": self.fact_ids,
                "has_unresolved_disagreement": self.has_unresolved_disagreement,
                "period_key": self.period_key,
                "policy_sha256": self.policy_sha256,
                "source": self.source.value,
                "source_authorized": self.source_authorized,
            }
        )

    _evidence_fresh_at = field_validator("evidence_fresh_at")(_validate_naive_utc)

    @field_validator("concept_keys")
    @classmethod
    def _canonical_concepts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({item.strip() for item in value if item.strip()}))
        if not normalized:
            raise ValueError("alternative resolution requires canonical concepts")
        return normalized

    @field_validator("evidence_ids", "fact_ids")
    @classmethod
    def _canonical_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(identifier <= 0 for identifier in value):
            raise ValueError("evidence and fact IDs must be positive")
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def _require_satisfying_proof(self) -> Self:
        if self.coverage_state is not AlternativeCoverageState.COMPLETE:
            raise ValueError("only complete, current, undisputed coverage may satisfy work")
        if not self.source_authorized or self.has_unresolved_disagreement:
            raise ValueError("alternative resolution must be authorized and undisputed")
        if self.coverage_proof_sha256 not in {None, self.canonical_proof_sha256}:
            raise ValueError("alternative coverage proof hash does not match typed proof")
        return self


class WorkSpec(_FrozenModel):
    ticker: str = Field(min_length=1, max_length=16)
    coverage_role: ListType
    artifact_kind: ArtifactKind = ArtifactKind.FINANCIAL_FACT
    endpoint_key: str = Field(min_length=1, max_length=96, pattern=r"^[a-z0-9_]+$")
    period_key: str = Field(min_length=1, max_length=96)
    cache_generation_id: str = Field(min_length=1, max_length=256)
    policy_sha256: str
    requested: bool = False
    owner_request_id: str | None = Field(default=None, min_length=1, max_length=256)
    corpus_snapshot: CorpusSnapshot | None = None
    fmp_snapshot: FmpSnapshotProof | None = None
    alternative_resolution: AlternativeResolution | None = None

    _policy_hash = field_validator("policy_sha256")(_validate_sha256)

    @field_validator("ticker")
    @classmethod
    def _uppercase_ticker(cls, value: str) -> str:
        ticker = value.strip().upper()
        if not ticker or not ticker.replace(".", "").replace("-", "").isalnum():
            raise ValueError("ticker contains unsupported characters")
        return ticker

    @model_validator(mode="after")
    def _cross_field_contract(self) -> Self:
        if self.artifact_kind is not ArtifactKind.FINANCIAL_FACT:
            raise ValueError("FMP recovery owns financial-fact work only")
        if self.coverage_role is ListType.EVALUATION and (
            not self.requested or self.owner_request_id is None
        ):
            raise ValueError("evaluation recovery requires an owner request_id")
        if self.fmp_snapshot is not None and (
            self.fmp_snapshot.work_id != make_work_id(self)
            or self.fmp_snapshot.cache_generation_id != self.cache_generation_id
            or self.fmp_snapshot.policy_sha256 != self.policy_sha256
        ):
            raise ValueError("FMP snapshot proof must match work generation and policy")
        if self.alternative_resolution is not None and (
            self.alternative_resolution.policy_sha256 != self.policy_sha256
            or self.alternative_resolution.endpoint_key != self.endpoint_key
            or self.alternative_resolution.period_key != self.period_key
        ):
            raise ValueError("alternative resolution must match work policy, endpoint, and period")
        return self


def make_work_id(spec: WorkSpec) -> str:
    return _sha256_json(
        {
            "artifact_kind": spec.artifact_kind.value,
            "cache_generation_id": spec.cache_generation_id,
            "endpoint_key": spec.endpoint_key,
            "period_key": spec.period_key,
            "policy_sha256": spec.policy_sha256,
            "provider": PROVIDER,
            "ticker": spec.ticker,
        }
    )


class PlanRunRequest(_FrozenModel):
    run_id: str = Field(min_length=1, max_length=256)
    worker_id: str = Field(min_length=1, max_length=256)
    now: datetime
    lease_seconds: int = Field(default=300, ge=1, le=86_400)
    credentials: CredentialAvailability
    circuit_config: CircuitConfig = Field(default_factory=CircuitConfig)
    work: tuple[WorkSpec, ...] = Field(min_length=1, max_length=500)

    _now = field_validator("now")(_validate_naive_utc)

    @model_validator(mode="after")
    def _unique_work(self) -> Self:
        identifiers = tuple(make_work_id(spec) for spec in self.work)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("plan contains duplicate FMP recovery work")
        return self


class EnqueueWorkRequest(_FrozenModel):
    now: datetime
    circuit_config: CircuitConfig = Field(default_factory=CircuitConfig)
    work: tuple[WorkSpec, ...] = Field(min_length=1, max_length=500)

    _now = field_validator("now")(_validate_naive_utc)

    @model_validator(mode="after")
    def _unique_work(self) -> Self:
        identifiers = tuple(make_work_id(spec) for spec in self.work)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("enqueue contains duplicate FMP recovery work")
        return self


class RecoveryAvailability(_FrozenModel):
    work_id: str
    corpus_snapshot: CorpusSnapshot | None = None
    fmp_snapshot: FmpSnapshotProof | None = None
    alternative_resolution: AlternativeResolution | None = None

    _work_hash = field_validator("work_id")(_validate_sha256)


class RecoverableWorkRequest(_FrozenModel):
    run_id: str = Field(min_length=1, max_length=256)
    worker_id: str = Field(min_length=1, max_length=256)
    now: datetime
    lease_seconds: int = Field(default=300, ge=1, le=86_400)
    credentials: CredentialAvailability
    provider_calls_permitted: bool = True
    availability: tuple[RecoveryAvailability, ...] = Field(default=(), max_length=500)
    allowed_work_ids: tuple[str, ...] | None = Field(default=None, max_length=500)
    limit: int = Field(default=100, ge=1, le=500)

    _now = field_validator("now")(_validate_naive_utc)

    @model_validator(mode="after")
    def _unique_availability(self) -> Self:
        identifiers = tuple(item.work_id for item in self.availability)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("recovery availability contains duplicate work IDs")
        if self.allowed_work_ids is not None:
            if len(set(self.allowed_work_ids)) != len(self.allowed_work_ids):
                raise ValueError("allowed recovery work IDs must be unique")
            for work_id in self.allowed_work_ids:
                _validate_sha256(work_id)
        return self


class PlannedWork(_FrozenModel):
    work_id: str
    ticker: str
    priority: int
    endpoint_key: str | None = None
    period_key: str | None = None
    cache_generation_id: str | None = None
    policy_sha256: str | None = None
    execution_mode: ExecutionMode
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    corpus_snapshot: CorpusSnapshot | None = None
    fmp_snapshot: FmpSnapshotProof | None = None
    alternative_resolution: AlternativeResolution | None = None


class BacklogStatus(_FrozenModel):
    pending_count: int
    leased_count: int
    satisfied_count: int
    terminal_count: int
    oldest_pending_age_seconds: float | None
    next_probe_at: datetime | None


class EnqueueWorkReceipt(_FrozenModel):
    work_ids: tuple[str, ...]
    enqueued_count: int
    backlog: BacklogStatus


class RunPlan(_FrozenModel):
    run_id: str
    circuit_state: CircuitState
    circuit_revision: int
    items: tuple[PlannedWork, ...]
    backlog: BacklogStatus


class WorkOutcome(_FrozenModel):
    work_id: str
    lease_token: str = Field(min_length=1, max_length=256)
    outcome_code: OutcomeCode
    observed_at: datetime
    http_status: int | None = Field(default=None, ge=100, le=599)
    retry_after_at: datetime | None = None
    corpus_snapshot: CorpusSnapshot | None = None
    fmp_snapshot: FmpSnapshotProof | None = None
    alternative_resolution: AlternativeResolution | None = None

    _work_hash = field_validator("work_id")(_validate_sha256)
    _observed_at = field_validator("observed_at")(_validate_naive_utc)
    _retry_after_at = field_validator("retry_after_at")(_validate_optional_naive_utc)

    @model_validator(mode="after")
    def _outcome_contract(self) -> Self:
        expected_status: dict[OutcomeCode, int] = {
            OutcomeCode.HTTP_UNAUTHORIZED: 401,
            OutcomeCode.ACCOUNT_PAYMENT_REQUIRED: 402,
            OutcomeCode.ACCOUNT_AUTH_FORBIDDEN: 403,
            OutcomeCode.ENDPOINT_FORBIDDEN: 403,
            OutcomeCode.RATE_LIMITED: 429,
        }
        exact = expected_status.get(self.outcome_code)
        if exact is not None and self.http_status != exact:
            raise ValueError(f"{self.outcome_code.value} requires HTTP {exact}")
        if self.outcome_code is OutcomeCode.LIVE_SUCCESS:
            if not (self.http_status is not None and 200 <= self.http_status <= 299):
                raise ValueError("live success requires a 2xx HTTP status")
            if self.fmp_snapshot is None:
                raise ValueError("live success requires a persisted FMP snapshot proof")
        if self.outcome_code is OutcomeCode.SERVER_ERROR and not (
            self.http_status is not None and 500 <= self.http_status <= 599
        ):
            raise ValueError("server error requires a 5xx HTTP status")
        if self.outcome_code is OutcomeCode.CLIENT_CONTRACT_ERROR and not (
            self.http_status is not None
            and (
                200 <= self.http_status <= 299
                or (400 <= self.http_status <= 499 and self.http_status not in {401, 402, 403, 429})
            )
        ):
            raise ValueError("client contract error requires a 2xx or non-auth client status")
        if self.outcome_code is OutcomeCode.ENDPOINT_EMPTY and not (
            self.http_status is not None and 200 <= self.http_status <= 299
        ):
            raise ValueError("endpoint empty requires a 2xx HTTP status")
        if self.outcome_code is OutcomeCode.TRANSPORT_ERROR and self.http_status is not None:
            raise ValueError("transport error must not invent an HTTP status")
        if self.outcome_code is OutcomeCode.CORPUS_SUCCESS and self.corpus_snapshot is None:
            raise ValueError("corpus success requires a typed corpus snapshot")
        if self.outcome_code is OutcomeCode.ALTERNATIVE_SUCCESS and (
            self.alternative_resolution is None
        ):
            raise ValueError("alternative success requires a canonical resolution")
        if self.outcome_code is OutcomeCode.RECONCILED_SUCCESS and self.fmp_snapshot is None:
            raise ValueError("reconciled success requires an FMP snapshot proof")
        if self.outcome_code is not OutcomeCode.RATE_LIMITED and self.retry_after_at is not None:
            raise ValueError("retry_after_at is valid only for rate limiting")
        return self


class RecordOutcomesRequest(_FrozenModel):
    run_id: str = Field(min_length=1, max_length=256)
    now: datetime
    expected_work_ids: tuple[str, ...] = Field(min_length=1, max_length=500)
    outcomes: tuple[WorkOutcome, ...] = Field(min_length=1, max_length=500)

    _now = field_validator("now")(_validate_naive_utc)

    @field_validator("expected_work_ids")
    @classmethod
    def _expected_hashes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_sha256(value) for value in values)

    @model_validator(mode="after")
    def _unique_ordered_outcomes(self) -> Self:
        if len(set(self.expected_work_ids)) != len(self.expected_work_ids):
            raise ValueError("expected work contains duplicate work IDs")
        identifiers = tuple(item.work_id for item in self.outcomes)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("outcome batch contains duplicate work IDs")
        if not set(identifiers).issubset(self.expected_work_ids):
            raise ValueError("outcome work is not part of the expected run plan")
        observed = tuple(item.observed_at for item in self.outcomes)
        if observed != tuple(sorted(observed)):
            raise ValueError("outcomes must be ordered by observation time")
        if any(item.observed_at > self.now for item in self.outcomes):
            raise ValueError("outcome observation cannot be after recording time")
        return self


class RefreshReceipt(_FrozenModel):
    run_id: str
    status: ReceiptStatus
    fresh_count: int
    corpus_count: int
    failed_count: int
    corpus_age_seconds: float | None
    circuit_state: CircuitState
    circuit_revision: int
    backlog: BacklogStatus


@contextmanager
def _short_transaction(connection: sqlite3.Connection) -> Generator[None, None, None]:
    if connection.in_transaction:
        raise RuntimeError("FMP recovery requires a connection with no active transaction")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _priority_for(spec: WorkSpec) -> WorkPriority:
    if spec.coverage_role is ListType.PORTFOLIO:
        return WorkPriority.PORTFOLIO
    if spec.coverage_role is ListType.EVALUATION and spec.requested:
        return WorkPriority.REQUESTED_EVALUATION
    if spec.coverage_role is ListType.INDEX_MEMBER:
        return WorkPriority.INDEX_SCREENING
    raise ValueError(f"unsupported FMP recovery role: {spec.coverage_role.value}")


def _authorize(spec: WorkSpec) -> None:
    decision = decision_for(
        spec.coverage_role,
        CollectionSource.FMP,
        spec.artifact_kind,
        requested=spec.requested,
    )
    if not decision.allowed:
        raise ValueError(
            f"FMP recovery work is not authorized: {spec.ticker} ({decision.reason.value})"
        )
    if (
        spec.coverage_role is ListType.INDEX_MEMBER
        and spec.endpoint_key not in SCREENING_ENDPOINT_KEYS
    ):
        raise ValueError(
            f"FMP recovery work is not authorized: index endpoint {spec.endpoint_key!r}"
        )


def _ensure_circuit(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    config: CircuitConfig,
) -> sqlite3.Row:
    connection.execute(
        """
        INSERT OR IGNORE INTO provider_circuit_state (
            provider,state,revision,consecutive_failures,consecutive_rate_limits,
            transient_failure_threshold,rate_limit_threshold,retry_delay_seconds,
            probe_delay_seconds,auth_probe_delay_seconds,rate_limit_probe_delay_seconds,
            updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            PROVIDER,
            CircuitState.CLOSED.value,
            0,
            0,
            0,
            config.transient_failure_threshold,
            config.rate_limit_threshold,
            config.retry_delay_seconds,
            config.probe_delay_seconds,
            config.auth_probe_delay_seconds,
            config.rate_limit_probe_delay_seconds,
            _iso(now),
        ),
    )
    row = _circuit_row(connection)
    persisted = (
        row["transient_failure_threshold"],
        row["rate_limit_threshold"],
        row["retry_delay_seconds"],
        row["probe_delay_seconds"],
        row["auth_probe_delay_seconds"],
        row["rate_limit_probe_delay_seconds"],
    )
    requested = (
        config.transient_failure_threshold,
        config.rate_limit_threshold,
        config.retry_delay_seconds,
        config.probe_delay_seconds,
        config.auth_probe_delay_seconds,
        config.rate_limit_probe_delay_seconds,
    )
    if persisted != requested:
        connection.execute(
            """
            UPDATE provider_circuit_state SET
                transient_failure_threshold=?,rate_limit_threshold=?,
                retry_delay_seconds=?,probe_delay_seconds=?,auth_probe_delay_seconds=?,
                rate_limit_probe_delay_seconds=?,revision=revision+1,updated_at=?
            WHERE provider=?
            """,
            (*requested, _iso(now), PROVIDER),
        )
        row = _circuit_row(connection)
    return row


def _circuit_row(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM provider_circuit_state WHERE provider=?", (PROVIDER,)
    ).fetchone()
    if row is None:
        raise RuntimeError("FMP circuit state is missing")
    return row


def _event(
    connection: sqlite3.Connection,
    *,
    event_key: object,
    recorded_at: datetime,
    event_type: str,
    reason_code: str | None = None,
    work_id: str | None = None,
    attempt_id: str | None = None,
    state_from: str | None = None,
    state_to: str | None = None,
    circuit_revision: int | None = None,
) -> None:
    event_id = _sha256_json(event_key)
    connection.execute(
        """
        INSERT OR IGNORE INTO fmp_recovery_events (
            event_id,provider,work_id,attempt_id,event_type,reason_code,state_from,
            state_to,circuit_revision,recorded_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            PROVIDER,
            work_id,
            attempt_id,
            event_type,
            reason_code,
            state_from,
            state_to,
            circuit_revision,
            _iso(recorded_at),
        ),
    )


def _open_circuit(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    reason: str,
    next_probe_at: datetime,
) -> sqlite3.Row:
    prior = _circuit_row(connection)
    prior_probe_at = _parse_time(prior["next_probe_at"])
    if (
        prior["state"] == CircuitState.OPEN.value
        and prior["last_reason_code"] == reason
        and prior_probe_at is not None
        and prior_probe_at >= next_probe_at
    ):
        return prior
    opened_at = prior["opened_at"] or _iso(now)
    connection.execute(
        """
        UPDATE provider_circuit_state SET
            state=?,revision=revision+1,opened_at=?,next_probe_at=?,
            probe_work_id=NULL,probe_lease_token=NULL,probe_lease_expires_at=NULL,
            last_reason_code=?,updated_at=?
        WHERE provider=?
        """,
        (
            CircuitState.OPEN.value,
            opened_at,
            _iso(next_probe_at),
            reason,
            _iso(now),
            PROVIDER,
        ),
    )
    current = _circuit_row(connection)
    _event(
        connection,
        event_key=("circuit-open", current["revision"], reason),
        recorded_at=now,
        event_type="circuit_opened",
        reason_code=reason,
        state_from=str(prior["state"]),
        state_to=CircuitState.OPEN.value,
        circuit_revision=int(current["revision"]),
    )
    return current


def _close_circuit(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    success_at: datetime,
    attempt_id: str,
) -> sqlite3.Row:
    prior = _circuit_row(connection)
    connection.execute(
        """
        UPDATE provider_circuit_state SET
            state=?,revision=revision+1,consecutive_failures=0,
            consecutive_rate_limits=0,opened_at=NULL,next_probe_at=NULL,
            probe_work_id=NULL,probe_lease_token=NULL,probe_lease_expires_at=NULL,
            last_reason_code=NULL,last_success_at=?,updated_at=?
        WHERE provider=?
        """,
        (CircuitState.CLOSED.value, _iso(success_at), _iso(now), PROVIDER),
    )
    current = _circuit_row(connection)
    _event(
        connection,
        event_key=("circuit-success", attempt_id),
        recorded_at=now,
        event_type="provider_success",
        state_from=str(prior["state"]),
        state_to=CircuitState.CLOSED.value,
        circuit_revision=int(current["revision"]),
        attempt_id=attempt_id,
    )
    return current


def _apply_credentials(
    connection: sqlite3.Connection,
    *,
    credentials: CredentialAvailability,
    now: datetime,
) -> sqlite3.Row:
    circuit = _circuit_row(connection)
    if credentials is CredentialAvailability.AVAILABLE:
        return circuit
    delay = int(circuit["auth_probe_delay_seconds"])
    reason = "auth_missing" if credentials is CredentialAvailability.MISSING else "auth_invalid"
    return _open_circuit(
        connection,
        now=now,
        reason=reason,
        next_probe_at=now + timedelta(seconds=delay),
    )


def _expire_leases(connection: sqlite3.Connection, *, now: datetime) -> None:
    expired = connection.execute(
        """
        SELECT work_id,lease_token,lease_mode FROM fmp_work_backlog
        WHERE state=? AND lease_expires_at <= ?
        ORDER BY work_id
        """,
        (WorkState.LEASED.value, _iso(now)),
    ).fetchall()
    for row in expired:
        connection.execute(
            """
            UPDATE fmp_work_backlog SET
                state=?,available_at=?,lease_owner=NULL,lease_token=NULL,
                lease_run_id=NULL,lease_mode=NULL,lease_expires_at=NULL,updated_at=?
            WHERE work_id=? AND lease_token=?
            """,
            (
                WorkState.PENDING.value,
                _iso(now),
                _iso(now),
                row["work_id"],
                row["lease_token"],
            ),
        )
        _event(
            connection,
            event_key=("lease-expired", row["work_id"], row["lease_token"]),
            recorded_at=now,
            event_type="lease_expired",
            reason_code="lease_expired",
            work_id=str(row["work_id"]),
            state_from=WorkState.LEASED.value,
            state_to=WorkState.PENDING.value,
        )
        circuit = _circuit_row(connection)
        if (
            row["lease_mode"] == ExecutionMode.PROBE.value
            and circuit["state"] == CircuitState.HALF_OPEN.value
            and circuit["probe_lease_token"] == row["lease_token"]
        ):
            _open_circuit(
                connection,
                now=now,
                reason="probe_lease_expired",
                next_probe_at=now + timedelta(seconds=int(circuit["probe_delay_seconds"])),
            )


def _upsert_work(
    connection: sqlite3.Connection,
    *,
    spec: WorkSpec,
    now: datetime,
) -> None:
    work_id = make_work_id(spec)
    connection.execute(
        """
        INSERT OR IGNORE INTO fmp_work_backlog (
            work_id,provider,ticker,coverage_role,artifact_kind,endpoint_key,period_key,
            cache_generation_id,policy_sha256,requested,owner_request_id,priority,state,
            available_at,attempt_count,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            work_id,
            PROVIDER,
            spec.ticker,
            spec.coverage_role.value,
            spec.artifact_kind.value,
            spec.endpoint_key,
            spec.period_key,
            spec.cache_generation_id,
            spec.policy_sha256,
            int(spec.requested),
            spec.owner_request_id,
            int(_priority_for(spec)),
            WorkState.PENDING.value,
            _iso(now),
            0,
            _iso(now),
            _iso(now),
        ),
    )
    priority = int(_priority_for(spec))
    connection.execute(
        """
        UPDATE fmp_work_backlog SET
            coverage_role=?,requested=?,owner_request_id=?,priority=?,
            state=CASE WHEN state=? THEN ? ELSE state END,
            terminal_reason_code=CASE WHEN state=? THEN NULL ELSE terminal_reason_code END,
            available_at=CASE WHEN state=? THEN ? ELSE available_at END,
            updated_at=?
        WHERE work_id=? AND priority < ?
        """,
        (
            spec.coverage_role.value,
            int(spec.requested),
            spec.owner_request_id,
            priority,
            WorkState.TERMINAL.value,
            WorkState.PENDING.value,
            WorkState.TERMINAL.value,
            WorkState.TERMINAL.value,
            _iso(now),
            _iso(now),
            work_id,
            priority,
        ),
    )


def _availability_from_spec(spec: WorkSpec) -> RecoveryAvailability:
    return RecoveryAvailability(
        work_id=make_work_id(spec),
        corpus_snapshot=spec.corpus_snapshot,
        fmp_snapshot=spec.fmp_snapshot,
        alternative_resolution=spec.alternative_resolution,
    )


def _proofs_match_work(row: sqlite3.Row, availability: RecoveryAvailability) -> None:
    if availability.fmp_snapshot is not None and (
        availability.fmp_snapshot.work_id != row["work_id"]
        or availability.fmp_snapshot.cache_generation_id != row["cache_generation_id"]
        or availability.fmp_snapshot.policy_sha256 != row["policy_sha256"]
    ):
        raise ValueError("FMP snapshot proof does not match queued work")
    if availability.alternative_resolution is not None and (
        availability.alternative_resolution.policy_sha256 != row["policy_sha256"]
        or availability.alternative_resolution.endpoint_key != row["endpoint_key"]
        or availability.alternative_resolution.period_key != row["period_key"]
    ):
        raise ValueError("alternative resolution policy does not match queued work")


def _claim_mode(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    availability: RecoveryAvailability,
    credentials: CredentialAvailability,
    provider_calls_permitted: bool,
    now: datetime,
) -> ExecutionMode:
    if availability.alternative_resolution is not None:
        return ExecutionMode.ALTERNATIVE
    if availability.fmp_snapshot is not None:
        return ExecutionMode.RECONCILE
    circuit = _circuit_row(connection)
    if provider_calls_permitted and credentials is CredentialAvailability.AVAILABLE:
        if circuit["state"] == CircuitState.CLOSED.value:
            return ExecutionMode.LIVE
        if (
            circuit["state"] == CircuitState.OPEN.value
            and (_parse_time(circuit["next_probe_at"]) or now) <= now
        ):
            return ExecutionMode.PROBE
    if availability.corpus_snapshot is not None:
        already_applied = connection.execute(
            """
            SELECT 1 FROM fmp_work_attempts
            WHERE work_id=? AND outcome_code=? AND corpus_generation_id=?
                AND corpus_content_sha256=?
            LIMIT 1
            """,
            (
                row["work_id"],
                OutcomeCode.CORPUS_SUCCESS.value,
                availability.corpus_snapshot.cache_generation_id,
                availability.corpus_snapshot.content_sha256,
            ),
        ).fetchone()
        if already_applied is not None:
            return ExecutionMode.ALREADY_APPLIED_CORPUS
        return ExecutionMode.CORPUS
    return ExecutionMode.UNAVAILABLE


def _lease_row(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    availability: RecoveryAvailability,
    mode: ExecutionMode,
    run_id: str,
    worker_id: str,
    now: datetime,
    lease_seconds: int,
) -> PlannedWork:
    token = uuid.uuid4().hex
    expires_at = now + timedelta(seconds=lease_seconds)
    changed = connection.execute(
        """
        UPDATE fmp_work_backlog SET
            state=?,lease_owner=?,lease_token=?,lease_run_id=?,lease_mode=?,
            lease_expires_at=?,updated_at=?
        WHERE work_id=? AND state=? AND available_at <= ?
        """,
        (
            WorkState.LEASED.value,
            worker_id,
            token,
            run_id,
            mode.value,
            _iso(expires_at),
            _iso(now),
            row["work_id"],
            WorkState.PENDING.value,
            _iso(now),
        ),
    ).rowcount
    if changed != 1:
        return PlannedWork(
            work_id=str(row["work_id"]),
            ticker=str(row["ticker"]),
            priority=int(row["priority"]),
            execution_mode=ExecutionMode.UNAVAILABLE,
        )
    if mode is ExecutionMode.PROBE:
        _circuit_row(connection)
        connection.execute(
            """
            UPDATE provider_circuit_state SET
                state=?,revision=revision+1,probe_work_id=?,probe_lease_token=?,
                probe_lease_expires_at=?,last_probe_at=?,updated_at=?
            WHERE provider=? AND state=? AND next_probe_at <= ?
            """,
            (
                CircuitState.HALF_OPEN.value,
                row["work_id"],
                token,
                _iso(expires_at),
                _iso(now),
                _iso(now),
                PROVIDER,
                CircuitState.OPEN.value,
                _iso(now),
            ),
        )
        current = _circuit_row(connection)
        if (
            current["state"] != CircuitState.HALF_OPEN.value
            or current["probe_lease_token"] != token
        ):
            raise RuntimeError("half-open probe claim lost its circuit compare-and-swap")
    _event(
        connection,
        event_key=("leased", row["work_id"], run_id, mode.value),
        recorded_at=now,
        event_type="work_leased",
        reason_code=mode.value.lower(),
        work_id=str(row["work_id"]),
        state_from=WorkState.PENDING.value,
        state_to=WorkState.LEASED.value,
    )
    return PlannedWork(
        work_id=str(row["work_id"]),
        ticker=str(row["ticker"]),
        priority=int(row["priority"]),
        endpoint_key=str(row["endpoint_key"]),
        period_key=str(row["period_key"]),
        cache_generation_id=str(row["cache_generation_id"]),
        policy_sha256=str(row["policy_sha256"]),
        execution_mode=mode,
        lease_token=token,
        lease_expires_at=expires_at,
        corpus_snapshot=availability.corpus_snapshot if mode is ExecutionMode.CORPUS else None,
        fmp_snapshot=availability.fmp_snapshot if mode is ExecutionMode.RECONCILE else None,
        alternative_resolution=(
            availability.alternative_resolution if mode is ExecutionMode.ALTERNATIVE else None
        ),
    )


def _claim_rows(
    connection: sqlite3.Connection,
    *,
    rows: Sequence[sqlite3.Row],
    availability_by_id: dict[str, RecoveryAvailability],
    credentials: CredentialAvailability,
    provider_calls_permitted: bool,
    run_id: str,
    worker_id: str,
    now: datetime,
    lease_seconds: int,
) -> tuple[PlannedWork, ...]:
    planned: list[PlannedWork] = []
    for row in rows:
        work_id = str(row["work_id"])
        availability = availability_by_id.get(work_id, RecoveryAvailability(work_id=work_id))
        _proofs_match_work(row, availability)
        if row["state"] == WorkState.SATISFIED.value:
            planned.append(
                PlannedWork(
                    work_id=work_id,
                    ticker=str(row["ticker"]),
                    priority=int(row["priority"]),
                    endpoint_key=str(row["endpoint_key"]),
                    period_key=str(row["period_key"]),
                    cache_generation_id=str(row["cache_generation_id"]),
                    policy_sha256=str(row["policy_sha256"]),
                    execution_mode=ExecutionMode.ALREADY_SATISFIED,
                )
            )
            continue
        if row["state"] == WorkState.LEASED.value:
            if row["lease_run_id"] == run_id and row["lease_owner"] == worker_id:
                mode = ExecutionMode(str(row["lease_mode"]))
                planned.append(
                    PlannedWork(
                        work_id=work_id,
                        ticker=str(row["ticker"]),
                        priority=int(row["priority"]),
                        endpoint_key=str(row["endpoint_key"]),
                        period_key=str(row["period_key"]),
                        cache_generation_id=str(row["cache_generation_id"]),
                        policy_sha256=str(row["policy_sha256"]),
                        execution_mode=mode,
                        lease_token=str(row["lease_token"]),
                        lease_expires_at=_parse_time(row["lease_expires_at"]),
                        corpus_snapshot=(
                            availability.corpus_snapshot if mode is ExecutionMode.CORPUS else None
                        ),
                        fmp_snapshot=(
                            availability.fmp_snapshot if mode is ExecutionMode.RECONCILE else None
                        ),
                        alternative_resolution=(
                            availability.alternative_resolution
                            if mode is ExecutionMode.ALTERNATIVE
                            else None
                        ),
                    )
                )
            else:
                planned.append(
                    PlannedWork(
                        work_id=work_id,
                        ticker=str(row["ticker"]),
                        priority=int(row["priority"]),
                        execution_mode=ExecutionMode.UNAVAILABLE,
                    )
                )
            continue
        if (
            row["state"] == WorkState.TERMINAL.value
            or (_parse_time(row["available_at"]) or now) > now
        ):
            planned.append(
                PlannedWork(
                    work_id=work_id,
                    ticker=str(row["ticker"]),
                    priority=int(row["priority"]),
                    execution_mode=ExecutionMode.UNAVAILABLE,
                )
            )
            continue
        mode = _claim_mode(
            connection,
            row=row,
            availability=availability,
            credentials=credentials,
            provider_calls_permitted=provider_calls_permitted,
            now=now,
        )
        if mode is ExecutionMode.UNAVAILABLE:
            planned.append(
                PlannedWork(
                    work_id=work_id,
                    ticker=str(row["ticker"]),
                    priority=int(row["priority"]),
                    execution_mode=mode,
                )
            )
            continue
        if mode is ExecutionMode.ALREADY_APPLIED_CORPUS:
            planned.append(
                PlannedWork(
                    work_id=work_id,
                    ticker=str(row["ticker"]),
                    priority=int(row["priority"]),
                    endpoint_key=str(row["endpoint_key"]),
                    period_key=str(row["period_key"]),
                    cache_generation_id=str(row["cache_generation_id"]),
                    policy_sha256=str(row["policy_sha256"]),
                    execution_mode=mode,
                )
            )
            continue
        planned.append(
            _lease_row(
                connection,
                row=row,
                availability=availability,
                mode=mode,
                run_id=run_id,
                worker_id=worker_id,
                now=now,
                lease_seconds=lease_seconds,
            )
        )
    return tuple(planned)


def _backlog_status(connection: sqlite3.Connection, *, now: datetime) -> BacklogStatus:
    counts = {
        str(row["state"]): int(row["count"])
        for row in connection.execute(
            "SELECT state,COUNT(*) AS count FROM fmp_work_backlog GROUP BY state"
        ).fetchall()
    }
    oldest = connection.execute(
        "SELECT MIN(created_at) FROM fmp_work_backlog WHERE state IN (?,?)",
        (WorkState.PENDING.value, WorkState.LEASED.value),
    ).fetchone()[0]
    oldest_age = (
        max(0.0, (now - datetime.fromisoformat(str(oldest))).total_seconds())
        if oldest is not None
        else None
    )
    circuit = _circuit_row(connection)
    return BacklogStatus(
        pending_count=counts.get(WorkState.PENDING.value, 0),
        leased_count=counts.get(WorkState.LEASED.value, 0),
        satisfied_count=counts.get(WorkState.SATISFIED.value, 0),
        terminal_count=counts.get(WorkState.TERMINAL.value, 0),
        oldest_pending_age_seconds=oldest_age,
        next_probe_at=_parse_time(circuit["next_probe_at"]),
    )


def plan_run(connection: sqlite3.Connection, request: PlanRunRequest) -> RunPlan:
    """Authorize, idempotently enqueue, and lease a bounded run plan."""
    for spec in request.work:
        _authorize(spec)
    with _short_transaction(connection):
        _ensure_circuit(connection, now=request.now, config=request.circuit_config)
        _expire_leases(connection, now=request.now)
        _apply_credentials(
            connection,
            credentials=request.credentials,
            now=request.now,
        )
        for spec in request.work:
            _upsert_work(connection, spec=spec, now=request.now)
        identifiers = tuple(make_work_id(spec) for spec in request.work)
        rows = connection.execute(
            """
            SELECT * FROM fmp_work_backlog
            WHERE work_id IN (SELECT value FROM json_each(?))
            ORDER BY priority DESC,created_at,ticker,work_id
            """,
            (_canonical_json(list(identifiers)),),
        ).fetchall()
        availability = {
            item.work_id: item for item in (_availability_from_spec(spec) for spec in request.work)
        }
        items = _claim_rows(
            connection,
            rows=rows,
            availability_by_id=availability,
            credentials=request.credentials,
            provider_calls_permitted=True,
            run_id=request.run_id,
            worker_id=request.worker_id,
            now=request.now,
            lease_seconds=request.lease_seconds,
        )
        circuit = _circuit_row(connection)
        backlog = _backlog_status(connection, now=request.now)
        return RunPlan(
            run_id=request.run_id,
            circuit_state=CircuitState(str(circuit["state"])),
            circuit_revision=int(circuit["revision"]),
            items=items,
            backlog=backlog,
        )


def enqueue_work(
    connection: sqlite3.Connection,
    request: EnqueueWorkRequest,
) -> EnqueueWorkReceipt:
    """Authorize and durably upsert desired work without leasing or external I/O."""
    for spec in request.work:
        _authorize(spec)
    identifiers = tuple(make_work_id(spec) for spec in request.work)
    with _short_transaction(connection):
        _ensure_circuit(connection, now=request.now, config=request.circuit_config)
        _expire_leases(connection, now=request.now)
        for spec in request.work:
            _upsert_work(connection, spec=spec, now=request.now)
        return EnqueueWorkReceipt(
            work_ids=identifiers,
            enqueued_count=len(identifiers),
            backlog=_backlog_status(connection, now=request.now),
        )


def recoverable_work(
    connection: sqlite3.Connection,
    request: RecoverableWorkRequest,
) -> RunPlan:
    """Lease previously authorized pending work without doing external I/O."""
    availability = {item.work_id: item for item in request.availability}
    with _short_transaction(connection):
        circuit = _circuit_row(connection)
        _expire_leases(connection, now=request.now)
        _apply_credentials(
            connection,
            credentials=request.credentials,
            now=request.now,
        )
        if request.allowed_work_ids is None:
            rows = connection.execute(
                """
                SELECT * FROM fmp_work_backlog
                WHERE state=? AND available_at <= ?
                ORDER BY priority DESC,created_at,ticker,work_id LIMIT ?
                """,
                (WorkState.PENDING.value, _iso(request.now), request.limit),
            ).fetchall()
        else:
            allowed_json = json.dumps(request.allowed_work_ids, separators=(",", ":"))
            rows = connection.execute(
                """
                SELECT work.* FROM fmp_work_backlog work
                JOIN json_each(?) allowed ON allowed.value=work.work_id
                WHERE work.state=? AND work.available_at <= ?
                ORDER BY work.priority DESC,work.created_at,work.ticker,work.work_id LIMIT ?
                """,
                (
                    allowed_json,
                    WorkState.PENDING.value,
                    _iso(request.now),
                    request.limit,
                ),
            ).fetchall()
        items = _claim_rows(
            connection,
            rows=rows,
            availability_by_id=availability,
            credentials=request.credentials,
            provider_calls_permitted=request.provider_calls_permitted,
            run_id=request.run_id,
            worker_id=request.worker_id,
            now=request.now,
            lease_seconds=request.lease_seconds,
        )
        circuit = _circuit_row(connection)
        return RunPlan(
            run_id=request.run_id,
            circuit_state=CircuitState(str(circuit["state"])),
            circuit_revision=int(circuit["revision"]),
            items=items,
            backlog=_backlog_status(connection, now=request.now),
        )


def _attempt_id(work_id: str, run_id: str) -> str:
    return _sha256_json({"run_id": run_id, "work_id": work_id})


def _validate_mode_outcome(
    row: sqlite3.Row,
    outcome: WorkOutcome,
    *,
    run_id: str,
    now: datetime,
) -> ExecutionMode:
    if row["state"] != WorkState.LEASED.value:
        raise ValueError(f"work {outcome.work_id} has no active lease")
    if row["lease_token"] != outcome.lease_token or row["lease_run_id"] != run_id:
        raise ValueError(f"work {outcome.work_id} lease does not match recorder")
    lease_expires_at = _parse_time(row["lease_expires_at"])
    if lease_expires_at is None or lease_expires_at <= now:
        raise ValueError(f"work {outcome.work_id} lease has expired")
    mode = ExecutionMode(str(row["lease_mode"]))
    allowed: dict[ExecutionMode, frozenset[OutcomeCode]] = {
        ExecutionMode.LIVE: frozenset(
            {
                OutcomeCode.LIVE_SUCCESS,
                OutcomeCode.HTTP_UNAUTHORIZED,
                OutcomeCode.ACCOUNT_PAYMENT_REQUIRED,
                OutcomeCode.ACCOUNT_AUTH_FORBIDDEN,
                OutcomeCode.ENDPOINT_FORBIDDEN,
                OutcomeCode.RATE_LIMITED,
                OutcomeCode.SERVER_ERROR,
                OutcomeCode.TRANSPORT_ERROR,
                OutcomeCode.CLIENT_CONTRACT_ERROR,
                OutcomeCode.ENDPOINT_EMPTY,
            }
        ),
        ExecutionMode.PROBE: frozenset(
            {
                OutcomeCode.LIVE_SUCCESS,
                OutcomeCode.HTTP_UNAUTHORIZED,
                OutcomeCode.ACCOUNT_PAYMENT_REQUIRED,
                OutcomeCode.ACCOUNT_AUTH_FORBIDDEN,
                OutcomeCode.ENDPOINT_FORBIDDEN,
                OutcomeCode.RATE_LIMITED,
                OutcomeCode.SERVER_ERROR,
                OutcomeCode.TRANSPORT_ERROR,
                OutcomeCode.CLIENT_CONTRACT_ERROR,
                OutcomeCode.ENDPOINT_EMPTY,
            }
        ),
        ExecutionMode.CORPUS: frozenset(
            {OutcomeCode.CORPUS_SUCCESS, OutcomeCode.CORPUS_UNAVAILABLE}
        ),
        ExecutionMode.ALTERNATIVE: frozenset({OutcomeCode.ALTERNATIVE_SUCCESS}),
        ExecutionMode.RECONCILE: frozenset({OutcomeCode.RECONCILED_SUCCESS}),
    }
    if outcome.outcome_code not in allowed[mode]:
        raise ValueError(
            f"outcome {outcome.outcome_code.value} is invalid for lease mode {mode.value}"
        )
    if outcome.alternative_resolution is not None and (
        outcome.alternative_resolution.policy_sha256 != row["policy_sha256"]
        or outcome.alternative_resolution.endpoint_key != row["endpoint_key"]
        or outcome.alternative_resolution.period_key != row["period_key"]
        or outcome.alternative_resolution.evidence_fresh_at > outcome.observed_at
    ):
        raise ValueError("alternative resolution does not match leased work or observation")
    if outcome.fmp_snapshot is not None and (
        outcome.fmp_snapshot.work_id != row["work_id"]
        or outcome.fmp_snapshot.cache_generation_id != row["cache_generation_id"]
        or outcome.fmp_snapshot.policy_sha256 != row["policy_sha256"]
        or outcome.fmp_snapshot.captured_at > outcome.observed_at
    ):
        raise ValueError("FMP snapshot proof does not match leased work")
    if mode in {ExecutionMode.LIVE, ExecutionMode.PROBE} and outcome.fmp_snapshot is not None:
        lease_started_at = datetime.fromisoformat(str(row["updated_at"]))
        if outcome.fmp_snapshot.captured_at < lease_started_at:
            raise ValueError("live FMP snapshot predates its lease")
    if (
        mode is ExecutionMode.RECONCILE
        and outcome.fmp_snapshot is not None
        and outcome.fmp_snapshot.captured_at < datetime.fromisoformat(str(row["created_at"]))
    ):
        raise ValueError("reconciled FMP snapshot predates queued work")
    return mode


def _insert_attempt(
    connection: sqlite3.Connection,
    *,
    request: RecordOutcomesRequest,
    outcome: WorkOutcome,
    mode: ExecutionMode,
) -> str:
    attempt_id = _attempt_id(outcome.work_id, request.run_id)
    corpus = outcome.corpus_snapshot
    fmp_snapshot = outcome.fmp_snapshot
    resolution = outcome.alternative_resolution
    connection.execute(
        """
        INSERT INTO fmp_work_attempts (
            attempt_id,work_id,run_id,execution_mode,outcome_code,http_status,
            retry_after_at,corpus_generation_id,corpus_content_sha256,corpus_captured_at,
            fmp_snapshot_content_sha256,fmp_snapshot_captured_at,resolution_source,
            resolution_policy_sha256,resolution_endpoint_key,resolution_period_key,
            resolution_concept_keys_json,resolution_evidence_fresh_at,
            resolution_source_authorized,resolution_has_disagreement,
            coverage_proof_sha256,evidence_ids_json,fact_ids_json,observed_at,recorded_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            attempt_id,
            outcome.work_id,
            request.run_id,
            mode.value,
            outcome.outcome_code.value,
            outcome.http_status,
            _iso(outcome.retry_after_at) if outcome.retry_after_at is not None else None,
            corpus.cache_generation_id if corpus is not None else None,
            corpus.content_sha256 if corpus is not None else None,
            _iso(corpus.captured_at) if corpus is not None else None,
            fmp_snapshot.content_sha256 if fmp_snapshot is not None else None,
            _iso(fmp_snapshot.captured_at) if fmp_snapshot is not None else None,
            resolution.source.value if resolution is not None else None,
            resolution.policy_sha256 if resolution is not None else None,
            resolution.endpoint_key if resolution is not None else None,
            resolution.period_key if resolution is not None else None,
            _canonical_json(list(resolution.concept_keys)) if resolution is not None else None,
            _iso(resolution.evidence_fresh_at) if resolution is not None else None,
            int(resolution.source_authorized) if resolution is not None else None,
            int(resolution.has_unresolved_disagreement) if resolution is not None else None,
            resolution.canonical_proof_sha256 if resolution is not None else None,
            (_canonical_json(list(resolution.evidence_ids)) if resolution is not None else None),
            _canonical_json(list(resolution.fact_ids)) if resolution is not None else None,
            _iso(outcome.observed_at),
            _iso(request.now),
        ),
    )
    return attempt_id


def _release_pending(
    connection: sqlite3.Connection,
    *,
    work_id: str,
    now: datetime,
    available_at: datetime,
) -> None:
    connection.execute(
        """
        UPDATE fmp_work_backlog SET
            state=?,available_at=?,lease_owner=NULL,lease_token=NULL,lease_run_id=NULL,
            lease_mode=NULL,lease_expires_at=NULL,attempt_count=attempt_count+1,updated_at=?
        WHERE work_id=?
        """,
        (WorkState.PENDING.value, _iso(available_at), _iso(now), work_id),
    )


def _satisfy_work(
    connection: sqlite3.Connection,
    *,
    work_id: str,
    now: datetime,
    resolution_source: str,
) -> None:
    connection.execute(
        """
        UPDATE fmp_work_backlog SET
            state=?,lease_owner=NULL,lease_token=NULL,lease_run_id=NULL,lease_mode=NULL,
            lease_expires_at=NULL,resolution_source=?,attempt_count=attempt_count+1,
            updated_at=?,satisfied_at=?
        WHERE work_id=?
        """,
        (
            WorkState.SATISFIED.value,
            resolution_source,
            _iso(now),
            _iso(now),
            work_id,
        ),
    )


def _terminal_work(
    connection: sqlite3.Connection,
    *,
    work_id: str,
    now: datetime,
    reason: str,
) -> None:
    connection.execute(
        """
        UPDATE fmp_work_backlog SET
            state=?,lease_owner=NULL,lease_token=NULL,lease_run_id=NULL,lease_mode=NULL,
            lease_expires_at=NULL,attempt_count=attempt_count+1,updated_at=?,
            terminal_reason_code=?
        WHERE work_id=?
        """,
        (WorkState.TERMINAL.value, _iso(now), reason, work_id),
    )


def _record_provider_outcome(
    connection: sqlite3.Connection,
    *,
    request: RecordOutcomesRequest,
    outcome: WorkOutcome,
    mode: ExecutionMode,
    attempt_id: str,
) -> None:
    code = outcome.outcome_code
    circuit = _circuit_row(connection)
    if code is OutcomeCode.LIVE_SUCCESS and mode is ExecutionMode.PROBE:
        _close_circuit(
            connection,
            now=request.now,
            success_at=outcome.observed_at,
            attempt_id=attempt_id,
        )
        return
    if code is OutcomeCode.LIVE_SUCCESS:
        if circuit["state"] == CircuitState.CLOSED.value:
            _close_circuit(
                connection,
                now=request.now,
                success_at=outcome.observed_at,
                attempt_id=attempt_id,
            )
        return
    if code in {
        OutcomeCode.HTTP_UNAUTHORIZED,
        OutcomeCode.ACCOUNT_PAYMENT_REQUIRED,
        OutcomeCode.ACCOUNT_AUTH_FORBIDDEN,
    }:
        _open_circuit(
            connection,
            now=request.now,
            reason=code.value,
            next_probe_at=request.now + timedelta(seconds=int(circuit["auth_probe_delay_seconds"])),
        )
        return
    if code is OutcomeCode.RATE_LIMITED:
        new_count = int(circuit["consecutive_rate_limits"]) + 1
        connection.execute(
            """
            UPDATE provider_circuit_state SET consecutive_rate_limits=?,
                consecutive_failures=0,revision=revision+1,last_reason_code=?,updated_at=?
            WHERE provider=?
            """,
            (new_count, code.value, _iso(request.now), PROVIDER),
        )
        circuit = _circuit_row(connection)
        if mode is ExecutionMode.PROBE or new_count >= int(circuit["rate_limit_threshold"]):
            default_probe = request.now + timedelta(
                seconds=int(circuit["rate_limit_probe_delay_seconds"])
            )
            next_probe = max(default_probe, outcome.retry_after_at or default_probe)
            _open_circuit(
                connection,
                now=request.now,
                reason=code.value,
                next_probe_at=next_probe,
            )
        return
    if code in {
        OutcomeCode.ENDPOINT_EMPTY,
        OutcomeCode.ENDPOINT_FORBIDDEN,
        OutcomeCode.CLIENT_CONTRACT_ERROR,
    }:
        if mode is ExecutionMode.PROBE or circuit["state"] == CircuitState.CLOSED.value:
            _close_circuit(
                connection,
                now=request.now,
                success_at=outcome.observed_at,
                attempt_id=attempt_id,
            )
        return
    if code in {OutcomeCode.SERVER_ERROR, OutcomeCode.TRANSPORT_ERROR}:
        new_count = int(circuit["consecutive_failures"]) + 1
        connection.execute(
            """
            UPDATE provider_circuit_state SET consecutive_failures=?,
                consecutive_rate_limits=0,revision=revision+1,last_reason_code=?,updated_at=?
            WHERE provider=?
            """,
            (new_count, code.value, _iso(request.now), PROVIDER),
        )
        circuit = _circuit_row(connection)
        if mode is ExecutionMode.PROBE or new_count >= int(circuit["transient_failure_threshold"]):
            _open_circuit(
                connection,
                now=request.now,
                reason=code.value,
                next_probe_at=request.now + timedelta(seconds=int(circuit["probe_delay_seconds"])),
            )
        return
    if mode is ExecutionMode.PROBE:
        _open_circuit(
            connection,
            now=request.now,
            reason=code.value,
            next_probe_at=request.now + timedelta(seconds=int(circuit["probe_delay_seconds"])),
        )


def _apply_work_outcome(
    connection: sqlite3.Connection,
    *,
    request: RecordOutcomesRequest,
    outcome: WorkOutcome,
    mode: ExecutionMode,
    attempt_id: str,
) -> None:
    code = outcome.outcome_code
    if code is OutcomeCode.LIVE_SUCCESS:
        _satisfy_work(
            connection,
            work_id=outcome.work_id,
            now=request.now,
            resolution_source="fmp_live",
        )
    elif code is OutcomeCode.RECONCILED_SUCCESS:
        _satisfy_work(
            connection,
            work_id=outcome.work_id,
            now=request.now,
            resolution_source="fmp_reconciled",
        )
    elif code is OutcomeCode.ALTERNATIVE_SUCCESS:
        resolution = outcome.alternative_resolution
        if resolution is None:
            raise AssertionError("validated alternative resolution disappeared")
        _satisfy_work(
            connection,
            work_id=outcome.work_id,
            now=request.now,
            resolution_source=resolution.source.value,
        )
    elif code is OutcomeCode.CORPUS_SUCCESS:
        _release_pending(
            connection,
            work_id=outcome.work_id,
            now=request.now,
            available_at=request.now,
        )
    elif code in {
        OutcomeCode.ENDPOINT_EMPTY,
        OutcomeCode.ENDPOINT_FORBIDDEN,
        OutcomeCode.CLIENT_CONTRACT_ERROR,
    }:
        _terminal_work(
            connection,
            work_id=outcome.work_id,
            now=request.now,
            reason=code.value,
        )
    else:
        circuit = _circuit_row(connection)
        retry_seconds = int(circuit["retry_delay_seconds"])
        available_at = outcome.retry_after_at or (request.now + timedelta(seconds=retry_seconds))
        _release_pending(
            connection,
            work_id=outcome.work_id,
            now=request.now,
            available_at=available_at,
        )
    _record_provider_outcome(
        connection,
        request=request,
        outcome=outcome,
        mode=mode,
        attempt_id=attempt_id,
    )
    _event(
        connection,
        event_key=("outcome", attempt_id),
        recorded_at=request.now,
        event_type="outcome_recorded",
        reason_code=code.value,
        work_id=outcome.work_id,
        attempt_id=attempt_id,
        state_from=WorkState.LEASED.value,
        state_to=(
            WorkState.SATISFIED.value
            if code
            in {
                OutcomeCode.LIVE_SUCCESS,
                OutcomeCode.ALTERNATIVE_SUCCESS,
                OutcomeCode.RECONCILED_SUCCESS,
            }
            else (
                WorkState.TERMINAL.value
                if code
                in {
                    OutcomeCode.ENDPOINT_EMPTY,
                    OutcomeCode.ENDPOINT_FORBIDDEN,
                    OutcomeCode.CLIENT_CONTRACT_ERROR,
                }
                else WorkState.PENDING.value
            )
        ),
    )


def _receipt_status(codes: Sequence[OutcomeCode]) -> ReceiptStatus:
    fresh = sum(
        code
        in {
            OutcomeCode.LIVE_SUCCESS,
            OutcomeCode.ALTERNATIVE_SUCCESS,
            OutcomeCode.RECONCILED_SUCCESS,
        }
        for code in codes
    )
    corpus = sum(code is OutcomeCode.CORPUS_SUCCESS for code in codes)
    if fresh == len(codes):
        return ReceiptStatus.FRESH
    if corpus == len(codes):
        return ReceiptStatus.DEGRADED_CORPUS
    if fresh + corpus > 0:
        return ReceiptStatus.PARTIAL
    return ReceiptStatus.FAILED


def _receipt(
    connection: sqlite3.Connection,
    *,
    request: RecordOutcomesRequest,
    codes: Sequence[OutcomeCode],
    corpus_snapshots: Sequence[CorpusSnapshot],
) -> RefreshReceipt:
    fresh_count = sum(
        code
        in {
            OutcomeCode.LIVE_SUCCESS,
            OutcomeCode.ALTERNATIVE_SUCCESS,
            OutcomeCode.RECONCILED_SUCCESS,
        }
        for code in codes
    )
    corpus_count = sum(code is OutcomeCode.CORPUS_SUCCESS for code in codes)
    ages = [max(0.0, (request.now - item.captured_at).total_seconds()) for item in corpus_snapshots]
    expected_rows = connection.execute(
        "SELECT work_id,state FROM fmp_work_backlog "
        "WHERE work_id IN (SELECT value FROM json_each(?))",
        (_canonical_json(list(request.expected_work_ids)),),
    ).fetchall()
    if len(expected_rows) != len(request.expected_work_ids):
        raise ValueError("expected run plan contains unknown FMP recovery work")
    unresolved_expected = sum(row["state"] != WorkState.SATISFIED.value for row in expected_rows)
    unresolved_same_run = connection.execute(
        "SELECT COUNT(*) FROM fmp_work_backlog WHERE lease_run_id=? AND state=?",
        (request.run_id, WorkState.LEASED.value),
    ).fetchone()[0]
    status = _receipt_status(codes)
    if (unresolved_expected or unresolved_same_run) and status is ReceiptStatus.FRESH:
        status = ReceiptStatus.PARTIAL
    circuit = _circuit_row(connection)
    return RefreshReceipt(
        run_id=request.run_id,
        status=status,
        fresh_count=fresh_count,
        corpus_count=corpus_count,
        failed_count=len(codes) - fresh_count - corpus_count,
        corpus_age_seconds=max(ages) if ages else None,
        circuit_state=CircuitState(str(circuit["state"])),
        circuit_revision=int(circuit["revision"]),
        backlog=_backlog_status(connection, now=request.now),
    )


def record_outcomes(
    connection: sqlite3.Connection,
    request: RecordOutcomesRequest,
) -> RefreshReceipt:
    """Atomically record a sanitized outcome batch and return one typed receipt."""
    codes: list[OutcomeCode] = []
    corpus_snapshots: list[CorpusSnapshot] = []
    with _short_transaction(connection):
        validated: list[tuple[WorkOutcome, sqlite3.Row, ExecutionMode | None, bool]] = []
        for outcome in request.outcomes:
            attempt_id = _attempt_id(outcome.work_id, request.run_id)
            existing = connection.execute(
                "SELECT outcome_code,corpus_generation_id,corpus_content_sha256,"
                "corpus_captured_at FROM fmp_work_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            work = connection.execute(
                "SELECT * FROM fmp_work_backlog WHERE work_id=?", (outcome.work_id,)
            ).fetchone()
            if work is None:
                raise ValueError(f"unknown FMP recovery work: {outcome.work_id}")
            if existing is not None:
                if existing["outcome_code"] != outcome.outcome_code.value:
                    raise ValueError("idempotent outcome replay changed the outcome code")
                validated.append((outcome, existing, None, True))
                continue
            mode = _validate_mode_outcome(
                work,
                outcome,
                run_id=request.run_id,
                now=request.now,
            )
            validated.append((outcome, work, mode, False))

        for outcome, row, mode, replay in validated:
            if replay:
                code = OutcomeCode(str(row["outcome_code"]))
                codes.append(code)
                captured = row["corpus_captured_at"]
                if code is OutcomeCode.CORPUS_SUCCESS and captured is not None:
                    corpus_snapshots.append(
                        CorpusSnapshot(
                            cache_generation_id=str(row["corpus_generation_id"]),
                            content_sha256=str(row["corpus_content_sha256"]),
                            captured_at=datetime.fromisoformat(str(captured)),
                        )
                    )
                continue
            if mode is None:
                raise AssertionError("new outcome lost its validated lease mode")
            attempt_id = _insert_attempt(
                connection,
                request=request,
                outcome=outcome,
                mode=mode,
            )
            _apply_work_outcome(
                connection,
                request=request,
                outcome=outcome,
                mode=mode,
                attempt_id=attempt_id,
            )
            codes.append(outcome.outcome_code)
            if outcome.corpus_snapshot is not None:
                corpus_snapshots.append(outcome.corpus_snapshot)
        return _receipt(
            connection,
            request=request,
            codes=codes,
            corpus_snapshots=corpus_snapshots,
        )


__all__ = [
    "SCREENING_ENDPOINT_KEYS",
    "AlternativeCoverageState",
    "AlternativeResolution",
    "AlternativeSource",
    "BacklogStatus",
    "CircuitConfig",
    "CircuitState",
    "CorpusSnapshot",
    "CredentialAvailability",
    "EnqueueWorkReceipt",
    "EnqueueWorkRequest",
    "ExecutionMode",
    "FmpSnapshotProof",
    "OutcomeCode",
    "PlanRunRequest",
    "PlannedWork",
    "ReceiptStatus",
    "RecordOutcomesRequest",
    "RecoverableWorkRequest",
    "RecoveryAvailability",
    "RefreshReceipt",
    "RunPlan",
    "WorkOutcome",
    "WorkSpec",
    "WorkState",
    "enqueue_work",
    "make_work_id",
    "plan_run",
    "record_outcomes",
    "recoverable_work",
]
