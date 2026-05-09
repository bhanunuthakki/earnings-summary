"""
src/portfolio.py
----------------
Single source of truth for the tracked portfolio + each ticker's fiscal-year
shape. Used by the monthly cron orchestrator (`check_quarterly_releases.py`)
to know who to poll FMP for and how to label fiscal quarters.

Resolution order:
  1. `tracked_companies` rows in the project SQLite DB (preferred — kept in
     sync with the front-end watchlist).
  2. Hardcoded `_DEFAULT_PORTFOLIO` below (used when the DB is empty / fresh
     clone).

Fiscal-year-end month is the calendar month in which the company's fiscal
year ends (1=Jan, 12=Dec). The per-quarter labels FMP returns on
`/stable/income-statement` come from this — we don't infer them locally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import db


@dataclass(frozen=True)
class PortfolioEntry:
    ticker: str
    name: str
    fiscal_year_end_month: int  # 1..12
    list_type: str               # "portfolio" | "watchlist"


# Hardcoded fallback. Mirrored from directives/fetch_ir_documents.md +
# fetch_fmp_statements.DEFAULT_TICKERS + the data_pipeline_dag fiscal table.
_DEFAULT_PORTFOLIO: tuple[PortfolioEntry, ...] = (
    # --- portfolio (11 names) -------------------------------------------------
    PortfolioEntry("AMZN", "Amazon",            12, "portfolio"),
    PortfolioEntry("GOOG", "Alphabet",          12, "portfolio"),
    PortfolioEntry("META", "Meta Platforms",    12, "portfolio"),
    PortfolioEntry("MELI", "MercadoLibre",      12, "portfolio"),
    PortfolioEntry("NU",   "Nu Holdings",       12, "portfolio"),
    PortfolioEntry("NVO",  "Novo Nordisk",      12, "portfolio"),
    PortfolioEntry("NOW",  "ServiceNow",        12, "portfolio"),
    PortfolioEntry("WIX",  "Wix",               12, "portfolio"),
    PortfolioEntry("RBRK", "Rubrik",             1, "portfolio"),  # FY ends Jan 31
    PortfolioEntry("VEEV", "Veeva Systems",      1, "portfolio"),  # FY ends Jan 31
    PortfolioEntry("BN",   "Brookfield Corp",   12, "portfolio"),

    # --- watchlist (16 names, mirrored from fetch_fmp_statements.DEFAULT_TICKERS) ---
    PortfolioEntry("CNQ",  "Canadian Natural Resources", 12, "watchlist"),
    PortfolioEntry("WY",   "Weyerhaeuser",                12, "watchlist"),
    PortfolioEntry("FNV",  "Franco-Nevada",               12, "watchlist"),
    PortfolioEntry("RIO",  "Rio Tinto",                   12, "watchlist"),
    PortfolioEntry("VALE", "Vale",                        12, "watchlist"),
    PortfolioEntry("FCX",  "Freeport-McMoRan",            12, "watchlist"),
    PortfolioEntry("ASML", "ASML Holding",                12, "watchlist"),
    PortfolioEntry("AMAT", "Applied Materials",           10, "watchlist"),  # FY ends ~last Sun in Oct
    PortfolioEntry("TOL",  "Toll Brothers",               10, "watchlist"),  # FY ends Oct 31
    PortfolioEntry("SOFI", "SoFi Technologies",           12, "watchlist"),
    PortfolioEntry("HDB",  "HDFC Bank",                    3, "watchlist"),  # FY ends Mar 31 (Indian FY)
    PortfolioEntry("ABNB", "Airbnb",                      12, "watchlist"),
    PortfolioEntry("BHP",  "BHP Group",                    6, "watchlist"),  # FY ends Jun 30 (Australian)
    PortfolioEntry("LLY",  "Eli Lilly",                   12, "watchlist"),
    PortfolioEntry("LMND", "Lemonade",                    12, "watchlist"),
    PortfolioEntry("JPM",  "JPMorgan Chase",              12, "watchlist"),
)

# Per-ticker fiscal-year-end overrides for entries pulled from the DB (the
# tracked_companies table doesn't carry this column today).
_FY_END_BY_TICKER: dict[str, int] = {e.ticker: e.fiscal_year_end_month for e in _DEFAULT_PORTFOLIO}


def get_portfolio(include_watchlist: bool = False) -> list[PortfolioEntry]:
    """Return the active portfolio. Reads tracked_companies first; if empty,
    falls back to the hardcoded baseline."""
    rows = db.get_tracked_companies()
    if rows:
        out: list[PortfolioEntry] = []
        for r in rows:
            list_type = str(r.get("list_type", "portfolio"))
            if list_type == "watchlist" and not include_watchlist:
                continue
            ticker = str(r["ticker"]).upper()
            out.append(
                PortfolioEntry(
                    ticker=ticker,
                    name=str(r.get("name", ticker)),
                    fiscal_year_end_month=_FY_END_BY_TICKER.get(ticker, 12),
                    list_type=list_type,
                )
            )
        return out

    if include_watchlist:
        return list(_DEFAULT_PORTFOLIO)
    return [e for e in _DEFAULT_PORTFOLIO if e.list_type == "portfolio"]


def lookup(ticker: str) -> PortfolioEntry | None:
    canonical = ticker.strip().upper()
    for e in get_portfolio(include_watchlist=True):
        if e.ticker == canonical:
            return e
    return None


def tickers(include_watchlist: bool = False) -> Iterable[str]:
    return [e.ticker for e in get_portfolio(include_watchlist=include_watchlist)]
