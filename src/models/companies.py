"""Company model — one row of tracked_companies.

Mirrors the DB shape after migration 0001_companies_provenance. instrument_type
and filing_regime are NULL for index-universe tickers added by the FMP backfill;
populated for the user's 28 tracked names.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class InstrumentType(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    ADR = "adr"


class FilingRegime(StrEnum):
    """SEC filing regime — drives which annual/quarterly forms to fetch."""

    FORM_10K = "10-K"
    FORM_20F = "20-F"
    FORM_40F = "40-F"

    @property
    def is_fpi(self) -> bool:
        return self in (FilingRegime.FORM_20F, FilingRegime.FORM_40F)

    @property
    def interim_doc_type(self) -> str:
        return "sec_6k" if self.is_fpi else "sec_10q"

    @property
    def annual_doc_type(self) -> str:
        if self == FilingRegime.FORM_20F:
            return "sec_20f"
        if self == FilingRegime.FORM_40F:
            return "sec_40f"
        return "sec_10k"



class BusinessModelClass(StrEnum):
    """Coarse business-model bucket the metrics engine's ``applicability.py``
    (src/compute/metrics_engine/) uses to exclude formulas that don't apply
    to a ticker's capital structure — e.g. `gross_margin` for a bank with no
    COGS concept. Mirrors `tracked_companies.business_model_class` (alembic
    0163). Default is `OPERATING_COMPANY`; `INSURANCE` has no current roster
    member but is kept as a first-class value since several formulas'
    ``excluded_business_models`` name it explicitly (docs/design/
    bottoms_up_metrics_engine.md §1) — extend the roster mapping, not this enum,
    when an insurer is added.
    """

    OPERATING_COMPANY = "operating_company"
    BANK = "bank"
    INSURANCE = "insurance"
    HOLDCO = "holdco"


class AccountingStandard(StrEnum):
    """Which line-item vocabulary a ticker's `financial_facts` rows follow.

    Drives `compute.metrics_engine.inputs.resolve_concept` — US_GAAP uses the
    identity-mapped field names already in `financial_facts.line_item`; IFRS
    uses a per-ticker-verified mapping populated incrementally (Phase 2).
    NOT inferred from `filing_regime` (a 20-F filer is not reliably IFRS —
    some file US-GAAP-reconciled), so this is seeded explicitly per ticker.
    """

    US_GAAP = "us_gaap"
    IFRS = "ifrs"


class ListType(StrEnum):
    """How a ticker enters the tracked_companies table.

    `etf` and `index_member` were added by the worktree session's FMP backfill
    (199 index tickers + FLKR). They overlap conceptually with `instrument_type`
    but the data is what it is — preserve fidelity over normalization.

    `evaluation` is for new-name screening: tickers we build eval-flavor briefs
    for but haven't committed to monitoring. Distinct from `watchlist` (holding
    pen, no auto-brief).
    """

    PORTFOLIO = "portfolio"
    WATCHLIST = "watchlist"
    EVALUATION = "evaluation"
    NONE = "none"
    ETF = "etf"
    INDEX_MEMBER = "index_member"


class Company(BaseModel):
    """One tracked_companies row."""

    id: int
    # Canonical tenant id — TEXT, FK to tenants.id (alembic 0073). Reconciled
    # from the legacy INTEGER namespace onto the same str the substrate stores
    # key on (identity.DEFAULT_USER_ID, default 'bhanu').
    user_id: str
    ticker: str
    name: str
    list_type: ListType
    added_at: datetime | None = None
    sec_validated: bool = False
    ir_url: str | None = None
    instrument_type: InstrumentType | None = None
    filing_regime: FilingRegime | None = None
    fiscal_year_end: str | None = None
    fmp_data_saved: bool = False
    fmp_data_upto: str | None = None
    # Metrics-engine classification columns (alembic 0163/0164). Both default
    # to the common case (an operating company reporting US-GAAP) so every
    # pre-existing row is well-formed the moment the column lands; a real
    # bank/holdco/IFRS filer is seeded explicitly by the migration's data step
    # or via a follow-up UPDATE, never inferred at read time.
    business_model_class: BusinessModelClass = BusinessModelClass.OPERATING_COMPANY
    accounting_standard: AccountingStandard = AccountingStandard.US_GAAP

    @property
    def is_fpi(self) -> bool:
        if self.filing_regime is not None:
            return self.filing_regime.is_fpi
        return self.instrument_type == InstrumentType.ADR

    @property
    def interim_doc_type(self) -> str:
        if self.filing_regime is not None:
            return self.filing_regime.interim_doc_type
        return "sec_6k" if self.is_fpi else "sec_10q"

    @property
    def annual_doc_type(self) -> str:
        if self.filing_regime is not None:
            return self.filing_regime.annual_doc_type
        return "sec_20f" if self.is_fpi else "sec_10k"

