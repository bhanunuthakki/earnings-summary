"""REST client for the companion portfolio-tracker FastAPI.

Pulls live positions / transactions / accounts from the sibling project's API
(default ``http://127.0.0.1:8000``) and derives the two things the tracker's raw
endpoints don't surface directly:

* each holding's ``percent_of_portfolio`` (market value / total book), and
* a per-account ``tax_treatment`` (``taxable`` / ``tax_deferred`` / ``tax_free``
  / ``unknown``) inferred from the Plaid account ``type`` + ``subtype``.

Degrades gracefully: when the tracker isn't running — or any call errors / returns
malformed JSON — :func:`fetch_live_portfolio` returns a ``LivePortfolio`` with
``available=False`` and an ``error`` reason instead of raising, so the dashboard's
Portfolio tab shows a "tracker offline" note rather than a 500.

Master build P2.1 adds the read-only analytics families the Portfolio theme
renders — ``/api/portfolio/performance`` (Modified-Dietz TWR vs cashflow-matched
SPY / QQQ / policy benchmarks), ``/position-alpha``, ``/positioning``, ``/beta``
and ``/api/policy`` — via :func:`fetch_portfolio_analytics`. Architecture rule
(the 2026-06 directive): the tracker owns ALL benchmark math; this client only
parses what the API returns and never recomputes returns, alpha, or beta.
Endpoint failures are isolated per section (``PortfolioAnalytics.errors``), with
the same never-raise contract as the live fetch.

A best-practices spec for clean first-class endpoints (server-derived percent +
tax_treatment, additive ``/api/v1`` namespace, ETag, pagination) lives at
``../portfolio-tracker/docs/api/positions-v1.md``. Once the tracker ships those,
this client can switch to ``GET /api/v1/portfolio/positions`` and drop the local
joins / derivations below.

**v1 transport (consolidation PRD §12 Phase 2 — consumer adoption).** The
tracker now serves the versioned ``/api/v1`` Portfolio Data Service, consumed
through the typed client in :mod:`integrations.portfolio_tracker_v1`. With
``PORTFOLIO_TRACKER_V1_READS=1`` the four public facades here
(:func:`fetch_live_portfolio`, :func:`fetch_transaction_history`,
:func:`fetch_portfolio_analytics`, :func:`fetch_exit_quality`) route through
that typed client — schema validation, the fail-closed major-version gate, and
the response envelope all enforced — then adapt back to the exact legacy
dataclass shapes so no call site changes. The envelope is surfaced additively
(``as_of`` / ``is_stale`` / ``is_partial`` / ``envelope_warnings``). The
switch defaults OFF; it flips ON only after ``execution/tracker_v1_parity.py``
passes and the owner approves cutover, and the legacy transport paths are
deleted in Phase 5. A broken v1 read reports ``available=False`` with the v1
reason — it never silently falls back to the legacy endpoints, which would
mask contract breaks. One documented exception: ``/api/policy`` has no v1
successor (policy weights are provider-owned calculation config, Phase-0
ruling PT-6), so the ``policy`` analytics section stays on its legacy
endpoint under either transport — tracked as a shared-contract gap.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TypeVar, cast
from urllib.parse import urlencode

import requests
from pydantic import BaseModel

from integrations.portfolio_tracker_v1 import (
    PerformanceV1Result,
    TrackerV1Client,
    V1Fetch,
    V1Meta,
)

log = logging.getLogger(__name__)

# 127.0.0.1 (not "localhost"): on Windows a DOWN tracker costs ~2s of
# OS-level refused-connect retries PER address family, and "localhost"
# resolves to ::1 + 127.0.0.1 — doubling the burn (measured 4.08s vs 2.07s,
# P6.1 latency pass). The tracker (uvicorn) binds IPv4 loopback by default.
_DEFAULT_API_URL = "http://127.0.0.1:8000"
_TIMEOUT_SECONDS = 4.0
# The analytics endpoints recompute TWR/alpha/beta over a year of dailies on
# request — give them more headroom than the instant holdings reads.
_ANALYTICS_TIMEOUT_SECONDS = 6.0
# Connect-phase cap, passed as the first half of requests' (connect, read)
# timeout tuple. A live loopback tracker connects in <1ms; a dead one now
# fails the whole call group in ~this long instead of riding the OS retry
# cycle to ~2s — the Portfolio / Decisions landing tabs degrade in ~1s
# total instead of 8s when the tracker is offline.
_CONNECT_TIMEOUT_SECONDS = 0.5

_T = TypeVar("_T")

# Tax-treatment buckets, in display order.
TAX_BUCKETS: tuple[str, ...] = ("taxable", "tax_deferred", "tax_free", "unknown")


@dataclass(slots=True)
class TaxLot:
    """One account's slice of a position, tagged with its tax treatment.

    ``cost_basis`` is the account-level basis the tracker reports (None on
    tracker builds that predate the field) — the honest position-level
    fallback when per-lot reconstruction can't be validated.
    """

    account_id: int
    account_name: str
    quantity: float
    market_value: float | None
    tax_treatment: str
    cost_basis: float | None = None


@dataclass(slots=True)
class LivePosition:
    ticker: str | None
    name: str | None
    quantity: float
    market_value: float | None
    cost_basis: float | None
    unrealized_pnl: float | None
    percent_of_portfolio: float | None
    accounts: list[TaxLot] = field(default_factory=list[TaxLot])


@dataclass(slots=True)
class LiveTransaction:
    date: str
    ticker: str | None
    name: str | None
    type: str
    subtype: str | None
    quantity: float | None
    amount: float | None
    account_name: str
    # Lot-reconstruction fields (tax-aware /review). Defaulted so older
    # positional constructions and canned payloads stay valid.
    account_id: int | None = None
    price: float | None = None
    fees: float | None = None


@dataclass(slots=True)
class LivePortfolio:
    """Consolidated live portfolio state, or a degraded marker when the tracker
    is unreachable (``available=False`` + ``error``)."""

    available: bool
    api_url: str
    error: str | None = None
    total_market_value: float = 0.0
    positions: list[LivePosition] = field(default_factory=list[LivePosition])
    transactions: list[LiveTransaction] = field(default_factory=list[LiveTransaction])
    # Provider envelope (consolidation PRD §8.1: surfaced, never swallowed).
    # Populated only on the v1 transport path (PORTFOLIO_TRACKER_V1_READS=1);
    # defaults keep legacy-path consumers and payloads unaffected. ``as_of`` is
    # the holdings observation date; ``envelope_warnings`` carries stable
    # warning CODES only (never messages, which can embed account detail).
    as_of: str | None = None
    is_stale: bool = False
    is_partial: bool = False
    envelope_warnings: list[str] = field(default_factory=list[str])
    # Market value summed into each tax bucket (taxable / tax_deferred / tax_free /
    # unknown). Bucketed at the LOT level since one position can span accounts with
    # different treatments (e.g. the same name in a Roth IRA and a brokerage).
    by_tax_treatment: dict[str, float] = field(default_factory=dict[str, float])


def tax_treatment(account_type: str | None, subtype: str | None) -> str:
    """Map a Plaid account ``type`` + ``subtype`` to a tax bucket.

    Roth (incl. Roth 401k/IRA) + HSA grow tax-free; traditional 401k/IRA/SEP/etc.
    are tax-deferred; an ordinary brokerage is taxable. The ``roth`` check runs
    first so "roth ira" / "roth 401k" land in ``tax_free``, not ``tax_deferred``.
    """
    s = (subtype or "").strip().lower()
    t = (account_type or "").strip().lower()
    if "roth" in s or s == "hsa":
        return "tax_free"
    if (
        "401k" in s
        or "ira" in s
        or s
        in {
            "403b",
            "457b",
            "sep",
            "simple",
            "pension",
            "keogh",
            "retirement",
            "rrsp",
            "sarsep",
            "profit sharing plan",
        }
    ):
        return "tax_deferred"
    if "brokerage" in s or t == "brokerage":
        return "taxable"
    return "unknown"


def tax_treatment_from_name(account_name: str | None) -> str:
    """Fallback bucket classifier from the ACCOUNT NAME, for accounts absent
    from ``/api/plaid/items`` (expired item, brokerage sub-accounts).

    Live examples this recovers: "BrokerageLink Roth" -> tax_free, "Health
    Savings Account" -> tax_free, "SoFi Self-directed" -> taxable. ``ira`` is
    matched word-ish (surrounded by non-letters) so e.g. "Admiral" can't hit.
    Anything unrecognized stays ``unknown`` — downstream tax math flags it
    rather than guessing.
    """
    n = (account_name or "").strip().lower()
    if not n:
        return "unknown"
    if "roth" in n or "hsa" in n or "health savings" in n:
        return "tax_free"
    padded = f" {n} "
    if "401k" in n or "401(k)" in n or " ira " in padded or "retirement" in n:
        return "tax_deferred"
    if "brokerage" in n or "individual" in n or "self-directed" in n or "taxable" in n:
        return "taxable"
    return "unknown"


def fetch_live_portfolio(
    *,
    api_url: str | None = None,
    timeout: float = _TIMEOUT_SECONDS,
    transactions_limit: int = 25,
) -> LivePortfolio:
    """Fetch + consolidate the live portfolio from the tracker REST API.

    Never raises on a tracker problem — returns ``available=False`` with an
    ``error`` reason so callers can degrade. ``api_url`` falls back to the
    ``PORTFOLIO_TRACKER_API_URL`` env var, then ``http://127.0.0.1:8000``.

    With ``PORTFOLIO_TRACKER_V1_READS=1`` the read routes through the typed
    ``/api/v1`` client instead (same return shape + envelope fields; see the
    module docstring).
    """
    base = (api_url or os.environ.get("PORTFOLIO_TRACKER_API_URL") or _DEFAULT_API_URL).rstrip("/")
    if _v1_reads_enabled():
        return _fetch_live_portfolio_v1(
            base, timeout=timeout, transactions_limit=transactions_limit
        )
    try:
        holdings = _get(base, "/api/portfolio/holdings", timeout=timeout)
        items = _get(base, "/api/plaid/items", timeout=timeout)
        # limit is coerced to int, so inlining it into the path is injection-safe
        # (and sidesteps the requests `params` typing).
        txns = _get(
            base, f"/api/portfolio/transactions?limit={int(transactions_limit)}", timeout=timeout
        )
    except requests.RequestException as exc:
        return LivePortfolio(available=False, api_url=base, error=f"{type(exc).__name__}: {exc}")
    except ValueError as exc:  # JSON decode / unexpected shape
        return LivePortfolio(available=False, api_url=base, error=f"bad response: {exc}")

    acct_tax = _account_tax_map(items)
    positions = _build_positions(holdings, acct_tax)
    total_mv = sum((p.market_value or 0.0) for p in positions)
    for p in positions:
        p.percent_of_portfolio = (
            (100.0 * (p.market_value or 0.0) / total_mv) if total_mv > 0 else None
        )
    by_tax = {bucket: 0.0 for bucket in TAX_BUCKETS}
    for p in positions:
        for lot in p.accounts:
            by_tax[lot.tax_treatment] = by_tax.get(lot.tax_treatment, 0.0) + (
                lot.market_value or 0.0
            )
    return LivePortfolio(
        available=True,
        api_url=base,
        total_market_value=total_mv,
        positions=positions,
        transactions=_build_transactions(txns),
        by_tax_treatment=by_tax,
    )


def _get(base: str, path: str, *, timeout: float) -> list[dict[str, object]]:
    resp = requests.get(base + path, timeout=(_CONNECT_TIMEOUT_SECONDS, timeout))
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"{path} returned {type(data).__name__}, expected a list")
    return cast("list[dict[str, object]]", data)


def _account_tax_map(items: list[dict[str, object]]) -> dict[int, str]:
    """Flatten ``/api/plaid/items`` → ``{account_id: tax_treatment}``."""
    out: dict[int, str] = {}
    for item in items:
        accounts = item.get("accounts")
        if not isinstance(accounts, list):
            continue
        for acct in cast("list[dict[str, object]]", accounts):
            aid = acct.get("account_id")
            if not isinstance(aid, int):
                continue
            out[aid] = tax_treatment(_s(acct.get("type")), _s(acct.get("subtype")))
    return out


def _build_positions(
    holdings: list[dict[str, object]], acct_tax: dict[int, str]
) -> list[LivePosition]:
    positions: list[LivePosition] = []
    for h in holdings:
        lots: list[TaxLot] = []
        raw_accounts = h.get("accounts")
        if isinstance(raw_accounts, list):
            for acct in cast("list[dict[str, object]]", raw_accounts):
                aid = acct.get("account_id")
                aid_int = aid if isinstance(aid, int) else -1
                account_name = _s(acct.get("account_name")) or "?"
                # Plaid type/subtype first; accounts missing from /plaid/items
                # (or with an unmapped subtype) fall back to the account NAME.
                treatment = acct_tax.get(aid_int, "unknown")
                if treatment == "unknown":
                    treatment = tax_treatment_from_name(account_name)
                lots.append(
                    TaxLot(
                        account_id=aid_int,
                        account_name=account_name,
                        quantity=_f(acct.get("quantity")) or 0.0,
                        market_value=_f(acct.get("institution_value")),
                        tax_treatment=treatment,
                        cost_basis=_f(acct.get("cost_basis")),
                    )
                )
        positions.append(
            LivePosition(
                ticker=_s(h.get("ticker")),
                name=_s(h.get("name")),
                quantity=_f(h.get("total_quantity")) or 0.0,
                market_value=_f(h.get("total_value")),
                cost_basis=_f(h.get("total_cost_basis")),
                unrealized_pnl=_f(h.get("unrealized_pnl")),
                percent_of_portfolio=None,  # filled in once the book total is known
                accounts=lots,
            )
        )
    return positions


def _build_transactions(txns: list[dict[str, object]]) -> list[LiveTransaction]:
    return [
        LiveTransaction(
            date=_s(t.get("date")) or "",
            ticker=_s(t.get("ticker")),
            name=_s(t.get("name")),
            type=_s(t.get("type")) or "",
            subtype=_s(t.get("subtype")),
            quantity=_f(t.get("quantity")),
            amount=_f(t.get("amount")),
            account_name=_s(t.get("account_name")) or "?",
            account_id=_i(t.get("account_id")),
            price=_f(t.get("price")),
            fees=_f(t.get("fees")),
        )
        for t in txns
    ]


# The tracker caps ``/api/portfolio/transactions`` at 5000 rows per call.
TRANSACTION_HISTORY_LIMIT = 5000
# Far enough back to cover any Plaid-ingested history; the endpoint's own
# default window is only 24 months.
_HISTORY_START_DATE = "2000-01-01"


def fetch_transaction_history(
    *,
    api_url: str | None = None,
    timeout: float = _ANALYTICS_TIMEOUT_SECONDS,
    start_date: str = _HISTORY_START_DATE,
    limit: int = TRANSACTION_HISTORY_LIMIT,
) -> list[LiveTransaction] | None:
    """Full transaction history for tax-lot reconstruction.

    Unlike the ``fetch_live_portfolio`` transactions tail (limit 25, recency
    feed), this pulls the deepest window the tracker allows so FIFO lots can be
    rebuilt per account. Never raises — ``None`` on any tracker problem, so the
    tax block degrades instead of breaking ``/review``. A result whose length
    equals ``limit`` may be TRUNCATED — callers must treat lot math built on it
    as approximate, not exact.
    """
    base = _resolve_base(api_url)
    if _v1_reads_enabled():
        return _fetch_transaction_history_v1(base, timeout=timeout, start_date=start_date)
    params = urlencode({"start_date": start_date, "limit": int(limit)})
    try:
        return _build_transactions(
            _get(base, f"/api/portfolio/transactions?{params}", timeout=timeout)
        )
    except (requests.RequestException, ValueError):
        return None


def _f(v: object) -> float | None:
    """Coerce a JSON number / Decimal-as-string to float; None on empty/bad."""
    if v is None or v == "":
        return None
    try:
        return float(cast("str | float | int", v))
    except (ValueError, TypeError):
        return None


def _s(v: object) -> str | None:
    return v if isinstance(v, str) else (str(v) if v is not None else None)


def _i(v: object) -> int | None:
    """Coerce a JSON integer (possibly float/string-encoded) to int; None on bad.

    ``bool`` is rejected explicitly — it IS an int in Python, but a JSON ``true``
    arriving where a count belongs is a shape error, not a 1.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    f = _f(v)
    return int(f) if f is not None else None


