"""
src/portfolio.py
----------------
Single source of truth for the tracked portfolio. Reads `tracked_companies`
from the project SQLite DB; the front-end watchlist writes there and this
module mirrors what's there.

Each `PortfolioEntry.fiscal_year_end_month` is parsed from the DB
`fiscal_year_end` column ("MM-DD" string). If a row's `fiscal_year_end` is
NULL or malformed we raise — there is no silent default. New tickers are
expected to have `fiscal_year_end` populated by the onboarder
(`execution/onboard_ticker.py`) once FMP data is fetched; the alembic
backfill (`0021_backfill_tracked_companies_fiscal_year_end`) handled the
existing rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import db


@dataclass(frozen=True)
class PortfolioEntry:
    ticker: str
    name: str
    fiscal_year_end_month: int  # 1..12, parsed from DB `fiscal_year_end`
    list_type: str               # "portfolio" | "watchlist" | "evaluation"


def _parse_fye_month(ticker: str, fye: object) -> int:
    if not isinstance(fye, str) or len(fye) < 2:
        raise ValueError(
            f"tracked_companies.fiscal_year_end is missing for {ticker!r}; "
            f"run the alembic backfill or have the onboarder populate it"
        )
    month = int(fye[:2])
    if not 1 <= month <= 12:
        raise ValueError(
            f"tracked_companies.fiscal_year_end={fye!r} for {ticker!r} is not a "
            f"valid MM-DD string"
        )
    return month


def get_portfolio(
    include_watchlist: bool = False,
    include_evaluation: bool = False,
) -> list[PortfolioEntry]:
    """Return the active portfolio from `tracked_companies`.

    `portfolio` rows are always included. `watchlist` and `evaluation` are
    opt-in via their respective flags so legacy callers (which only knew about
    portfolio + watchlist) keep their previous semantics.
    """
    rows = db.get_tracked_companies()
    out: list[PortfolioEntry] = []
    for r in rows:
        list_type = str(r.get("list_type", "portfolio"))
        if list_type not in db.ACTIVE_LIST_TYPES:
            continue
        if list_type == "watchlist" and not include_watchlist:
            continue
        if list_type == "evaluation" and not include_evaluation:
            continue
        ticker = str(r["ticker"]).upper()
        out.append(
            PortfolioEntry(
                ticker=ticker,
                name=str(r.get("name", ticker)),
                fiscal_year_end_month=_parse_fye_month(ticker, r.get("fiscal_year_end")),
                list_type=list_type,
            )
        )
    return out


def lookup(ticker: str) -> PortfolioEntry | None:
    canonical = ticker.strip().upper()
    for e in get_portfolio(include_watchlist=True, include_evaluation=True):
        if e.ticker == canonical:
            return e
    return None


def tickers(
    include_watchlist: bool = False,
    include_evaluation: bool = False,
) -> Iterable[str]:
    return [
        e.ticker
        for e in get_portfolio(
            include_watchlist=include_watchlist,
            include_evaluation=include_evaluation,
        )
    ]
