"""Foreign filer normalization and interim document classification.

Handles SEC foreign private issuer forms (20-F, 40-F, 6-K) and issuer-IR inputs.
Enforces explicit currency, unit, cadence (annual/semiannual/quarterly), and
fails closed / degrades on non-inline HTML instead of silently faking XBRL facts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

RequestedFiscalPeriod = Literal["FY", "H1", "H2", "Q1", "Q2", "Q3", "Q4"]


class ForeignFilingForm(StrEnum):
    FORM_20F = "20-F"
    FORM_20FA = "20-F/A"
    FORM_40F = "40-F"
    FORM_40FA = "40-F/A"
    FORM_6K = "6-K"
    FORM_6KA = "6-K/A"
    ISSUER_IR_SPREADSHEET = "IR_SPREADSHEET"
    ISSUER_STATEMENT_CACHE = "STATEMENT_CACHE"


class ReportingCadence(StrEnum):
    ANNUAL = "annual"
    SEMIANNUAL = "semiannual"
    QUARTERLY = "quarterly"


class InterimDisposition(StrEnum):
    ADMITTED_XBRL = "ADMITTED_XBRL"
    ADMITTED_GOVERNED_SPREADSHEET = "ADMITTED_GOVERNED_SPREADSHEET"
    ADMITTED_STATEMENT_CACHE = "ADMITTED_STATEMENT_CACHE"
    REJECTED_NON_INLINE_HTML = "REJECTED_NON_INLINE_HTML"
    DEGRADED_UNSUPPORTED_FORMAT = "DEGRADED_UNSUPPORTED_FORMAT"
    NOT_APPLICABLE_SEMIANNUAL = "NOT_APPLICABLE_SEMIANNUAL"


class ForeignFilerProfile(BaseModel):
    """Immutable profile defining foreign filer authority, reporting cadence, and currency."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    country_of_origin: str
    primary_form: ForeignFilingForm
    cadence: ReportingCadence
    reporting_currency: str
    is_foreign_private_issuer: bool = True
    admitted_document_hashes: tuple[str, ...] = ()


class ForeignFactObservation(BaseModel):
    """Immutable normalized fact extracted from a foreign filer document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    form: ForeignFilingForm
    accession_number: str | None = None
    period_start: date
    period_end: date
    fiscal_year: int
    fiscal_period: RequestedFiscalPeriod
    concept: str
    canonical_concept: str | None = None
    is_canonical_mapped: bool = True
    value: Decimal
    currency: str
    unit: str = "currency"
    source_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    extracted_at: datetime


def compute_sha256_bytes(content: bytes) -> str:
    """Compute 64-char SHA-256 hexadecimal digest for raw content bytes."""
    return hashlib.sha256(content).hexdigest()


class ForeignNormalizationReceipt(BaseModel):
    """Immutable receipt of foreign document normalization pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    form: ForeignFilingForm
    document_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    disposition: InterimDisposition
    facts_extracted_count: int
    facts: tuple[ForeignFactObservation, ...] = ()
    reason: str
    verified_at: datetime

    @model_validator(mode="after")
    def verify_fact_hashes_match_document(self) -> ForeignNormalizationReceipt:
        """Enforce strict invariant: every extracted fact source_hash must equal document_hash."""
        for fact in self.facts:
            if fact.source_hash != self.document_hash:
                raise ValueError(
                    f"Fact source_hash ({fact.source_hash}) does not match document_hash ({self.document_hash})"
                )
        return self


# IFRS and International Taxonomy Concept Mapping
_IFRS_CONCEPT_DICT: dict[str, str] = {
    "revenues": "revenue",
    "totalrevenue": "revenue",
    "total_revenue": "revenue",
    "revenue": "revenue",
    "sales": "revenue",
    "operatingprofit": "operating_income",
    "operating_profit": "operating_income",
    "operatingincome": "operating_income",
    "operating_income": "operating_income",
    "ebit": "operating_income",
    "netincome": "net_income",
    "net_income": "net_income",
    "profitfortheyear": "net_income",
    "profit_for_the_year": "net_income",
    "grossprofit": "gross_profit",
    "gross_profit": "gross_profit",
}

IFRS_TO_CANONICAL_CONCEPT: MappingProxyType[str, str] = MappingProxyType(_IFRS_CONCEPT_DICT)