def _b(v: object) -> bool | None:
    """Tri-state bool: None when the key was absent / null (a tracker build that
    predates the field), else ``bool(v)``. Distinguishes "not provided" from an
    explicit ``False`` — load-bearing for ``alpha_significant`` (None must not
    read as "insignificant")."""
    return None if v is None else bool(v)


def _dicts(v: object) -> list[dict[str, object]]:
    """The dict elements of a JSON list; [] for absent / non-list / odd shapes."""
    if not isinstance(v, list):
        return []
    return [
        cast("dict[str, object]", item)
        for item in cast("list[object]", v)
        if isinstance(item, dict)
    ]


# ---------------------------------------------------------------------------
# Portfolio analytics (master build P2.1). Read-only consumers of the
# tracker's performance / alpha / positioning / policy / beta endpoints.
# The tracker is the source of record for every number here — fields map
# 1:1 onto its response models (see the tracker's schemas.py) and nothing
# is recomputed client-side beyond float coercion.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PerformancePoint:
    """One day of the TWR series: cumulative window return % per book/benchmark.

    Benchmark fields are None when the tracker lacks that benchmark's price
    history for the day (its ``PerformancePoint`` marks them Optional).
    """

    date: str
    portfolio_return_pct: float | None
    spy_return_pct: float | None
    qqq_return_pct: float | None
    policy_return_pct: float | None


