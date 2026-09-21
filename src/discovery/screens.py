"""Factor screens over the tracked universe (master build P5.3).

All financial screens use canonical resolved observations and exact calculation
references. ROIC stays unavailable until its definition is selected. Raw vendor
metrics are retained only as a dual-read shadow; they never supply screen values.

Deterministic and offline: the universe is tracked index members. Financial
values cross the canonical resolver and retain observation/definition/period
references; provider market context requires a hash-bound captured profile.
Raw local FMP metrics are numerical shadows only, including their legacy
quarter-summing conventions. Their availability or agreement does not prove
canonical definition parity. Missing required inputs fail closed; optional
leverage remains explicitly unavailable. The existing active-trading and
reporting-period freshness gates still apply.

Each screen returns the actual numbers as its evidence detail, so a
candidate explains itself in the queue.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

from identity import DEFAULT_USER_ID
from provenance.immutable_artifact import read_stable_artifact
from sources.discovery_financial_inputs import (
    financial_parity_receipt,
    free_cash_flow_yield,
    read_financial_inputs,
)
from sources.discovery_financials import (
    GrowthFinancials,
    growth_parity_receipt,
    read_growth_financials,
)
from sources.discovery_market import read_market_context
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

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
    market_cap_currency: str | None = None
    source_hashes: dict[str, str] = field(default_factory=lambda: dict[str, str]())


@dataclass(slots=True)
class ScreenHit:
    """One (ticker, screen) pass with its self-explaining detail line."""

    ticker: str
    name: str | None
    screen: str
    detail: str
    evidence: dict[str, object] | None = None


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
    if m.market_cap_currency != "USD" or m.market_cap is None or m.market_cap < 2e9:
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


def _load_records(
    fmp_dir: Path, ticker: str, suffix: str, *, source_hashes: dict[str, str] | None = None
) -> list[dict[str, object]]:
    """One cache file as a date-DESC-sorted record list; [] when absent/bad."""
    path = fmp_dir / f"{ticker.upper()}_{suffix}.json"
    if not path.exists():
        return []
    try:
        snapshot, content = read_stable_artifact(path)
        raw: object = json.loads(content)
        if source_hashes is not None:
            source_hashes[path.name] = snapshot.file_sha256
    except (OSError, ValueError, RuntimeError):
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
    source_hashes: dict[str, str] = {}
    profile_recs = _load_records(fmp_dir, ticker, "profile", source_hashes=source_hashes)
    profile = profile_recs[0] if profile_recs else {}
    km = _load_records(fmp_dir, ticker, "key_metrics_quarterly", source_hashes=source_hashes)
    inc = _load_records(fmp_dir, ticker, "income_statement_quarterly", source_hashes=source_hashes)

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
        source_hashes=source_hashes,
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


def canonical_ticker_metrics(
    conn: sqlite3.Connection, source_dir: Path, ticker: str, name: str | None, *, as_of: date
) -> tuple[TickerMetrics, dict[str, object]]:
    financials = read_financial_inputs(conn, ticker, as_of=as_of)
    growth = read_growth_financials(conn, ticker, as_of=as_of)
    market = read_market_context(conn, source_dir, ticker, as_of=as_of)
    cash_yield = free_cash_flow_yield(financials, market)
    legacy = load_ticker_metrics(source_dir, ticker, name)

    def number(value: Decimal | None) -> float | None:
        return float(value) if value is not None else None

    refs = financials.revenue_yoy.references
    latest = max((item.period_end for item in refs), default=None)
    return TickerMetrics(
        ticker=ticker.upper(),
        name=name or market.name,
        sector=market.sector,
        industry=market.industry,
        market_cap=number(market.market_cap) if market.status == "available" else None,
        market_cap_currency=market.currency,
        roic_ttm=number(financials.roic_ttm.value),
        fcf_yield_ttm=number(cash_yield.value),
        nd_to_ebitda_ttm=number(financials.net_debt_to_ebitda.value),
        rev_yoy=number(financials.revenue_yoy.value),
        rev_yoy_prior=number(growth.revenue_yoy_prior) if growth.status == "available" else None,
        gross_margin_ttm=number(growth.gross_margin_ttm) if growth.status == "available" else None,
        op_margin_ttm=number(financials.operating_margin_ttm.value),
        is_actively_trading=market.actively_trading is not False,
        latest_income_date=latest.isoformat() if latest else None,
    ), {
        "financials": financials.model_dump(mode="json"),
        "market": market.model_dump(mode="json"),
        "free_cash_flow_yield": cash_yield.model_dump(mode="json"),
        "growth": growth.model_dump(mode="json"),
        "dual_read_parity": financial_parity_receipt(
            financials,
            cash_yield,
            legacy_values=(legacy.rev_yoy, legacy.op_margin_ttm, legacy.fcf_yield_ttm),
            legacy_source_hashes=legacy.source_hashes,
        ),
    }


def screening_universe(db_path: Path, *, user_id: str = DEFAULT_USER_ID) -> list[tuple[str, str]]:
    """(ticker, name) rows to screen — the index_member list. Active names
    (portfolio / watchlist / evaluation) carry a different list_type per the
    tracked_companies model, so they are excluded by construction."""
    if not db_path.exists():
        return []
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
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
    growth_coverage_sink: Callable[[GrowthFinancials], None] | None = None,
    financial_coverage_sink: Callable[[str, dict[str, object]], None] | None = None,
) -> list[ScreenHit]:
    """Screen the universe using canonical financial inputs and captured market context.
    Missing or incomparable facts yield explicit unavailable coverage. Ghost gates: a
    profile flagged not-actively-trading is skipped outright, and so is a
    name whose newest income quarter predates ``as_of`` (default today) by
    more than ``max_staleness_days`` — frozen caches of delisted names
    otherwise pass value screens on years-old numbers."""
    hits: list[ScreenHit] = []
    cutoff = ((as_of or date.today()) - timedelta(days=max_staleness_days)).isoformat()
    skipped_ghosts = 0
    universe = screening_universe(db_path, user_id=user_id)
    if not universe:
        return hits
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    canonical_unavailable = 0
    try:
        for ticker, name in universe:
            legacy_metrics = load_ticker_metrics(fmp_dir, ticker, name)
            metrics, canonical_evidence = canonical_ticker_metrics(
                conn, fmp_dir, ticker, name, as_of=as_of or date.today()
            )
            if financial_coverage_sink is not None:
                financial_coverage_sink(ticker, canonical_evidence)
            if not metrics.is_actively_trading:
                skipped_ghosts += 1
                continue
            for screen_key, check in SCREENS.items():
                candidate = metrics
                evidence: dict[str, object] | None = canonical_evidence
                if screen_key == "growth_inflection":
                    resolved = GrowthFinancials.model_validate(canonical_evidence["growth"])
                    if (
                        resolved.latest_period_end is not None
                        and resolved.latest_period_end.isoformat() < cutoff
                    ):
                        resolved = resolved.model_copy(
                            update={
                                "status": "degraded",
                                "reason_codes": ("stale_latest_reporting_period",),
                            }
                        )
                    if growth_coverage_sink is not None:
                        growth_coverage_sink(resolved)
                    if resolved.status != "available":
                        canonical_unavailable += 1
                        log.info(
                            {
                                "event": "discovery_growth_unavailable",
                                "ticker": ticker,
                                "reason_codes": resolved.reason_codes,
                            }
                        )
                        continue
                    candidate = replace(
                        metrics,
                        rev_yoy=float(resolved.revenue_yoy)
                        if resolved.revenue_yoy is not None
                        else None,
                        rev_yoy_prior=float(resolved.revenue_yoy_prior)
                        if resolved.revenue_yoy_prior is not None
                        else None,
                        gross_margin_ttm=float(resolved.gross_margin_ttm)
                        if resolved.gross_margin_ttm is not None
                        else None,
                        latest_income_date=resolved.latest_period_end.isoformat()
                        if resolved.latest_period_end
                        else None,
                    )
                    evidence = resolved.model_dump(mode="json")
                    evidence["dual_read_parity"] = growth_parity_receipt(
                        resolved,
                        legacy_values=(
                            legacy_metrics.rev_yoy,
                            legacy_metrics.rev_yoy_prior,
                            legacy_metrics.gross_margin_ttm,
                        ),
                        legacy_source_hashes=legacy_metrics.source_hashes,
                    )
                if (
                    candidate.latest_income_date is not None
                    and candidate.latest_income_date < cutoff
                ):
                    skipped_ghosts += 1
                    continue
                detail = check(candidate)
                if detail is not None:
                    hits.append(
                        ScreenHit(
                            ticker=ticker,
                            name=candidate.name,
                            screen=screen_key,
                            detail=detail,
                            evidence=evidence,
                        )
                    )
    finally:
        conn.close()
    log.info(
        {
            "event": "discovery_screens_done",
            "universe": len(universe),
            "hits": len(hits),
            "skipped_ghosts": skipped_ghosts,
            "canonical_growth_unavailable": canonical_unavailable,
        }
    )
    return hits
