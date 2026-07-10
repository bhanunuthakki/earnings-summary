"""Report-side models for the ETF brief shape.

Separate from `report.models.ReportSpec` (which is equity-shaped) on purpose
— see `directives/cross_asset_data_model.md` §4. An ETF has no income
statement, no segments, no DCF; trying to render those as
`NOT_APPLICABLE` stubs through the equity renderer adds noise without
helping anyone. The renderer dispatcher in `execution/build_artifacts.py`
picks this shape when `tracked_companies.instrument_type='etf'`.

The structures here intentionally mirror the conventions of
`report.models` (Pydantic, `SectionStatus`, `MissingReason`) so that future
sections can graduate to a shared base or unified renderer without
re-wiring everything.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from report.models import MissingReason, SectionStatus


class EtfProfileSection(BaseModel):
    """§1 of the ETF brief — identity, AUM, expense ratio, benchmark, etc."""

    status: SectionStatus
    missing: MissingReason | None = None
    ticker: str
    name: str | None = None
    issuer: str | None = None
    expense_ratio: float | None = None  # decimal; 0.0035 = 35 bps
    aum_usd_m: float | None = None
    inception_date: date | None = None
    asset_class: str | None = None
    benchmark_index: str | None = None
    domicile: str | None = None
    listed_exchange: str | None = None
    distribution_yield: float | None = None  # decimal
    description: str | None = None
    sector_label: str | None = None
    nav: float | None = None
    price: float | None = None
    premium_discount_pct: float | None = None
    profile_fetched_at: datetime | None = None


class EtfHoldingRow(BaseModel):
    """One row in the ETF holdings table."""

    rank: int | None = None
    constituent_ticker: str | None = None
    name: str | None = None
    weight_pct: float | None = None  # decimal
    sector: str | None = None
    market_value_usd: float | None = None


class EtfSectorBreakdownRow(BaseModel):
    sector: str
    weight_pct: float  # sum of constituent weights in that sector


class EtfHoldingsSection(BaseModel):
    """§5 of the ETF brief — top constituents + sector breakdown."""

    status: SectionStatus
    missing: MissingReason | None = None
    as_of_date: date | None = None
    total_constituents: int = 0
    top_holdings: list[EtfHoldingRow] = Field(default_factory=list[EtfHoldingRow])
    sector_breakdown: list[EtfSectorBreakdownRow] = Field(
        default_factory=list[EtfSectorBreakdownRow]
    )


class EtfRoleSynthesis(BaseModel):
    """The governed 'role in portfolio' one-pager (purpose
    ``etf_role_synthesis``) — the ONLY semantic judgment in the ETF workup.
    Every number the model saw was deterministic (score/fit factors, overlap,
    country rollup, what-if deltas, target gaps); the synthesis says what
    role the fund would actually play in THIS book. Schema-validated before
    persisting to ``llm_artifacts`` (sha-keyed, so reruns are free until the
    inputs move)."""

    role_summary: str = Field(max_length=900)  # <=120 words, plain language
    what_it_adds: list[str] = Field(default_factory=list[str], max_length=4)
    overlap_caution: str | None = None
    verdict: Literal[
        "closes_target_gap", "diversifier", "redundant", "style_crowding", "insufficient_data"
    ]
    suggested_weight_band: str | None = None  # e.g. "2-4%"
    watch_items: list[str] = Field(default_factory=list[str], max_length=3)


class EtfBriefSpec(BaseModel):
    """The ETF brief. One per (ticker, generation_date).

    Intentionally minimal for the cross-asset MVP. As more lenses prove out
    on ETFs (rotation tracker, look-through portfolio overlap, premium/
    discount band) they slot in here as additional Optional sections.
    """

    ticker: str
    generation_date: date
    repo_root: str
    profile: EtfProfileSection
    holdings: EtfHoldingsSection