@dataclass(slots=True)
class PerformanceSeries:
    """``GET /api/portfolio/performance`` — Modified-Dietz TWR vs synthetic
    SPY / QQQ / policy books that receive the same external cashflows."""

    start_date: str | None
    end_date: str | None
    base_value: float | None
    net_external_cashflow_in: float | None
    backfill_start_unreliable: bool
    # First date backed by a real observed snapshot. Served by BOTH transports
    # and load-bearing for provenance: a series whose ``start_date`` precedes
    # this is partly the provider's MODELED transaction walk-back, so its
    # returns are rebased off a reconstructed value. Compare the two fields to
    # classify the basis — ``backfill_start_unreliable`` does NOT answer this
    # (it flags an untrustworthy walk-back START VALUE, and measured False on
    # both transports across observed, trailing-365d, and 26-year windows on
    # 2026-07-24, so it is constant in practice). WINDOW-RELATIVE: it reports
    # the earliest observation INSIDE the requested window, never before it.
    earliest_observed_date: str | None = None
    points: list[PerformancePoint] = field(default_factory=list[PerformancePoint])


@dataclass(slots=True)
class PositionAlphaRow:
    """One ticker's window P&L vs its SPY-counterfactual (dollar alpha)."""

    ticker: str | None
    name: str | None
    value_at_start: float | None
    bought_in_window: float | None
    sold_in_window: float | None
    value_at_end: float | None
    actual_pl: float | None
    spy_counterfactual_pl: float | None
    alpha: float | None
    alpha_vs_qqq: float | None
    alpha_vs_policy: float | None
    incomplete: bool


@dataclass(slots=True)
class PositionAlpha:
    """``GET /api/portfolio/position-alpha`` — per-ticker dollar alpha where each
    benchmark receives the position's exact buy/sell flows on the same days."""

    start_date: str | None
    end_date: str | None
    has_policy: bool
    total_actual_pl: float | None
    total_spy_pl: float | None
    total_alpha: float | None
    total_alpha_vs_qqq: float | None
    total_alpha_vs_policy: float | None
    rows: list[PositionAlphaRow] = field(default_factory=list[PositionAlphaRow])


@dataclass(slots=True)
class AllocationBucket:
    """One slice of a positioning breakdown (asset type / sector / region /
    account type): its share of the book plus how many names sit in it."""

    label: str
    value: float | None
    weight_pct: float | None
    count: int | None


@dataclass(slots=True)
class Concentration:
    """Concentration summary. ``hhi`` is on the 0–10,000 scale;
    ``effective_holdings`` reads as "behaves like ~N equal positions"."""

    num_positions: int | None
    top1_weight_pct: float | None
    top5_weight_pct: float | None
    top10_weight_pct: float | None
    hhi: float | None
    effective_holdings: float | None


@dataclass(slots=True)
class PositionCorrelationRow:
    """One holding's correlation + beta to each benchmark over the window
    (``GET /api/portfolio/positioning`` → ``correlations``). Each
    ``correlation_*`` / ``beta_*`` is None when the name lacks enough overlapping
    price history (see ``sample_size``). L5 stopped discarding these rows — they
    feed the book factor/style exposure roll-up."""

    security_id: int | None
    ticker: str | None
    name: str | None
    value: float | None
    weight_pct: float | None
    sample_size: int | None
    correlation_spy: float | None
    beta_spy: float | None
    correlation_qqq: float | None
    beta_qqq: float | None
    correlation_policy: float | None
    beta_policy: float | None


@dataclass(slots=True)
class Positioning:
    """``GET /api/portfolio/positioning`` — allocation cuts + concentration +
    the per-ticker correlation/beta table.

    ``weighted_avg_correlation_spy`` is the single book-level read; ``correlations``
    (L5) is the per-name table behind it, consumed by the factor/style roll-up.
    """

    snapshot_date: str | None
    total_value: float | None
    concentration: Concentration | None
    weighted_avg_correlation_spy: float | None
    has_policy: bool = False
    by_asset_type: list[AllocationBucket] = field(default_factory=list[AllocationBucket])
    by_sector: list[AllocationBucket] = field(default_factory=list[AllocationBucket])
    by_region: list[AllocationBucket] = field(default_factory=list[AllocationBucket])
    by_account_type: list[AllocationBucket] = field(default_factory=list[AllocationBucket])
    correlations: list[PositionCorrelationRow] = field(default_factory=list[PositionCorrelationRow])


@dataclass(slots=True)
class PolicyWeight:
    """One row of the owner's policy-portfolio target mix (percent units)."""

    ticker: str
    weight_pct: float | None
    notes: str | None


@dataclass(slots=True)
class PolicyMix:
    """``GET /api/policy`` — the policy benchmark's target weights."""

    total_pct: float | None
    is_balanced: bool
    weights: list[PolicyWeight] = field(default_factory=list[PolicyWeight])


@dataclass(slots=True)
class BetaStats:
    """``GET /api/portfolio/beta`` — regression + risk-adjusted stats vs one
    benchmark. ``alpha_annualized_pct`` is in percent; the volatility /
    tracking-error fields are FRACTIONS (0.18 = 18% annualized) per the
    tracker's units.

    The skill-vs-luck trio (``alpha_t_stat`` / ``alpha_std_error_annualized_pct``
    / ``alpha_significant``) answers "is the alpha distinguishable from zero?" —
    a t-stat on the regression intercept, its annualized standard error, and the
    tracker's significance verdict. ``alpha_significant`` is tri-state: ``None``
    when the tracker predates the trio (older build), else the bool it returned.
    """

    benchmark: str | None
    start_date: str | None
    end_date: str | None
    sample_size: int | None
    risk_free_annual: float | None
    beta: float | None
    alpha_annualized_pct: float | None
    alpha_t_stat: float | None
    alpha_std_error_annualized_pct: float | None
    alpha_significant: bool | None
    r_squared: float | None
    correlation: float | None
    sharpe: float | None
    sortino: float | None
    information_ratio: float | None
    portfolio_volatility_annualized: float | None
    benchmark_volatility_annualized: float | None
    tracking_error_annualized: float | None
    notes: list[str] = field(default_factory=list[str])


@dataclass(slots=True)
class UnderwaterPoint:
    """One day of the underwater curve: how far below the running peak the book
    sat (``drawdown_pct`` is negative or zero; 0 = at a new high)."""

    date: str
    drawdown_pct: float | None


@dataclass(slots=True)
class Drawdown:
    """``GET /api/portfolio/drawdown`` — peak-to-trough pain over the TWR series.

    ``max_drawdown_pct`` is the worst peak-to-trough decline in the window;
    ``current_drawdown_pct`` how far below the running peak the book sits today
    (0 = at a high). ``calmar`` is ``annualized_return_pct / |max_drawdown_pct|``
    — return per unit of worst-case pain. ``days_to_recovery`` is None while the
    book is still underwater from the max-DD trough (``recovery_date`` None too).
    Percent units throughout (matching the tracker's other percent fields)."""

    start_date: str | None
    end_date: str | None
    max_drawdown_pct: float | None
    peak_date: str | None
    trough_date: str | None
    recovery_date: str | None
    days_to_recovery: int | None
    current_drawdown_pct: float | None
    annualized_return_pct: float | None
    calmar: float | None
    underwater: list[UnderwaterPoint] = field(default_factory=list[UnderwaterPoint])


