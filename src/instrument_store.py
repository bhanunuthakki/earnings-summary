"""Typed read/write API for cross-asset instruments.

The registry lives on `tracked_companies` (the polymorphic instruments table,
discriminator = `instrument_type`). Per-kind details live in dedicated tables
introduced by migration 0044 onward (`etf_profile`, `etf_holdings`; future
kinds get `bond_profile`, `option_chain_snapshots`, etc.).

This module gives callers a thin, kind-aware API so the SQL stays here and
section builders / fetchers don't hand-roll it. See
`directives/cross_asset_data_model.md` for the broader design.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date, datetime

from models.companies import InstrumentType
from models.instruments import EtfHolding, EtfProfile

# ---------------------------------------------------------------------------
# Kind discriminator
# ---------------------------------------------------------------------------


def get_instrument_kind(conn: sqlite3.Connection, ticker: str) -> InstrumentType | None:
    """Return the InstrumentType for `ticker`, or None if not tracked/unknown.

    Returns None both when the ticker isn't in `tracked_companies` at all
    and when it is but `instrument_type` is NULL (legacy / index-backfill
    rows). Callers should treat None as "unclassified — fall back to equity
    behavior" since equity is the established default of the system.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT instrument_type FROM tracked_companies WHERE ticker = ? LIMIT 1",
        (ticker.upper(),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    value = row["instrument_type"] if isinstance(row, sqlite3.Row) else row[0]
    if value is None:
        return None
    return InstrumentType(value)


# ---------------------------------------------------------------------------
# etf_profile
# ---------------------------------------------------------------------------


def upsert_etf_profile(conn: sqlite3.Connection, profile: EtfProfile) -> None:
    """Insert-or-update one row of `etf_profile` keyed on ticker."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO etf_profile (
            ticker, name, issuer, expense_ratio, aum_usd_m, inception_date,
            asset_class, benchmark_index, domicile, listed_exchange,
            distribution_yield, description, sector_label,
            nav, price, premium_discount_pct, source, profile_fetched_at
        ) VALUES (
            :ticker, :name, :issuer, :expense_ratio, :aum_usd_m, :inception_date,
            :asset_class, :benchmark_index, :domicile, :listed_exchange,
            :distribution_yield, :description, :sector_label,
            :nav, :price, :premium_discount_pct, :source, :profile_fetched_at
        )
        ON CONFLICT(ticker) DO UPDATE SET
            name                  = excluded.name,
            issuer                = excluded.issuer,
            expense_ratio         = excluded.expense_ratio,
            aum_usd_m             = excluded.aum_usd_m,
            inception_date        = excluded.inception_date,
            asset_class           = excluded.asset_class,
            benchmark_index       = excluded.benchmark_index,
            domicile              = excluded.domicile,
            listed_exchange       = excluded.listed_exchange,
            distribution_yield    = excluded.distribution_yield,
            description           = excluded.description,
            sector_label          = excluded.sector_label,
            nav                   = excluded.nav,
            price                 = excluded.price,
            premium_discount_pct  = excluded.premium_discount_pct,
            source                = excluded.source,
            profile_fetched_at    = excluded.profile_fetched_at
        """,
        {
            "ticker": profile.ticker.upper(),
            "name": profile.name,
            "issuer": profile.issuer,
            "expense_ratio": profile.expense_ratio,
            "aum_usd_m": profile.aum_usd_m,
            "inception_date": profile.inception_date.isoformat()
            if profile.inception_date
            else None,
            "asset_class": profile.asset_class,
            "benchmark_index": profile.benchmark_index,
            "domicile": profile.domicile,
            "listed_exchange": profile.listed_exchange,
            "distribution_yield": profile.distribution_yield,
            "description": profile.description,
            "sector_label": profile.sector_label,
            "nav": profile.nav,
            "price": profile.price,
            "premium_discount_pct": profile.premium_discount_pct,
            "source": profile.source,
            "profile_fetched_at": profile.profile_fetched_at.isoformat(),
        },
    )