# Standard Foreign Filer Roster Definition
_ROSTER_DICT: dict[str, ForeignFilerProfile] = {
    "NVO": ForeignFilerProfile(
        ticker="NVO",
        country_of_origin="Denmark",
        primary_form=ForeignFilingForm.FORM_20F,
        cadence=ReportingCadence.QUARTERLY,
        reporting_currency="DKK",
        admitted_document_hashes=(),
    ),
    "BN": ForeignFilerProfile(
        ticker="BN",
        country_of_origin="Canada",
        primary_form=ForeignFilingForm.FORM_40F,
        cadence=ReportingCadence.QUARTERLY,
        reporting_currency="USD",
        admitted_document_hashes=(),
    ),
    "NU": ForeignFilerProfile(
        ticker="NU",
        country_of_origin="Cayman Islands / Brazil",
        primary_form=ForeignFilingForm.FORM_20F,
        cadence=ReportingCadence.QUARTERLY,
        reporting_currency="USD",
        admitted_document_hashes=(
            "6719875a6438ee2cf931d86d634db560a927d6efc1fbe239a5ee0495f5735b54",
            "bed91b33182d30c6e400a111a9cd469c18e1c2d3e47258a3f5793d6f9db6b0a8",
        ),
    ),
    "WIX": ForeignFilerProfile(
        ticker="WIX",
        country_of_origin="Israel",
        primary_form=ForeignFilingForm.FORM_20F,
        cadence=ReportingCadence.QUARTERLY,
        reporting_currency="USD",
        admitted_document_hashes=(),
    ),
    "ASML": ForeignFilerProfile(
        ticker="ASML",
        country_of_origin="Netherlands",
        primary_form=ForeignFilingForm.FORM_20F,
        cadence=ReportingCadence.QUARTERLY,
        reporting_currency="EUR",
        admitted_document_hashes=(),
    ),
    "BHP": ForeignFilerProfile(
        ticker="BHP",
        country_of_origin="Australia",
        primary_form=ForeignFilingForm.FORM_20F,
        cadence=ReportingCadence.SEMIANNUAL,
        reporting_currency="USD",
        admitted_document_hashes=(),
    ),
}

FOREIGN_FILER_ROSTER: MappingProxyType[str, ForeignFilerProfile] = MappingProxyType(_ROSTER_DICT)