@dataclass(slots=True)
class ExitQualityRow:
    """One sold ticker's hold-counterfactual: did selling beat holding? The
    tracker computes ``value_if_held`` (the sold shares marked at today's price),
    ``regret_vs_hold`` (value_if_held − sold_proceeds; positive = selling cost
    you), and ``exit_alpha_vs_spy`` (proceeds reinvested in SPY vs holding —
    positive = the exit + redeploy beat just holding). ``still_held`` flags a
    name only partially sold (the position isn't fully closed)."""

    ticker: str | None
    name: str | None
    sold_shares: float | None
    sold_proceeds: float | None
    avg_sell_price: float | None
    price_now: float | None
    value_if_held: float | None
    regret_vs_hold: float | None
    spy_value_if_reinvested: float | None
    exit_alpha_vs_spy: float | None
    still_held: bool


@dataclass(slots=True)
class ExitQuality:
    """``GET /api/portfolio/exit-quality`` — the sell side: for every name sold
    in the window, whether the exit beat holding (and beat redeploying into SPY).
    The ``total_*`` fields are the book-level rollups the tracker returns."""

    start_date: str | None
    end_date: str | None
    total_sold_proceeds: float | None
    total_value_if_held: float | None
    total_regret_vs_hold: float | None
    total_spy_value_if_reinvested: float | None
    total_exit_alpha_vs_spy: float | None
    rows: list[ExitQualityRow] = field(default_factory=list[ExitQualityRow])
    # Provider envelope (consolidation PRD §8.1) — populated on the v1 path
    # (both via the analytics aggregate and the standalone fetch_exit_quality);
    # defaults keep legacy-path consumers unaffected. Warning CODES only.
    as_of: str | None = None
    is_stale: bool = False
    is_partial: bool = False
    envelope_warnings: list[str] = field(default_factory=list[str])


@dataclass(slots=True)
class AfterTaxTerm:
    """One holding-period bucket of the after-tax realized-gain breakdown
    (``term`` is 'short' / 'long'): the pre-tax realized gain in the bucket, the
    tax on it, and the after-tax remainder."""

    term: str
    realized_gain_pretax: float | None
    tax: float | None
    realized_gain_aftertax: float | None


@dataclass(slots=True)
class AfterTax:
    """``GET /api/portfolio/after-tax`` — realized gains for a tax year netted
    down by the supplied short/long rates. ``total_tax`` is the summed estimated
    tax; ``by_term`` splits pre-tax/tax/after-tax across short- vs long-term.
    The rates are the caller's assumptions, not the tracker's — it just applies
    them, so ``notes`` carries the tracker's caveats verbatim."""

    tax_year: int | None
    st_rate: float | None
    lt_rate: float | None
    realized_gain_pretax: float | None
    realized_gain_aftertax: float | None
    total_tax: float | None
    by_term: list[AfterTaxTerm] = field(default_factory=list[AfterTaxTerm])
    notes: list[str] = field(default_factory=list[str])


@dataclass(slots=True)
class PortfolioAnalytics:
    """Aggregate of the analytics payloads; each is None when its call failed,
    with the reason keyed under ``errors`` ("performance" / "position_alpha" /
    "positioning" / "policy" / "beta" / "drawdown" / "exit_quality").
    ``available`` is True when at least one section loaded — False means the
    tracker itself is unreachable (or returned nothing usable).

    ``drawdown`` and ``exit_quality`` are NOT in the default fetch set — they
    are opt-in via ``only=`` so the established advisor/portfolio reads keep
    their exact round-trip count; the risk cockpit requests them explicitly."""

    available: bool
    api_url: str
    errors: dict[str, str] = field(default_factory=dict[str, str])
    performance: PerformanceSeries | None = None
    position_alpha: PositionAlpha | None = None
    positioning: Positioning | None = None
    policy: PolicyMix | None = None
    beta: BetaStats | None = None
    drawdown: Drawdown | None = None
    exit_quality: ExitQuality | None = None
    # Provider envelope (consolidation PRD §8.1) — v1 transport path only;
    # defaults keep legacy-path consumers unaffected. ``as_of`` is the holdings
    # OBSERVATION date the analytics were computed over (a fresh calculation on
    # a week-old book reads stale); flags OR across the fetched sections.
    # ``envelope_warnings`` carries stable warning CODES only.
    as_of: str | None = None
    is_stale: bool = False
    is_partial: bool = False
    envelope_warnings: list[str] = field(default_factory=list[str])


def fetch_portfolio_analytics(
    *,
    api_url: str | None = None,
    timeout: float = _ANALYTICS_TIMEOUT_SECONDS,
    start_date: str | None = None,
    end_date: str | None = None,
    include_backfill: bool = False,
    only: set[str] | frozenset[str] | None = None,
) -> PortfolioAnalytics:
    """Fetch the five analytics payloads with per-endpoint fault isolation.

    Never raises on a tracker problem. A failed endpoint records its reason in
    ``errors`` while the others still load (so a tracker build that predates an
    endpoint degrades to a partial page, not a blank one). A ``ConnectionError``
    short-circuits the remaining calls — the host is down, retrying four more
    times only burns timeouts. ``api_url`` resolves like the live fetch:
    explicit arg → ``PORTFOLIO_TRACKER_API_URL`` → ``http://127.0.0.1:8000``.

    ``start_date`` / ``end_date`` (ISO ``YYYY-MM-DD``) scope the four windowed
    endpoints (``/api/policy`` is window-less); ``include_backfill`` extends
    ``/performance`` backward through the tracker's MODELED transaction
    walk-back (its docs caution the backfilled values are reconstructed, not
    observed). All omitted → the tracker's own defaults: a snapshot-derived
    window for ``/performance``, trailing 365 days elsewhere. The window is
    passed through verbatim — date arithmetic for presets lives with the UI,
    return math stays in the tracker.

    ``only`` restricts the fetch to the named sections (``performance`` /
    ``position_alpha`` / ``positioning`` / ``policy`` / ``beta`` / ``drawdown``
    / ``exit_quality``) — callers that need one payload (e.g. the sizing audit's
    alpha join) skip the other round-trips. Skipped sections stay ``None``
    without an ``errors`` entry. ``drawdown`` / ``exit_quality`` are opt-in: a
    bare ``only=None`` fetch loads only ``_DEFAULT_SECTIONS`` (the original five),
    so existing callers keep their round-trip count — request the two new ones
    by name.
    """
    base = _resolve_base(api_url)
    if _v1_reads_enabled():
        return _fetch_portfolio_analytics_v1(
            base,
            timeout=timeout,
            start_date=start_date,
            end_date=end_date,
            include_backfill=include_backfill,
            only=only,
        )
    out = PortfolioAnalytics(available=False, api_url=base)
    conn_down: str | None = None

    def want(key: str) -> bool:
        return key in only if only is not None else key in _DEFAULT_SECTIONS

    window: dict[str, str] = {}
    if start_date:
        window["start_date"] = start_date
    if end_date:
        window["end_date"] = end_date
    perf_params = dict(window)
    if include_backfill:
        perf_params["include_backfill"] = "true"

    def q(params: dict[str, str]) -> str:
        return f"?{urlencode(params)}" if params else ""

    def load(key: str, path: str, parse: Callable[[dict[str, object]], _T]) -> _T | None:
        nonlocal conn_down
        if conn_down is not None:
            out.errors[key] = conn_down
            return None
        try:
            return parse(_get_obj(base, path, timeout=timeout))
        except requests.ConnectionError as exc:
            conn_down = f"{type(exc).__name__}: {exc}"
            out.errors[key] = conn_down
        except requests.RequestException as exc:
            out.errors[key] = f"{type(exc).__name__}: {exc}"
        except ValueError as exc:  # JSON decode / unexpected shape
            out.errors[key] = f"bad response: {exc}"
        return None

    if want("performance"):
        out.performance = load(
            "performance", f"/api/portfolio/performance{q(perf_params)}", _parse_performance
        )
    if want("position_alpha"):
        out.position_alpha = load(
            "position_alpha", f"/api/portfolio/position-alpha{q(window)}", _parse_position_alpha
        )
    if want("positioning"):
        out.positioning = load(
            "positioning", f"/api/portfolio/positioning{q(window)}", _parse_positioning
        )
    if want("policy"):
        out.policy = load("policy", "/api/policy", _parse_policy)
    if want("beta"):
        out.beta = load("beta", f"/api/portfolio/beta{q(window)}", _parse_beta)
    if want("drawdown"):
        out.drawdown = load("drawdown", f"/api/portfolio/drawdown{q(window)}", _parse_drawdown)
    if want("exit_quality"):
        out.exit_quality = load(
            "exit_quality", f"/api/portfolio/exit-quality{q(window)}", _parse_exit_quality
        )
    out.available = any(
        section is not None
        for section in (
            out.performance,
            out.position_alpha,
            out.positioning,
            out.policy,
            out.beta,
            out.drawdown,
            out.exit_quality,
        )
    )
    return out


