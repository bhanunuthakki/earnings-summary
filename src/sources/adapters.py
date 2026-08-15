"""Provider-neutral data adapters and frozen domain schemas.

Defines canonical Pydantic V2 frozen contracts for filing sections, dated analyst
estimates, product/geographic segments, and corporate-action-adjusted price series.
Decouples core business logic and synthesis readers from provider-specific JSON schemas.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from log_redact import redact


class FilingAuthority(StrEnum):
    SEC = "SEC"
    ISSUER_IR = "ISSUER_IR"
    VENDOR = "VENDOR"


class CorporateActionAdjustment(StrEnum):
    SPLIT_ONLY = "split_only"
    SPLIT_AND_DIVIDEND = "split_and_dividend"
    UNADJUSTED = "unadjusted"


class FilingSectionPayload(BaseModel):
    """Provider-neutral representation of a single filing section."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(..., min_length=1)
    authority: FilingAuthority
    accession_number: str | None = None
    form: str = Field(..., min_length=2)  # "10-K", "10-Q", "20-F", "40-F", "6-K", "8-K"
    fiscal_year: int | None = None
    fiscal_period: str | None = None  # "Q1", "Q2", "Q3", "Q4", "FY", "9M", "H1", "H2"
    section_name: str = Field(..., min_length=1)
    raw_text: str
    section_hash: str = Field(..., min_length=64, max_length=64)
    source_url: str | None = None
    fetched_at: datetime
    provider: str = Field(..., min_length=1)


class DatedEstimateObservation(BaseModel):
    """Provider-neutral dated analyst consensus estimate observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    observation_date: datetime
    target_period_end: datetime
    fiscal_year: int
    fiscal_period: str  # "Q1", "Q2", "Q3", "Q4", "FY"
    metric: str  # "revenue", "eps", "ebitda", "ebit", "net_income", "sga"
    estimated_avg: Decimal
    estimated_low: Decimal | None = None
    estimated_high: Decimal | None = None
    analyst_count: int | None = None
    currency: str = "USD"
    source_payload_hash: str = Field(..., min_length=64, max_length=64)


class SegmentStructureObservation(BaseModel):
    """Provider-neutral segment cross-section observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    period_end: datetime
    fiscal_year: int
    fiscal_period: str  # "Q1", "Q2", "Q3", "Q4", "FY"
    dim_type: str  # "product", "geography", "channel", "business_unit"
    segment_name: str = Field(..., min_length=1)
    metric: str = "revenue"
    value: Decimal
    currency: str = "USD"
    unit: str = "actual"
    source_payload_hash: str = Field(..., min_length=64, max_length=64)


class AdjustedPricePoint(BaseModel):
    """Single bar in an adjusted price series."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of_date: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    split_ratio: Decimal = Decimal("1.0")
    dividend_amount: Decimal | None = None


class AdjustedPriceSeries(BaseModel):
    """Provider-neutral price time series with explicit corporate action basis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    adjustment_method: CorporateActionAdjustment
    currency: str = "USD"
    points: tuple[AdjustedPricePoint, ...]
    source_payload_hash: str = Field(..., min_length=64, max_length=64)


def _compute_sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


class ProviderAdapter(ABC):
    """Abstract boundary adapter interface for third-party financial data."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the data provider (e.g., 'fmp', 'sec_edgar', 'synthetic')."""
        ...

    @abstractmethod
    def parse_filing_sections(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        form: str = "10-K",
        fiscal_year: int | None = None,
        fetched_at: datetime | None = None,
    ) -> list[FilingSectionPayload]:
        """Parse structured filing sections from raw provider response."""
        ...

    @abstractmethod
    def parse_estimates(
        self,
        raw_content: bytes | str,
        ticker: str,
    ) -> list[DatedEstimateObservation]:
        """Parse consensus analyst estimates from raw provider response."""
        ...

    @abstractmethod
    def parse_segments(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        dim_type: str = "geography",
    ) -> list[SegmentStructureObservation]:
        """Parse product or geographic segment structure from raw provider response."""
        ...

    @abstractmethod
    def parse_prices(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        adjustment_method: CorporateActionAdjustment = CorporateActionAdjustment.SPLIT_AND_DIVIDEND,
        currency: str = "USD",
    ) -> AdjustedPriceSeries:
        """Parse corporate-action adjusted historical prices from raw provider response."""
        ...


