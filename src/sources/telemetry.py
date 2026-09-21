"""Source-regime cost attribution and telemetry models.

Tracks provider API usage, network payload sizes, latencies, HTTP statuses,
retries, and cost accounting across SEC-primary, vendor-only, and combined regimes.
All events and summaries are strictly typed and credential-redacted.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from log_redact import redact
from provenance.source_regime import SourceRegime as CanonicalSourceRegime


class SourceRegime(StrEnum):
    SEC_PRIMARY = "sec_primary"
    VENDOR_ONLY = "vendor_only"
    COMBINED = "combined"


class SourceRegimeCostEvent(BaseModel):
    """Atomic telemetry event for a single data retrieval or extraction step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    regime: SourceRegime
    run_id: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    ticker: str = Field(..., min_length=1)
    endpoint: str = Field(..., min_length=1)
    bytes_transferred: int = Field(ge=0, default=0)
    latency_ms: int = Field(ge=0, default=0)
    http_status: int = Field(ge=100, le=599, default=200)
    retry_count: int = Field(ge=0, default=0)
    record_count: int = Field(ge=0, default=0)
    provider_cost_usd: Decimal = Field(default=Decimal("0.0"))
    llm_cost_usd: Decimal = Field(default=Decimal("0.0"))
    operator_time_seconds: Decimal = Field(default=Decimal("0.0"))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    notes: str | None = None


class RegimeCostBreakdown(BaseModel):
    """Aggregated cost summary for a single regime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    regime: SourceRegime
    total_calls: int
    total_bytes: int
    total_latency_ms: int
    total_provider_cost_usd: Decimal
    total_llm_cost_usd: Decimal
    total_operator_time_seconds: Decimal
    total_cost_usd: Decimal
    unique_tickers: int


class SourceRegimeCostSummary(BaseModel):
    """Full cross-regime cost and telemetry summary receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    generated_at: datetime
    events_count: int
    regimes: dict[SourceRegime, RegimeCostBreakdown]
    total_cost_usd: Decimal


