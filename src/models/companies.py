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


class ListType(StrEnum):
    """How a ticker enters the tracked_companies table.

    `etf` and `index_member` were added by the worktree session's FMP backfill
    (199 index tickers + FLKR). They overlap conceptually with `instrument_type`
    but the data is what it is — preserve fidelity over normalization.
    """

    PORTFOLIO = "portfolio"
    WATCHLIST = "watchlist"
    NONE = "none"
    ETF = "etf"
    INDEX_MEMBER = "index_member"


class Company(BaseModel):
    """One tracked_companies row."""

    id: int
    user_id: int
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
