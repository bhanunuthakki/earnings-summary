"""REST client for the companion portfolio-tracker FastAPI.

Pulls live positions / transactions / accounts from the sibling project's API
(default ``http://localhost:8000``) and derives the two things the tracker's raw
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
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar, cast
from urllib.parse import urlencode

import requests

_DEFAULT_API_URL = "http://localhost:8000"
_TIMEOUT_SECONDS = 4.0
# The analytics endpoints recompute TWR/alpha/beta over a year of dailies on
# request — give them more headroom than the instant holdings reads.
_ANALYTICS_TIMEOUT_SECONDS = 6.0

_T = TypeVar("_T")

# Tax-treatment buckets, in display order.
TAX_BUCKETS: tuple[str, ...] = ("taxable", "tax_deferred", "tax_free", "unknown")


@dataclass(slots=True)
class TaxLot:
    """One account's slice of a position, tagged with its tax treatment."""

    account_id: int
    account_name: str
    quantity: float
    market_value: float | None
    tax_treatment: str


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


def fetch_live_portfolio(
    *,
    api_url: str | None = None,
    timeout: float = _TIMEOUT_SECONDS,
    transactions_limit: int = 25,
) -> LivePortfolio:
    """Fetch + consolidate the live portfolio from the tracker REST API.

    Never raises on a tracker problem — returns ``available=False`` with an
    ``error`` reason so callers can degrade. ``api_url`` falls back to the
    ``PORTFOLIO_TRACKER_API_URL`` env var, then ``http://localhost:8000``.
    """
    base = (api_url or os.environ.get("PORTFOLIO_TRACKER_API_URL") or _DEFAULT_API_URL).rstrip("/")
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
    resp = requests.get(base + path, timeout=timeout)
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
                lots.append(
                    TaxLot(
                        account_id=aid_int,
                        account_name=_s(acct.get("account_name")) or "?",
                        quantity=_f(acct.get("quantity")) or 0.0,
                        market_value=_f(acct.get("institution_value")),
                        tax_treatment=acct_tax.get(aid_int, "unknown"),
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
        )
        for t in txns
    ]


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
class Positioning:
    """``GET /api/portfolio/positioning`` — allocation cuts + concentration.

    The endpoint also returns per-ticker correlation/beta rows; v1 consumes only
    the book-level ``weighted_avg_correlation_spy`` single-number read.
    """

    snapshot_date: str | None
    total_value: float | None
    concentration: Concentration | None
    weighted_avg_correlation_spy: float | None
    by_asset_type: list[AllocationBucket] = field(default_factory=list[AllocationBucket])
    by_sector: list[AllocationBucket] = field(default_factory=list[AllocationBucket])
    by_region: list[AllocationBucket] = field(default_factory=list[AllocationBucket])
    by_account_type: list[AllocationBucket] = field(default_factory=list[AllocationBucket])


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
    tracker's units."""

    benchmark: str | None
    start_date: str | None
    end_date: str | None
    sample_size: int | None
    risk_free_annual: float | None
    beta: float | None
    alpha_annualized_pct: float | None
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
class PortfolioAnalytics:
    """Aggregate of the five analytics payloads; each is None when its call
    failed, with the reason keyed under ``errors`` ("performance" /
    "position_alpha" / "positioning" / "policy" / "beta"). ``available`` is True
    when at least one section loaded — False means the tracker itself is
    unreachable (or returned nothing usable)."""

    available: bool
    api_url: str
    errors: dict[str, str] = field(default_factory=dict[str, str])
    performance: PerformanceSeries | None = None
    position_alpha: PositionAlpha | None = None
    positioning: Positioning | None = None
    policy: PolicyMix | None = None
    beta: BetaStats | None = None


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
    explicit arg → ``PORTFOLIO_TRACKER_API_URL`` → ``http://localhost:8000``.

    ``start_date`` / ``end_date`` (ISO ``YYYY-MM-DD``) scope the four windowed
    endpoints (``/api/policy`` is window-less); ``include_backfill`` extends
    ``/performance`` backward through the tracker's MODELED transaction
    walk-back (its docs caution the backfilled values are reconstructed, not
    observed). All omitted → the tracker's own defaults: a snapshot-derived
    window for ``/performance``, trailing 365 days elsewhere. The window is
    passed through verbatim — date arithmetic for presets lives with the UI,
    return math stays in the tracker.

    ``only`` restricts the fetch to the named sections (``performance`` /
    ``position_alpha`` / ``positioning`` / ``policy`` / ``beta``) — callers
    that need one payload (e.g. the sizing audit's alpha join) skip the other
    round-trips. Skipped sections stay ``None`` without an ``errors`` entry.
    """
    base = (api_url or os.environ.get("PORTFOLIO_TRACKER_API_URL") or _DEFAULT_API_URL).rstrip("/")
    out = PortfolioAnalytics(available=False, api_url=base)
    conn_down: str | None = None

    def want(key: str) -> bool:
        return only is None or key in only

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
    out.available = any(
        section is not None
        for section in (out.performance, out.position_alpha, out.positioning, out.policy, out.beta)
    )
    return out


def _get_obj(base: str, path: str, *, timeout: float) -> dict[str, object]:
    """GET a JSON object (the analytics endpoints' shape; ``_get`` covers lists)."""
    resp = requests.get(base + path, timeout=timeout)
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
        by_asset_type=_parse_buckets(data.get("by_asset_type")),
        by_sector=_parse_buckets(data.get("by_sector")),
        by_region=_parse_buckets(data.get("by_region")),
        by_account_type=_parse_buckets(data.get("by_account_type")),
    )


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