def _get_obj(base: str, path: str, *, timeout: float) -> dict[str, object]:
    """GET a JSON object (the analytics endpoints' shape; ``_get`` covers lists)."""
    resp = requests.get(base + path, timeout=(_CONNECT_TIMEOUT_SECONDS, timeout))
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError(f"{path} returned {type(data).__name__}, expected an object")
    return cast("dict[str, object]", data)


def _parse_performance(data: dict[str, object]) -> PerformanceSeries:
    points = [
        PerformancePoint(
            date=_s(p.get("date")) or "",
            portfolio_return_pct=_f(p.get("portfolio_return_pct")),
            spy_return_pct=_f(p.get("spy_return_pct")),
            qqq_return_pct=_f(p.get("qqq_return_pct")),
            policy_return_pct=_f(p.get("policy_return_pct")),
        )
        for p in _dicts(data.get("points"))
    ]
    return PerformanceSeries(
        start_date=_s(data.get("start_date")),
        end_date=_s(data.get("end_date")),
        base_value=_f(data.get("base_value")),
        net_external_cashflow_in=_f(data.get("net_external_cashflow_in")),
        backfill_start_unreliable=bool(data.get("backfill_start_unreliable")),
        earliest_observed_date=_s(data.get("earliest_observed_date")),
        points=points,
    )


def _parse_position_alpha(data: dict[str, object]) -> PositionAlpha:
    rows = [
        PositionAlphaRow(
            ticker=_s(r.get("ticker")),
            name=_s(r.get("name")),
            value_at_start=_f(r.get("value_at_start")),
            bought_in_window=_f(r.get("bought_in_window")),
            sold_in_window=_f(r.get("sold_in_window")),
            value_at_end=_f(r.get("value_at_end")),
            actual_pl=_f(r.get("actual_pl")),
            spy_counterfactual_pl=_f(r.get("spy_counterfactual_pl")),
            alpha=_f(r.get("alpha")),
            alpha_vs_qqq=_f(r.get("alpha_vs_qqq")),
            alpha_vs_policy=_f(r.get("alpha_vs_policy")),
            incomplete=bool(r.get("incomplete")),
        )
        for r in _dicts(data.get("rows"))
    ]
    return PositionAlpha(
        start_date=_s(data.get("start_date")),
        end_date=_s(data.get("end_date")),
        has_policy=bool(data.get("has_policy")),
        total_actual_pl=_f(data.get("total_actual_pl")),
        total_spy_pl=_f(data.get("total_spy_pl")),
        total_alpha=_f(data.get("total_alpha")),
        total_alpha_vs_qqq=_f(data.get("total_alpha_vs_qqq")),
        total_alpha_vs_policy=_f(data.get("total_alpha_vs_policy")),
        rows=rows,
    )


def _parse_buckets(v: object) -> list[AllocationBucket]:
    return [
        AllocationBucket(
            label=_s(b.get("label")) or "?",
            value=_f(b.get("value")),
            weight_pct=_f(b.get("weight_pct")),
            count=_i(b.get("count")),
        )
        for b in _dicts(v)
    ]


def _parse_positioning(data: dict[str, object]) -> Positioning:
    concentration: Concentration | None = None
    raw_conc = data.get("concentration")
    if isinstance(raw_conc, dict):
        c = cast("dict[str, object]", raw_conc)
        concentration = Concentration(
            num_positions=_i(c.get("num_positions")),
            top1_weight_pct=_f(c.get("top1_weight_pct")),
            top5_weight_pct=_f(c.get("top5_weight_pct")),
            top10_weight_pct=_f(c.get("top10_weight_pct")),
            hhi=_f(c.get("hhi")),
            effective_holdings=_f(c.get("effective_holdings")),
        )
    return Positioning(
        snapshot_date=_s(data.get("snapshot_date")),
        total_value=_f(data.get("total_value")),
        concentration=concentration,
        weighted_avg_correlation_spy=_f(data.get("weighted_avg_correlation_spy")),
        has_policy=bool(data.get("has_policy")),
        by_asset_type=_parse_buckets(data.get("by_asset_type")),
        by_sector=_parse_buckets(data.get("by_sector")),
        by_region=_parse_buckets(data.get("by_region")),
        by_account_type=_parse_buckets(data.get("by_account_type")),
        correlations=_parse_correlations(data.get("correlations")),
    )


def _parse_correlations(v: object) -> list[PositionCorrelationRow]:
    return [
        PositionCorrelationRow(
            security_id=_i(r.get("security_id")),
            ticker=_s(r.get("ticker")),
            name=_s(r.get("name")),
            value=_f(r.get("value")),
            weight_pct=_f(r.get("weight_pct")),
            sample_size=_i(r.get("sample_size")),
            correlation_spy=_f(r.get("correlation_spy")),
            beta_spy=_f(r.get("beta_spy")),
            correlation_qqq=_f(r.get("correlation_qqq")),
            beta_qqq=_f(r.get("beta_qqq")),
            correlation_policy=_f(r.get("correlation_policy")),
            beta_policy=_f(r.get("beta_policy")),
        )
        for r in _dicts(v)
    ]


def _parse_policy(data: dict[str, object]) -> PolicyMix:
    weights = [
        PolicyWeight(
            ticker=_s(w.get("ticker")) or "?",
            weight_pct=_f(w.get("weight_pct")),
            notes=_s(w.get("notes")),
        )
        for w in _dicts(data.get("weights"))
    ]
    return PolicyMix(
        total_pct=_f(data.get("total_pct")),
        is_balanced=bool(data.get("is_balanced")),
        weights=weights,
    )


def _parse_beta(data: dict[str, object]) -> BetaStats:
    raw_notes = data.get("notes")
    notes = (
        [n for n in cast("list[object]", raw_notes) if isinstance(n, str)]
        if isinstance(raw_notes, list)
        else []
    )
    return BetaStats(
        benchmark=_s(data.get("benchmark")),
        start_date=_s(data.get("start_date")),
        end_date=_s(data.get("end_date")),
        sample_size=_i(data.get("sample_size")),
        risk_free_annual=_f(data.get("risk_free_annual")),
        beta=_f(data.get("beta")),
        alpha_annualized_pct=_f(data.get("alpha_annualized_pct")),
        alpha_t_stat=_f(data.get("alpha_t_stat")),
        alpha_std_error_annualized_pct=_f(data.get("alpha_std_error_annualized_pct")),
        alpha_significant=_b(data.get("alpha_significant")),
        r_squared=_f(data.get("r_squared")),
        correlation=_f(data.get("correlation")),
        sharpe=_f(data.get("sharpe")),
        sortino=_f(data.get("sortino")),
        information_ratio=_f(data.get("information_ratio")),
        portfolio_volatility_annualized=_f(data.get("portfolio_volatility_annualized")),
        benchmark_volatility_annualized=_f(data.get("benchmark_volatility_annualized")),
        tracking_error_annualized=_f(data.get("tracking_error_annualized")),
        notes=notes,
    )


def _parse_drawdown(data: dict[str, object]) -> Drawdown:
    underwater = [
        UnderwaterPoint(date=_s(p.get("date")) or "", drawdown_pct=_f(p.get("drawdown_pct")))
        for p in _dicts(data.get("underwater"))
    ]
    return Drawdown(
        start_date=_s(data.get("start_date")),
        end_date=_s(data.get("end_date")),
        max_drawdown_pct=_f(data.get("max_drawdown_pct")),
        peak_date=_s(data.get("peak_date")),
        trough_date=_s(data.get("trough_date")),
        recovery_date=_s(data.get("recovery_date")),
        days_to_recovery=_i(data.get("days_to_recovery")),
        current_drawdown_pct=_f(data.get("current_drawdown_pct")),
        annualized_return_pct=_f(data.get("annualized_return_pct")),
        calmar=_f(data.get("calmar")),
        underwater=underwater,
    )