class FmpProviderAdapter(ProviderAdapter):
    """Adapter for Financial Modeling Prep (FMP) JSON cached payloads."""

    @property
    def provider_name(self) -> str:
        return "fmp"

    def parse_filing_sections(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        form: str = "10-K",
        fiscal_year: int | None = None,
        fetched_at: datetime | None = None,
    ) -> list[FilingSectionPayload]:
        text = raw_content if isinstance(raw_content, str) else raw_content.decode("utf-8", errors="replace")
        raw_obj: object = json.loads(text)
        if not isinstance(raw_obj, dict):
            raise ValueError(f"FMP filing section payload must be a JSON object, got {type(raw_obj)}")

        payload = cast("dict[str, object]", raw_obj)
        fetch_ts = fetched_at or datetime.now(UTC)
        symbol = str(payload.get("symbol", ticker)).upper()
        raw_year = payload.get("year")
        fy = int(str(raw_year)) if raw_year is not None and str(raw_year).isdigit() else fiscal_year
        period = str(payload.get("period", "FY"))

        sections: list[FilingSectionPayload] = []
        metadata_keys = {"symbol", "period", "year", "link", "finalLink"}

        for sec_name, sec_content in payload.items():
            if sec_name in metadata_keys or sec_content is None:
                continue
            if isinstance(sec_content, str):
                cleaned_text = sec_content.strip()
            elif isinstance(sec_content, (list, dict)):
                cleaned_text = json.dumps(sec_content, sort_keys=True)
            else:
                cleaned_text = str(sec_content).strip()

            if not cleaned_text:
                continue

            sec_hash = _compute_sha256(cleaned_text)
            source_url = payload.get("link") or payload.get("finalLink")
            sections.append(
                FilingSectionPayload(
                    ticker=symbol,
                    authority=FilingAuthority.VENDOR,
                    accession_number=None,
                    form=form,
                    fiscal_year=fy,
                    fiscal_period=period,
                    section_name=sec_name,
                    raw_text=cleaned_text,
                    section_hash=sec_hash,
                    source_url=str(source_url) if source_url is not None else None,
                    fetched_at=fetch_ts,
                    provider=self.provider_name,
                )
            )
        return sections

    def parse_estimates(
        self,
        raw_content: bytes | str,
        ticker: str,
    ) -> list[DatedEstimateObservation]:
        text = raw_content if isinstance(raw_content, str) else raw_content.decode("utf-8", errors="replace")
        payload_hash = _compute_sha256(text)
        raw_obj: object = json.loads(text)
        if not isinstance(raw_obj, list):
            raise ValueError(f"FMP estimate payload must be a JSON list, got {type(raw_obj)}")

        raw_list = cast("list[object]", raw_obj)
        results: list[DatedEstimateObservation] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            rec = cast("dict[str, object]", item)
            symbol = str(rec.get("symbol", ticker)).upper()
            date_str = str(rec.get("date", "")).strip()
            if not date_str:
                continue
            obs_dt = datetime.fromisoformat(date_str).replace(tzinfo=UTC)

            fy = obs_dt.year
            fp = "FY"
            if "quarter" in rec and rec["quarter"] is not None:
                fp = f"Q{rec['quarter']}"

            metric_mappings = [
                ("revenue", "revenueAvg", "revenueLow", "revenueHigh", "numAnalystsRevenue"),
                ("eps", "epsAvg", "epsLow", "epsHigh", "numAnalystsEps"),
                ("ebitda", "ebitdaAvg", "ebitdaLow", "ebitdaHigh", None),
                ("ebit", "ebitAvg", "ebitLow", "ebitHigh", None),
                ("net_income", "netIncomeAvg", "netIncomeLow", "netIncomeHigh", None),
            ]

            for metric_name, avg_k, low_k, high_k, count_k in metric_mappings:
                avg_val = rec.get(avg_k)
                if avg_val is None:
                    continue
                try:
                    d_avg = Decimal(str(avg_val))
                except Exception:
                    continue

                d_low = Decimal(str(rec[low_k])) if rec.get(low_k) is not None else None
                d_high = Decimal(str(rec[high_k])) if rec.get(high_k) is not None else None
                count = int(str(rec[count_k])) if count_k and rec.get(count_k) is not None else None

                results.append(
                    DatedEstimateObservation(
                        ticker=symbol,
                        provider=self.provider_name,
                        observation_date=obs_dt,
                        target_period_end=obs_dt,
                        fiscal_year=fy,
                        fiscal_period=fp,
                        metric=metric_name,
                        estimated_avg=d_avg,
                        estimated_low=d_low,
                        estimated_high=d_high,
                        analyst_count=count,
                        currency="USD",
                        source_payload_hash=payload_hash,
                    )
                )
        return results

    def parse_segments(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        dim_type: str = "geography",
    ) -> list[SegmentStructureObservation]:
        text = raw_content if isinstance(raw_content, str) else raw_content.decode("utf-8", errors="replace")
        payload_hash = _compute_sha256(text)
        raw_obj: object = json.loads(text)
        if not isinstance(raw_obj, list):
            raise ValueError(f"FMP segment payload must be a JSON list, got {type(raw_obj)}")

        raw_list = cast("list[object]", raw_obj)
        results: list[SegmentStructureObservation] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            rec = cast("dict[str, object]", item)
            symbol = str(rec.get("symbol", ticker)).upper()
            date_str = str(rec.get("date", "")).strip()
            if not date_str:
                continue
            period_dt = datetime.fromisoformat(date_str).replace(tzinfo=UTC)
            fy = int(str(rec["fiscalYear"])) if "fiscalYear" in rec and rec["fiscalYear"] is not None else period_dt.year
            fp = str(rec.get("period", "FY"))
            currency = str(rec.get("reportedCurrency", "USD")).upper()
            seg_data_obj = rec.get("data")
            if not isinstance(seg_data_obj, dict):
                continue
            seg_data = cast("dict[str, object]", seg_data_obj)

            for seg_name, val in seg_data.items():
                if val is None:
                    continue
                try:
                    dec_val = Decimal(str(val))
                except Exception:
                    continue

                results.append(
                    SegmentStructureObservation(
                        ticker=symbol,
                        provider=self.provider_name,
                        period_end=period_dt,
                        fiscal_year=fy,
                        fiscal_period=fp,
                        dim_type=dim_type,
                        segment_name=str(seg_name),
                        metric="revenue",
                        value=dec_val,
                        currency=currency,
                        unit="actual",
                        source_payload_hash=payload_hash,
                    )
                )
        return results

    def parse_prices(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        adjustment_method: CorporateActionAdjustment = CorporateActionAdjustment.SPLIT_AND_DIVIDEND,
        currency: str = "USD",
    ) -> AdjustedPriceSeries:
        text = raw_content if isinstance(raw_content, str) else raw_content.decode("utf-8", errors="replace")
        payload_hash = _compute_sha256(text)
        raw_obj: object = json.loads(text)

        records: list[object] = []
        if isinstance(raw_obj, list):
            records = cast("list[object]", raw_obj)
        elif isinstance(raw_obj, dict):
            raw_dict = cast("dict[str, object]", raw_obj)
            historical = raw_dict.get("historical")
            if isinstance(historical, list):
                records = cast("list[object]", historical)

        if not records and not isinstance(raw_obj, (list, dict)):
            raise ValueError(f"FMP price payload shape unexpected: {type(raw_obj)}")

        points: list[AdjustedPricePoint] = []
        for item in records:
            if not isinstance(item, dict):
                continue
            rec = cast("dict[str, object]", item)
            date_str = str(rec.get("date", "")).strip()
            if not date_str:
                continue
            dt = datetime.fromisoformat(date_str).replace(tzinfo=UTC)

            if adjustment_method == CorporateActionAdjustment.SPLIT_AND_DIVIDEND:
                o = rec.get("adjOpen") if rec.get("adjOpen") is not None else rec.get("open")
                h = rec.get("adjHigh") if rec.get("adjHigh") is not None else rec.get("high")
                low_p = rec.get("adjLow") if rec.get("adjLow") is not None else rec.get("low")
                c = rec.get("adjClose") if rec.get("adjClose") is not None else rec.get("close")
            else:
                o = rec.get("open")
                h = rec.get("high")
                low_p = rec.get("low")
                c = rec.get("close")

            if any(v is None for v in (o, h, low_p, c)):
                continue

            vol_val = rec.get("volume", 0)
            vol = int(str(vol_val)) if vol_val is not None else 0
            points.append(
                AdjustedPricePoint(
                    as_of_date=dt,
                    open=Decimal(str(o)),
                    high=Decimal(str(h)),
                    low=Decimal(str(low_p)),
                    close=Decimal(str(c)),
                    volume=vol,
                    split_ratio=Decimal("1.0"),
                    dividend_amount=None,
                )
            )

        points.sort(key=lambda p: p.as_of_date)

        return AdjustedPriceSeries(
            ticker=ticker.upper(),
            provider=self.provider_name,
            adjustment_method=adjustment_method,
            currency=currency.upper(),
            points=tuple(points),
            source_payload_hash=payload_hash,
        )


