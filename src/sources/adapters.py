"""Provider-neutral adapters with sealed, fail-closed source contracts."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from log_redact import redact
from models.facts import Currency, FiscalPeriodType

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_FILING_FORM_PATTERN = r"^(?:10-(?:K|Q)|20-F|40-F|6-K|8-K|S-1)(?:/A)?$"


class FilingAuthority(StrEnum):
    SEC = "SEC"
    ISSUER_IR = "ISSUER_IR"
    VENDOR = "VENDOR"


class CorporateActionAdjustment(StrEnum):
    SPLIT_ONLY = "split_only"
    SPLIT_AND_DIVIDEND = "split_and_dividend"
    UNADJUSTED = "unadjusted"


class EstimateMetric(StrEnum):
    REVENUE = "revenue"
    EPS = "eps"
    EBITDA = "ebitda"
    EBIT = "ebit"
    NET_INCOME = "net_income"


class SegmentDimension(StrEnum):
    PRODUCT = "product"
    GEOGRAPHY = "geography"
    CHANNEL = "channel"
    BUSINESS_UNIT = "business_unit"


class CurrencyBindingBasis(StrEnum):
    """What an immutable companion packet proves about a numeric payload."""

    ISSUER_REPORTED = "issuer_reported"
    QUOTE = "quote"


class CurrencyBindingSourceFamily(StrEnum):
    """The packet family that directly declared a returned numeric currency."""

    FMP_FINANCIAL_STATEMENT = "fmp_financial_statement"
    FMP_PROFILE = "fmp_profile"
    SECONDARY_CONSENSUS = "secondary_consensus"
    SECONDARY_PRICE_PAYLOAD = "secondary_price_payload"


class CurrencyBinding(BaseModel):
    """A ticker-scoped currency assertion, sealed to its companion raw packet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(..., pattern=r"^[A-Z0-9.\-]+$")
    currency: Currency
    basis: CurrencyBindingBasis
    source_payload_hash: str = Field(..., pattern=_SHA256_PATTERN)
    source_family: CurrencyBindingSourceFamily


