"""Hermetic unit tests for foreign filer backfill validation against sealed oracle."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from sources.foreign_filers import (
    ForeignFilerNormalizer,
    ForeignFilingForm,
)
from sources.foreign_oracle_backfill import (
    ForeignBackfillReceipt,
    ForeignOracleBackfillValidator,
    ForeignOracleComparisonObservation,
    OracleComparisonClassification,
)


def test_oracle_comparison_models_frozen_immutability() -> None:
    """Assert comparison observation and backfill receipts reject mutation and extra fields."""
    obs = ForeignOracleComparisonObservation(
        ticker="NVO",
        concept="Revenues",
        canonical_concept="revenue",
        fiscal_year=2025,
        fiscal_period="FY",
        currency="DKK",
        sec_fact_value=Decimal("250000000000"),
        oracle_fmp_value=Decimal("250000000000"),
        classification=OracleComparisonClassification.EXACT_MATCH,
        divergence_ratio=Decimal("0.0"),
        source_hash="0" * 64,
        notes="Exact match",
    )
    with pytest.raises(ValidationError):
        obs.sec_fact_value = Decimal("200")  # type: ignore[misc]

    receipt = ForeignBackfillReceipt(
        run_id="test_run",
        ticker="NVO",
        reporting_cadence="quarterly",
        reporting_currency="DKK",
        comparisons_count=1,
        exact_matches_count=1,
        discrepancies_count=0,
        degraded_or_na_count=0,
        status="PASS",
        comparisons=(obs,),
        reason="OK",
        verified_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        receipt.status = "HOLD"  # type: ignore[misc]


def test_oracle_comparison_exact_matches() -> None:
    """Assert validator identifies exact fact matches against sealed oracle."""
    normalizer = ForeignFilerNormalizer()
    validator = ForeignOracleBackfillValidator()

    sec_receipt = normalizer.normalize_document(
        "NVO",
        b'{"facts": {"Revenues": 250000000000, "OperatingProfit": 100000000000}}',
        form=ForeignFilingForm.FORM_20F,
        fiscal_year=2025,
        period_end=date(2025, 12, 31),
        is_inline_xbrl=True,
    )
    oracle_facts = {
        "revenue": Decimal("250000000000"),
        "operating_income": Decimal("100000000000"),
    }

    backfill_receipt = validator.compare_facts("NVO", sec_receipt, oracle_facts)
    assert backfill_receipt.status == "PASS"
    assert backfill_receipt.exact_matches_count == 2
    assert backfill_receipt.discrepancies_count == 0
    assert len(backfill_receipt.comparisons) == 2
    assert (
        backfill_receipt.comparisons[0].classification == OracleComparisonClassification.EXACT_MATCH
    )
    assert (
        backfill_receipt.comparisons[1].classification == OracleComparisonClassification.EXACT_MATCH
    )


def test_oracle_missing_and_divergence_classifications() -> None:
    """Assert validator classifies missing facts and material disagreements accurately."""
    normalizer = ForeignFilerNormalizer()
    validator = ForeignOracleBackfillValidator()

    # SEC has 2 facts: Revenues (100) and OperatingProfit (50)
    sec_receipt = normalizer.normalize_document(
        "BN",
        b'{"facts": {"TotalRevenue": 100000000, "NetIncome": 50000000}}',
        form=ForeignFilingForm.FORM_40F,
        fiscal_year=2025,
        period_end=date(2025, 12, 31),
        is_inline_xbrl=True,
    )

    # 1. Missing fact in oracle: oracle only has revenue
    oracle_partial = {
        "revenue": Decimal("100000000"),
    }
    receipt_partial = validator.compare_facts("BN", sec_receipt, oracle_partial)
    assert receipt_partial.status == "HOLD"
    assert receipt_partial.exact_matches_count == 1
    assert receipt_partial.discrepancies_count == 1
    assert (
        receipt_partial.comparisons[1].classification
        == OracleComparisonClassification.MISSING_EXTRACTION
    )

    # 2. Material disagreement (>5% difference)
    oracle_material_diff = {
        "revenue": Decimal("100000000"),
        "net_income": Decimal("40000000"),  # 20% divergence from 50M
    }
    receipt_diff = validator.compare_facts("BN", sec_receipt, oracle_material_diff)
    assert (
        receipt_diff.comparisons[1].classification
        == OracleComparisonClassification.MATERIAL_DISAGREEMENT
    )
    assert receipt_diff.comparisons[1].divergence_ratio == Decimal("0.25")

    # 3. Provider normalization (<=5% difference)
    oracle_minor_diff = {
        "revenue": Decimal("100000000"),
        "net_income": Decimal("49000000"),  # ~2% divergence
    }
    receipt_minor = validator.compare_facts("BN", sec_receipt, oracle_minor_diff)
    assert (
        receipt_minor.comparisons[1].classification
        == OracleComparisonClassification.PROVIDER_NORMALIZATION
    )


def test_oracle_degraded_and_semiannual_dispositions() -> None:
    """Assert non-inline 6-K and semiannual N/A dispositions pass into typed comparison receipts."""
    normalizer = ForeignFilerNormalizer()
    validator = ForeignOracleBackfillValidator()

    # 1. Non-inline 6-K degradation
    wix_sec = normalizer.normalize_document(
        "WIX",
        b"<html>Press Release</html>",
        form=ForeignFilingForm.FORM_6K,
        fiscal_year=2026,
        period_end=date(2026, 3, 31),
        is_inline_xbrl=False,
    )
    wix_backfill = validator.compare_facts("WIX", wix_sec, {})
    assert wix_backfill.status == "PASS"
    assert wix_backfill.degraded_or_na_count == 1
    assert (
        wix_backfill.comparisons[0].classification
        == OracleComparisonClassification.DEGRADED_NON_INLINE
    )

    # 2. Semiannual quarterly slice N/A
    bhp_sec = normalizer.normalize_document(
        "BHP",
        b"<html>BHP Release</html>",
        form=ForeignFilingForm.FORM_6K,
        fiscal_year=2025,
        period_end=date(2025, 9, 30),
        requested_period="Q1",
        is_inline_xbrl=False,
    )
    bhp_backfill = validator.compare_facts("BHP", bhp_sec, {})
    assert bhp_backfill.status == "PASS"
    assert bhp_backfill.degraded_or_na_count == 1
    assert (
        bhp_backfill.comparisons[0].classification
        == OracleComparisonClassification.NOT_APPLICABLE_SEMIANNUAL
    )