class SourceCostTelemetryAccumulator:
    """Thread-safe, append-only accumulator for source telemetry events."""

    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id or f"run_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        self._lock = threading.Lock()
        self._events: list[SourceRegimeCostEvent] = []

    def record(
        self,
        *,
        regime: SourceRegime,
        provider: str,
        ticker: str,
        endpoint: str,
        bytes_transferred: int = 0,
        latency_ms: int = 0,
        http_status: int = 200,
        retry_count: int = 0,
        record_count: int = 0,
        provider_cost_usd: Decimal = Decimal("0.0"),
        llm_cost_usd: Decimal = Decimal("0.0"),
        operator_time_seconds: Decimal = Decimal("0.0"),
        notes: str | None = None,
        timestamp: datetime | None = None,
    ) -> SourceRegimeCostEvent:
        """Record an event with sanitization on notes/endpoint in a thread-safe manner."""
        sanitized_endpoint = redact(endpoint)
        sanitized_notes = redact(notes) if notes else None

        event = SourceRegimeCostEvent(
            regime=regime,
            run_id=self.run_id,
            provider=provider,
            ticker=ticker.upper(),
            endpoint=sanitized_endpoint,
            bytes_transferred=bytes_transferred,
            latency_ms=latency_ms,
            http_status=http_status,
            retry_count=retry_count,
            record_count=record_count,
            provider_cost_usd=provider_cost_usd,
            llm_cost_usd=llm_cost_usd,
            operator_time_seconds=operator_time_seconds,
            timestamp=timestamp or datetime.now(UTC),
            notes=sanitized_notes,
        )
        with self._lock:
            self._events.append(event)
        return event

    @property
    def events(self) -> Sequence[SourceRegimeCostEvent]:
        with self._lock:
            return tuple(self._events)

    def summarize(self) -> SourceRegimeCostSummary:
        """Compute regime-by-regime aggregation."""
        breakdowns: dict[SourceRegime, dict[str, Any]] = {
            r: {
                "regime": r,
                "total_calls": 0,
                "total_bytes": 0,
                "total_latency_ms": 0,
                "total_provider_cost_usd": Decimal("0.0"),
                "total_llm_cost_usd": Decimal("0.0"),
                "total_operator_time_seconds": Decimal("0.0"),
                "error_count": 0,
                "retry_count": 0,
                "tickers": set(),
            }
            for r in SourceRegime
        }

        with self._lock:
            snapshot_events = list(self._events)

        for ev in snapshot_events:
            b = breakdowns[ev.regime]
            b["total_calls"] += 1
            b["total_bytes"] += ev.bytes_transferred
            b["total_latency_ms"] += ev.latency_ms
            b["total_provider_cost_usd"] += ev.provider_cost_usd
            b["total_llm_cost_usd"] += ev.llm_cost_usd
            b["total_operator_time_seconds"] += ev.operator_time_seconds
            b["tickers"].add(ev.ticker)

        final_regimes: dict[SourceRegime, RegimeCostBreakdown] = {}
        grand_total = Decimal("0.0")

        for r, b in breakdowns.items():
            tot_cost = b["total_provider_cost_usd"] + b["total_llm_cost_usd"]
            grand_total += tot_cost
            final_regimes[r] = RegimeCostBreakdown(
                regime=r,
                total_calls=b["total_calls"],
                total_bytes=b["total_bytes"],
                total_latency_ms=b["total_latency_ms"],
                total_provider_cost_usd=b["total_provider_cost_usd"],
                total_llm_cost_usd=b["total_llm_cost_usd"],
                total_operator_time_seconds=b["total_operator_time_seconds"],
                total_cost_usd=tot_cost,
                unique_tickers=len(b["tickers"]),
            )

        return SourceRegimeCostSummary(
            run_id=self.run_id,
            generated_at=datetime.now(UTC),
            events_count=len(snapshot_events),
            regimes=final_regimes,
            total_cost_usd=grand_total,
        )


# The durable transport path uses the canonical regime owner and nullable
# measurements. Legacy in-memory summaries above remain import-compatible.


class SourceMeasurementScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: str = Field(min_length=1)
    regime: CanonicalSourceRegime
    ticker_scope: tuple[str, ...] = ()


_MEASUREMENT_SCOPE: ContextVar[SourceMeasurementScope | None] = ContextVar(
    "source_measurement_scope", default=None
)


@contextmanager
def source_measurement_scope(scope: SourceMeasurementScope) -> Generator[None]:
    token = _MEASUREMENT_SCOPE.set(scope)
    try:
        yield
    finally:
        _MEASUREMENT_SCOPE.reset(token)


def current_measurement_scope() -> SourceMeasurementScope | None:
    return _MEASUREMENT_SCOPE.get()