class SyntheticSecondaryProviderAdapter(ProviderAdapter):
    """Mock secondary provider adapter demonstrating multi-provider neutrality."""

    @property
    def provider_name(self) -> str:
        return "synthetic_secondary"

    def parse_filing_sections(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        form: str = "10-K",
        fiscal_year: int | None = None,
        fetched_at: datetime | None = None,
    ) -> list[FilingSectionPayload]:
        text = raw_content if isinstance(raw_content, str) else raw_content.decode("utf-8", errors="replace")
        raw_obj: object = json.loads(text)
        payload = cast("dict[str, object]", raw_obj) if isinstance(raw_obj, dict) else {}
        fetch_ts = fetched_at or datetime.now(UTC)
        sections: list[FilingSectionPayload] = []
        raw_sections = payload.get("sections", [])
        if isinstance(raw_sections, list):
            for sec_obj in cast("list[object]", raw_sections):
                if not isinstance(sec_obj, dict):
                    continue
                sec = cast("dict[str, object]", sec_obj)
                raw = str(sec.get("text", ""))
                accession = sec.get("accession")
                period = sec.get("period", "FY")
                source_url = sec.get("url")
                sections.append(
                    FilingSectionPayload(
                        ticker=ticker.upper(),
                        authority=FilingAuthority.SEC,
                        accession_number=str(accession) if accession is not None else None,
                        form=form,
                        fiscal_year=fiscal_year,
                        fiscal_period=str(period) if period is not None else "FY",
                        section_name=str(sec.get("name", "")),
                        raw_text=raw,
                        section_hash=_compute_sha256(raw),
                        source_url=str(source_url) if source_url is not None else None,
                        fetched_at=fetch_ts,
                        provider=self.provider_name,
                    )
                )
        return sections

    def parse_estimates(
        self,
        raw_content: bytes | str,
        ticker: str,
    ) -> list[DatedEstimateObservation]:
        text = raw_content if isinstance(raw_content, str) else raw_content.decode("utf-8", errors="replace")
        payload_hash = _compute_sha256(text)
        raw_obj: object = json.loads(text)
        payload = cast("dict[str, object]", raw_obj) if isinstance(raw_obj, dict) else {}
        results: list[DatedEstimateObservation] = []
        raw_consensus = payload.get("consensus", [])
        if isinstance(raw_consensus, list):
            for est_obj in cast("list[object]", raw_consensus):
                if not isinstance(est_obj, dict):
                    continue
                est = cast("dict[str, object]", est_obj)
                as_of_str = str(est.get("as_of", ""))
                if not as_of_str:
                    continue
                dt = datetime.fromisoformat(as_of_str).replace(tzinfo=UTC)
                count_val = est.get("count")
                results.append(
                    DatedEstimateObservation(
                        ticker=ticker.upper(),
                        provider=self.provider_name,
                        observation_date=dt,
                        target_period_end=dt,
                        fiscal_year=int(str(est.get("year", dt.year))),
                        fiscal_period=str(est.get("period", "Q1")),
                        metric=str(est.get("metric", "revenue")),
                        estimated_avg=Decimal(str(est.get("mean", "0"))),
                        estimated_low=Decimal(str(est["min"])) if "min" in est and est["min"] is not None else None,
                        estimated_high=Decimal(str(est["max"])) if "max" in est and est["max"] is not None else None,
                        analyst_count=int(str(count_val)) if count_val is not None else None,
                        currency=str(est.get("currency", "USD")),
                        source_payload_hash=payload_hash,
                    )
                )
        return results

    def parse_segments(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        dim_type: str = "geography",
    ) -> list[SegmentStructureObservation]:
        text = raw_content if isinstance(raw_content, str) else raw_content.decode("utf-8", errors="replace")
        payload_hash = _compute_sha256(text)
        raw_obj: object = json.loads(text)
        payload = cast("dict[str, object]", raw_obj) if isinstance(raw_obj, dict) else {}
        results: list[SegmentStructureObservation] = []
        raw_breakdowns = payload.get("breakdowns", [])
        if isinstance(raw_breakdowns, list):
            for s_obj in cast("list[object]", raw_breakdowns):
                if not isinstance(s_obj, dict):
                    continue
                s = cast("dict[str, object]", s_obj)
                period_str = str(s.get("period_end", ""))
                if not period_str:
                    continue
                dt = datetime.fromisoformat(period_str).replace(tzinfo=UTC)
                results.append(
                    SegmentStructureObservation(
                        ticker=ticker.upper(),
                        provider=self.provider_name,
                        period_end=dt,
                        fiscal_year=int(str(s.get("year", dt.year))),
                        fiscal_period=str(s.get("period", "FY")),
                        dim_type=dim_type,
                        segment_name=str(s.get("segment", "")),
                        metric=str(s.get("metric", "revenue")),
                        value=Decimal(str(s.get("amount", "0"))),
                        currency=str(s.get("currency", "USD")),
                        unit="actual",
                        source_payload_hash=payload_hash,
                    )
                )
        return results

    def parse_prices(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        adjustment_method: CorporateActionAdjustment = CorporateActionAdjustment.SPLIT_AND_DIVIDEND,
        currency: str = "USD",
    ) -> AdjustedPriceSeries:
        text = raw_content if isinstance(raw_content, str) else raw_content.decode("utf-8", errors="replace")
        payload_hash = _compute_sha256(text)
        raw_obj: object = json.loads(text)
        payload = cast("dict[str, object]", raw_obj) if isinstance(raw_obj, dict) else {}
        points: list[AdjustedPricePoint] = []
        raw_bars = payload.get("bars", [])
        if isinstance(raw_bars, list):
            for p_obj in cast("list[object]", raw_bars):
                if not isinstance(p_obj, dict):
                    continue
                p = cast("dict[str, object]", p_obj)
                ts_str = str(p.get("timestamp", ""))
                if not ts_str:
                    continue
                dt = datetime.fromisoformat(ts_str).replace(tzinfo=UTC)
                div_val = p.get("dividend")
                points.append(
                    AdjustedPricePoint(
                        as_of_date=dt,
                        open=Decimal(str(p.get("open", "0"))),
                        high=Decimal(str(p.get("high", "0"))),
                        low=Decimal(str(p.get("low", "0"))),
                        close=Decimal(str(p.get("close", "0"))),
                        volume=int(str(p.get("volume", "0"))),
                        split_ratio=Decimal(str(p.get("split_factor", "1.0"))),
                        dividend_amount=Decimal(str(div_val)) if div_val is not None else None,
                    )
                )
        points.sort(key=lambda x: x.as_of_date)
        return AdjustedPriceSeries(
            ticker=ticker.upper(),
            provider=self.provider_name,
            adjustment_method=adjustment_method,
            currency=currency.upper(),
            points=tuple(points),
            source_payload_hash=payload_hash,
        )


def format_error_envelope(exc: Exception, raw_payload: str | None = None) -> dict[str, str]:
    """Format an exception into a safe, credential-redacted error envelope."""
    msg = f"{type(exc).__name__}: {exc}"
    redacted_msg = redact(msg)
    redacted_body = redact(raw_payload) if raw_payload else None
    return {
        "status": "error",
        "error_type": type(exc).__name__,
        "message": redacted_msg,
        "sanitized_payload_snippet": (redacted_body[:256] + "...") if redacted_body else "none",
    }
