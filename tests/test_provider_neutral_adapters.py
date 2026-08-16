"""Tests for provider-neutral data adapters and Pydantic V2 frozen schemas."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from sources.adapters import (
    CorporateActionAdjustment,
    CurrencyBinding,
    CurrencyBindingBasis,
    FilingAuthority,
    FilingSectionPayload,
    FmpProviderAdapter,
    SegmentDimension,
    SyntheticSecondaryProviderAdapter,
    format_error_envelope,
    issuer_reported_currency_binding,
    quote_currency_binding,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FMP_DIR = REPO_ROOT / "data" / "historical" / "fmp"
OBSERVED_AT = datetime(2026, 4, 1, tzinfo=UTC)


def test_frozen_schema_immutability_and_extra_forbid() -> None:
    now = datetime.now(UTC)
    sha = "a" * 64
    payload = FilingSectionPayload(
        ticker="WIX",
        authority=FilingAuthority.SEC,
        form="10-K",
        section_name="Item 1",
        raw_text="Business description",
        section_hash=sha,
        source_payload_hash=sha,
        fetched_at=now,
        provider="sec",
    )

    # Immutability check
    with pytest.raises(ValidationError):
        payload.__setattr__("ticker", "NEW")

    # Extra field forbidden check
    data = payload.model_dump()
    data["extra_forbidden_key"] = "bad"
    with pytest.raises(ValidationError):
        FilingSectionPayload.model_validate(data)


def test_fmp_adapter_parses_all_contract_shapes_without_local_corpus() -> None:
    """The adapter contract is portable; cache fixtures are additive evidence only."""
    adapter = FmpProviderAdapter()

    sections = adapter.parse_filing_sections(
        json.dumps(
            {
                "symbol": "WIX",
                "year": "2024",
                "period": "FY",
                "reportedCurrency": "USD",
                "link": "https://example.test/wix-2024",
                "business": "Website creation platform",
            }
        ),
        "WIX",
        form="20-F",
    )
    assert [(item.ticker, item.form, item.section_name) for item in sections] == [
        ("WIX", "20-F", "business")
    ]

    issuer_currency = issuer_reported_currency_binding(
        '[{"symbol":"WIX","reportedCurrency":"USD"}]', "WIX"
    )
    estimates = adapter.parse_estimates(
        json.dumps(
            [
                {
                    "symbol": "WIX",
                    "date": "2026-03-31",
                    "quarter": 1,
                    "revenueAvg": "125000000",
                    "revenueLow": "120000000",
                    "revenueHigh": "130000000",
                    "numAnalystsRevenue": 14,
                }
            ]
        ),
        "WIX",
        observed_at=OBSERVED_AT,
        currency_binding=issuer_currency,
    )
    assert [(item.metric, item.fiscal_period, item.estimated_avg) for item in estimates] == [
        ("revenue", "Q1", Decimal("125000000"))
    ]

    segments = adapter.parse_segments(
        json.dumps(
            [
                {
                    "symbol": "WIX",
                    "date": "2026-03-31",
                    "fiscalYear": 2026,
                    "period": "Q1",
                    "reportedCurrency": "USD",
                    "data": {"North America": "80000000"},
                }
            ]
        ),
        "WIX",
    )
    assert [(item.segment_name, item.value) for item in segments] == [
        ("North America", Decimal("80000000"))
    ]

    quote_currency = quote_currency_binding('[{"symbol":"WIX","currency":"USD"}]', "WIX")
    prices = adapter.parse_prices(
        json.dumps(
            {
                "historical": [
                    {
                        "date": "2026-03-31",
                        "adjOpen": "100",
                        "adjHigh": "110",
                        "adjLow": "95",
                        "adjClose": "105",
                        "volume": 500000,
                    }
                ],
            }
        ),
        "WIX",
        currency_binding=quote_currency,
    )
    assert [(item.as_of_date.date().isoformat(), item.close) for item in prices.points] == [
        ("2026-03-31", Decimal("105"))
    ]


def test_fmp_filing_sections_parsing() -> None:
    wix_10k_file = FMP_DIR / "WIX_form_10k_2024.json"
    if not wix_10k_file.exists():
        pytest.skip(f"{wix_10k_file} not found on disk")

    adapter = FmpProviderAdapter()
    raw = wix_10k_file.read_text(encoding="utf-8")
    sections = adapter.parse_filing_sections(raw, "WIX", form="10-K", fiscal_year=2024)

    assert len(sections) > 0
    first = sections[0]
    assert first.ticker == "WIX"
    assert first.authority == FilingAuthority.VENDOR
    assert first.form == "10-K"
    assert first.fiscal_year == 2024
    assert len(first.section_hash) == 64
    assert first.provider == "fmp"
    assert first.raw_text.strip() != ""


def test_fmp_estimates_parsing() -> None:
    wix_est_file = FMP_DIR / "WIX_analyst_estimates_quarterly.json"
    if not wix_est_file.exists():
        pytest.skip(f"{wix_est_file} not found on disk")

    adapter = FmpProviderAdapter()
    raw = wix_est_file.read_text(encoding="utf-8")
    issuer_currency = issuer_reported_currency_binding(
        (FMP_DIR / "WIX_income_statement_annual.json").read_bytes(), "WIX"
    )
    estimates = adapter.parse_estimates(
        raw, "WIX", observed_at=OBSERVED_AT, currency_binding=issuer_currency
    )

    assert len(estimates) > 0
    rev_estimates = [e for e in estimates if e.metric == "revenue"]
    assert len(rev_estimates) > 0
    first_rev = rev_estimates[0]
    assert first_rev.ticker == "WIX"
    assert first_rev.provider == "fmp"
    assert isinstance(first_rev.estimated_avg, Decimal)
    assert first_rev.currency == "USD"
    assert len(first_rev.source_payload_hash) == 64


def test_fmp_segments_parsing() -> None:
    abnb_seg_file = FMP_DIR / "ABNB_geo_segments_annual.json"
    if not abnb_seg_file.exists():
        pytest.skip(f"{abnb_seg_file} not found on disk")

    adapter = FmpProviderAdapter()
    raw = abnb_seg_file.read_text(encoding="utf-8")
    segments = adapter.parse_segments(raw, "ABNB", dim_type=SegmentDimension.GEOGRAPHY)

    assert len(segments) > 0
    first = segments[0]
    assert first.ticker == "ABNB"
    assert first.provider == "fmp"
    assert first.dim_type == "geography"
    assert isinstance(first.value, Decimal)
    assert first.unit == "actual"
    assert len(first.source_payload_hash) == 64


def test_fmp_prices_parsing() -> None:
    abnb_price_file = FMP_DIR / "ABNB_price_chart_10y_div_adj.json"
    if not abnb_price_file.exists():
        pytest.skip(f"{abnb_price_file} not found on disk")

    adapter = FmpProviderAdapter()
    raw = abnb_price_file.read_text(encoding="utf-8")
    quote_currency = quote_currency_binding((FMP_DIR / "ABNB_profile.json").read_bytes(), "ABNB")
    series = adapter.parse_prices(
        raw,
        "ABNB",
        adjustment_method=CorporateActionAdjustment.SPLIT_AND_DIVIDEND,
        currency_binding=quote_currency,
    )

    assert series.ticker == "ABNB"
    assert series.provider == "fmp"
    assert series.adjustment_method == CorporateActionAdjustment.SPLIT_AND_DIVIDEND
    assert len(series.points) > 0
    # Chronological sort check
    for i in range(len(series.points) - 1):
        assert series.points[i].as_of_date <= series.points[i + 1].as_of_date
    first_pt = series.points[0]
    assert isinstance(first_pt.close, Decimal)
    assert first_pt.volume >= 0


def test_synthetic_secondary_provider_and_parity() -> None:
    secondary_adapter = SyntheticSecondaryProviderAdapter()

    # Synthetic filing sections
    sec_payload = json.dumps(
        {
            "sections": [
                {
                    "name": "Item 1. Business",
                    "text": "Core cloud platform operations",
                    "accession": "0001234567-25-000001",
                    "period": "FY",
                }
            ]
        }
    )
    sections = secondary_adapter.parse_filing_sections(
        sec_payload, "TEST", form="10-K", fiscal_year=2025
    )
    assert len(sections) == 1
    assert sections[0].ticker == "TEST"
    assert sections[0].authority == FilingAuthority.VENDOR
    assert sections[0].section_name == "Item 1. Business"

    # Synthetic estimates
    est_payload = json.dumps(
        {
            "consensus": [
                {
                    "as_of": "2026-03-31T00:00:00",
                    "year": 2026,
                    "period": "Q1",
                    "metric": "revenue",
                    "mean": 125000000.0,
                    "min": 120000000.0,
                    "max": 130000000.0,
                    "count": 14,
                    "currency": "USD",
                }
            ]
        }
    )
    estimates = secondary_adapter.parse_estimates(est_payload, "TEST", observed_at=OBSERVED_AT)
    assert len(estimates) == 1
    assert estimates[0].estimated_avg == Decimal("125000000.0")
    assert estimates[0].analyst_count == 14

    # Synthetic price
    price_payload = json.dumps(
        {
            "currency": "USD",
            "bars": [
                {
                    "timestamp": "2026-06-01T00:00:00",
                    "open": 100.0,
                    "high": 105.0,
                    "low": 99.5,
                    "close": 104.2,
                    "volume": 500000,
                    "split_factor": 1.0,
                }
            ],
        }
    )
    prices = secondary_adapter.parse_prices(price_payload, "TEST")
    assert len(prices.points) == 1
    assert prices.points[0].close == Decimal("104.2")


def test_error_envelope_redaction() -> None:
    # Error message containing API key or credential
    err = ValueError("Failed request to https://api.site.com/data?apikey=SECRET_API_KEY_12345")
    raw_body = "Error response from https://api.site.com?token=BEARER_TOKEN_ABCXYZ"
    env = format_error_envelope(err, raw_body)

    assert env["status"] == "error"
    assert "SECRET_API_KEY_12345" not in env["message"]
    assert "BEARER_TOKEN_ABCXYZ" not in env["sanitized_payload_snippet"]


def test_contracts_reject_unsealed_or_unknown_provenance() -> None:
    now = datetime.now(UTC)
    common: dict[str, object] = {
        "ticker": "WIX",
        "authority": FilingAuthority.VENDOR,
        "form": "20-F",
        "section_name": "Business",
        "raw_text": "text",
        "section_hash": "a" * 64,
        "source_payload_hash": "b" * 64,
        "fetched_at": now,
        "provider": "fmp",
    }
    with pytest.raises(ValidationError):
        FilingSectionPayload.model_validate({**common, "form": "not-a-filing"})
    with pytest.raises(ValidationError):
        FilingSectionPayload.model_validate({**common, "section_hash": "g" * 64})
    with pytest.raises(ValidationError, match="timezone-aware"):
        FilingSectionPayload.model_validate({**common, "fetched_at": datetime(2026, 3, 31)})


def test_currency_bindings_are_hash_sealed_and_purpose_limited() -> None:
    issuer = issuer_reported_currency_binding(
        '[{"symbol":"MELI","reportedCurrency":"USD"}]', "MELI"
    )
    quote = quote_currency_binding('[{"symbol":"MELI","currency":"USD"}]', "MELI")
    assert issuer.basis is CurrencyBindingBasis.ISSUER_REPORTED
    assert quote.basis is CurrencyBindingBasis.QUOTE
    assert len(issuer.source_payload_hash) == 64
    with pytest.raises(ValidationError):
        CurrencyBinding.model_validate(
            {
                "ticker": "MELI",
                "currency": "USD",
                "basis": CurrencyBindingBasis.ISSUER_REPORTED,
                "source_payload_hash": "not-a-hash",
            }
        )


def test_adapters_fail_closed_and_preserve_byte_and_timezone_provenance() -> None:
    adapter = FmpProviderAdapter()
    raw = b'{"symbol":"WIX","period":"FY","business":"text"}'
    section = adapter.parse_filing_sections(raw, "WIX", form="20-F")[0]
    assert section.source_payload_hash != section.section_hash
    assert section.source_payload_hash == __import__("hashlib").sha256(raw).hexdigest()

    estimate = json.dumps(
        [
            {
                "symbol": "WIX",
                "date": "2026-03-31T01:00:00+02:00",
                "quarter": 1,
                "reportedCurrency": "EUR",
                "revenueAvg": "5",
            }
        ]
    )
    issuer_currency = issuer_reported_currency_binding(
        '[{"symbol":"WIX","reportedCurrency":"EUR"}]', "WIX"
    )
    parsed_estimate = adapter.parse_estimates(
        estimate, "WIX", observed_at=OBSERVED_AT, currency_binding=issuer_currency
    )[0]
    assert parsed_estimate.observation_date == OBSERVED_AT
    assert parsed_estimate.target_period_end == datetime(2026, 3, 30, 23, tzinfo=UTC)

    with pytest.raises(ValueError, match="ticker"):
        adapter.parse_estimates(
            '[{"symbol":"NOPE","date":"2026-03-31","reportedCurrency":"USD","revenueAvg":1}]',
            "WIX",
            observed_at=OBSERVED_AT,
            currency_binding=issuer_currency,
        )
    with pytest.raises(ValueError, match="issuer_reported"):
        adapter.parse_estimates(
            '[{"symbol":"WIX","date":"2026-03-31","revenueAvg":1}]',
            "WIX",
            observed_at=OBSERVED_AT,
            currency_binding=quote_currency_binding('[{"symbol":"WIX","currency":"EUR"}]', "WIX"),
        )
    with pytest.raises(ValueError, match="error response"):
        adapter.parse_filing_sections('{"symbol":"WIX","error":"denied"}', "WIX")
    with pytest.raises(ValueError, match="ticker"):
        adapter.parse_prices(
            '{"symbol":"NOPE","historical":[{"date":"2026-03-31","open":1,"high":2,"low":1,"close":2,"volume":3}]}',
            "WIX",
            currency_binding=quote_currency_binding('[{"symbol":"WIX","currency":"USD"}]', "WIX"),
        )


def test_fixture_provider_never_forges_sec_authority_or_values() -> None:
    adapter = SyntheticSecondaryProviderAdapter()
    sections = adapter.parse_filing_sections(
        '{"sections":[{"name":"Business","text":"text","period":"FY"}]}',
        "WIX",
        form="20-F",
    )
    assert sections[0].authority is FilingAuthority.VENDOR

    with pytest.raises(ValueError, match="mean"):
        adapter.parse_estimates(
            '{"consensus":[{"as_of":"2026-03-31","year":2026,"period":"Q1","metric":"revenue","currency":"USD"}]}',
            "WIX",
            observed_at=OBSERVED_AT,
        )
    with pytest.raises(ValueError, match="currency"):
        adapter.parse_prices(
            '{"bars":[{"timestamp":"2026-03-31","open":1,"high":2,"low":1,"close":2,"volume":3}]}',
            "WIX",
        )
