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
    assert (
        compute_sha256_bytes(b"")
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"  # pragma: allowlist secret
    )
    assert (
        compute_sha256_bytes(b"hello")
        == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"  # pragma: allowlist secret
    )


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
        setattr(profile, "reporting_currency", "USD")

    with pytest.raises(ValidationError):
        ForeignFilerProfile.model_validate({**profile.model_dump(), "extra_field": "invalid"})

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
        setattr(receipt, "facts_extracted_count", 5)

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


@pytest.mark.parametrize("form", list(ForeignFilingForm))
@pytest.mark.parametrize("payload", [b'{"facts":{"Revenues":100}}', b"<html>not XBRL</html>"])
def test_unbound_raw_payload_cannot_claim_admission(
    form: ForeignFilingForm, payload: bytes
) -> None:
    result = ForeignFilerNormalizer().normalize_document(
        "NVO",
        payload,
        form=form,
        fiscal_year=2025,
        period_end=date(2025, 12, 31),
        is_inline_xbrl=True,
    )
    assert result.disposition == InterimDisposition.DEGRADED_UNSUPPORTED_FORMAT
    assert result.facts_extracted_count == 0
    assert result.facts == ()


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
    assert bhp_h1_receipt.disposition == InterimDisposition.DEGRADED_UNSUPPORTED_FORMAT
    assert bhp_h1_receipt.facts_extracted_count == 0


def test_allowlisted_hash_does_not_authorize_values() -> None:
    content = b'{"facts":{"Revenues":100}}'
    profile = FOREIGN_FILER_ROSTER["NU"].model_copy(
        update={"admitted_document_hashes": (compute_sha256_bytes(content),)}
    )
    receipt = ForeignFilerNormalizer({"NU": profile}).normalize_document(
        "NU",
        content,
        form=ForeignFilingForm.ISSUER_IR_SPREADSHEET,
        fiscal_year=2026,
        period_end=date(2026, 3, 31),
        requested_period="Q1",
    )
    assert receipt.disposition == InterimDisposition.DEGRADED_UNSUPPORTED_FORMAT
    assert not receipt.facts
