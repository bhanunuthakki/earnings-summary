"""Pydantic models for yfinance analyst-estimate table shapes.

Structured types for the JSON payloads stored in ``data/historical/yfinance/``
and the point-in-time archive ``data/historical/yfinance_snapshots/<date>/`` —
the same latest-file + dated-snapshot storage pattern ``execution/save_fmp_data.py``
uses for FMP (``data/historical/fmp`` + ``fmp_snapshots``).

Field names mirror yfinance's exact column names (camelCase, and the vendor's
inconsistent casing like ``downLast7Days``) — same convention as
``models/fmp_payloads.py``; ruff N815 is suppressed for this file in
pyproject.toml. Columns whose vendor name starts with a digit (``7daysAgo``)
use a Field alias because they cannot be Python identifiers.

We model only fields consumed downstream (forward revenue/EPS consensus,
growth out-years, revision drift); extras are ignored so a Yahoo column
addition never breaks validation. Every numeric field is optional — Yahoo
omits rows/cells for thin-coverage names and the fetcher degrades per table.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Row labels Yahoo uses for its four forward periods.
YF_PERIODS: tuple[str, ...] = ("0q", "+1q", "0y", "+1y")
#: growth_estimates carries a fifth row: long-term (3-5y) EPS growth.
YF_PERIOD_LTG = "LTG"


class _YfRowBase(BaseModel):
    """Shared base: ignore vendor extras and normalize pandas NaN/Inf cells to
    None BEFORE validation. yfinance frames encode "missing" as float NaN;
    without this, NaN leaks into the persisted JSON (an invalid strict-JSON
    literal) and poisons downstream arithmetic (verified live: AAPL's LTG
    stockTrend arrives as NaN)."""

    model_config = ConfigDict(extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _nan_to_none(cls, v: object) -> object:
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            return None
        return v


class YfEstimateRow(_YfRowBase):
    """One period row from ``Ticker.earnings_estimate`` or
    ``Ticker.revenue_estimate`` (avg/low/high consensus + analyst count +
    implied YoY growth). ``yearAgoEps`` appears on the earnings table,
    ``yearAgoRevenue`` on the revenue table; both optional here so one model
    covers both tables."""

    period: str
    avg: float | None = None
    low: float | None = None
    high: float | None = None
    yearAgoEps: float | None = None
    yearAgoRevenue: float | None = None
    numberOfAnalysts: int | None = None
    growth: float | None = None
    currency: str | None = None


class YfGrowthRow(_YfRowBase):
    """One period row from ``Ticker.growth_estimates`` (stock vs index trend).
    The ``LTG`` row is Yahoo's long-term (3-5y) EPS growth consensus — the only
    free out-year growth anchor beyond +1y."""

    model_config = ConfigDict(extra="ignore")

    period: str
    stockTrend: float | None = None
    indexTrend: float | None = None


class YfEpsTrendRow(_YfRowBase):
    """One period row from ``Ticker.eps_trend`` — the current consensus EPS and
    where it stood 7/30/60/90 days ago (revision drift)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    period: str
    current: float | None = None
    days7ago: float | None = Field(default=None, alias="7daysAgo")
    days30ago: float | None = Field(default=None, alias="30daysAgo")
    days60ago: float | None = Field(default=None, alias="60daysAgo")
    days90ago: float | None = Field(default=None, alias="90daysAgo")
    currency: str | None = None


class YfEpsRevisionsRow(_YfRowBase):
    """One period row from ``Ticker.eps_revisions`` (up/down revision counts).
    ``downLast7Days`` capitalization is the vendor's own quirk — preserved."""

    model_config = ConfigDict(extra="ignore")

    period: str
    upLast7days: int | None = None
    upLast30days: int | None = None
    downLast30days: int | None = None
    downLast7Days: int | None = None
    currency: str | None = None


class YfPriceTargets(_YfRowBase):
    """``Ticker.analyst_price_targets`` — a plain dict, not a frame."""

    model_config = ConfigDict(extra="ignore")

    current: float | None = None
    low: float | None = None
    high: float | None = None
    mean: float | None = None
    median: float | None = None


class YfEstimatesSnapshot(BaseModel):
    """The persisted per-ticker payload: every Yahoo analysis table plus pull
    provenance. ``asof_date`` is the snapshot's point-in-time key (matches the
    dated directory it is archived under)."""

    model_config = ConfigDict(extra="ignore")

    ticker: str
    asof_date: str
    fetched_at: str
    source: str = "yfinance"
    earnings_estimate: list[YfEstimateRow] = Field(default_factory=list[YfEstimateRow])
    revenue_estimate: list[YfEstimateRow] = Field(default_factory=list[YfEstimateRow])
    growth_estimates: list[YfGrowthRow] = Field(default_factory=list[YfGrowthRow])
    eps_trend: list[YfEpsTrendRow] = Field(default_factory=list[YfEpsTrendRow])
    eps_revisions: list[YfEpsRevisionsRow] = Field(default_factory=list[YfEpsRevisionsRow])
    analyst_price_targets: YfPriceTargets | None = None

    def table_names_present(self) -> list[str]:
        """Names of the non-empty tables — the fetcher's per-ticker tally."""
        present: list[str] = []
        for name in (
            "earnings_estimate",
            "revenue_estimate",
            "growth_estimates",
            "eps_trend",
            "eps_revisions",
        ):
            if getattr(self, name):
                present.append(name)
        if self.analyst_price_targets is not None:
            present.append("analyst_price_targets")
        return present
