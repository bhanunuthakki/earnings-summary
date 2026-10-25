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

import re
from datetime import date, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EtfFieldEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    value: str | float
    source: str = Field(min_length=1)
    captured_at: datetime
    source_as_of: date | None = None

    document_id: int | None = Field(default=None, gt=0)
    document_version_id: str | None = Field(default=None, min_length=1)
    observation_id: str | None = Field(default=None, min_length=1)
    content_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    source_url: str | None = Field(default=None, min_length=1)
    json_pointer: str | None = None
    parser_version: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _check_capture_linkage(self) -> Self:
        linked = (
            self.document_id,
            self.document_version_id,
            self.observation_id,
            self.content_sha256,
            self.source_url,
            self.json_pointer,
            self.parser_version,
        )
        present = [item is not None for item in linked]
        if any(present) and not all(present):
            raise ValueError("capture linkage must be fully populated or entirely absent")
        if self.content_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.content_sha256
        ):
            raise ValueError("content_sha256 must be 64 lowercase hexadecimal characters")
        if self.json_pointer is not None:
            if not self.json_pointer.startswith("/"):
                raise ValueError("json_pointer must start with '/'")
            if re.search(r"~(?![01])", self.json_pointer):
                raise ValueError("json_pointer has an invalid '~' escape")
        return self

    @property
    def has_capture_linkage(self) -> bool:
        return self.document_id is not None


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
    field_evidence: dict[str, EtfFieldEvidence] = Field(default_factory=dict[str, EtfFieldEvidence])


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
