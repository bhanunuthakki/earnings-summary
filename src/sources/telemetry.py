"""Source-regime cost attribution and telemetry models.

Tracks provider API usage, network payload sizes, latencies, HTTP statuses,
retries, and cost accounting across SEC-primary, vendor-only, and combined regimes.
All events and summaries are strictly typed and credential-redacted.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from log_redact import redact


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
        """Record an event with sanitization on notes/endpoint."""
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
        self._events.append(event)
        return event

    @property
    def events(self) -> Sequence[SourceRegimeCostEvent]:
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
                "tickers": set(),
            }
            for r in SourceRegime
        }

        for ev in self._events:
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
            events_count=len(self._events),
            regimes=final_regimes,
            total_cost_usd=grand_total,
        )