def _parse_exit_quality(data: dict[str, object]) -> ExitQuality:
    rows = [
        ExitQualityRow(
            ticker=_s(r.get("ticker")),
            name=_s(r.get("name")),
            sold_shares=_f(r.get("sold_shares")),
            sold_proceeds=_f(r.get("sold_proceeds")),
            avg_sell_price=_f(r.get("avg_sell_price")),
            price_now=_f(r.get("price_now")),
            value_if_held=_f(r.get("value_if_held")),
            regret_vs_hold=_f(r.get("regret_vs_hold")),
            spy_value_if_reinvested=_f(r.get("spy_value_if_reinvested")),
            exit_alpha_vs_spy=_f(r.get("exit_alpha_vs_spy")),
            still_held=bool(r.get("still_held")),
        )
        for r in _dicts(data.get("rows"))
    ]
    # Totals: prefer a nested ``totals`` object if the tracker nests them, else
    # read the top-level ``total_*`` keys (the PositionAlpha convention).
    raw_totals = data.get("totals")
    totals = cast("dict[str, object]", raw_totals) if isinstance(raw_totals, dict) else data
    return ExitQuality(
        start_date=_s(data.get("start_date")),
        end_date=_s(data.get("end_date")),
        total_sold_proceeds=_f(totals.get("total_sold_proceeds")),
        total_value_if_held=_f(totals.get("total_value_if_held")),
        total_regret_vs_hold=_f(totals.get("total_regret_vs_hold")),
        total_spy_value_if_reinvested=_f(totals.get("total_spy_value_if_reinvested")),
        total_exit_alpha_vs_spy=_f(totals.get("total_exit_alpha_vs_spy")),
        rows=rows,
    )


def _parse_after_tax(data: dict[str, object]) -> AfterTax:
    by_term = [
        AfterTaxTerm(
            term=_s(t.get("term")) or "?",
            realized_gain_pretax=_f(t.get("realized_gain_pretax")),
            tax=_f(t.get("tax")),
            realized_gain_aftertax=_f(t.get("realized_gain_aftertax")),
        )
        for t in _dicts(data.get("by_term"))
    ]
    raw_notes = data.get("notes")
    notes = (
        [n for n in cast("list[object]", raw_notes) if isinstance(n, str)]
        if isinstance(raw_notes, list)
        else []
    )
    return AfterTax(
        tax_year=_i(data.get("tax_year")),
        st_rate=_f(data.get("st_rate")),
        lt_rate=_f(data.get("lt_rate")),
        realized_gain_pretax=_f(data.get("realized_gain_pretax")),
        realized_gain_aftertax=_f(data.get("realized_gain_aftertax")),
        total_tax=_f(data.get("total_tax")),
        by_term=by_term,
        notes=notes,
    )


# Sections fetched on a bare ``only=None`` call — the original five, so existing
# callers keep their round-trip count. ``drawdown`` / ``exit_quality`` are opt-in.
_DEFAULT_SECTIONS: frozenset[str] = frozenset(
    {"performance", "position_alpha", "positioning", "policy", "beta"}
)


def _resolve_base(api_url: str | None) -> str:
    """Resolve the tracker base URL: explicit arg → env → loopback default."""
    return (api_url or os.environ.get("PORTFOLIO_TRACKER_API_URL") or _DEFAULT_API_URL).rstrip("/")


# One-shot liveness probe budget (wave B B4b). Deliberately tighter than the
# data timeouts: the probe exists so a DOWN tracker costs ONE ~1s round-trip
# instead of a serial walk of every data GET's failure path.
_PROBE_TIMEOUT_SECONDS = 1.0


def probe_tracker(
    api_url: str | None = None, *, timeout: float = _PROBE_TIMEOUT_SECONDS
) -> tuple[bool, str]:
    """ONE cheap liveness probe: ``(alive, resolved_base_url)``.

    Composite pages that make several serial tracker GETs call this first and
    skip the whole data walk when the host is down, rendering the offline
    banner immediately. ANY HTTP response (even a 404 on ``/``) means the
    server is up — only a transport-level failure (refused connect, timeout)
    reads as down. Never raises. The data fetchers' own timeouts are untouched.
    """
    base = _resolve_base(api_url)
    try:
        requests.get(f"{base}/", timeout=(min(_CONNECT_TIMEOUT_SECONDS, timeout), timeout))
    except requests.RequestException:
        return (False, base)
    return (True, base)


def _fetch_section(
    path: str,
    parse: Callable[[dict[str, object]], _T],
    *,
    api_url: str | None,
    timeout: float,
) -> _T | None:
    """GET + parse one analytics object with the never-raise contract — None on
    any tracker problem (offline, HTTP error, malformed JSON). Used by the
    single-section convenience fetchers below; the aggregate has its own
    per-section fault isolation."""
    try:
        return parse(_get_obj(_resolve_base(api_url), path, timeout=timeout))
    except (requests.RequestException, ValueError):
        return None


def fetch_drawdown(
    *,
    api_url: str | None = None,
    timeout: float = _ANALYTICS_TIMEOUT_SECONDS,
    start_date: str | None = None,
    end_date: str | None = None,
) -> Drawdown | None:
    """``GET /api/portfolio/drawdown`` — max drawdown + underwater curve + Calmar.
    None when the tracker can't answer (offline / predates the endpoint)."""
    window: dict[str, str] = {}
    if start_date:
        window["start_date"] = start_date
    if end_date:
        window["end_date"] = end_date
    suffix = f"?{urlencode(window)}" if window else ""
    return _fetch_section(
        f"/api/portfolio/drawdown{suffix}", _parse_drawdown, api_url=api_url, timeout=timeout
    )


def fetch_exit_quality(
    *,
    api_url: str | None = None,
    timeout: float = _ANALYTICS_TIMEOUT_SECONDS,
    start_date: str | None = None,
    end_date: str | None = None,
) -> ExitQuality | None:
    """``GET /api/portfolio/exit-quality`` — the sell side: did each exit beat
    holding? None when the tracker can't answer."""
    if _v1_reads_enabled():
        return _fetch_exit_quality_v1(
            _resolve_base(api_url), timeout=timeout, start_date=start_date, end_date=end_date
        )
    window: dict[str, str] = {}
    if start_date:
        window["start_date"] = start_date
    if end_date:
        window["end_date"] = end_date
    suffix = f"?{urlencode(window)}" if window else ""
    return _fetch_section(
        f"/api/portfolio/exit-quality{suffix}",
        _parse_exit_quality,
        api_url=api_url,
        timeout=timeout,
    )


def fetch_after_tax(
    *,
    tax_year: int | None = None,
    st_rate: float | None = None,
    lt_rate: float | None = None,
    api_url: str | None = None,
    timeout: float = _ANALYTICS_TIMEOUT_SECONDS,
) -> AfterTax | None:
    """``GET /api/portfolio/after-tax`` — realized gains netted by the supplied
    short/long rates. Orthogonal params (a tax-year, not a TWR window) so it has
    its own fetcher rather than riding the windowed aggregate. None when the
    tracker can't answer."""
    params: dict[str, str] = {}
    if tax_year is not None:
        params["tax_year"] = str(int(tax_year))
    if st_rate is not None:
        params["st_rate"] = str(st_rate)
    if lt_rate is not None:
        params["lt_rate"] = str(lt_rate)
    suffix = f"?{urlencode(params)}" if params else ""
    return _fetch_section(
        f"/api/portfolio/after-tax{suffix}", _parse_after_tax, api_url=api_url, timeout=timeout
    )


# ---------------------------------------------------------------------------
# v1 transport (consolidation PRD §12 Phase 2 — consumer adoption)
#
# The four public facades route here when PORTFOLIO_TRACKER_V1_READS=1. The
# typed client owns validation, the fail-closed major-version gate, cursor
# pagination, and envelope extraction; these adapters only convert the typed
# models back to the legacy dataclass shapes (Decimal -> float at this one
# boundary) so no consumer changes. Analytics adapters funnel the typed
# model's inner section back through the SAME legacy parsers via
# ``model_dump(mode="json")`` — one shape-truth per section, and the v1 inner
# payloads are contractually parity-shaped with the legacy endpoints (tracker
# PR #52 fixtures). A broken v1 read degrades with the v1 reason; it never
# falls back to legacy endpoints.
# ---------------------------------------------------------------------------