class ForeignFilerNormalizer:
    """Deterministic normalizer for foreign filer SEC forms and IR packages."""

    def __init__(self, roster: MappingProxyType[str, ForeignFilerProfile] | dict[str, ForeignFilerProfile] | None = None) -> None:
        self.roster = roster if roster is not None else FOREIGN_FILER_ROSTER

    def normalize_document(
        self,
        ticker: str,
        content: bytes,
        *,
        form: ForeignFilingForm,
        fiscal_year: int,
        period_end: date,
        period_start: date | None = None,
        accession_number: str | None = None,
        requested_period: RequestedFiscalPeriod = "FY",
        is_inline_xbrl: bool = False,
    ) -> ForeignNormalizationReceipt:
        """Process a raw foreign filer document and emit an immutable normalization receipt."""
        ticker_clean = ticker.upper().strip()
        doc_hash = compute_sha256_bytes(content)
        profile = self.roster.get(ticker_clean)
        now_ts = datetime.now(UTC)

        # 1. Reject unknown filer (fail closed on unregistered ticker)
        if not profile:
            return ForeignNormalizationReceipt(
                ticker=ticker_clean,
                form=form,
                document_hash=doc_hash,
                disposition=InterimDisposition.DEGRADED_UNSUPPORTED_FORMAT,
                facts_extracted_count=0,
                facts=(),
                reason=f"Unknown foreign filer ticker '{ticker_clean}': not registered in governance roster.",
                verified_at=now_ts,
            )

        currency = profile.reporting_currency

        # 2. Semiannual filter guard (e.g. BHP)
        if profile.cadence == ReportingCadence.SEMIANNUAL and requested_period in ("Q1", "Q2", "Q3", "Q4"):
            return ForeignNormalizationReceipt(
                ticker=ticker_clean,
                form=form,
                document_hash=doc_hash,
                disposition=InterimDisposition.NOT_APPLICABLE_SEMIANNUAL,
                facts_extracted_count=0,
                facts=(),
                reason=f"{ticker_clean} reports on semiannual cadence; quarterly slice '{requested_period}' is not applicable.",
                verified_at=now_ts,
            )

        # 3. Reject/degrade non-inline SEC forms (20-F, 40-F, 6-K) to prevent fake XBRL hallucination
        sec_statutory_forms = {
            ForeignFilingForm.FORM_20F,
            ForeignFilingForm.FORM_20FA,
            ForeignFilingForm.FORM_40F,
            ForeignFilingForm.FORM_40FA,
            ForeignFilingForm.FORM_6K,
            ForeignFilingForm.FORM_6KA,
        }
        if form in sec_statutory_forms and not is_inline_xbrl:
            return ForeignNormalizationReceipt(
                ticker=ticker_clean,
                form=form,
                document_hash=doc_hash,
                disposition=InterimDisposition.REJECTED_NON_INLINE_HTML,
                facts_extracted_count=0,
                facts=(),
                reason=f"Form {form.value} for {ticker_clean} is non-inline HTML; rejected zero-fact fake XBRL ingest.",
                verified_at=now_ts,
            )

        # 4. Parse admitted spreadsheet or statement cache
        if (
            form == ForeignFilingForm.ISSUER_IR_SPREADSHEET
            and (not profile.admitted_document_hashes or doc_hash not in profile.admitted_document_hashes)
        ):
            return ForeignNormalizationReceipt(
                ticker=ticker_clean,
                form=form,
                document_hash=doc_hash,
                disposition=InterimDisposition.DEGRADED_UNSUPPORTED_FORMAT,
                facts_extracted_count=0,
                facts=(),
                reason=f"Spreadsheet hash {doc_hash} is not in admitted roster for {ticker_clean}.",
                verified_at=now_ts,
            )

        # 5. Extract standardized concepts from structured payload
        facts: list[ForeignFactObservation] = []
        try:
            raw_text = content.decode("utf-8")
            raw_obj: object = json.loads(raw_text)
            if not isinstance(raw_obj, dict):
                raise ValueError("Payload must be a JSON dictionary")
            data = cast("dict[str, Any]", raw_obj)
            raw_facts_obj = data.get("facts")
            if not isinstance(raw_facts_obj, dict) or not raw_facts_obj:
                raise ValueError("Payload has empty or missing facts dictionary")
            raw_facts = cast("dict[str, Any]", raw_facts_obj)

            # Determine period start date
            if period_start is not None:
                start_dt = period_start
            elif requested_period in ("FY", "H1", "Q1"):
                start_dt = date(fiscal_year, 1, 1)
            elif requested_period in ("H2", "Q3"):
                start_dt = date(fiscal_year, 7, 1)
            elif requested_period == "Q2":
                start_dt = date(fiscal_year, 4, 1)
            elif requested_period == "Q4":
                start_dt = date(fiscal_year, 10, 1)
            else:
                start_dt = date(fiscal_year, 1, 1)

            for concept_name, val in raw_facts.items():
                if val is not None:
                    lookup_key = concept_name.lower().replace(" ", "_")
                    canonical = IFRS_TO_CANONICAL_CONCEPT.get(lookup_key)
                    is_mapped = canonical is not None
                    facts.append(
                        ForeignFactObservation(
                            ticker=ticker_clean,
                            form=form,
                            accession_number=accession_number,
                            period_start=start_dt,
                            period_end=period_end,
                            fiscal_year=fiscal_year,
                            fiscal_period=requested_period,
                            concept=concept_name,
                            canonical_concept=canonical,
                            is_canonical_mapped=is_mapped,
                            value=Decimal(str(val)),
                            currency=currency,
                            unit="currency",
                            source_hash=doc_hash,
                            extracted_at=now_ts,
                        )
                    )
        except Exception as e:
            return ForeignNormalizationReceipt(
                ticker=ticker_clean,
                form=form,
                document_hash=doc_hash,
                disposition=InterimDisposition.DEGRADED_UNSUPPORTED_FORMAT,
                facts_extracted_count=0,
                facts=(),
                reason=f"Failed parsing structured foreign facts for {ticker_clean}: {type(e).__name__}: {e}",
                verified_at=now_ts,
            )

        disposition = (
            InterimDisposition.ADMITTED_XBRL
            if is_inline_xbrl
            else (
                InterimDisposition.ADMITTED_GOVERNED_SPREADSHEET
                if form == ForeignFilingForm.ISSUER_IR_SPREADSHEET
                else InterimDisposition.ADMITTED_STATEMENT_CACHE
            )
        )

        return ForeignNormalizationReceipt(
            ticker=ticker_clean,
            form=form,
            document_hash=doc_hash,
            disposition=disposition,
            facts_extracted_count=len(facts),
            facts=tuple(facts),
            reason=f"Successfully extracted {len(facts)} normalized foreign facts for {ticker_clean}.",
            verified_at=now_ts,
        )
