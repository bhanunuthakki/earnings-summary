"""Ingest orchestration for the ETF published-data lane.

One call refreshes everything the evaluation lane needs for an ETF, from
published sources only (directives/etf_data.md):

  1. N-PORT spine   — full holdings + per-constituent country from EDGAR.
                      Idempotent on (ticker, rep_period_date, source): a
                      report already ingested is an explicit "already done".
                      A fetched-but-unparseable document HALTS loudly
                      (NportParseError propagates; raw XML is in .tmp/).
  2. Issuer overlay — fresher holdings and/or basket characteristics when
                      the ticker's issuer adapter exists. Soft: any failure
                      degrades to the spine.
  3. Price history  — dividend-adjusted daily closes via the yfinance
                      factor-proxy store when the FMP price-chart cache has
                      nothing for the ticker (allocation/price_history reads
                      the proxy store as its fallback).

DB writes go through instrument_store; the profile merge is read-modify-write
so an overlay that publishes only an expense ratio never blanks identity
fields some earlier source filled in.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from etf_sources import nport
from etf_sources.issuer_registry import IssuerCharacteristics, fetch_issuer_data
from factor_proxies import fetch_proxy_series, store_proxy_series
from instrument_store import get_etf_profile, upsert_etf_holdings, upsert_etf_profile
from models.instruments import EtfProfile

#: yfinance window for a new ETF's price series — comfortably covers the
#: 252-observation fit/OLS lookbacks plus calendar-intersection losses.
PRICE_HISTORY_PERIOD = "5y"


@dataclass(frozen=True, slots=True)
class PublishedDataResult:
    """What one refresh actually did — for stage logs and tests."""

    ticker: str
    nport_status: str  # 'ingested' | 'already_done' | 'unavailable'
    nport_as_of: date | None
    nport_rows: int
    issuer_status: str  # 'ingested' | 'unavailable'
    issuer_rows: int
    issuer_as_of: date | None
    characteristics_applied: bool
    price_status: str  # 'present' | 'fetched' | 'failed' | 'skipped'
    price_rows: int


def _has_holdings_snapshot(conn: sqlite3.Connection, ticker: str, as_of: date, source: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM etf_holdings WHERE ticker = ? AND as_of_date = ? AND source = ? LIMIT 1",
            (ticker.upper(), as_of.isoformat(), source),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def apply_characteristics(
    conn: sqlite3.Connection, ticker: str, chars: IssuerCharacteristics
) -> None:
    """Merge issuer-published characteristics into ``etf_profile``.

    Read-modify-write: only fields the overlay actually publishes move;
    everything else keeps its current value (or stays None on a fresh row).
    """
    existing = get_etf_profile(conn, ticker)
    now = datetime.now()
    if existing is None:
        merged = EtfProfile(
            ticker=ticker.upper(),
            name=chars.name,
            issuer=chars.issuer,
            expense_ratio=chars.expense_ratio,
            distribution_yield=chars.distribution_yield,
            pe_ratio=chars.pe_ratio,
            pb_ratio=chars.pb_ratio,
            weighted_avg_mktcap_usd_m=chars.weighted_avg_mktcap_usd_m,
            characteristics_as_of=chars.as_of,
            characteristics_source=chars.source,
            source=chars.source,
            profile_fetched_at=now,
        )
    else:
        merged = existing.model_copy(
            update={
                "name": existing.name or chars.name,
                "issuer": existing.issuer or chars.issuer,
                "expense_ratio": (
                    chars.expense_ratio
                    if chars.expense_ratio is not None
                    else existing.expense_ratio
                ),
                "distribution_yield": (
                    chars.distribution_yield
                    if chars.distribution_yield is not None
                    else existing.distribution_yield
                ),
                "pe_ratio": chars.pe_ratio if chars.pe_ratio is not None else existing.pe_ratio,
                "pb_ratio": chars.pb_ratio if chars.pb_ratio is not None else existing.pb_ratio,
                "weighted_avg_mktcap_usd_m": (
                    chars.weighted_avg_mktcap_usd_m
                    if chars.weighted_avg_mktcap_usd_m is not None
                    else existing.weighted_avg_mktcap_usd_m
                ),
                "characteristics_as_of": chars.as_of or existing.characteristics_as_of,
                "characteristics_source": chars.source,
                "profile_fetched_at": now,
            }
        )
    upsert_etf_profile(conn, merged)


def refresh_published_data(
    conn: sqlite3.Connection,
    ticker: str,
    repo_root: Path,
    *,
    force: bool = False,
    skip_prices: bool = False,
    user_agent: str = nport.DEFAULT_USER_AGENT,
) -> PublishedDataResult:
    """Refresh one ETF's holdings + characteristics + price history.

    Raises :class:`etf_sources.nport.NportParseError` on schema drift in a
    fetched N-PORT document (halt-and-inspect); every other miss degrades
    into the result's status fields.
    """
    upper = ticker.upper()

    # -- 1. N-PORT spine ----------------------------------------------------
    nport_status, nport_as_of, nport_rows = "unavailable", None, 0
    report = nport.fetch_holdings(
        upper, user_agent=user_agent, tmp_dir=repo_root / ".tmp" / "etf_nport"
    )
    if report is not None:
        nport_as_of = report.rep_period_date
        nport_rows = len(report.holdings)
        if not force and _has_holdings_snapshot(conn, upper, nport_as_of, nport.SOURCE):
            nport_status = "already_done"
        else:
            upsert_etf_holdings(conn, upper, nport_as_of, report.holdings)
            nport_status = "ingested"

    # -- 2. Issuer overlay (soft) --------------------------------------------
    issuer_status, issuer_rows, issuer_as_of = "unavailable", 0, None
    characteristics_applied = False
    overlay = fetch_issuer_data(upper)
    if overlay is not None:
        if overlay.holdings and overlay.holdings_as_of is not None:
            upsert_etf_holdings(conn, upper, overlay.holdings_as_of, overlay.holdings)
            issuer_rows = len(overlay.holdings)
            issuer_as_of = overlay.holdings_as_of
            issuer_status = "ingested"
        if overlay.characteristics is not None:
            apply_characteristics(conn, upper, overlay.characteristics)
            characteristics_applied = True
            issuer_status = "ingested"

    # -- 3. Price history (yfinance → proxy store) ---------------------------
    price_status, price_rows = "skipped", 0
    if not skip_prices:
        from allocation.price_history import load_daily_closes

        existing_closes = load_daily_closes(upper, repo_root)
        if existing_closes and not force:
            price_status, price_rows = "present", len(existing_closes)
        else:
            rows = fetch_proxy_series(upper, period=PRICE_HISTORY_PERIOD)
            stored = store_proxy_series(repo_root, upper, rows)
            if stored is not None:
                price_status, price_rows = "fetched", len(rows)
            else:
                price_status = "failed" if not existing_closes else "present"
                price_rows = len(existing_closes)

    return PublishedDataResult(
        ticker=upper,
        nport_status=nport_status,
        nport_as_of=nport_as_of,
        nport_rows=nport_rows,
        issuer_status=issuer_status,
        issuer_rows=issuer_rows,
        issuer_as_of=issuer_as_of,
        characteristics_applied=characteristics_applied,
        price_status=price_status,
        price_rows=price_rows,
    )
