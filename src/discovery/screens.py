"""Factor screens over the tracked universe (master build P5.3).

Deterministic and offline: the screening universe is
``tracked_companies WHERE list_type='index_member'`` (~2,340 S&P 500 +
Russell 2000 names) and every input comes from the LOCAL FMP caches under
``data/historical/fmp/`` — ``{T}_profile.json`` (sector/industry/mcap),
``{T}_key_metrics_quarterly.json`` (ROIC/ROE/FCF-yield/ND-EBITDA, ~25
quarters), ``{T}_income_statement_quarterly.json`` (revenue/margin
series). No network, no LLM; a name with no cache simply can't screen in
(coverage today is ~97% of the universe).

Convention notes baked into the math:
  * FMP stable-API ratio fields are FRACTIONS PER QUARTER (NU ROE 0.069 =
    6.9% for the quarter) — TTM-ize by summing the last 4 quarters for
    return/yield ratios; netDebtToEBITDA divides by ONE quarter's EBITDA,
    so /4 approximates the TTM multiple.
  * Revenue YoY compares the latest quarter against the same quarter a
    year earlier from the income-statement series (date-sorted, gap-safe).
  * A missing metric fails the conditions that need it — EXCEPT
    net-debt/EBITDA, which is skipped when absent or non-positive-EBITDA
    (banks/financials, where the multiple is meaningless).
  * Index lists carry GHOSTS — the prod dry-run surfaced 61 delisted
    names (Mentor Graphics, Apollo Education, ...) whose frozen caches
    happily pass value screens on years-old numbers. Two gates: the
    profile's ``isActivelyTrading`` flag (absent = assumed trading), and
    a freshness cut — the latest income-statement quarter must be within
    ``max_staleness_days`` of the run date.

Each screen returns the actual numbers as its evidence detail, so a
candidate explains itself in the queue.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import cast

from identity import DEFAULT_USER_ID

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TickerMetrics:
    """The screenable bundle for one ticker — None where the cache lacks it."""

    ticker: str
    name: str | None
    sector: str | None
    industry: str | None
    market_cap: float | None
    roic_ttm: float | None
    fcf_yield_ttm: float | None
    nd_to_ebitda_ttm: float | None
    rev_yoy: float | None
    rev_yoy_prior: float | None  # the YoY print 4 quarters earlier (acceleration base)
    gross_margin_ttm: float | None
    op_margin_ttm: float | None
    is_actively_trading: bool  # profile flag; absent = assumed True
    latest_income_date: str | None  # ISO date of the newest income quarter


@dataclass(slots=True)
class ScreenHit:
    """One (ticker, screen) pass with its self-explaining detail line."""

    ticker: str
    name: str | None
    screen: str
    detail: str


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _screen_quality_compounder(m: TickerMetrics) -> str | None:
    """High-return grower: ROIC >= 15% TTM, revenue YoY >= 10%, operating
    margin >= 10%, net debt <= 2.5x TTM EBITDA (skipped when meaningless)."""
    if m.roic_ttm is None or m.roic_ttm < 0.15:
        return None
    if m.rev_yoy is None or m.rev_yoy < 0.10:
        return None
    if m.op_margin_ttm is None or m.op_margin_ttm < 0.10:
        return None
    if m.nd_to_ebitda_ttm is not None and m.nd_to_ebitda_ttm > 2.5:
        return None
    nd = "n/a" if m.nd_to_ebitda_ttm is None else f"{m.nd_to_ebitda_ttm:.1f}x"
    return (
        f"ROIC {_pct(m.roic_ttm)} TTM, rev YoY {_pct(m.rev_yoy)}, "
        f"op margin {_pct(m.op_margin_ttm)}, ND/EBITDA {nd}"
    )


def _screen_fcf_value(m: TickerMetrics) -> str | None:
    """Cash-generative value: FCF yield >= 5% TTM with ROIC >= 8%, revenue
    not shrinking, market cap >= $2B (liquidity floor)."""
    if m.fcf_yield_ttm is None or m.fcf_yield_ttm < 0.05:
        return None
    if m.roic_ttm is None or m.roic_ttm < 0.08:
        return None
    if m.rev_yoy is None or m.rev_yoy < 0.0:
        return None
    if m.market_cap is None or m.market_cap < 2e9:
        return None
    return (
        f"FCF yield {_pct(m.fcf_yield_ttm)} TTM, ROIC {_pct(m.roic_ttm)}, "
        f"rev YoY {_pct(m.rev_yoy)}, mcap ${m.market_cap / 1e9:.1f}B"
    )


def _screen_growth_inflection(m: TickerMetrics) -> str | None:
    """Accelerating high-gross-margin growth: revenue YoY >= 15% AND at
    least 5pp faster than a year ago, gross margin >= 40%."""
    if m.rev_yoy is None or m.rev_yoy < 0.15:
        return None
    if m.rev_yoy_prior is None or (m.rev_yoy - m.rev_yoy_prior) < 0.05:
        return None
    if m.gross_margin_ttm is None or m.gross_margin_ttm < 0.40:
        return None
    accel = (m.rev_yoy - m.rev_yoy_prior) * 100
    return (
        f"rev YoY {_pct(m.rev_yoy)} (accelerating +{accel:.1f}pp vs a year ago), "
        f"gross margin {_pct(m.gross_margin_ttm)}"
    )


ScreenFn = Callable[[TickerMetrics], "str | None"]

SCREENS: dict[str, ScreenFn] = {
    "quality_compounder": _screen_quality_compounder,
    "fcf_value": _screen_fcf_value,
    "growth_inflection": _screen_growth_inflection,
}


# ---------------------------------------------------------------------------
# Cache loading
# ---------------------------------------------------------------------------


def _load_records(fmp_dir: Path, ticker: str, suffix: str) -> list[dict[str, object]]:
    """One cache file as a date-DESC-sorted record list; [] when absent/bad."""
    path = fmp_dir / f"{ticker.upper()}_{suffix}.json"
    if not path.exists():
        return []
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    records = [
        cast("dict[str, object]", r) for r in cast("list[object]", raw) if isinstance(r, dict)
    ]
    records.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    return records


def _num(record: dict[str, object], key: str) -> float | None:
    v = record.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _sum_last4(records: list[dict[str, object]], key: str) -> float | None:
    """TTM-ize a per-quarter fraction by summing the latest 4 quarters; None
    unless all 4 are present (a partial sum understates silently)."""
    vals = [_num(r, key) for r in records[:4]]
    if len(vals) < 4 or any(v is None for v in vals):
        return None
    return sum(cast("list[float]", vals))


def _rev_yoy_at(records: list[dict[str, object]], idx: int) -> float | None:
    """YoY revenue growth for the record at ``idx`` vs 4 quarters earlier."""
    if len(records) <= idx + 4:
        return None
    curr = _num(records[idx], "revenue")
    base = _num(records[idx + 4], "revenue")
    if curr is None or base is None or base <= 0:
        return None
    return curr / base - 1


def _ttm_margin(records: list[dict[str, object]], num_key: str) -> float | None:
    """TTM numerator / TTM revenue over the latest 4 quarters."""
    if len(records) < 4:
        return None
    num = 0.0
    rev = 0.0
    for r in records[:4]:
        n = _num(r, num_key)
        v = _num(r, "revenue")
        if n is None or v is None:
            return None
        num += n
        rev += v
    if rev <= 0:
        return None
    return num / rev


def load_ticker_metrics(fmp_dir: Path, ticker: str, name: str | None) -> TickerMetrics:
    """Build the screenable bundle for one ticker from the local caches."""
    profile_recs = _load_records(fmp_dir, ticker, "profile")
    profile = profile_recs[0] if profile_recs else {}
    km = _load_records(fmp_dir, ticker, "key_metrics_quarterly")
    inc = _load_records(fmp_dir, ticker, "income_statement_quarterly")

    nd_q = _num(km[0], "netDebtToEBITDA") if km else None
    sector_raw = profile.get("sector")
    industry_raw = profile.get("industry")
    latest_date = str(inc[0].get("date") or "") if inc else ""
    return TickerMetrics(
        ticker=ticker.upper(),
        name=name or (str(profile["companyName"]) if "companyName" in profile else None),
        sector=str(sector_raw) if isinstance(sector_raw, str) else None,
        industry=str(industry_raw) if isinstance(industry_raw, str) else None,
        market_cap=_num(profile, "marketCap") or (_num(km[0], "marketCap") if km else None),
        roic_ttm=_sum_last4(km, "returnOnInvestedCapital"),
        fcf_yield_ttm=_sum_last4(km, "freeCashFlowYield"),
        # net debt / ONE quarter's EBITDA -> /4 approximates the TTM multiple;
        # skip non-positive (negative EBITDA or net cash makes it meaningless).
        nd_to_ebitda_ttm=(nd_q / 4) if nd_q is not None and nd_q > 0 else None,
        rev_yoy=_rev_yoy_at(inc, 0),
        rev_yoy_prior=_rev_yoy_at(inc, 4),
        gross_margin_ttm=_ttm_margin(inc, "grossProfit"),
        op_margin_ttm=_ttm_margin(inc, "operatingIncome"),
        is_actively_trading=profile.get("isActivelyTrading") is not False,
        latest_income_date=latest_date or None,
    )


def is_actively_trading(fmp_dir: Path, ticker: str) -> bool:
    """The profile's listing flag alone (absent profile = assumed trading) —
    the adjacency lexicon's ghost filter, cheaper than the full bundle."""
    recs = _load_records(fmp_dir, ticker, "profile")
    if not recs:
        return True
    return recs[0].get("isActivelyTrading") is not False


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def screening_universe(db_path: Path, *, user_id: str = DEFAULT_USER_ID) -> list[tuple[str, str]]:
    """(ticker, name) rows to screen — the index_member list. Active names
    (portfolio / watchlist / evaluation) carry a different list_type per the
    tracked_companies model, so they are excluded by construction."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT ticker, name FROM tracked_companies "
            "WHERE user_id = ? AND list_type = 'index_member' ORDER BY ticker",
            (user_id,),
        ).fetchall()
        return [(str(r[0]).upper(), str(r[1])) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def run_screens(
    db_path: Path,
    fmp_dir: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    as_of: date | None = None,
    max_staleness_days: int = 400,
) -> list[ScreenHit]:
    """Screen the whole universe. Pure cache reads — a few seconds for ~2.3k
    names; tickers without caches simply produce no hits. Ghost gates: a
    profile flagged not-actively-trading is skipped outright, and so is a
    name whose newest income quarter predates ``as_of`` (default today) by
    more than ``max_staleness_days`` — frozen caches of delisted names
    otherwise pass value screens on years-old numbers."""
    hits: list[ScreenHit] = []
    cutoff = ((as_of or date.today()) - timedelta(days=max_staleness_days)).isoformat()
    skipped_ghosts = 0
    universe = screening_universe(db_path, user_id=user_id)
    for ticker, name in universe:
        metrics = load_ticker_metrics(fmp_dir, ticker, name)
        if not metrics.is_actively_trading or (
            metrics.latest_income_date is not None and metrics.latest_income_date < cutoff
        ):
            skipped_ghosts += 1
            continue
        for screen_key, check in SCREENS.items():
            detail = check(metrics)
            if detail is not None:
                hits.append(
                    ScreenHit(ticker=ticker, name=metrics.name, screen=screen_key, detail=detail)
                )
    log.info(
        {
            "event": "discovery_screens_done",
            "universe": len(universe),
            "hits": len(hits),
            "skipped_ghosts": skipped_ghosts,
        }
    )
    return hits
