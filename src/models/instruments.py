"""Cross-asset instrument models.

The polymorphic registry (`tracked_companies`) plus its kind discriminator
(`instrument_type`) was added back in migration 0001 — see
[`src/models/companies.py`](companies.py). This module layers the per-kind
detail types on top:

  EtfProfile   — one row of `etf_profile`
  EtfHolding   — one row of `etf_holdings`

Future kinds (bond, option, etc.) get their own models here; the registry
side stays in `companies.py`.

See `directives/cross_asset_data_model.md` for the design rationale.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class EtfProfile(BaseModel):
    """One row of `etf_profile`.

    Identity + descriptive fields for an ETF. All non-key fields are nullable
    because real-world coverage is sparse (some issuers don't publish AUM in
    real time; some smaller ETFs lack benchmark metadata).
    """

    ticker: str
    name: str | None = None
    issuer: str | None = None
    expense_ratio: float | None = None  # decimal; 0.0035 = 35 bps
    aum_usd_m: float | None = None  # millions USD
    inception_date: date | None = None
    asset_class: str | None = None  # 'equity' | 'fixed_income' | ...
    benchmark_index: str | None = None
    domicile: str | None = None  # 'US' | 'IE' | ...
    listed_exchange: str | None = None
    distribution_yield: float | None = None  # decimal
    description: str | None = None
    sector_label: str | None = None
    nav: float | None = None
    price: float | None = None
    premium_discount_pct: float | None = None  # decimal
    # Basket characteristics published by the issuer (directives/etf_data.md).
    # Ratios are multiples-of-1x (14.3 = 14.3x), not percents; they carry
    # their own as-of + source because issuers refresh characteristics on a
    # different cadence than the identity fields above.
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    weighted_avg_mktcap_usd_m: float | None = None  # millions USD
    characteristics_as_of: date | None = None
    characteristics_source: str | None = None
    source: str = "fmp"
    profile_fetched_at: datetime


class EtfHolding(BaseModel):
    """One constituent row of `etf_holdings`.

    Natural key is (ticker, as_of_date, constituent_ticker). `constituent_ticker`
    may be None for cash sleeves / unmapped exposures — keep them in the table
    for weight accounting.
    """

    ticker: str
    as_of_date: date
    constituent_ticker: str | None = None
    name: str | None = None
    weight_pct: float | None = None  # decimal; 0.0742 = 7.42%
    shares_held: float | None = None
    market_value_usd: float | None = None
    sector: str | None = None
    asset_class: str | None = None
    rank_position: int | None = None
    country: str | None = None  # constituent domicile (N-PORT invCountry, ISO-3166 alpha-2)
    source: str = "fmp"
    fetched_at: datetime
