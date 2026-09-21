import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from sources.adapters import AdjustedPriceSeries
from sources.readers import (
    DualReadParityReceipt,
    DualReadShadowingVerifier,
    ParityStatus,
    ProviderNeutralDataReader,
    ReaderUnavailableStatus,
)


def test_reader_receipt_frozen_immutability() -> None:
    """Assert DualReadParityReceipt and ReaderUnavailableStatus reject mutations and extra fields."""
    receipt = DualReadParityReceipt(
        run_id="run_test",
        ticker="WIX",
        consumer="price_history",
        legacy_record_count=100,
        adapter_record_count=100,
        legacy_unique_count=100,
        adapter_unique_count=100,
        status=ParityStatus.VERIFIED_MATCH,
        parity_passed=True,
        discrepancy_details=(),
        verified_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        setattr(receipt, "parity_passed", False)

    with pytest.raises(ValidationError):
        DualReadParityReceipt.model_validate({**receipt.model_dump(), "extra_field": "invalid"})


def test_reader_unavailable_on_empty_repo(tmp_path: Path) -> None:
    """Assert missing files gracefully return typed ReaderUnavailableStatus instead of crashing."""
    empty_repo = tmp_path / "empty_repo"
    empty_repo.mkdir()
    reader = ProviderNeutralDataReader(repo_root=empty_repo)

    sec_res = reader.get_filing_sections("XYZ")
    assert isinstance(sec_res, ReaderUnavailableStatus)
    assert sec_res.data_type == "filing_sections"
    assert "No filing cache found" in sec_res.reason

    est_res = reader.get_analyst_estimates("XYZ")
    assert isinstance(est_res, ReaderUnavailableStatus)
    assert est_res.data_type == "analyst_estimates"

    seg_res = reader.get_segment_structure("XYZ")
    assert isinstance(seg_res, ReaderUnavailableStatus)

    price_res = reader.get_adjusted_price_series("XYZ")
    assert isinstance(price_res, ReaderUnavailableStatus)

    latest_res = reader.get_latest_price("XYZ")
    assert isinstance(latest_res, ReaderUnavailableStatus)


def test_reader_and_dual_read_parity_on_mock_corpus(tmp_path: Path) -> None:
    """Assert reader parses synthetic corpus and verifier records exact field-level parity."""
    repo = tmp_path / "repo"
    fmp_dir = repo / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True)

    # 1. Mock prices
    price_payload = {
        "symbol": "TEST",
        "historical": [
            {
                "date": "2026-03-31",
                "open": 150.0,
                "high": 155.0,
                "low": 149.0,
                "close": 154.0,
                "adjClose": 154.0,
                "volume": 1000000,
            },
            {
                "date": "2026-04-01",
                "open": 154.0,
                "high": 158.0,
                "low": 153.0,
                "close": 157.0,
                "adjClose": 157.0,
                "volume": 1200000,
            },
        ],
    }
    (fmp_dir / "TEST_price_chart_10y_div_adj.json").write_text(
        json.dumps(price_payload), encoding="utf-8"
    )
    (fmp_dir / "TEST_profile.json").write_text(
        json.dumps([{"symbol": "TEST", "currency": "USD"}]), encoding="utf-8"
    )

    # 2. Mock estimates (1 entry with 2 metrics = 2 observation points)
    estimates_payload = [
        {
            "symbol": "TEST",
            "date": "2026-03-31",
            "fiscalYear": 2026,
            "quarter": 1,
            "revenueAvg": 500000000,
            "epsAvg": 1.25,
        }
    ]
    (fmp_dir / "TEST_analyst_estimates.json").write_text(
        json.dumps(estimates_payload), encoding="utf-8"
    )

    (fmp_dir / "TEST_income_statement.json").write_text(
        json.dumps([{"symbol": "TEST", "reportedCurrency": "USD", "date": "2026-03-31"}]),
        encoding="utf-8",
    )

    # 3. Mock geographic segments
    segments_payload = [
        {
            "date": "2026-03-31",
            "symbol": "TEST",
            "reportedCurrency": "USD",
            "data": {
                "North America": 300000000,
                "Europe": 200000000,
            },
        }
    ]
    (fmp_dir / "TEST_revenue_geographic_segmentation.json").write_text(
        json.dumps(segments_payload), encoding="utf-8"
    )

    # 4. Mock 10-K sections
    filing_payload = {
        "symbol": "TEST",
        "year": 2025,
        "period": "FY",
        "Item 1": "Business Description text",
        "Item 7": "Management Discussion and Analysis text",
    }
    (fmp_dir / "TEST_form_10k_2025.json").write_text(json.dumps(filing_payload), encoding="utf-8")

    reader = ProviderNeutralDataReader(repo_root=repo)
    prices = reader.get_adjusted_price_series("TEST")
    assert isinstance(prices, AdjustedPriceSeries)
    assert len(prices.points) == 2
    assert prices.points[0].close == Decimal("154.0")

    latest = reader.get_latest_price("TEST")
    assert isinstance(latest, tuple)
    assert latest[0] == Decimal("157.0")

    # Test metric filter branch in reader
    filtered_rev = reader.get_analyst_estimates("TEST", metric="revenue")
    assert isinstance(filtered_rev, list)
    assert len(filtered_rev) == 1
    assert filtered_rev[0].metric == "revenue"

    # Test Dual-Read Shadowing Verifier
    verifier = DualReadShadowingVerifier(repo_root=repo)

    price_receipt = verifier.verify_price_parity("TEST")
    assert price_receipt.status == ParityStatus.VERIFIED_MATCH
    assert price_receipt.parity_passed is True
    assert price_receipt.legacy_record_count == 2
    assert price_receipt.adapter_record_count == 2
    assert price_receipt.legacy_unique_count == 2
    assert price_receipt.adapter_unique_count == 2
    assert len(price_receipt.discrepancy_details) == 0

    est_receipt = verifier.verify_estimates_parity("TEST")
    assert est_receipt.status == ParityStatus.VERIFIED_MATCH
    assert est_receipt.parity_passed is True
    assert est_receipt.legacy_record_count == 1
    assert est_receipt.adapter_record_count == 2
    assert est_receipt.legacy_unique_count == 2
    assert est_receipt.adapter_unique_count == 2
    assert len(est_receipt.discrepancy_details) == 0

    seg_receipt = verifier.verify_segments_parity("TEST", dim_type="geography")
    assert seg_receipt.status == ParityStatus.VERIFIED_MATCH
    assert seg_receipt.parity_passed is True
    assert seg_receipt.legacy_record_count == 1
    assert seg_receipt.adapter_record_count == 2
    assert seg_receipt.legacy_unique_count == 2
    assert seg_receipt.adapter_unique_count == 2
    assert len(seg_receipt.discrepancy_details) == 0

    filing_receipt = verifier.verify_filing_sections_parity("TEST", form="10-K")
    assert filing_receipt.status == ParityStatus.VERIFIED_MATCH
    assert filing_receipt.parity_passed is True
    assert filing_receipt.legacy_record_count == 2
    assert filing_receipt.adapter_record_count == 2
    assert len(filing_receipt.discrepancy_details) == 0


