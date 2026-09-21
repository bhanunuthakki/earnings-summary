"""Captured provider market context, kept separate from reported financial facts."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from pipeline.cadence_policy import cadence_hours
from provenance.immutable_artifact import read_stable_artifact


class ProviderProfilePacket(BaseModel):
    model_config = ConfigDict(extra="ignore")
    symbol: str
    company_name: str | None = Field(default=None, alias="companyName")
    sector: str | None = None
    industry: str | None = None
    currency: str | None = None
    market_cap: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False, alias="marketCap")
    # Keep an invalid quote local to the quote: a malformed provider price must
    # not discard the otherwise valid captured market-cap packet.
    price: str | Decimal | int | float | None = None
    actively_trading: bool | None = Field(default=None, alias="isActivelyTrading")

    @field_validator("price", mode="before")
    @classmethod
    def _normalize_price(cls, value: object) -> object | None:
        if value is None or isinstance(value, bool):
            return None
        return value if isinstance(value, (str, Decimal, int, float)) else None


class DiscoveryMarketContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ticker: str
    status: Literal["available", "degraded", "unavailable"]
    reason_codes: tuple[str, ...] = ()
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    actively_trading: bool | None = None
    market_cap: Decimal | None = None
    price: Decimal | None = None
    currency: str | None = None
    captured_at: datetime | None = None
    source_payload_sha256: str | None = None
    document_version_id: str | None = None
    freshness_status: Literal["fresh", "stale", "unverified"] = "unverified"
    freshness_policy: str | None = None
    freshness_limit_hours: float | None = None
    acquisition_completeness: Literal["unverified"] = "unverified"
    provider: Literal["fmp"] = "fmp"
    authority: Literal["captured_provider_market_snapshot"] = "captured_provider_market_snapshot"
    decision_grade: Literal[False] = False


def read_market_context(
    conn: sqlite3.Connection, source_dir: Path, ticker: str, *, as_of: date | datetime
) -> DiscoveryMarketContext:
    ticker = ticker.upper()
    if isinstance(as_of, datetime):
        if as_of.tzinfo is None:
            raise ValueError("market context cutoff must be timezone-aware")
        cutoff = as_of.astimezone(UTC)
    else:
        cutoff = datetime.combine(as_of, time.max, tzinfo=UTC)
    try:
        snapshot, payload = read_stable_artifact(source_dir / f"{ticker}_profile.json")
        packets = TypeAdapter(tuple[ProviderProfilePacket, ...]).validate_json(payload)
        if len(packets) != 1:
            raise ValueError("ambiguous provider profile")
        profile = packets[0]
        if profile.symbol != ticker:
            raise ValueError("provider symbol mismatch")
        rows = conn.execute(
            "SELECT version.document_version_id,observation.retrieved_at,observation.observed_at,version.recorded_at FROM evidence_document_versions version JOIN evidence_source_observations observation ON observation.observation_id=version.observation_id WHERE version.ticker=? AND version.document_type='fmp_profile' AND version.blob_sha256=? AND observation.blob_sha256=version.blob_sha256 AND observation.source_kind='fmp'",
            (ticker, snapshot.file_sha256),
        ).fetchall()
        captures: list[tuple[datetime, str]] = []
        for row in rows:
            captured = datetime.fromisoformat(str(row[1]))
            captured = (
                captured.replace(tzinfo=UTC)
                if captured.tzinfo is None
                else captured.astimezone(UTC)
            )
            knowledge_times: list[datetime] = []
            for stamp in row[2:]:
                known = datetime.fromisoformat(str(stamp))
                knowledge_times.append(
                    known.replace(tzinfo=UTC) if known.tzinfo is None else known.astimezone(UTC)
                )
            if captured <= cutoff and all(known <= cutoff for known in knowledge_times):
                captures.append((captured, str(row[0])))
        if not captures:
            raise ValueError("captured market source unavailable at cutoff")
        captured, version = max(captures)
        tiers = conn.execute(
            "SELECT DISTINCT list_type FROM tracked_companies WHERE ticker=? AND archived_at IS NULL",
            (ticker,),
        ).fetchall()
        limits = [cadence_hours(str(row[0]), "time_sensitive") for row in tiers]
        limit = min(limits) if limits else None
        fresh = limit is not None and (cutoff - captured).total_seconds() / 3600 < limit
        freshness = "unverified" if limit is None else "fresh" if fresh else "stale"
        currency = profile.currency
        valid_currency = (
            currency is not None
            and len(currency) == 3
            and currency.isalpha()
            and currency.isupper()
        )
        price = _valid_price(profile.price)
        return DiscoveryMarketContext(
            ticker=ticker,
            status="available"
            if valid_currency and profile.market_cap is not None and fresh
            else "degraded",
            reason_codes=(
                (
                    ()
                    if valid_currency and profile.market_cap is not None
                    else ("market_cap_or_quote_currency_unavailable",)
                )
                + (() if price is not None else ("current_price_unavailable",))
                + (() if fresh else (f"market_snapshot_freshness_{freshness}",))
            ),
            freshness_status=freshness,
            freshness_policy="pipeline.cadence_policy/profile/time_sensitive"
            if limit is not None
            else None,
            freshness_limit_hours=limit,
            name=profile.company_name,
            sector=profile.sector,
            industry=profile.industry,
            actively_trading=profile.actively_trading,
            market_cap=profile.market_cap if valid_currency else None,
            price=price if valid_currency else None,
            currency=currency if valid_currency else None,
            captured_at=captured,
            source_payload_sha256=snapshot.file_sha256,
            document_version_id=version,
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        return DiscoveryMarketContext(
            ticker=ticker,
            status="unavailable",
            reason_codes=("captured_market_context_unavailable_or_invalid",),
        )


def _valid_price(value: str | Decimal | int | float | None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None
