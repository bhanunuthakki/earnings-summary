"""ETF brief sections — profile (§1) and holdings (§5).

The section builders read from `etf_profile` and `etf_holdings` via the
`instrument_store` API. `build_etf_brief` glues both into a single
`EtfBriefSpec` for the renderer dispatch.

For the MVP we keep both subsections in this single module — one file as
the brief asked. As ETF coverage deepens (premium/discount band, rotation
lens, look-through overlap with equity holdings), they can split into their
own modules following the equity-section convention.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

from instrument_store import get_etf_holdings, get_etf_profile
from report.etf_models import (
    EtfBriefSpec,
    EtfHoldingRow,
    EtfHoldingsSection,
    EtfProfileSection,
    EtfSectorBreakdownRow,
)
from report.models import MissingReason, SectionStatus
from report.render_clock import render_today
from report.sections._common import open_repo_db


def build_profile(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> EtfProfileSection:
    """Build the §1 ETF profile section."""
    db_conn = open_repo_db(repo_root, conn)
    if db_conn is None:
        return EtfProfileSection(
            status=SectionStatus.MISSING_DATA,
            missing=MissingReason(
                stage="DB",
                fix_command="initialize data/portfolio.db (alembic upgrade head)",
            ),
            ticker=ticker.upper(),
        )
    try:
        profile = get_etf_profile(db_conn, ticker)
    finally:
        if conn is None:
            db_conn.close()

    if profile is None:
        return EtfProfileSection(
            status=SectionStatus.MISSING_DATA,
            missing=MissingReason(
                stage="INGEST(fmp_etf)",
                fix_command=(f"python execution/fetch_etf_data.py --ticker {ticker.upper()}"),
            ),
            ticker=ticker.upper(),
        )

    return EtfProfileSection(
        status=SectionStatus.OK,
        ticker=profile.ticker,
        name=profile.name,
        issuer=profile.issuer,
        expense_ratio=profile.expense_ratio,
        aum_usd_m=profile.aum_usd_m,
        inception_date=profile.inception_date,
        asset_class=profile.asset_class,
        benchmark_index=profile.benchmark_index,
        domicile=profile.domicile,
        listed_exchange=profile.listed_exchange,
        distribution_yield=profile.distribution_yield,
        description=profile.description,
        sector_label=profile.sector_label,
        nav=profile.nav,
        price=profile.price,
        premium_discount_pct=profile.premium_discount_pct,
        profile_fetched_at=profile.profile_fetched_at,
    )


def build_holdings(
    ticker: str,
    repo_root: Path,
    top_n: int = 10,
    *,
    conn: sqlite3.Connection | None = None,
) -> EtfHoldingsSection:
    """Build the §5 ETF holdings section (top N + sector breakdown)."""
    db_conn = open_repo_db(repo_root, conn)
    if db_conn is None:
        return EtfHoldingsSection(
            status=SectionStatus.MISSING_DATA,
            missing=MissingReason(
                stage="DB",
                fix_command="initialize data/portfolio.db (alembic upgrade head)",
            ),
        )
    try:
        # Load EVERY holding for sector aggregation; truncate the table to top_n.
        all_holdings = get_etf_holdings(db_conn, ticker)
    finally:
        if conn is None:
            db_conn.close()

    if not all_holdings:
        return EtfHoldingsSection(
            status=SectionStatus.MISSING_DATA,
            missing=MissingReason(
                stage="INGEST(fmp_etf)",
                fix_command=(f"python execution/fetch_etf_data.py --ticker {ticker.upper()}"),
            ),
        )

    as_of = all_holdings[0].as_of_date
    top_rows = [
        EtfHoldingRow(
            rank=h.rank_position,
            constituent_ticker=h.constituent_ticker,
            name=h.name,
            weight_pct=h.weight_pct,
            sector=h.sector,
            market_value_usd=h.market_value_usd,
        )
        for h in all_holdings[:top_n]
    ]

    sector_totals: dict[str, float] = defaultdict(float)
    for h in all_holdings:
        if h.weight_pct is None:
            continue
        label = h.sector or "Unclassified"
        sector_totals[label] += h.weight_pct
    sector_rows = [
        EtfSectorBreakdownRow(sector=s, weight_pct=w)
        for s, w in sorted(sector_totals.items(), key=lambda kv: kv[1], reverse=True)
    ]

    return EtfHoldingsSection(
        status=SectionStatus.OK,
        as_of_date=as_of,
        total_constituents=len(all_holdings),
        top_holdings=top_rows,
        sector_breakdown=sector_rows,
    )


def build_etf_brief(ticker: str, repo_root: Path, top_n: int = 10) -> EtfBriefSpec:
    """Glue both subsections into the EtfBriefSpec the renderer consumes."""
    conn = open_repo_db(repo_root)
    try:
        return EtfBriefSpec(
            ticker=ticker.upper(),
            generation_date=render_today(),
            repo_root=str(repo_root),
            profile=build_profile(ticker, repo_root, conn=conn),
            holdings=build_holdings(ticker, repo_root, top_n=top_n, conn=conn),
        )
    finally:
        if conn is not None:
            conn.close()