def test_dual_read_field_divergence_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assert verifier fails closed and flags detailed errors when values, dates, or availability diverge."""
    repo = tmp_path / "repo"
    fmp_dir = repo / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True)

    # 1. Price divergence (corrupted JSON with wrong date/close)
    price_payload = {
        "symbol": "DIVERGE",
        "historical": [
            {
                "date": "2026-03-31",
                "open": 100.0,
                "high": 105.0,
                "low": 99.0,
                "close": 104.0,
                "adjClose": 104.0,
                "volume": 500,
            },
            {
                "date": "2026-04-01",
                "open": 104.0,
                "high": 108.0,
                "low": 103.0,
                "close": 107.0,
                "adjClose": 107.0,
                "volume": 600,
            },
        ],
    }
    (fmp_dir / "DIVERGE_price_chart_10y_div_adj.json").write_text(
        json.dumps(price_payload), encoding="utf-8"
    )
    (fmp_dir / "DIVERGE_profile.json").write_text(
        json.dumps([{"symbol": "DIVERGE", "currency": "USD"}]), encoding="utf-8"
    )

    verifier = DualReadShadowingVerifier(repo_root=repo)
    # Patch reader to simulate adapter discrepancy
    orig_series = verifier.reader.get_adjusted_price_series("DIVERGE")
    assert isinstance(orig_series, AdjustedPriceSeries)

    def empty_prices(t: str) -> AdjustedPriceSeries:
        return AdjustedPriceSeries(
            ticker=t,
            provider="fmp",
            adjustment_method=orig_series.adjustment_method,
            currency=orig_series.currency,
            currency_binding=orig_series.currency_binding,
            points=(),  # empty points = divergence
            source_payload_hash="0" * 64,
        )

    monkeypatch.setattr(verifier.reader, "get_adjusted_price_series", empty_prices)
    receipt = verifier.verify_price_parity("DIVERGE")
    assert receipt.parity_passed is False
    assert receipt.status == ParityStatus.VERIFIED_DIVERGENCE
    assert len(receipt.discrepancy_details) > 0
    assert "Price count mismatch" in receipt.discrepancy_details[0]

    # 2. Legacy present, adapter unavailable divergence path
    def unavailable_prices(t: str) -> ReaderUnavailableStatus:
        return ReaderUnavailableStatus(
            ticker=t,
            provider="fmp",
            data_type="adjusted_prices",
            reason="Forced adapter simulation failure",
            as_of=datetime.now(UTC),
        )

    monkeypatch.setattr(verifier.reader, "get_adjusted_price_series", unavailable_prices)
    unavail_receipt = verifier.verify_price_parity("DIVERGE")
    assert unavail_receipt.parity_passed is False
    assert unavail_receipt.status == ParityStatus.VERIFIED_DIVERGENCE
    assert any("Adapter read unavailable" in d for d in unavail_receipt.discrepancy_details)

    # 3. Estimates divergence
    est_payload = [{"symbol": "DIVERGE", "date": "2026-03-31", "revenueAvg": 1000000}]
    (fmp_dir / "DIVERGE_analyst_estimates.json").write_text(
        json.dumps(est_payload), encoding="utf-8"
    )

    def empty_results(*_args: object, **_kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr(verifier.reader, "get_analyst_estimates", empty_results)
    est_receipt = verifier.verify_estimates_parity("DIVERGE")
    assert est_receipt.parity_passed is False
    assert est_receipt.status == ParityStatus.VERIFIED_DIVERGENCE
    assert len(est_receipt.discrepancy_details) > 0

    # 4. Segments divergence
    seg_payload = [
        {"date": "2026-03-31", "symbol": "DIVERGE", "reportedCurrency": "USD", "data": {"US": 100}}
    ]
    (fmp_dir / "DIVERGE_revenue_geographic_segmentation.json").write_text(
        json.dumps(seg_payload), encoding="utf-8"
    )
    monkeypatch.setattr(verifier.reader, "get_segment_structure", empty_results)
    seg_receipt = verifier.verify_segments_parity("DIVERGE", dim_type="geography")
    assert seg_receipt.parity_passed is False
    assert seg_receipt.status == ParityStatus.VERIFIED_DIVERGENCE
    assert len(seg_receipt.discrepancy_details) > 0

    # 5. Filing sections divergence
    filing_payload = {"symbol": "DIVERGE", "year": 2025, "Item 1": "Text"}
    (fmp_dir / "DIVERGE_form_10k_2025.json").write_text(
        json.dumps(filing_payload), encoding="utf-8"
    )
    monkeypatch.setattr(verifier.reader, "get_filing_sections", empty_results)
    filing_receipt = verifier.verify_filing_sections_parity("DIVERGE", form="10-K")
    assert filing_receipt.parity_passed is False
    assert filing_receipt.status == ParityStatus.VERIFIED_DIVERGENCE
    assert len(filing_receipt.discrepancy_details) > 0


def test_missing_currency_packet_cannot_be_replaced_with_manufactured_usd(tmp_path: Path) -> None:
    cache = tmp_path / "data" / "historical" / "fmp"
    cache.mkdir(parents=True)
    (cache / "NVO_analyst_estimates.json").write_text(
        json.dumps([{"symbol": "NVO", "date": "2027-12-31", "fiscalYear": 2027, "revenueAvg": 100}])
    )
    (cache / "NVO_price_chart_10y_div_adj.json").write_text(
        json.dumps(
            {
                "symbol": "NVO",
                "historical": [{"date": "2026-03-31", "close": 50, "adjClose": 50, "volume": 1}],
            }
        )
    )
    reader = ProviderNeutralDataReader(tmp_path)
    estimates = reader.get_analyst_estimates("NVO")
    assert isinstance(estimates, ReaderUnavailableStatus)
    assert estimates.reason == "issuer_reporting_currency_evidence_unavailable"
    prices = reader.get_adjusted_price_series("NVO", currency="USD")
    assert isinstance(prices, ReaderUnavailableStatus)
    assert prices.reason == "quote_currency_evidence_unavailable"
    (cache / "NVO_profile.json").write_text(json.dumps([{"symbol": "NVO", "currency": "DKK"}]))
    assert isinstance(
        reader.get_adjusted_price_series("NVO", currency="USD"), ReaderUnavailableStatus
    )
    bound = reader.get_adjusted_price_series("NVO")
    assert isinstance(bound, AdjustedPriceSeries)
    assert bound.currency.value == "DKK"
    (cache / "NVO_income_statement.json").write_text(
        json.dumps([{"symbol": "NVO", "reportedCurrency": "DKK", "date": "2026-03-31"}])
    )
    bound_estimates = reader.get_analyst_estimates("NVO")
    assert isinstance(bound_estimates, list)
    assert all(item.currency.value == "DKK" for item in bound_estimates)
