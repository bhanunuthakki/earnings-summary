"""Provider-neutral, append-only outcomes for exact data coverage obligations.

These dispositions explain why an expected artifact is present or absent. They
never substitute for the evidence receipt or canonical row that proves data
completeness.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from calendar import monthrange
from collections.abc import Mapping
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Self, cast

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from operations.context import current as current_operation_context
from schema_compat import require_current_for_write

_STRICT_FROZEN = ConfigDict(extra="forbid", frozen=True, strict=True)
_Ticker = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,15}$")]
_Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

COMMITMENT_SCAN_POLICY_NAME = "commitment_scan"
COMMITMENT_SCAN_POLICY_VERSION = "2026-09-05.1"
COMMITMENT_SCAN_POLICY_PROVIDERS = ("governed_llm",)
EARNINGS_SURPRISE_POLICY_NAME = "earnings_surprise_sources"
EARNINGS_SURPRISE_POLICY_VERSION = "2026-09-05.1"
EARNINGS_SURPRISE_POLICY_PROVIDERS = ("fmp_calendar", "yfinance")


class CoverageArtifactKind(StrEnum):
    TEXT_TRANSCRIPT = "text_transcript"
    COMMITMENT_SCAN = "commitment_scan"
    EARNINGS_SURPRISE = "earnings_surprise"


class CoverageDispositionStatus(StrEnum):
    SATISFIED = "satisfied"
    SOURCE_UNAVAILABLE = "source_unavailable"
    POLICY_BLOCKED = "policy_blocked"
    PROVIDER_COVERAGE_GAP = "provider_coverage_gap"
    REPAIR_EVIDENCE_MISSING = "repair_evidence_missing"
    OPERATIONAL_ERROR = "operational_error"


class CoverageAttemptStatus(StrEnum):
    EVIDENCE_PRESENT = "evidence_present"
    ACQUIRED = "acquired"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    AUTHORIZED_MISS = "authorized_miss"
    POLICY_DENIED = "policy_denied"
    SOURCE_HIT = "source_hit"
    SOURCE_MISS = "source_miss"
    FAILED = "failed"


class CoverageAttempt(BaseModel):
    model_config = _STRICT_FROZEN

    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_.:-]+$")
    status: CoverageAttemptStatus
    authorization_key: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"
    )


class DataCoverageDispositionRequest(BaseModel):
    model_config = _STRICT_FROZEN

    artifact_kind: CoverageArtifactKind
    ticker: _Ticker
    fiscal_year: int = Field(ge=2000, le=2100)
    fiscal_quarter: int = Field(ge=1, le=4)
    period_end: date
    status: CoverageDispositionStatus
    reason_code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_]+$")
    attempts: tuple[CoverageAttempt, ...]
    policy_name: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)
    policy_config_sha256: _Sha256
    evidence_reference: str | None = Field(default=None, min_length=1, max_length=256)
    evidence_sha256: _Sha256 | None = None
    operation_id: str | None = Field(default=None, pattern=r"^operation:[0-9a-f]{64}$")
    observed_at: AwareDatetime
    retry_after: AwareDatetime | None = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("attempts")
    @classmethod
    def _canonical_attempts(cls, value: tuple[CoverageAttempt, ...]) -> tuple[CoverageAttempt, ...]:
        keys = [(item.provider, item.authorization_key or "") for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("coverage attempts must have unique provider/authorization identities")
        return tuple(sorted(value, key=lambda item: (item.provider, item.authorization_key or "")))

    @model_validator(mode="after")
    def _validate_evidence_and_retry(self) -> Self:
        if self.status is CoverageDispositionStatus.SATISFIED:
            if self.evidence_reference is None or self.evidence_sha256 is None:
                raise ValueError("satisfied coverage requires exact evidence identity and SHA-256")
            if self.retry_after is not None:
                raise ValueError("satisfied coverage cannot carry retry_after")
        elif self.evidence_reference is not None or self.evidence_sha256 is not None:
            raise ValueError("non-satisfied coverage cannot claim evidence completeness")
        if (
            self.status
            in {
                CoverageDispositionStatus.SOURCE_UNAVAILABLE,
                CoverageDispositionStatus.PROVIDER_COVERAGE_GAP,
                CoverageDispositionStatus.OPERATIONAL_ERROR,
            }
            and self.retry_after is None
        ):
            raise ValueError(f"{self.status.value} coverage requires retry_after")
        if self.retry_after is not None and self.retry_after <= self.observed_at:
            raise ValueError("retry_after must be later than observed_at")
        return self


class DataCoverageDisposition(BaseModel):
    model_config = _STRICT_FROZEN

    disposition_id: _Sha256
    idempotency_key: _Sha256
    request: DataCoverageDispositionRequest
    attempts_sha256: _Sha256
    revision: int = Field(gt=0)
    supersedes_disposition_id: _Sha256 | None = None
    recorded_at: AwareDatetime


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def policy_config_sha256(
    *, policy_name: str, policy_version: str, providers: tuple[str, ...]
) -> str:
    """Commit a provider-neutral source-chain configuration without URLs or secrets."""

    return _sha256(
        _canonical_json(
            {
                "policy_name": policy_name,
                "policy_version": policy_version,
                "providers": list(providers),
            }
        )
    )


def fiscal_quarter_period_end(fiscal_year: int, fiscal_quarter: int, fye_month: int) -> date:
    """Return the calendar period end for an issuer fiscal-year quarter."""

    if not 2000 <= fiscal_year <= 2100 or not 1 <= fiscal_quarter <= 4:
        raise ValueError("fiscal period is outside the supported range")
    if not 1 <= fye_month <= 12:
        raise ValueError("fiscal year-end month must be between 1 and 12")
    months_before_fye = (4 - fiscal_quarter) * 3
    year = fiscal_year
    month = fye_month - months_before_fye
    while month < 1:
        month += 12
        year -= 1
    return date(year, month, monthrange(year, month)[1])


def recent_completed_fiscal_quarters(
    *, fye_month: int, as_of: date, limit: int
) -> tuple[tuple[int, int, date], ...]:
    """Return newest completed fiscal-quarter identities with exact period ends."""

    if limit < 1:
        raise ValueError("quarter limit must be positive")
    completed: list[tuple[date, int, int]] = []
    for fiscal_year in range(as_of.year - 3, as_of.year + 2):
        for fiscal_quarter in range(1, 5):
            period_end = fiscal_quarter_period_end(fiscal_year, fiscal_quarter, fye_month)
            if period_end <= as_of:
                completed.append((period_end, fiscal_year, fiscal_quarter))
    return tuple(
        (fiscal_year, fiscal_quarter, period_end)
        for period_end, fiscal_year, fiscal_quarter in sorted(completed, reverse=True)[:limit]
    )


def _request_payload(request: DataCoverageDispositionRequest) -> dict[str, object]:
    return request.model_dump(mode="json")


def _attempts_json(request: DataCoverageDispositionRequest) -> str:
    return _canonical_json([attempt.model_dump(mode="json") for attempt in request.attempts])


def _from_row(row: sqlite3.Row | tuple[object, ...]) -> DataCoverageDisposition:
    values = tuple(cast(object, row[index]) for index in range(22))
    attempts_raw = cast(object, json.loads(str(values[9])))
    if not isinstance(attempts_raw, list):  # Database constraint defense in depth.
        raise ValueError("persisted coverage attempts are not an array")
    attempts: list[CoverageAttempt] = []
    for raw_item in cast(list[object], attempts_raw):
        if not isinstance(raw_item, dict):
            raise ValueError("persisted coverage attempt is not an object")
        item = cast(Mapping[str, object], raw_item)
        attempts.append(
            CoverageAttempt(
                provider=str(item.get("provider", "")),
                status=CoverageAttemptStatus(str(item.get("status", ""))),
                authorization_key=(
                    None
                    if item.get("authorization_key") is None
                    else str(item["authorization_key"])
                ),
            )
        )
    request = DataCoverageDispositionRequest(
        artifact_kind=CoverageArtifactKind(str(values[2])),
        ticker=str(values[3]),
        fiscal_year=int(str(values[4])),
        fiscal_quarter=int(str(values[5])),
        period_end=date.fromisoformat(str(values[6])),
        status=CoverageDispositionStatus(str(values[7])),
        reason_code=str(values[8]),
        attempts=tuple(attempts),
        policy_name=str(values[11]),
        policy_version=str(values[12]),
        policy_config_sha256=str(values[13]),
        evidence_reference=None if values[14] is None else str(values[14]),
        evidence_sha256=None if values[15] is None else str(values[15]),
        operation_id=None if values[16] is None else str(values[16]),
        observed_at=datetime.fromisoformat(str(values[17]).replace("Z", "+00:00")),
        retry_after=(
            None
            if values[18] is None
            else datetime.fromisoformat(str(values[18]).replace("Z", "+00:00"))
        ),
    )
    attempts_json = _attempts_json(request)
    if _sha256(attempts_json) != str(values[10]):
        raise ValueError("persisted coverage attempt commitment is invalid")
    return DataCoverageDisposition(
        disposition_id=str(values[0]),
        idempotency_key=str(values[1]),
        request=request,
        attempts_sha256=str(values[10]),
        revision=int(str(values[19])),
        supersedes_disposition_id=None if values[20] is None else str(values[20]),
        recorded_at=datetime.fromisoformat(str(values[21]).replace("Z", "+00:00")),
    )


def current_data_coverage_disposition(
    conn: sqlite3.Connection,
    *,
    artifact_kind: CoverageArtifactKind,
    ticker: str,
    fiscal_year: int,
    fiscal_quarter: int,
) -> DataCoverageDisposition | None:
    row = conn.execute(
        "SELECT disposition_id,idempotency_key,artifact_kind,ticker,fiscal_year,fiscal_quarter,"
        "period_end,status,reason_code,attempts_json,attempts_sha256,policy_name,policy_version,"
        "policy_config_sha256,evidence_reference,evidence_sha256,operation_id,observed_at,"
        "retry_after,revision,supersedes_disposition_id,recorded_at "
        "FROM v_data_coverage_dispositions_current "
        "WHERE artifact_kind=? AND ticker=? AND fiscal_year=? AND fiscal_quarter=?",
        (artifact_kind.value, ticker.strip().upper(), fiscal_year, fiscal_quarter),
    ).fetchone()
    return None if row is None else _from_row(row)


def append_data_coverage_disposition(
    conn: sqlite3.Connection,
    request: DataCoverageDispositionRequest,
    *,
    recorded_at: datetime | None = None,
) -> DataCoverageDisposition:
    """Append one exact target observation, or return its idempotent replay."""

    require_current_for_write(conn)
    validated = DataCoverageDispositionRequest.model_validate(request, strict=True)
    context = current_operation_context()
    if validated.operation_id is None and context is not None:
        validated = validated.model_copy(update={"operation_id": context.operation_id})
    recorded = (recorded_at or datetime.now(UTC)).astimezone(UTC)
    if recorded < validated.observed_at.astimezone(UTC):
        raise ValueError("recorded_at cannot precede observed_at")
    request_json = _canonical_json(_request_payload(validated))
    idempotency_key = _sha256(request_json)
    existing = conn.execute(
        "SELECT disposition_id,idempotency_key,artifact_kind,ticker,fiscal_year,fiscal_quarter,"
        "period_end,status,reason_code,attempts_json,attempts_sha256,policy_name,policy_version,"
        "policy_config_sha256,evidence_reference,evidence_sha256,operation_id,observed_at,"
        "retry_after,revision,supersedes_disposition_id,recorded_at "
        "FROM data_coverage_dispositions WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        replay = _from_row(existing)
        if replay.request != validated:
            raise ValueError("data coverage disposition idempotency collision")
        return replay

    prior = current_data_coverage_disposition(
        conn,
        artifact_kind=validated.artifact_kind,
        ticker=validated.ticker,
        fiscal_year=validated.fiscal_year,
        fiscal_quarter=validated.fiscal_quarter,
    )
    revision = 1 if prior is None else prior.revision + 1
    supersedes = None if prior is None else prior.disposition_id
    attempts_json = _attempts_json(validated)
    attempts_sha256 = _sha256(attempts_json)
    disposition_id = _sha256(
        _canonical_json(
            {
                "idempotency_key": idempotency_key,
                "revision": revision,
                "supersedes_disposition_id": supersedes,
            }
        )
    )
    timestamp = recorded.isoformat(timespec="microseconds").replace("+00:00", "Z")
    conn.execute(
        "INSERT INTO data_coverage_dispositions ("
        "disposition_id,idempotency_key,artifact_kind,ticker,fiscal_year,fiscal_quarter,"
        "period_end,status,reason_code,attempts_json,attempts_sha256,policy_name,policy_version,"
        "policy_config_sha256,evidence_reference,evidence_sha256,operation_id,observed_at,"
        "retry_after,revision,supersedes_disposition_id,recorded_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            disposition_id,
            idempotency_key,
            validated.artifact_kind.value,
            validated.ticker,
            validated.fiscal_year,
            validated.fiscal_quarter,
            validated.period_end.isoformat(),
            validated.status.value,
            validated.reason_code,
            attempts_json,
            attempts_sha256,
            validated.policy_name,
            validated.policy_version,
            validated.policy_config_sha256,
            validated.evidence_reference,
            validated.evidence_sha256,
            validated.operation_id,
            validated.observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            (
                None
                if validated.retry_after is None
                else validated.retry_after.astimezone(UTC).isoformat().replace("+00:00", "Z")
            ),
            revision,
            supersedes,
            timestamp,
        ),
    )
    row = conn.execute(
        "SELECT disposition_id,idempotency_key,artifact_kind,ticker,fiscal_year,fiscal_quarter,"
        "period_end,status,reason_code,attempts_json,attempts_sha256,policy_name,policy_version,"
        "policy_config_sha256,evidence_reference,evidence_sha256,operation_id,observed_at,"
        "retry_after,revision,supersedes_disposition_id,recorded_at "
        "FROM data_coverage_dispositions WHERE disposition_id=?",
        (disposition_id,),
    ).fetchone()
    if row is None:  # pragma: no cover - SQLite insert/read invariant
        raise RuntimeError("data coverage disposition disappeared after insert")
    return _from_row(row)


__all__ = [
    "COMMITMENT_SCAN_POLICY_NAME",
    "COMMITMENT_SCAN_POLICY_PROVIDERS",
    "COMMITMENT_SCAN_POLICY_VERSION",
    "EARNINGS_SURPRISE_POLICY_NAME",
    "EARNINGS_SURPRISE_POLICY_PROVIDERS",
    "EARNINGS_SURPRISE_POLICY_VERSION",
    "CoverageArtifactKind",
    "CoverageAttempt",
    "CoverageAttemptStatus",
    "CoverageDispositionStatus",
    "DataCoverageDisposition",
    "DataCoverageDispositionRequest",
    "append_data_coverage_disposition",
    "current_data_coverage_disposition",
    "fiscal_quarter_period_end",
    "policy_config_sha256",
    "recent_completed_fiscal_quarters",
]