def _normalized_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class FilingSectionPayload(BaseModel):
    """One section, bound to both its text and the exact source packet bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(..., pattern=r"^[A-Z0-9.\-]+$")
    authority: FilingAuthority
    accession_number: str | None = Field(default=None, min_length=1)
    form: str = Field(..., pattern=_FILING_FORM_PATTERN)
    fiscal_year: int | None = None
    fiscal_period: FiscalPeriodType | None = None
    section_name: str = Field(..., min_length=1)
    raw_text: str = Field(..., min_length=1)
    section_hash: str = Field(..., pattern=_SHA256_PATTERN)
    source_payload_hash: str = Field(..., pattern=_SHA256_PATTERN)
    source_url: str | None = None
    fetched_at: datetime
    provider: str = Field(..., min_length=1)

    @field_validator("fetched_at")
    @classmethod
    def fetched_at_is_utc(cls, value: datetime) -> datetime:
        return _normalized_utc(value)


class DatedEstimateObservation(BaseModel):
    """Provider-neutral dated analyst consensus observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(..., pattern=r"^[A-Z0-9.\-]+$")
    provider: str = Field(..., min_length=1)
    observation_date: datetime
    target_period_end: datetime
    fiscal_year: int
    fiscal_period: FiscalPeriodType
    metric: EstimateMetric
    estimated_avg: Decimal
    estimated_low: Decimal | None = None
    estimated_high: Decimal | None = None
    analyst_count: int | None = Field(default=None, ge=0)
    currency: Currency
    currency_binding: CurrencyBinding
    source_payload_hash: str = Field(..., pattern=_SHA256_PATTERN)

    @field_validator("observation_date", "target_period_end")
    @classmethod
    def estimate_timestamps_are_utc(cls, value: datetime) -> datetime:
        return _normalized_utc(value)

    @field_validator("estimated_avg", "estimated_low", "estimated_high")
    @classmethod
    def decimal_is_finite(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("numeric values must be finite")
        return value


class SegmentStructureObservation(BaseModel):
    """Provider-neutral segment cross-section observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(..., pattern=r"^[A-Z0-9.\-]+$")
    provider: str = Field(..., min_length=1)
    period_end: datetime
    fiscal_year: int
    fiscal_period: FiscalPeriodType
    dim_type: SegmentDimension
    segment_name: str = Field(..., min_length=1)
    metric: EstimateMetric = EstimateMetric.REVENUE
    value: Decimal
    currency: Currency
    unit: str = Field(default="actual", pattern=r"^(?:actual|thousands|millions|billions)$")
    source_payload_hash: str = Field(..., pattern=_SHA256_PATTERN)

    @field_validator("period_end")
    @classmethod
    def period_end_is_utc(cls, value: datetime) -> datetime:
        return _normalized_utc(value)

    @field_validator("value")
    @classmethod
    def value_is_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("numeric values must be finite")
        return value


class AdjustedPricePoint(BaseModel):
    """One bar; absent action fields remain absent rather than being fabricated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of_date: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = Field(ge=0)
    split_ratio: Decimal | None = None
    dividend_amount: Decimal | None = None

    @field_validator("as_of_date")
    @classmethod
    def as_of_date_is_utc(cls, value: datetime) -> datetime:
        return _normalized_utc(value)

    @field_validator("open", "high", "low", "close", "split_ratio", "dividend_amount")
    @classmethod
    def prices_are_finite(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("numeric values must be finite")
        return value


class AdjustedPriceSeries(BaseModel):
    """Provider-neutral price series with an explicit adjustment basis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(..., pattern=r"^[A-Z0-9.\-]+$")
    provider: str = Field(..., min_length=1)
    adjustment_method: CorporateActionAdjustment
    currency: Currency
    currency_binding: CurrencyBinding
    points: tuple[AdjustedPricePoint, ...]
    source_payload_hash: str = Field(..., pattern=_SHA256_PATTERN)


def _payload_bytes(raw_content: object) -> bytes:
    if isinstance(raw_content, bytes):
        return raw_content
    if isinstance(raw_content, str):
        return raw_content.encode("utf-8")
    raise ValueError("raw packet must be bytes or text")


def _decode_json(raw_content: bytes | str, *, label: str) -> tuple[object, str]:
    raw_bytes = _payload_bytes(raw_content)
    try:
        return json.loads(raw_bytes.decode("utf-8")), hashlib.sha256(raw_bytes).hexdigest()
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc


def _as_dict(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    typed_value = cast("dict[object, object]", value)
    if not all(isinstance(key, str) for key in typed_value):
        raise ValueError(f"{label} must be a JSON object with string keys")
    return cast("dict[str, object]", value)


def _as_list(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a JSON list")
    return cast("list[object]", value)


def _require_text(record: dict[str, object], key: str, *, label: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} requires non-empty {key}")
    return value.strip()


def _requested_ticker(record: dict[str, object], ticker: str) -> str:
    requested = ticker.strip().upper()
    if not requested:
        raise ValueError("ticker must be non-empty")
    declared = record.get("symbol")
    if declared is not None and str(declared).strip().upper() != requested:
        raise ValueError("payload ticker does not match requested ticker")
    return requested


def _parse_datetime(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO-8601") from exc
    # Date-only FMP fields have no offset; their documented calendar-date policy is UTC.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _parse_decimal(value: object, *, label: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label} is required")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _parse_int(value: object, *, label: str, minimum: int = 0) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label} is required")
    try:
        parsed = int(str(value))
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return parsed


def _currency(record: dict[str, object], *, label: str, key: str = "currency") -> Currency:
    raw = _require_text(record, key, label=label)
    try:
        return Currency(raw.upper())
    except ValueError as exc:
        raise ValueError(f"{label} has unsupported currency {raw!r}") from exc


def _currency_binding(
    raw_content: bytes | str,
    ticker: str,
    *,
    key: str,
    basis: CurrencyBindingBasis,
    source_family: CurrencyBindingSourceFamily,
) -> CurrencyBinding:
    raw, payload_hash = _decode_json(raw_content, label="currency binding packet")
    records = _as_list(raw, label="currency binding packet")
    if not records:
        raise ValueError("currency binding packet must not be empty")
    record = _as_dict(records[0], label="currency binding record")
    symbol = _requested_ticker(record, ticker)
    return CurrencyBinding(
        ticker=symbol,
        currency=_currency(record, label="currency binding record", key=key),
        basis=basis,
        source_payload_hash=payload_hash,
        source_family=source_family,
    )


def issuer_reported_currency_binding(raw_content: bytes | str, ticker: str) -> CurrencyBinding:
    """Seal a reporting-currency assertion from an FMP financial-statement packet."""
    return _currency_binding(
        raw_content,
        ticker,
        key="reportedCurrency",
        basis=CurrencyBindingBasis.ISSUER_REPORTED,
        source_family=CurrencyBindingSourceFamily.FMP_FINANCIAL_STATEMENT,
    )


def quote_currency_binding(raw_content: bytes | str, ticker: str) -> CurrencyBinding:
    """Seal a quote-currency assertion from an FMP profile packet."""
    return _currency_binding(
        raw_content,
        ticker,
        key="currency",
        basis=CurrencyBindingBasis.QUOTE,
        source_family=CurrencyBindingSourceFamily.FMP_PROFILE,
    )


def _require_binding(
    binding: CurrencyBinding,
    ticker: str,
    *,
    basis: CurrencyBindingBasis,
    source_family: CurrencyBindingSourceFamily,
) -> Currency:
    if binding.ticker != ticker.upper():
        raise ValueError("currency binding ticker does not match requested ticker")
    if binding.basis is not basis:
        raise ValueError(f"currency binding must use {basis.value} basis")
    if binding.source_family != source_family:
        raise ValueError(f"currency binding must use {source_family} source family")
    return binding.currency


def _fiscal_period(value: object, *, label: str) -> FiscalPeriodType:
    if not isinstance(value, str):
        raise ValueError(f"{label} requires fiscal period")
    try:
        return FiscalPeriodType(value.upper())
    except ValueError as exc:
        raise ValueError(f"{label} has unsupported fiscal period {value!r}") from exc


def _price_value(
    record: dict[str, object],
    key: str,
    adjustment_method: CorporateActionAdjustment,
) -> Decimal:
    adjusted = f"adj{key[0].upper()}{key[1:]}"
    selected = (
        record.get(adjusted)
        if adjustment_method is CorporateActionAdjustment.SPLIT_AND_DIVIDEND
        and record.get(adjusted) is not None
        else record.get(key)
    )
    return _parse_decimal(selected, label=f"FMP price {key}")


class ProviderAdapter(ABC):
    """The only boundary through which untrusted provider payloads enter readers."""

    @property
    @abstractmethod
    def provider_name(self) -> str: ...

    @abstractmethod
    def parse_filing_sections(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        form: str = "10-K",
        fiscal_year: int | None = None,
        fetched_at: datetime | None = None,
    ) -> list[FilingSectionPayload]: ...

    @abstractmethod
    def parse_estimates(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        observed_at: datetime,
        currency_packet: bytes | str | None = None,
    ) -> list[DatedEstimateObservation]: ...

    @abstractmethod
    def parse_segments(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        dim_type: SegmentDimension = SegmentDimension.GEOGRAPHY,
    ) -> list[SegmentStructureObservation]: ...

    @abstractmethod
    def parse_prices(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        adjustment_method: CorporateActionAdjustment = CorporateActionAdjustment.SPLIT_AND_DIVIDEND,
        currency_packet: bytes | str | None = None,
    ) -> AdjustedPriceSeries: ...


class FmpProviderAdapter(ProviderAdapter):
    """Strict adapter for immutable FMP cache packets."""

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
        raw, payload_hash = _decode_json(raw_content, label="FMP filing section payload")
        payload = _as_dict(raw, label="FMP filing section payload")
        symbol = _requested_ticker(payload, ticker)
        period = _fiscal_period(payload.get("period", "FY"), label="FMP filing section payload")
        raw_year = payload.get("year", fiscal_year)
        year = (
            _parse_int(raw_year, label="FMP filing section year", minimum=1)
            if raw_year is not None
            else None
        )
        source_url = payload.get("link") or payload.get("finalLink")
        if source_url is not None and (not isinstance(source_url, str) or not source_url.strip()):
            raise ValueError("FMP filing section source URL must be text")
        metadata = {"symbol", "period", "year", "link", "finalLink", "reportedCurrency"}
        if any(key.lower() in {"error", "error message", "message"} for key in payload):
            raise ValueError("FMP filing section payload contains an error response")
        results: list[FilingSectionPayload] = []
        for name, content in payload.items():
            if name in metadata:
                continue
            if isinstance(content, str):
                text = content
            elif isinstance(content, (list, dict)):
                text = json.dumps(
                    content, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                )
            else:
                raise ValueError("FMP filing section text must be non-empty text")
            if not text.strip():
                raise ValueError("FMP filing section text must be non-empty text")
            results.append(
                FilingSectionPayload(
                    ticker=symbol,
                    authority=FilingAuthority.VENDOR,
                    form=form,
                    fiscal_year=year,
                    fiscal_period=period,
                    section_name=name,
                    raw_text=text,
                    section_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    source_payload_hash=payload_hash,
                    source_url=source_url,
                    fetched_at=fetched_at or datetime.now(UTC),
                    provider=self.provider_name,
                )
            )
        if not results:
            raise ValueError("FMP filing section payload has no section text")
        return results

    def parse_estimates(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        observed_at: datetime,
        currency_packet: bytes | str | None = None,
    ) -> list[DatedEstimateObservation]:
        raw, payload_hash = _decode_json(raw_content, label="FMP estimate payload")
        if currency_packet is None:
            raise ValueError("FMP estimate payload requires an issuer currency packet")
        currency_binding = issuer_reported_currency_binding(currency_packet, ticker)
        results: list[DatedEstimateObservation] = []
        mappings = (
            (
                EstimateMetric.REVENUE,
                "revenueAvg",
                "revenueLow",
                "revenueHigh",
                "numAnalystsRevenue",
            ),
            (EstimateMetric.EPS, "epsAvg", "epsLow", "epsHigh", "numAnalystsEps"),
            (EstimateMetric.EBITDA, "ebitdaAvg", "ebitdaLow", "ebitdaHigh", None),
            (EstimateMetric.EBIT, "ebitAvg", "ebitLow", "ebitHigh", None),
            (EstimateMetric.NET_INCOME, "netIncomeAvg", "netIncomeLow", "netIncomeHigh", None),
        )
        for index, item in enumerate(_as_list(raw, label="FMP estimate payload")):
            record = _as_dict(item, label=f"FMP estimate record {index}")
            symbol = _requested_ticker(record, ticker)
            observed = _parse_datetime(
                _require_text(record, "date", label="FMP estimate record"),
                label="FMP estimate date",
            )
            quarter = record.get("quarter")
            period = (
                FiscalPeriodType.FY
                if quarter is None
                else _fiscal_period(
                    f"Q{_parse_int(quarter, label='FMP estimate quarter', minimum=1)}",
                    label="FMP estimate record",
                )
            )
            currency = _require_binding(
                currency_binding,
                symbol,
                basis=CurrencyBindingBasis.ISSUER_REPORTED,
                source_family=CurrencyBindingSourceFamily.FMP_FINANCIAL_STATEMENT,
            )
            emitted = False
            for metric, average_key, low_key, high_key, count_key in mappings:
                if average_key not in record or record[average_key] is None:
                    continue
                results.append(
                    DatedEstimateObservation(
                        ticker=symbol,
                        provider=self.provider_name,
                        observation_date=observed_at,
                        target_period_end=observed,
                        fiscal_year=observed.year,
                        fiscal_period=period,
                        metric=metric,
                        estimated_avg=_parse_decimal(
                            record[average_key], label=f"FMP {metric} average"
                        ),
                        estimated_low=_parse_decimal(record[low_key], label=f"FMP {metric} low")
                        if record.get(low_key) is not None
                        else None,
                        estimated_high=_parse_decimal(record[high_key], label=f"FMP {metric} high")
                        if record.get(high_key) is not None
                        else None,
                        analyst_count=_parse_int(
                            record[count_key], label=f"FMP {metric} analyst count"
                        )
                        if count_key and record.get(count_key) is not None
                        else None,
                        currency=currency,
                        currency_binding=currency_binding,
                        source_payload_hash=payload_hash,
                    )
                )
                emitted = True
            if not emitted:
                raise ValueError(f"FMP estimate record {index} has no supported estimate metric")
        return results

    def parse_segments(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        dim_type: SegmentDimension = SegmentDimension.GEOGRAPHY,
    ) -> list[SegmentStructureObservation]:
        raw, payload_hash = _decode_json(raw_content, label="FMP segment payload")
        results: list[SegmentStructureObservation] = []
        for index, item in enumerate(_as_list(raw, label="FMP segment payload")):
            record = _as_dict(item, label=f"FMP segment record {index}")
            symbol = _requested_ticker(record, ticker)
            period_end = _parse_datetime(
                _require_text(record, "date", label="FMP segment record"), label="FMP segment date"
            )
            data = _as_dict(record.get("data"), label="FMP segment data")
            if not data:
                raise ValueError("FMP segment data must not be empty")
            year = _parse_int(
                record.get("fiscalYear", period_end.year), label="FMP fiscal year", minimum=1
            )
            period = _fiscal_period(record.get("period", "FY"), label="FMP segment record")
            currency = _currency(record, label="FMP segment record", key="reportedCurrency")
            for name, value in data.items():
                if not name.strip():
                    raise ValueError("FMP segment name must be non-empty")
                results.append(
                    SegmentStructureObservation(
                        ticker=symbol,
                        provider=self.provider_name,
                        period_end=period_end,
                        fiscal_year=year,
                        fiscal_period=period,
                        dim_type=dim_type,
                        segment_name=name,
                        value=_parse_decimal(value, label=f"FMP segment {name}"),
                        currency=currency,
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
        currency_packet: bytes | str | None = None,
    ) -> AdjustedPriceSeries:
        raw, payload_hash = _decode_json(raw_content, label="FMP price payload")
        if currency_packet is None:
            raise ValueError("FMP price payload requires a quote currency packet")
        currency_binding = quote_currency_binding(currency_packet, ticker)
        root = (
            _as_dict(cast("object", raw), label="FMP price payload")
            if isinstance(raw, dict)
            else None
        )
        symbol = _requested_ticker(root, ticker) if root is not None else ticker.upper()
        records = (
            _as_list(root.get("historical"), label="FMP historical prices")
            if root is not None
            else _as_list(cast("object", raw), label="FMP historical prices")
        )
        currency = _require_binding(
            currency_binding,
            symbol,
            basis=CurrencyBindingBasis.QUOTE,
            source_family=CurrencyBindingSourceFamily.FMP_PROFILE,
        )
        points: list[AdjustedPricePoint] = []
        for index, item in enumerate(records):
            record = _as_dict(item, label=f"FMP price record {index}")
            date = _parse_datetime(
                _require_text(record, "date", label="FMP price record"), label="FMP price date"
            )

            points.append(
                AdjustedPricePoint(
                    as_of_date=date,
                    open=_price_value(record, "open", adjustment_method),
                    high=_price_value(record, "high", adjustment_method),
                    low=_price_value(record, "low", adjustment_method),
                    close=_price_value(record, "close", adjustment_method),
                    volume=_parse_int(record.get("volume"), label="FMP price volume"),
                    split_ratio=_parse_decimal(record["splitRatio"], label="FMP split ratio")
                    if record.get("splitRatio") is not None
                    else None,
                    dividend_amount=_parse_decimal(record["dividend"], label="FMP dividend")
                    if record.get("dividend") is not None
                    else None,
                )
            )
        if not points:
            raise ValueError("FMP historical prices must not be empty")
        points.sort(key=lambda point: point.as_of_date)
        return AdjustedPriceSeries(
            ticker=symbol,
            provider=self.provider_name,
            adjustment_method=adjustment_method,
            currency=currency,
            currency_binding=currency_binding,
            points=tuple(points),
            source_payload_hash=payload_hash,
        )


class SyntheticSecondaryProviderAdapter(ProviderAdapter):
    """Fixture-backed second provider, deliberately vendor-authoritative only."""

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
        raw, payload_hash = _decode_json(raw_content, label="secondary filing payload")
        payload = _as_dict(raw, label="secondary filing payload")
        sections = _as_list(payload.get("sections"), label="secondary filing sections")
        results: list[FilingSectionPayload] = []
        for index, item in enumerate(sections):
            record = _as_dict(item, label=f"secondary filing section {index}")
            text = _require_text(record, "text", label="secondary filing section")
            results.append(
                FilingSectionPayload(
                    ticker=ticker.upper(),
                    authority=FilingAuthority.VENDOR,
                    accession_number=str(record["accession"])
                    if record.get("accession") is not None
                    else None,
                    form=form,
                    fiscal_year=fiscal_year,
                    fiscal_period=_fiscal_period(
                        record.get("period", "FY"), label="secondary filing section"
                    ),
                    section_name=_require_text(record, "name", label="secondary filing section"),
                    raw_text=text,
                    section_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    source_payload_hash=payload_hash,
                    source_url=str(record["url"]) if record.get("url") is not None else None,
                    fetched_at=fetched_at or datetime.now(UTC),
                    provider=self.provider_name,
                )
            )
        if not results:
            raise ValueError("secondary filing sections must not be empty")
        return results

    def parse_estimates(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        observed_at: datetime,
        currency_packet: bytes | str | None = None,
    ) -> list[DatedEstimateObservation]:
        raw, payload_hash = _decode_json(raw_content, label="secondary estimate payload")
        if currency_packet is not None and _payload_bytes(currency_packet) != _payload_bytes(
            raw_content
        ):
            raise ValueError("secondary estimate currency packet must be the estimate packet")
        payload = _as_dict(raw, label="secondary estimate payload")
        results: list[DatedEstimateObservation] = []
        for index, item in enumerate(
            _as_list(payload.get("consensus"), label="secondary consensus")
        ):
            record = _as_dict(item, label=f"secondary consensus record {index}")
            target_period_end = _parse_datetime(
                _require_text(record, "as_of", label="secondary consensus record"),
                label="secondary estimate date",
            )
            try:
                metric = EstimateMetric(
                    _require_text(record, "metric", label="secondary consensus record")
                )
            except ValueError as exc:
                raise ValueError("secondary estimate metric is unsupported") from exc
            results.append(
                DatedEstimateObservation(
                    ticker=ticker.upper(),
                    provider=self.provider_name,
                    observation_date=observed_at,
                    target_period_end=target_period_end,
                    fiscal_year=_parse_int(
                        record.get("year"), label="secondary estimate year", minimum=1
                    ),
                    fiscal_period=_fiscal_period(
                        record.get("period"), label="secondary consensus record"
                    ),
                    metric=metric,
                    estimated_avg=_parse_decimal(
                        record.get("mean"), label="secondary estimate mean"
                    ),
                    estimated_low=_parse_decimal(record["min"], label="secondary estimate min")
                    if record.get("min") is not None
                    else None,
                    estimated_high=_parse_decimal(record["max"], label="secondary estimate max")
                    if record.get("max") is not None
                    else None,
                    analyst_count=_parse_int(record["count"], label="secondary estimate count")
                    if record.get("count") is not None
                    else None,
                    currency=_currency(record, label="secondary consensus record"),
                    currency_binding=CurrencyBinding(
                        ticker=ticker.upper(),
                        currency=_currency(record, label="secondary consensus record"),
                        basis=CurrencyBindingBasis.ISSUER_REPORTED,
                        source_payload_hash=payload_hash,
                        source_family=CurrencyBindingSourceFamily.SECONDARY_CONSENSUS,
                    ),
                    source_payload_hash=payload_hash,
                )
            )
        return results

    def parse_segments(
        self,
        raw_content: bytes | str,
        ticker: str,
        *,
        dim_type: SegmentDimension = SegmentDimension.GEOGRAPHY,
    ) -> list[SegmentStructureObservation]:
        raw, payload_hash = _decode_json(raw_content, label="secondary segment payload")
        payload = _as_dict(raw, label="secondary segment payload")
        results: list[SegmentStructureObservation] = []
        for index, item in enumerate(
            _as_list(payload.get("breakdowns"), label="secondary breakdowns")
        ):
            record = _as_dict(item, label=f"secondary breakdown {index}")
            date = _parse_datetime(
                _require_text(record, "period_end", label="secondary breakdown"),
                label="secondary segment date",
            )
            try:
                metric = EstimateMetric(
                    _require_text(record, "metric", label="secondary breakdown")
                )
            except ValueError as exc:
                raise ValueError("secondary segment metric is unsupported") from exc
            results.append(
                SegmentStructureObservation(
                    ticker=ticker.upper(),
                    provider=self.provider_name,
                    period_end=date,
                    fiscal_year=_parse_int(
                        record.get("year"), label="secondary segment year", minimum=1
                    ),
                    fiscal_period=_fiscal_period(record.get("period"), label="secondary breakdown"),
                    dim_type=dim_type,
                    segment_name=_require_text(record, "segment", label="secondary breakdown"),
                    metric=metric,
                    value=_parse_decimal(record.get("amount"), label="secondary segment amount"),
                    currency=_currency(record, label="secondary breakdown"),
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
        currency_packet: bytes | str | None = None,
    ) -> AdjustedPriceSeries:
        raw, payload_hash = _decode_json(raw_content, label="secondary price payload")
        if currency_packet is not None and _payload_bytes(currency_packet) != _payload_bytes(
            raw_content
        ):
            raise ValueError("secondary price currency packet must be the price packet")
        payload = _as_dict(raw, label="secondary price payload")
        currency = _currency(payload, label="secondary price payload")
        points: list[AdjustedPricePoint] = []
        for index, item in enumerate(_as_list(payload.get("bars"), label="secondary price bars")):
            record = _as_dict(item, label=f"secondary price bar {index}")
            points.append(
                AdjustedPricePoint(
                    as_of_date=_parse_datetime(
                        _require_text(record, "timestamp", label="secondary price bar"),
                        label="secondary price timestamp",
                    ),
                    open=_parse_decimal(record.get("open"), label="secondary price open"),
                    high=_parse_decimal(record.get("high"), label="secondary price high"),
                    low=_parse_decimal(record.get("low"), label="secondary price low"),
                    close=_parse_decimal(record.get("close"), label="secondary price close"),
                    volume=_parse_int(record.get("volume"), label="secondary price volume"),
                    split_ratio=_parse_decimal(
                        record["split_factor"], label="secondary split ratio"
                    )
                    if record.get("split_factor") is not None
                    else None,
                    dividend_amount=_parse_decimal(record["dividend"], label="secondary dividend")
                    if record.get("dividend") is not None
                    else None,
                )
            )
        if not points:
            raise ValueError("secondary price bars must not be empty")
        points.sort(key=lambda point: point.as_of_date)
        return AdjustedPriceSeries(
            ticker=ticker.upper(),
            provider=self.provider_name,
            adjustment_method=adjustment_method,
            currency=currency,
            currency_binding=CurrencyBinding(
                ticker=ticker.upper(),
                currency=currency,
                basis=CurrencyBindingBasis.QUOTE,
                source_payload_hash=payload_hash,
                source_family=CurrencyBindingSourceFamily.SECONDARY_PRICE_PAYLOAD,
            ),
            points=tuple(points),
            source_payload_hash=payload_hash,
        )


def format_error_envelope(exc: Exception, raw_payload: str | None = None) -> dict[str, str]:
    """Return a credential-redacted operational error envelope."""
    body = redact(raw_payload) if raw_payload else None
    return {
        "status": "error",
        "error_type": type(exc).__name__,
        "message": redact(f"{type(exc).__name__}: {exc}"),
        "sanitized_payload_snippet": (body[:256] + "...") if body else "none",
    }