def get_etf_profile(conn: sqlite3.Connection, ticker: str) -> EtfProfile | None:
    """Load the `etf_profile` row for `ticker`, or None if not present."""
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM etf_profile WHERE ticker = ? LIMIT 1",
        (ticker.upper(),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return EtfProfile(
        ticker=row["ticker"],
        name=row["name"],
        issuer=row["issuer"],
        expense_ratio=row["expense_ratio"],
        aum_usd_m=row["aum_usd_m"],
        inception_date=date.fromisoformat(row["inception_date"]) if row["inception_date"] else None,
        asset_class=row["asset_class"],
        benchmark_index=row["benchmark_index"],
        domicile=row["domicile"],
        listed_exchange=row["listed_exchange"],
        distribution_yield=row["distribution_yield"],
        description=row["description"],
        sector_label=row["sector_label"],
        nav=row["nav"],
        price=row["price"],
        premium_discount_pct=row["premium_discount_pct"],
        source=row["source"],
        profile_fetched_at=datetime.fromisoformat(row["profile_fetched_at"]),
    )


# ---------------------------------------------------------------------------
# etf_holdings
# ---------------------------------------------------------------------------


def upsert_etf_holdings(
    conn: sqlite3.Connection,
    ticker: str,
    as_of_date: date,
    holdings: Iterable[EtfHolding],
) -> int:
    """Upsert a batch of `etf_holdings` rows for one (ticker, as_of_date).

    Each holding's `(ticker, as_of_date, constituent_ticker)` natural key
    drives the conflict. Returns the number of rows touched.

    SQLite's UNIQUE/PK semantics treat NULL as distinct, so multiple cash
    rows (constituent_ticker=NULL) for the same (ticker, as_of_date) would
    coexist. That's tolerable for the MVP — FMP returns at most one cash
    sleeve per snapshot — but be aware if you start ingesting from sources
    that report multiple NULL-tickered exposures.
    """
    cur = conn.cursor()
    rows = list(holdings)
    for h in rows:
        cur.execute(
            """
            INSERT INTO etf_holdings (
                ticker, as_of_date, constituent_ticker, name, weight_pct,
                shares_held, market_value_usd, sector, asset_class,
                rank_position, source, fetched_at
            ) VALUES (
                :ticker, :as_of_date, :constituent_ticker, :name, :weight_pct,
                :shares_held, :market_value_usd, :sector, :asset_class,
                :rank_position, :source, :fetched_at
            )
            ON CONFLICT(ticker, as_of_date, constituent_ticker) DO UPDATE SET
                name              = excluded.name,
                weight_pct        = excluded.weight_pct,
                shares_held       = excluded.shares_held,
                market_value_usd  = excluded.market_value_usd,
                sector            = excluded.sector,
                asset_class       = excluded.asset_class,
                rank_position     = excluded.rank_position,
                source            = excluded.source,
                fetched_at        = excluded.fetched_at
            """,
            {
                "ticker": ticker.upper(),
                "as_of_date": as_of_date.isoformat(),
                "constituent_ticker": (
                    h.constituent_ticker.upper() if h.constituent_ticker else None
                ),
                "name": h.name,
                "weight_pct": h.weight_pct,
                "shares_held": h.shares_held,
                "market_value_usd": h.market_value_usd,
                "sector": h.sector,
                "asset_class": h.asset_class,
                "rank_position": h.rank_position,
                "source": h.source,
                "fetched_at": h.fetched_at.isoformat(),
            },
        )
    return len(rows)


def get_etf_holdings(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    as_of_date: date | None = None,
    top_n: int | None = None,
) -> list[EtfHolding]:
    """Return holdings for `ticker`.

    `as_of_date=None` returns the most recent snapshot date in the table.
    `top_n` clips to the highest-weight N rows (NULL weights sorted last).
    """
    cur = conn.cursor()
    if as_of_date is None:
        cur.execute(
            "SELECT MAX(as_of_date) AS d FROM etf_holdings WHERE ticker = ?",
            (ticker.upper(),),
        )
        d_row = cur.fetchone()
        d_val = d_row["d"] if d_row is not None else None
        if d_val is None:
            return []
        resolved = date.fromisoformat(d_val)
    else:
        resolved = as_of_date

    query = (
        "SELECT * FROM etf_holdings "
        "WHERE ticker = ? AND as_of_date = ? "
        "ORDER BY (weight_pct IS NULL), weight_pct DESC, rank_position"
    )
    params: list[object] = [ticker.upper(), resolved.isoformat()]
    if top_n is not None:
        query += " LIMIT ?"
        params.append(int(top_n))
    cur.execute(query, params)
    out: list[EtfHolding] = []
    for row in cur.fetchall():
        out.append(
            EtfHolding(
                ticker=row["ticker"],
                as_of_date=date.fromisoformat(row["as_of_date"]),
                constituent_ticker=row["constituent_ticker"],
                name=row["name"],
                weight_pct=row["weight_pct"],
                shares_held=row["shares_held"],
                market_value_usd=row["market_value_usd"],
                sector=row["sector"],
                asset_class=row["asset_class"],
                rank_position=row["rank_position"],
                source=row["source"],
                fetched_at=datetime.fromisoformat(row["fetched_at"]),
            )
        )
    return out


def list_instruments_by_kind(conn: sqlite3.Connection, kind: InstrumentType) -> list[str]:
    """Tickers in `tracked_companies` whose instrument_type == kind."""
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker FROM tracked_companies "
        "WHERE instrument_type = ? AND archived_at IS NULL "
        "ORDER BY ticker",
        (kind.value,),
    )
    return [row["ticker"] for row in cur.fetchall()]
