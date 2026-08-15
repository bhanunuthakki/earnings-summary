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
    FilingAuthority,
    FilingSectionPayload,
    FmpProviderAdapter,
    SyntheticSecondaryProviderAdapter,
    format_error_envelope,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FMP_DIR = REPO_ROOT / "data" / "historical" / "fmp"


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
        fetched_at=now,
        provider="sec",
    )

    # Immutability check
    with pytest.raises(ValidationError):
        payload.ticker = "NEW"  # type: ignore[misc]

    # Extra field forbidden check
    data = payload.model_dump()
    data["extra_forbidden_key"] = "bad"
    with pytest.raises(ValidationError):
        FilingSectionPayload.model_validate(data)


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
    estimates = adapter.parse_estimates(raw, "WIX")

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
    segments = adapter.parse_segments(raw, "ABNB", dim_type="geography")

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
    series = adapter.parse_prices(
        raw,
        "ABNB",
        adjustment_method=CorporateActionAdjustment.SPLIT_AND_DIVIDEND,
        currency="USD",
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
    assert sections[0].authority == FilingAuthority.SEC
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
    estimates = secondary_adapter.parse_estimates(est_payload, "TEST")
    assert len(estimates) == 1
    assert estimates[0].estimated_avg == Decimal("125000000.0")
    assert estimates[0].analyst_count == 14

    # Synthetic price
    price_payload = json.dumps(
        {
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
            ]
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