class SourceAttemptMeasurement(BaseModel):
    """One observed HTTP attempt; unavailable amounts are never zero defaults."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["source-attempt-measurement/v1"] = "source-attempt-measurement/v1"
    measurement_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str = Field(min_length=1)
    regime: CanonicalSourceRegime | None = None
    provider: str = Field(min_length=1)
    ticker_scope: tuple[str, ...] = ()
    endpoint: str = Field(min_length=1)
    bytes_received: int | None = Field(default=None, ge=0)
    latency_ms: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    status: Literal["ok", "http_error", "network_error", "retry"]
    http_status: int | None = Field(default=None, ge=100, le=599)
    record_count: int | None = Field(default=None, ge=0)
    provider_cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    llm_cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    operator_time_seconds: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    measured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("endpoint")
    @classmethod
    def _safe_endpoint(cls, value: str) -> str:
        # Transport supplies only host+path. Never retain query/userinfo/body.
        from urllib.parse import urlsplit

        parsed = urlsplit(value if "://" in value else "https://" + value)
        return redact((parsed.hostname or "") + (parsed.path or "/"))

    @field_validator("measured_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("measurement clock requires timezone")
        return value.astimezone(UTC)


def persist_source_attempt(conn: sqlite3.Connection, measurement: SourceAttemptMeasurement) -> int:
    """Atomically pair a real call row and immutable measured companion; replay exact."""
    if conn.in_transaction:
        raise ValueError("source measurement requires its own transaction")
    payload = json.dumps(measurement.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    existing = conn.execute(
        "SELECT source_call_id,payload_json,payload_sha256 FROM source_regime_measurements WHERE measurement_id=?",
        (measurement.measurement_id,),
    ).fetchone()
    if existing:
        if existing[1] != payload or existing[2] != digest:
            raise ValueError("source measurement replay conflict")
        return int(existing[0])
    with conn:
        row = conn.execute(
            "INSERT INTO source_calls (source_name,kind,ticker,called_at,latency_ms,status,http_code,record_count,notes) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
            (
                measurement.provider,
                "http_attempt",
                measurement.ticker_scope[0] if len(measurement.ticker_scope) == 1 else None,
                measurement.measured_at.isoformat(),
                measurement.latency_ms,
                "ok" if measurement.status == "ok" else "error",
                measurement.http_status,
                measurement.record_count,
                None,
            ),
        ).fetchone()
        assert row is not None
        call_id = int(row[0])
        conn.execute(
            "INSERT INTO source_regime_measurements VALUES (?,?,?,?,?,?,?,?)",
            (
                measurement.measurement_id,
                call_id,
                measurement.run_id,
                measurement.regime.value if measurement.regime else None,
                measurement.provider,
                measurement.measured_at.isoformat(),
                payload,
                digest,
            ),
        )
    return call_id


def source_measurement_report(
    conn: sqlite3.Connection, *, run_id: str | None = None
) -> dict[str, object]:
    """Summarize retained measured fields without backfilling missing acquisition costs."""
    sql = "SELECT payload_json,payload_sha256 FROM source_regime_measurements"
    parameters: tuple[object, ...] = ()
    if run_id is not None:
        sql += " WHERE run_id=?"
        parameters = (run_id,)
    events: list[SourceAttemptMeasurement] = []
    for payload, digest in conn.execute(sql, parameters):
        if hashlib.sha256(str(payload).encode()).hexdigest() != digest:
            raise ValueError("source measurement commitment mismatch")
        events.append(SourceAttemptMeasurement.model_validate_json(str(payload)))
    missing = {
        field: sum(getattr(event, field) is None for event in events)
        for field in (
            "regime",
            "bytes_received",
            "record_count",
            "provider_cost_usd",
            "llm_cost_usd",
            "operator_time_seconds",
        )
    }
    costs: dict[str, str | None] = {}
    for field in ("provider_cost_usd", "llm_cost_usd", "operator_time_seconds"):
        values = [getattr(event, field) for event in events]
        costs[field] = str(sum(values, Decimal(0))) if values and not missing[field] else None
    return {
        "status": "measured" if events and not any(missing.values()) else "partial",
        "run_id": run_id,
        "measurement_scope": "selected_run" if run_id else "all_retained_measured_attempts",
        "measured_attempts": len(events),
        "measured_bytes": sum(event.bytes_received or 0 for event in events),
        "latency_ms": sum(event.latency_ms for event in events),
        "retry_attempts": sum(event.retry_count > 0 for event in events),
        "missing_measurements": missing,
        "unmeasured_legacy_calls_in_database": int(
            conn.execute(
                "SELECT count(*) FROM source_calls call LEFT JOIN source_regime_measurements measurement ON measurement.source_call_id=call.id WHERE measurement.measurement_id IS NULL"
            ).fetchone()[0]
        ),
        "regimes_observed": sorted({event.regime.value for event in events if event.regime}),
        **costs,
        "current_entitlement": "unverified",
        "output_readiness": "unverified",
    }
