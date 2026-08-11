"""Typed, append-only intake for earnings-surprise source observations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator


class EarningsSurpriseRecordV1(BaseModel):
    """Closed source contract for one observed actual-versus-consensus record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1, max_length=16, pattern=r"^[A-Z0-9.-]+$")
    release_date: date
    eps_estimate: Decimal | None = None
    eps_actual: Decimal | None = None
    revenue_estimate: Decimal | None = None
    revenue_actual: Decimal | None = None
    eps_surprise_pct: Decimal | None = None
    revenue_surprise_pct: Decimal | None = None
    num_analysts_eps: int | None = Field(default=None, ge=0)
    num_analysts_revenue: int | None = Field(default=None, ge=0)
    source_name: str = Field(min_length=1, max_length=32)
    source_url: str | None = None
    fetched_at: AwareDatetime

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator(
        "eps_estimate",
        "eps_actual",
        "revenue_estimate",
        "revenue_actual",
        "eps_surprise_pct",
        "revenue_surprise_pct",
        mode="before",
    )
    @classmethod
    def _require_decimal_string(cls, value: object) -> object:
        if value is None or isinstance(value, str):
            return value
        raise ValueError("decimal fields must be JSON strings or null")

    @field_validator("num_analysts_eps", "num_analysts_revenue", mode="before")
    @classmethod
    def _reject_boolean_counts(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("analyst counts must be integers or null")
        return value

    @field_validator("source_url")
    @classmethod
    def _source_url_is_http(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("https://", "http://")):
            raise ValueError("source_url must be HTTP(S)")
        return value

    @field_validator("fetched_at", mode="before")
    @classmethod
    def _normalize_legacy_naive_timestamp(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    @classmethod
    def from_source(cls, raw: object, *, ticker_hint: str) -> Self:
        record = cls.model_validate(raw)
        if record.ticker != ticker_hint.upper():
            raise ValueError(
                f"record ticker {record.ticker!r} does not match cache ticker "
                f"{ticker_hint.upper()!r}"
            )
        return record


class ValidationDisposition(BaseModel):
    """Typed result so invalid source payloads never continue as loose dictionaries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record: EarningsSurpriseRecordV1 | None
    reason_code: str | None
    reason_details: dict[str, str]


def validate_source_record(raw: object, *, ticker_hint: str) -> ValidationDisposition:
    try:
        record = EarningsSurpriseRecordV1.from_source(raw, ticker_hint=ticker_hint)
    except (ValidationError, ValueError) as exc:
        return ValidationDisposition(
            record=None,
            reason_code="schema_validation_failed",
            reason_details={"error": str(exc)[:1000]},
        )
    return ValidationDisposition(record=record, reason_code=None, reason_details={})


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _record_payload(record: EarningsSurpriseRecordV1) -> dict[str, object]:
    return record.model_dump(mode="json")


def observation_identity(record: EarningsSurpriseRecordV1) -> str:
    return _sha256(_canonical_json(_record_payload(record)))


def append_observation(
    conn: sqlite3.Connection,
    *,
    record: EarningsSurpriseRecordV1,
    raw_payload: object,
    cache_path: str,
    record_ordinal: int,
    recorded_at: datetime | None = None,
) -> tuple[str, bool]:
    """Append one immutable observation; return ``(observation_id, inserted)``."""
    canonical_json = _canonical_json(_record_payload(record))
    canonical_sha = _sha256(canonical_json)
    raw_json = _canonical_json(raw_payload)
    raw_sha = _sha256(raw_json)
    observation_id = canonical_sha
    timestamp = (recorded_at or datetime.now(UTC)).isoformat()
    values = (
        observation_id,
        f"earnings-surprise-observation:{observation_id}",
        record.ticker,
        record.release_date.isoformat(),
        *(
            str(getattr(record, name)) if getattr(record, name) is not None else None
            for name in (
                "eps_estimate",
                "eps_actual",
                "revenue_estimate",
                "revenue_actual",
                "eps_surprise_pct",
                "revenue_surprise_pct",
            )
        ),
        record.num_analysts_eps,
        record.num_analysts_revenue,
        record.source_name,
        record.source_url,
        record.fetched_at.isoformat(),
        cache_path,
        record_ordinal,
        raw_json,
        raw_sha,
        canonical_json,
        canonical_sha,
        timestamp,
    )
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO earnings_surprise_observations (
            observation_id,idempotency_key,ticker,release_date,
            eps_estimate,eps_actual,revenue_estimate,revenue_actual,
            eps_surprise_pct,revenue_surprise_pct,num_analysts_eps,
            num_analysts_revenue,source_name,source_url,fetched_at,
            cache_path,record_ordinal,raw_payload_json,raw_payload_sha256,
            canonical_payload_json,canonical_payload_sha256,recorded_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        values,
    )
    return observation_id, cursor.rowcount == 1


def quarantine_payload(
    conn: sqlite3.Connection,
    *,
    raw_payload: object,
    ticker_hint: str | None,
    cache_path: str,
    record_ordinal: int,
    reason_code: str,
    reason_details: dict[str, str],
    recorded_at: datetime | None = None,
) -> tuple[str, bool]:
    """Append one immutable quarantine disposition, idempotently."""
    raw_json = _canonical_json(raw_payload)
    raw_sha = _sha256(raw_json)
    details_json = _canonical_json(reason_details)
    details_sha = _sha256(details_json)
    identity_payload = {
        "cache_path": cache_path,
        "raw_payload_sha256": raw_sha,
        "reason_code": reason_code,
        "reason_details_sha256": details_sha,
        "record_ordinal": record_ordinal,
        "ticker_hint": ticker_hint,
    }
    quarantine_id = _sha256(_canonical_json(identity_payload))
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO earnings_surprise_quarantine (
            quarantine_id,idempotency_key,ticker_hint,cache_path,record_ordinal,
            raw_payload_json,raw_payload_sha256,reason_code,reason_details_json,
            reason_details_sha256,recorded_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            quarantine_id,
            f"earnings-surprise-quarantine:{quarantine_id}",
            ticker_hint,
            cache_path,
            record_ordinal,
            raw_json,
            raw_sha,
            reason_code,
            details_json,
            details_sha,
            (recorded_at or datetime.now(UTC)).isoformat(),
        ),
    )
    return quarantine_id, cursor.rowcount == 1
