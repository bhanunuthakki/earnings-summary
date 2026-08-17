"""Hermetic tests for company filing regime taxonomy and helper methods."""

from __future__ import annotations

from models.companies import Company, FilingRegime, InstrumentType, ListType


def test_filing_regime_properties() -> None:
    # 10-K regime (US Domestic)
    reg_10k = FilingRegime.FORM_10K
    assert not reg_10k.is_fpi
    assert reg_10k.interim_doc_type == "sec_10q"
    assert reg_10k.annual_doc_type == "sec_10k"

    # 20-F regime (FPI)
    reg_20f = FilingRegime.FORM_20F
    assert reg_20f.is_fpi
    assert reg_20f.interim_doc_type == "sec_6k"
    assert reg_20f.annual_doc_type == "sec_20f"

    # 40-F regime (Canadian MJDS)
    reg_40f = FilingRegime.FORM_40F
    assert reg_40f.is_fpi
    assert reg_40f.interim_doc_type == "sec_6k"
    assert reg_40f.annual_doc_type == "sec_40f"


def test_company_model_filing_regime_helpers() -> None:
    wix = Company(
        id=1,
        user_id="bhanu",
        ticker="WIX",
        name="Wix.com Ltd.",
        list_type=ListType.PORTFOLIO,
        instrument_type=InstrumentType.ADR,
        filing_regime=FilingRegime.FORM_20F,
    )
    assert wix.is_fpi is True
    assert wix.interim_doc_type == "sec_6k"
    assert wix.annual_doc_type == "sec_20f"

    aapl = Company(
        id=2,
        user_id="bhanu",
        ticker="AAPL",
        name="Apple Inc.",
        list_type=ListType.PORTFOLIO,
        instrument_type=InstrumentType.EQUITY,
        filing_regime=FilingRegime.FORM_10K,
    )
    assert aapl.is_fpi is False
    assert aapl.interim_doc_type == "sec_10q"
    assert aapl.annual_doc_type == "sec_10k"