_V1_READS_ENV = "PORTFOLIO_TRACKER_V1_READS"

# Ratified five-way tax treatment (SC-1) -> this module's coarse legacy
# buckets (positions-v1.md: consumers needing the old buckets map
# pretax->tax_deferred and roth+hsa->tax_free).
_COARSE_TAX: dict[str, str] = {
    "taxable": "taxable",
    "pretax": "tax_deferred",
    "roth": "tax_free",
    "hsa": "tax_free",
    "unknown": "unknown",
}


def _v1_reads_enabled() -> bool:
    """True when the owner has switched reads onto the ``/api/v1`` transport."""
    return os.environ.get(_V1_READS_ENV, "").strip().lower() in {"1", "true"}


def _opt_float(v: Decimal | None) -> float | None:
    return None if v is None else float(v)


def _dump_model(m: BaseModel) -> dict[str, object]:
    """JSON-mode dump of a typed v1 model, fed back through the legacy parsers
    (dates -> ISO strings, Decimals -> strings ``_f`` coerces). The model
    already validated shape and units; this keeps the legacy dataclass mapping
    in ONE place per section instead of a parallel field-by-field adapter."""
    return cast("dict[str, object]", m.model_dump(mode="json"))


def _envelope_codes(meta: V1Meta | None) -> list[str]:
    return [w.code for w in meta.warnings] if meta is not None else []


def _fetch_live_portfolio_v1(
    base: str, *, timeout: float, transactions_limit: int
) -> LivePortfolio:
    """v1 transport for :func:`fetch_live_portfolio`.

    ``/api/v1/portfolio/positions`` is contractually envelope-less, so
    ``as_of`` comes from its ``snapshot_date`` and the staleness flags ride
    the transactions read's envelope (same book, same observation cycle) —
    no extra round-trip. Both reads must succeed, matching the legacy
    all-or-nothing fetch."""
    client = TrackerV1Client(base_url=base, read_timeout=timeout)
    pos = client.get_positions()
    if not pos.available or pos.data is None:
        return LivePortfolio(available=False, api_url=base, error=f"v1 positions: {pos.error}")
    txns = client.get_transactions_page(limit=max(1, int(transactions_limit)))
    if not txns.available or txns.data is None:
        return LivePortfolio(available=False, api_url=base, error=f"v1 transactions: {txns.error}")

    positions: list[LivePosition] = []
    for p in pos.data.positions:
        lots = [
            TaxLot(
                account_id=lot.account_id,
                account_name=lot.account_name,
                quantity=float(lot.quantity),
                market_value=_opt_float(lot.market_value),
                tax_treatment=_COARSE_TAX.get(lot.tax_treatment, "unknown"),
                cost_basis=_opt_float(lot.cost_basis),
            )
            for lot in p.accounts
        ]
        positions.append(
            LivePosition(
                ticker=p.ticker,
                name=p.name,
                quantity=float(p.quantity),
                market_value=_opt_float(p.market_value),
                cost_basis=_opt_float(p.cost_basis),
                unrealized_pnl=_opt_float(p.unrealized_pnl),
                # Server-derived on v1 (percent 0-100, same convention).
                percent_of_portfolio=_opt_float(p.percent_of_portfolio),
                accounts=lots,
            )
        )
    by_tax = {bucket: 0.0 for bucket in TAX_BUCKETS}
    for pos_row in positions:
        for lot in pos_row.accounts:
            by_tax[lot.tax_treatment] = by_tax.get(lot.tax_treatment, 0.0) + (
                lot.market_value or 0.0
            )
    meta = txns.meta
    as_of = (
        pos.data.snapshot_date.isoformat()
        if pos.data.snapshot_date is not None
        else (meta.as_of.isoformat() if meta is not None and meta.as_of is not None else None)
    )
    return LivePortfolio(
        available=True,
        api_url=base,
        total_market_value=float(pos.data.total_market_value),
        positions=positions,
        transactions=_live_transactions_from_v1(txns.data.transactions),
        by_tax_treatment=by_tax,
        as_of=as_of,
        is_stale=meta.is_stale if meta is not None else False,
        is_partial=meta.is_partial if meta is not None else False,
        envelope_warnings=_envelope_codes(meta),
    )


def _live_transactions_from_v1(txns: Sequence[BaseModel]) -> list[LiveTransaction]:
    out: list[LiveTransaction] = []
    for t in txns:
        d = _dump_model(t)
        out.append(
            LiveTransaction(
                date=_s(d.get("date")) or "",
                ticker=_s(d.get("ticker")),
                name=_s(d.get("name")),
                type=_s(d.get("type")) or "",
                subtype=_s(d.get("subtype")),
                quantity=_f(d.get("quantity")),
                amount=_f(d.get("amount")),
                account_name=_s(d.get("account_name")) or "?",
                account_id=_i(d.get("account_id")),
                price=_f(d.get("price")),
                fees=_f(d.get("fees")),
            )
        )
    return out


def _fetch_transaction_history_v1(
    base: str, *, timeout: float, start_date: str
) -> list[LiveTransaction] | None:
    """v1 transport for :func:`fetch_transaction_history`: cursor pagination
    replaces the legacy 5,000-row single-call cap, so the v1 read returns the
    FULL window (a superset of the legacy read — strictly better for FIFO lot
    reconstruction; the legacy "len == limit means truncated" caveat no longer
    triggers). ``None`` on any v1 problem, same as legacy."""
    client = TrackerV1Client(base_url=base, read_timeout=timeout)
    res = client.get_all_transactions(start_date=start_date)
    if not res.available or res.data is None:
        return None
    return _live_transactions_from_v1(res.data)


def _merge_v1_envelope(out: PortfolioAnalytics, metas: list[V1Meta]) -> None:
    """Aggregate per-section envelopes onto the analytics result: earliest
    ``as_of`` (most conservative), flags OR'd, warning codes unioned in
    first-seen order."""
    dates = [m.as_of for m in metas if m.as_of is not None]
    out.as_of = min(dates).isoformat() if dates else None
    out.is_stale = any(m.is_stale for m in metas)
    out.is_partial = any(m.is_partial for m in metas)
    seen: list[str] = []
    for m in metas:
        for w in m.warnings:
            if w.code not in seen:
                seen.append(w.code)
    out.envelope_warnings = seen


_V1_WIDE_HISTORY_START = "2000-01-01"

# Client-side envelope code (not a provider warning): the performance series
# carried no ``earliest_observed_date``, so the observed-window rebase could not
# run and the returned series may include the provider's MODELED walk-back. Read
# it as "these returns may not be observation-based" — a log line alone would
# not reach a rendering surface, and PRD §13.3 forbids presenting a partial or
# reconstructed read as current.
#
# Relationship to the risk-snapshot ``rebase_basis`` stamp (migration 0199), which
# keys off the same provider field: the implication runs ONE WAY ONLY.
#
#     this code emitted  =>  that capture's rebase_basis == "unknown"     (holds)
#     rebase_basis "unknown"  =>  this code emitted                   (does NOT)
#
# The stamp is the broader condition — it also resolves "unknown" when the series
# arrives with no ``start_date`` at all (a degraded payload that still parses),
# which is not what this code guards. And this code is deliberately scoped to the
# rebase path, so an explicit caller window or ``include_backfill`` suppresses it
# while the stamp may still say "unknown". Any alerting built on the pair must
# test the forward implication only; the equivalence cries wolf.
_UNMARKED_OBSERVATION_CODE = "performance_observation_start_unmarked"


