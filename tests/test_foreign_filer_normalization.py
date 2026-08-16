"""Hermetic unit tests for foreign filer normalization and interim document classification."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from sources.foreign_filers import (
    FOREIGN_FILER_ROSTER,
    ForeignFactObservation,
    ForeignFilerNormalizer,
    ForeignFilerProfile,
    ForeignFilingForm,
    ForeignNormalizationReceipt,
    InterimDisposition,
    ReportingCadence,
    compute_sha256_bytes,
)


def test_compute_sha256_bytes_known_vector() -> None:
    """Assert compute_sha256_bytes matches the standard empty-string and test string SHA-256 vectors."""
    assert compute_sha256_bytes(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert compute_sha256_bytes(b"hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_foreign_filer_models_frozen_immutability() -> None:
    """Assert foreign filer models reject mutations, extra fields, non-hex regex, and hash mismatches."""
    profile = ForeignFilerProfile(
        ticker="NVO",
        country_of_origin="Denmark",
        primary_form=ForeignFilingForm.FORM_20F,
        cadence=ReportingCadence.QUARTERLY,
        reporting_currency="DKK",
        admitted_document_hashes=(),
    )
    with pytest.raises(ValidationError):
        profile.reporting_currency = "USD"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        ForeignFilerProfile(
            ticker="NVO",
            country_of_origin="Denmark",
            primary_form=ForeignFilingForm.FORM_20F,
            cadence=ReportingCadence.QUARTERLY,
            reporting_currency="DKK",
            extra_field="invalid",  # type: ignore[call-arg]
        )

    receipt = ForeignNormalizationReceipt(
        ticker="NVO",
        form=ForeignFilingForm.FORM_20F,
        document_hash="0" * 64,
        disposition=InterimDisposition.ADMITTED_XBRL,
        facts_extracted_count=0,
        facts=(),
        reason="OK",
        verified_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        receipt.facts_extracted_count = 5  # type: ignore[misc]

    # Reject non-hex 64-character hash pattern
    with pytest.raises(ValidationError):
        ForeignNormalizationReceipt(
            ticker="NVO",
            form=ForeignFilingForm.FORM_20F,
            document_hash="Z" * 64,  # Non-hex character
            disposition=InterimDisposition.ADMITTED_XBRL,
            facts_extracted_count=0,
            facts=(),
            reason="OK",
            verified_at=datetime.now(UTC),
        )

    # Model validator rejects fact with mismatched source_hash
    fact_with_diff_hash = ForeignFactObservation(
        ticker="NVO",
        form=ForeignFilingForm.FORM_20F,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        fiscal_year=2025,
        fiscal_period="FY",
        concept="Revenues",
        value=Decimal("100"),
        currency="DKK",
        source_hash="1" * 64,
        extracted_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError, match="does not match document_hash"):
        ForeignNormalizationReceipt(
            ticker="NVO",
            form=ForeignFilingForm.FORM_20F,
            document_hash="0" * 64,
            disposition=InterimDisposition.ADMITTED_XBRL,
            facts_extracted_count=1,
            facts=(fact_with_diff_hash,),
            reason="Mismatch",
            verified_at=datetime.now(UTC),
        )


def test_foreign_20f_40f_and_asml_normalization() -> None:
    """Assert NVO (DKK), BN (USD), and ASML (EUR) parse with exact native currency, canonical taxonomy, and hash coupling."""
    normalizer = ForeignFilerNormalizer()

    # 1. NVO 20-F in DKK with whitespace normalization
    nvo_payload = b'{"facts": {"Revenues": 250000000000, "OperatingProfit": 100000000000}}'
    nvo_receipt = normalizer.normalize_document(
        "  nvo  ",
        nvo_payload,
        form=ForeignFilingForm.FORM_20F,
        accession_number="0001193125-26-100001",
        fiscal_year=2025,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        requested_period="FY",
        is_inline_xbrl=True,
    )
    assert nvo_receipt.disposition == InterimDisposition.ADMITTED_XBRL
    assert nvo_receipt.facts_extracted_count == 2
    assert len(nvo_receipt.facts) == 2
    assert nvo_receipt.facts[0].currency == "DKK"
    assert nvo_receipt.facts[0].canonical_concept == "revenue"
    assert nvo_receipt.facts[0].is_canonical_mapped is True
    assert nvo_receipt.facts[1].canonical_concept == "operating_income"
    assert nvo_receipt.facts[1].is_canonical_mapped is True
    assert nvo_receipt.facts[0].source_hash == nvo_receipt.document_hash
    assert nvo_receipt.facts[1].source_hash == nvo_receipt.document_hash

    # 2. BN 40-F in USD
    bn_payload = b'{"facts": {"TotalRevenue": 95000000000, "NetIncome": 5000000000}}'
    bn_receipt = normalizer.normalize_document(
        "BN",
        bn_payload,
        form=ForeignFilingForm.FORM_40F,
        accession_number="0001193125-26-200002",
        fiscal_year=2025,
        period_end=date(2025, 12, 31),
        requested_period="FY",
        is_inline_xbrl=True,
    )
    assert bn_receipt.disposition == InterimDisposition.ADMITTED_XBRL
    assert bn_receipt.facts_extracted_count == 2
    assert bn_receipt.facts[0].currency == "USD"
    assert bn_receipt.facts[0].canonical_concept == "revenue"
    assert bn_receipt.facts[1].canonical_concept == "net_income"

    # 3. ASML 20-F/A in EUR
    asml_payload = b'{"facts": {"Sales": 27500000000, "GrossProfit": 14000000000}}'
    asml_receipt = normalizer.normalize_document(
        "ASML",
        asml_payload,
        form=ForeignFilingForm.FORM_20FA,
        accession_number="0001193125-26-250005",
        fiscal_year=2025,
        period_end=date(2025, 12, 31),
        requested_period="FY",
        is_inline_xbrl=True,
    )
    assert asml_receipt.disposition == InterimDisposition.ADMITTED_XBRL
    assert asml_receipt.facts_extracted_count == 2
    assert asml_receipt.facts[0].currency == "EUR"
    assert asml_receipt.facts[0].canonical_concept == "revenue"
    assert asml_receipt.facts[1].canonical_concept == "gross_profit"


def test_unmapped_concept_emits_unmapped_flag() -> None:
    """Assert unmapped foreign concepts set canonical_concept=None and is_canonical_mapped=False."""
    normalizer = ForeignFilerNormalizer()
    payload = b'{"facts": {"CustomForeignMetricXYZ": "1234567.890123"}}'
    receipt = normalizer.normalize_document(
        "NVO",
        payload,
        form=ForeignFilingForm.FORM_20F,
        fiscal_year=2025,
        period_end=date(2025, 12, 31),
        is_inline_xbrl=True,
    )
    assert receipt.disposition == InterimDisposition.ADMITTED_XBRL
    assert receipt.facts_extracted_count == 1
    fact = receipt.facts[0]
    assert fact.concept == "CustomForeignMetricXYZ"
    assert fact.canonical_concept is None
    assert fact.is_canonical_mapped is False
    assert fact.value == Decimal("1234567.890123")


def test_malformed_inline_xbrl_fails_closed_to_degraded() -> None:
    """Assert malformed or empty facts payloads fail closed to DEGRADED_UNSUPPORTED_FORMAT instead of empty ADMITTED_XBRL."""
    normalizer = ForeignFilerNormalizer()

    # Corrupt JSON bytes
    corrupt_payload = b"<html>NOT JSON {corrupted"
    receipt = normalizer.normalize_document(
        "NVO",
        corrupt_payload,
        form=ForeignFilingForm.FORM_20F,
        fiscal_year=2025,
        period_end=date(2025, 12, 31),
        is_inline_xbrl=True,
    )
    assert receipt.disposition == InterimDisposition.DEGRADED_UNSUPPORTED_FORMAT
    assert receipt.facts_extracted_count == 0
    assert "Failed parsing structured foreign facts" in receipt.reason

    # Empty facts dictionary
    empty_payload = b'{"facts": {}}'
    receipt_empty = normalizer.normalize_document(
        "NVO",
        empty_payload,
        form=ForeignFilingForm.FORM_20F,
        fiscal_year=2025,
        period_end=date(2025, 12, 31),
        is_inline_xbrl=True,
    )
    assert receipt_empty.disposition == InterimDisposition.DEGRADED_UNSUPPORTED_FORMAT
    assert receipt_empty.facts_extracted_count == 0


def test_unknown_ticker_rejection() -> None:
    """Assert unknown foreign filer ticker fails closed and never silently assumes USD."""
    normalizer = ForeignFilerNormalizer()
    receipt = normalizer.normalize_document(
        "UNKNOWN_CORP",
        b'{"facts": {"Revenues": 100}}',
        form=ForeignFilingForm.FORM_20F,
        fiscal_year=2025,
        period_end=date(2025, 12, 31),
    )
    assert receipt.disposition == InterimDisposition.DEGRADED_UNSUPPORTED_FORMAT
    assert receipt.facts_extracted_count == 0
    assert "Unknown foreign filer ticker" in receipt.reason


def test_non_inline_forms_rejection() -> None:
    """Assert non-inline 6-K and 20-F are rejected to prevent zero-fact fake XBRL ingest."""
    normalizer = ForeignFilerNormalizer()

    # 1. WIX non-inline 6-K HTML
    wix_html = b"<html><body>WIX Q1 2026 Earnings Release (Non-inline HTML)</body></html>"
    wix_receipt = normalizer.normalize_document(
        "WIX",
        wix_html,
        form=ForeignFilingForm.FORM_6K,
        accession_number="0001193125-26-300003",
        fiscal_year=2026,
        period_end=date(2026, 3, 31),
        requested_period="Q1",
        is_inline_xbrl=False,
    )
    assert wix_receipt.disposition == InterimDisposition.REJECTED_NON_INLINE_HTML
    assert wix_receipt.facts_extracted_count == 0
    assert "rejected zero-fact fake XBRL" in wix_receipt.reason

    # 2. NVO non-inline 20-F HTML
    nvo_html = b"<html><body>NVO 20-F (Non-inline HTML)</body></html>"
    nvo_receipt = normalizer.normalize_document(
        "NVO",
        nvo_html,
        form=ForeignFilingForm.FORM_20F,
        fiscal_year=2025,
        period_end=date(2025, 12, 31),
        requested_period="FY",
        is_inline_xbrl=False,
    )
    assert nvo_receipt.disposition == InterimDisposition.REJECTED_NON_INLINE_HTML
    assert nvo_receipt.facts_extracted_count == 0


def test_semiannual_filer_dispositions() -> None:
    """Assert semiannual reporters (BHP) degrade on quarterly slices but admit valid H1 semiannual slices."""
    normalizer = ForeignFilerNormalizer()

    # 1. Quarterly slice -> NOT_APPLICABLE_SEMIANNUAL
    bhp_payload = b"<html>BHP Semiannual Release</html>"
    bhp_q_receipt = normalizer.normalize_document(
        "BHP",
        bhp_payload,
        form=ForeignFilingForm.FORM_6K,
        accession_number="0001193125-26-400004",
        fiscal_year=2025,
        period_end=date(2025, 9, 30),
        requested_period="Q1",
        is_inline_xbrl=False,
    )
    assert bhp_q_receipt.disposition == InterimDisposition.NOT_APPLICABLE_SEMIANNUAL
    assert bhp_q_receipt.facts_extracted_count == 0

    # 2. Semiannual H1 slice -> ADMITTED_XBRL
    bhp_h1_payload = b'{"facts": {"Revenue": 28000000000, "Profit": 7000000000}}'
    bhp_h1_receipt = normalizer.normalize_document(
        "BHP",
        bhp_h1_payload,
        form=ForeignFilingForm.FORM_6K,
        accession_number="0001193125-26-400005",
        fiscal_year=2025,
        period_end=date(2025, 12, 31),
        requested_period="H1",
        is_inline_xbrl=True,
    )
    assert bhp_h1_receipt.disposition == InterimDisposition.ADMITTED_XBRL
    assert bhp_h1_receipt.facts_extracted_count == 2
    assert bhp_h1_receipt.facts[0].fiscal_period == "H1"
    assert bhp_h1_receipt.facts[0].period_start == date(2025, 1, 1)


def test_admitted_governed_spreadsheet_hash_verification() -> None:
    """Assert only exact admitted spreadsheet hashes are accepted for NU and unconfigured profiles reject spreadsheets."""
    normalizer = ForeignFilerNormalizer()

    nu_payload = b'{"facts": {"TotalRevenue": 3000000000}}'
    actual_hash = compute_sha256_bytes(nu_payload)

    # 1. Unadmitted hash -> DEGRADED_UNSUPPORTED_FORMAT
    bad_receipt = normalizer.normalize_document(
        "NU",
        nu_payload,
        form=ForeignFilingForm.ISSUER_IR_SPREADSHEET,
        fiscal_year=2026,
        period_end=date(2026, 3, 31),
    )
    assert bad_receipt.disposition == InterimDisposition.DEGRADED_UNSUPPORTED_FORMAT
    assert bad_receipt.facts_extracted_count == 0

    # 2. Admitted hash -> ADMITTED_GOVERNED_SPREADSHEET
    mock_roster = dict(FOREIGN_FILER_ROSTER)
    mock_roster["NU"] = ForeignFilerProfile(
        ticker="NU",
        country_of_origin="Brazil",
        primary_form=ForeignFilingForm.FORM_20F,
        cadence=ReportingCadence.QUARTERLY,
        reporting_currency="USD",
        admitted_document_hashes=(actual_hash,),
    )
    custom_normalizer = ForeignFilerNormalizer(roster=mock_roster)
    good_receipt = custom_normalizer.normalize_document(
        "NU",
        nu_payload,
        form=ForeignFilingForm.ISSUER_IR_SPREADSHEET,
        fiscal_year=2026,
        period_end=date(2026, 3, 31),
    )
    assert good_receipt.disposition == InterimDisposition.ADMITTED_GOVERNED_SPREADSHEET
    assert good_receipt.facts_extracted_count == 1

    # 3. Profile with empty admitted hashes rejects spreadsheet entirely
    nvo_spreadsheet = normalizer.normalize_document(
        "NVO",
        nu_payload,
        form=ForeignFilingForm.ISSUER_IR_SPREADSHEET,
        fiscal_year=2025,
        period_end=date(2025, 12, 31),
    )
    assert nvo_spreadsheet.disposition == InterimDisposition.DEGRADED_UNSUPPORTED_FORMAT
    assert nvo_spreadsheet.facts_extracted_count == 0