def _get_performance_v1_observed(
    client: TrackerV1Client,
    *,
    start_date: str | None,
    end_date: str | None,
    include_backfill: bool,
) -> V1Fetch[PerformanceV1Result]:
    """Request ``analytics/performance`` over the OBSERVED window, matching the
    legacy transport's default.

    The two transports default differently, and the difference rebases every
    return in the series rather than just trimming its head. Legacy
    ``/api/portfolio/performance`` defaults to the snapshot-derived window
    (first observed snapshot → today). v1 defaults to trailing 365 days and
    fills the pre-observation span with the provider's MODELED transaction
    walk-back, so its ``base_value`` is a reconstructed figure from a year ago.
    Measured live 2026-07-24: legacy 73 points from 2026-05-09 at +0.0664%,
    v1 default 362 points from 2025-07-24 at +8.62% — same book, same day.

    ``earliest_observed_date`` on the series is the provider's own marker for
    where observation begins, so re-requesting from it yields the observed-only
    window (verified identical to legacy: 73 points, same ``base_value``, same
    return to full precision). That marker is WINDOW-RELATIVE — it reports the
    earliest observation *inside* the requested window — hence the widen step
    below rather than a single cheap probe.

    Call cost: 1 request when the caller passes ``start_date`` or asks for the
    walk-back explicitly, 2 in the normal default case, 3 only when observation
    predates the probe's trailing-365d window (measured 2.74s / 2.5MB for the
    widened discovery read).
    """
    if start_date is not None:
        # An explicit caller window is authoritative under both transports.
        return client.get_performance(
            start_date=start_date, end_date=end_date, include_backfill=include_backfill
        )
    if include_backfill:
        # The caller asked for the modeled walk-back, which is precisely what
        # the wide default window provides; no rebase wanted.
        return client.get_performance(end_date=end_date, include_backfill=True)

    probe = client.get_performance(end_date=end_date)
    if not probe.available or probe.data is None:
        return probe
    observed = probe.data.series.earliest_observed_date
    if observed is None:
        # Provider declined to mark the observation start, so there is nothing
        # to rebase onto and the probe window stands. That window is the
        # trailing-365d default, which INCLUDES the modeled walk-back — so this
        # branch hands back reconstructed returns dressed as ordinary ones. The
        # field is Optional on the v1 model, so a provider-side rename or
        # dropped field lands here and validates cleanly: log it and let the
        # caller mark it (see _UNMARKED_OBSERVATION_CODE) rather than degrading
        # in silence.
        log.info(
            {
                "event": "tracker_v1_performance_observation_start_unmarked",
                "probe_window_start": probe.data.series.start_date.isoformat(),
            }
        )
        return probe
    if observed > probe.data.series.start_date:
        return client.get_performance(start_date=observed.isoformat(), end_date=end_date)

    # Observation starts at (or before) the probe window's own start, so the
    # trailing-365d probe may be clipping real history. Widen once to let the
    # provider report the true observation start, then rebase onto it.
    wide = client.get_performance(start_date=_V1_WIDE_HISTORY_START, end_date=end_date)
    if not wide.available or wide.data is None:
        return probe
    wide_observed = wide.data.series.earliest_observed_date
    if wide_observed is None or wide_observed <= wide.data.series.start_date:
        # History reaches back past _V1_WIDE_HISTORY_START — beyond anything
        # this book should contain. Return the widened read as-is rather than
        # inventing a start date, and say so.
        log.info(
            {
                "event": "tracker_v1_performance_history_predates_wide_start",
                "wide_start": _V1_WIDE_HISTORY_START,
            }
        )
        return wide
    if wide_observed == probe.data.series.start_date:
        return probe
    return client.get_performance(start_date=wide_observed.isoformat(), end_date=end_date)


def _fetch_portfolio_analytics_v1(
    base: str,
    *,
    timeout: float,
    start_date: str | None,
    end_date: str | None,
    include_backfill: bool,
    only: set[str] | frozenset[str] | None,
) -> PortfolioAnalytics:
    """v1 transport for :func:`fetch_portfolio_analytics`. Same per-section
    fault isolation and ``only=`` semantics; ``beta`` and ``drawdown`` share
    the single ``analytics/risk`` read. The ``policy`` section stays on the
    legacy ``/api/policy`` endpoint under either transport — it has no v1
    successor (provider-owned calculation config, Phase-0 ruling PT-6);
    documented shared-contract gap, not a silent fallback."""
    # The legacy 6s analytics budget models per-endpoint cost; v1 bundles more
    # work per call (risk = beta+drawdown, position-performance carries the
    # counterfactual series) and measured >6s live. Floor the read budget at
    # the v1 transport's own default; a caller-passed LARGER timeout still wins.
    client = TrackerV1Client(base_url=base, analytics_read_timeout=max(timeout, 20.0))
    out = PortfolioAnalytics(available=False, api_url=base)
    metas: list[V1Meta] = []

    def want(key: str) -> bool:
        return key in only if only is not None else key in _DEFAULT_SECTIONS

    def note_meta(meta: V1Meta | None) -> None:
        if meta is not None:
            metas.append(meta)

    observation_unmarked = False
    if want("performance"):
        perf = _get_performance_v1_observed(
            client, start_date=start_date, end_date=end_date, include_backfill=include_backfill
        )
        if perf.available and perf.data is not None:
            out.performance = _parse_performance(_dump_model(perf.data.series))
            note_meta(perf.meta)
            # Only meaningful when the caller took the rebase path: an explicit
            # window or include_backfill is the caller OWNING the window, not
            # the client failing to establish one.
            observation_unmarked = (
                start_date is None
                and not include_backfill
                and perf.data.series.earliest_observed_date is None
            )
        else:
            out.errors["performance"] = f"v1: {perf.error}"
    if want("position_alpha"):
        alpha = client.get_position_performance(start_date=start_date, end_date=end_date)
        if alpha.available and alpha.data is not None:
            out.position_alpha = _parse_position_alpha(_dump_model(alpha.data.result))
            note_meta(alpha.meta)
        else:
            out.errors["position_alpha"] = f"v1: {alpha.error}"
    if want("positioning"):
        positioning = client.get_positioning(start_date=start_date, end_date=end_date)
        if positioning.available and positioning.data is not None:
            out.positioning = _parse_positioning(_dump_model(positioning.data.positioning))
            note_meta(positioning.meta)
        else:
            out.errors["positioning"] = f"v1: {positioning.error}"
    if want("policy"):
        try:
            out.policy = _parse_policy(_get_obj(base, "/api/policy", timeout=timeout))
        except requests.RequestException as exc:
            out.errors["policy"] = f"{type(exc).__name__}: {exc}"
        except ValueError as exc:
            out.errors["policy"] = f"bad response: {exc}"
    if want("beta") or want("drawdown"):
        risk = client.get_risk(start_date=start_date, end_date=end_date)
        if risk.available and risk.data is not None:
            if want("beta"):
                out.beta = _parse_beta(_dump_model(risk.data.beta))
            if want("drawdown"):
                out.drawdown = _parse_drawdown(_dump_model(risk.data.drawdown))
            note_meta(risk.meta)
        else:
            if want("beta"):
                out.errors["beta"] = f"v1: {risk.error}"
            if want("drawdown"):
                out.errors["drawdown"] = f"v1: {risk.error}"
    if want("exit_quality"):
        exit_q = client.get_exit_quality(start_date=start_date, end_date=end_date)
        if exit_q.available and exit_q.data is not None:
            out.exit_quality = _parse_exit_quality(_dump_model(exit_q.data.result))
            note_meta(exit_q.meta)
        else:
            out.errors["exit_quality"] = f"v1: {exit_q.error}"

    out.available = any(
        section is not None
        for section in (
            out.performance,
            out.position_alpha,
            out.positioning,
            out.policy,
            out.beta,
            out.drawdown,
            out.exit_quality,
        )
    )
    _merge_v1_envelope(out, metas)
    # Appended AFTER the merge, which assigns envelope_warnings wholesale from
    # provider metas and would otherwise drop a client-side code.
    if observation_unmarked and _UNMARKED_OBSERVATION_CODE not in out.envelope_warnings:
        out.envelope_warnings.append(_UNMARKED_OBSERVATION_CODE)
    return out


def _fetch_exit_quality_v1(
    base: str, *, timeout: float, start_date: str | None, end_date: str | None
) -> ExitQuality | None:
    """v1 transport for the standalone :func:`fetch_exit_quality`."""
    # Same v1 analytics read-budget floor as _fetch_portfolio_analytics_v1.
    client = TrackerV1Client(base_url=base, analytics_read_timeout=max(timeout, 20.0))
    res = client.get_exit_quality(start_date=start_date, end_date=end_date)
    if not res.available or res.data is None:
        return None
    parsed = _parse_exit_quality(_dump_model(res.data.result))
    meta = res.meta
    if meta is not None:
        parsed.as_of = meta.as_of.isoformat() if meta.as_of is not None else None
        parsed.is_stale = meta.is_stale
        parsed.is_partial = meta.is_partial
        parsed.envelope_warnings = _envelope_codes(meta)
    return parsed
