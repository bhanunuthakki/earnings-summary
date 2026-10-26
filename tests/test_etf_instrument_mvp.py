"""Retained ETF instrument-store identities, records and shared company boundaries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import pytest

from instrument_store import (
    get_etf_holdings,
    get_etf_profile,
    get_instrument_kind,
    list_instruments_by_kind,
    upsert_etf_holdings,
    upsert_etf_profile,
)
from models.companies import InstrumentType
from models.instruments import EtfHolding, EtfProfile

# ---------------------------------------------------------------------------
# Schema fixture — minimum to exercise the ETF tables
# ---------------------------------------------------------------------------


@pytest.fixture()
def etf_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """In-memory DB with the migration-0044 ETF tables + tracked_companies."""
    db_path = tmp_path / "etf_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER DEFAULT 1,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            list_type TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            instrument_type VARCHAR,
            archived_at TIMESTAMP,
            UNIQUE(user_id, ticker)
        );

        CREATE TABLE etf_profile (
            ticker TEXT PRIMARY KEY,
            name TEXT,
            issuer TEXT,
            expense_ratio REAL,
            aum_usd_m REAL,
            inception_date TEXT,
            asset_class TEXT,
            benchmark_index TEXT,
            domicile TEXT,
            listed_exchange TEXT,
            distribution_yield REAL,
            description TEXT,
            sector_label TEXT,
            nav REAL,
            price REAL,
            premium_discount_pct REAL,
            pe_ratio REAL,
            pb_ratio REAL,
            weighted_avg_mktcap_usd_m REAL,
            characteristics_as_of TEXT,
            characteristics_source TEXT,
            source TEXT NOT NULL DEFAULT 'fmp',
            field_evidence_json TEXT NOT NULL DEFAULT '{}',
    profile_fetched_at TIMESTAMP NOT NULL
        );

        CREATE TABLE etf_holdings (
            ticker TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            constituent_ticker TEXT,
            name TEXT,
            weight_pct REAL,
            shares_held REAL,
            market_value_usd REAL,
            sector TEXT,
            asset_class TEXT,
            rank_position INTEGER,
            country TEXT,
            source TEXT NOT NULL DEFAULT 'fmp',
            fetched_at TIMESTAMP NOT NULL,
            PRIMARY KEY (ticker, as_of_date, constituent_ticker)
        );
        """
    )
    conn.commit()
    yield conn
    conn.close()


def _seed_tc(
    conn: sqlite3.Connection, ticker: str, kind: str | None, list_type: str = "portfolio"
) -> None:
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type, instrument_type) "
        "VALUES (?, ?, ?, ?)",
        (ticker, f"{ticker} fund", list_type, kind),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1. Schema roundtrip
# ---------------------------------------------------------------------------


def test_etf_profile_upsert_and_read_roundtrip(etf_db: sqlite3.Connection) -> None:
    p = EtfProfile(
        ticker="SOXX",
        name="iShares Semiconductor ETF",
        issuer="BlackRock",
        expense_ratio=0.0035,
        aum_usd_m=14_500.0,
        inception_date=date(2001, 7, 13),
        asset_class="equity",
        benchmark_index="ICE Semiconductor Index",
        listed_exchange="NASDAQ",
        domicile="US",
        distribution_yield=0.0085,
        description="Tracks US-listed semiconductor companies.",
        sector_label="Semiconductors",
        nav=251.42,
        price=251.55,
        premium_discount_pct=0.0005,
        profile_fetched_at=datetime(2026, 5, 25, 12, 0, 0),
    )
    upsert_etf_profile(etf_db, p)
    fetched = get_etf_profile(etf_db, "SOXX")
    assert fetched is not None
    assert fetched.ticker == "SOXX"
    assert fetched.issuer == "BlackRock"
    assert fetched.expense_ratio == pytest.approx(0.0035)
    assert fetched.aum_usd_m == pytest.approx(14_500.0)
    assert fetched.inception_date == date(2001, 7, 13)


def test_etf_profile_upsert_is_idempotent(etf_db: sqlite3.Connection) -> None:
    p = EtfProfile(
        ticker="SOXX",
        name="iShares Semiconductor ETF",
        profile_fetched_at=datetime(2026, 5, 25, 12, 0, 0),
    )
    upsert_etf_profile(etf_db, p)
    upsert_etf_profile(etf_db, p)
    rows = etf_db.execute("SELECT COUNT(*) AS n FROM etf_profile").fetchone()
    assert rows["n"] == 1


def test_etf_profile_upsert_overwrites_on_conflict(etf_db: sqlite3.Connection) -> None:
    p1 = EtfProfile(
        ticker="SOXX",
        name="Old name",
        aum_usd_m=10_000.0,
        profile_fetched_at=datetime(2026, 5, 1, 12, 0, 0),
    )
    p2 = EtfProfile(
        ticker="SOXX",
        name="iShares Semiconductor ETF",
        aum_usd_m=14_500.0,
        profile_fetched_at=datetime(2026, 5, 25, 12, 0, 0),
    )
    upsert_etf_profile(etf_db, p1)
    upsert_etf_profile(etf_db, p2)
    fetched = get_etf_profile(etf_db, "SOXX")
    assert fetched is not None
    assert fetched.name == "iShares Semiconductor ETF"
    assert fetched.aum_usd_m == pytest.approx(14_500.0)


def test_etf_holdings_upsert_and_read_roundtrip(etf_db: sqlite3.Connection) -> None:
    as_of = date(2026, 5, 25)
    fetched_at = datetime(2026, 5, 25, 12, 0, 0)
    holdings = [
        EtfHolding(
            ticker="SOXX",
            as_of_date=as_of,
            constituent_ticker="NVDA",
            name="NVIDIA Corp",
            weight_pct=0.0942,
            sector="Semiconductors",
            rank_position=1,
            fetched_at=fetched_at,
        ),
        EtfHolding(
            ticker="SOXX",
            as_of_date=as_of,
            constituent_ticker="AVGO",
            name="Broadcom Inc",
            weight_pct=0.0851,
            sector="Semiconductors",
            rank_position=2,
            fetched_at=fetched_at,
        ),
    ]
    n = upsert_etf_holdings(etf_db, "SOXX", as_of, holdings)
    assert n == 2
    rows = get_etf_holdings(etf_db, "SOXX")
    assert len(rows) == 2
    assert rows[0].constituent_ticker == "NVDA"
    assert rows[0].weight_pct == pytest.approx(0.0942)


def test_etf_holdings_uses_most_recent_date_when_unspecified(
    etf_db: sqlite3.Connection,
) -> None:
    older = date(2026, 4, 1)
    newer = date(2026, 5, 25)
    fa = datetime(2026, 5, 25)
    for d in (older, newer):
        upsert_etf_holdings(
            etf_db,
            "SOXX",
            d,
            [
                EtfHolding(
                    ticker="SOXX",
                    as_of_date=d,
                    constituent_ticker="NVDA",
                    weight_pct=0.10,
                    rank_position=1,
                    fetched_at=fa,
                )
            ],
        )
    rows = get_etf_holdings(etf_db, "SOXX")
    assert len(rows) == 1
    assert rows[0].as_of_date == newer


def test_etf_holdings_top_n_truncates(etf_db: sqlite3.Connection) -> None:
    as_of = date(2026, 5, 25)
    fa = datetime(2026, 5, 25)
    holdings = [
        EtfHolding(
            ticker="SOXX",
            as_of_date=as_of,
            constituent_ticker=t,
            weight_pct=w,
            rank_position=i + 1,
            fetched_at=fa,
        )
        for i, (t, w) in enumerate(
            [("NVDA", 0.10), ("AVGO", 0.09), ("AMD", 0.08), ("INTC", 0.07), ("TSM", 0.06)]
        )
    ]
    upsert_etf_holdings(etf_db, "SOXX", as_of, holdings)
    rows = get_etf_holdings(etf_db, "SOXX", top_n=3)
    assert len(rows) == 3
    assert [r.constituent_ticker for r in rows] == ["NVDA", "AVGO", "AMD"]


# ---------------------------------------------------------------------------
# 2. Instrument kind discriminator
# ---------------------------------------------------------------------------


def test_get_instrument_kind_returns_etf(etf_db: sqlite3.Connection) -> None:
    _seed_tc(etf_db, "SOXX", "etf")
    assert get_instrument_kind(etf_db, "SOXX") is InstrumentType.ETF


def test_get_instrument_kind_returns_none_for_missing_ticker(
    etf_db: sqlite3.Connection,
) -> None:
    assert get_instrument_kind(etf_db, "ZZZ") is None


def test_get_instrument_kind_returns_none_for_null_kind(
    etf_db: sqlite3.Connection,
) -> None:
    _seed_tc(etf_db, "FOO", None)
    assert get_instrument_kind(etf_db, "FOO") is None


def test_list_instruments_by_kind_filters_correctly(etf_db: sqlite3.Connection) -> None:
    _seed_tc(etf_db, "SOXX", "etf")
    _seed_tc(etf_db, "SPY", "etf")
    _seed_tc(etf_db, "AMZN", "equity")
    etfs = list_instruments_by_kind(etf_db, InstrumentType.ETF)
    equities = list_instruments_by_kind(etf_db, InstrumentType.EQUITY)
    assert etfs == ["SOXX", "SPY"]
    assert equities == ["AMZN"]


def test_list_instruments_by_kind_excludes_archived(etf_db: sqlite3.Connection) -> None:
    _seed_tc(etf_db, "SOXX", "etf")
    _seed_tc(etf_db, "DEAD", "etf")
    etf_db.execute(
        "UPDATE tracked_companies SET archived_at = ? WHERE ticker = 'DEAD'",
        (datetime.now(),),
    )
    etf_db.commit()
    assert list_instruments_by_kind(etf_db, InstrumentType.ETF) == ["SOXX"]


# ---------------------------------------------------------------------------
# 3. Parsers — FMP shape coercion
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 4. Ingest orchestrators — idempotency
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. Section builders + dispatch
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 6. Equity path unaffected — sanity test
# ---------------------------------------------------------------------------


def test_equity_ticker_kind_resolves_independently(etf_db: sqlite3.Connection) -> None:
    """Adding an ETF row must not affect equity ticker kind resolution."""
    _seed_tc(etf_db, "SOXX", "etf")
    _seed_tc(etf_db, "AMZN", "equity")
    assert get_instrument_kind(etf_db, "AMZN") is InstrumentType.EQUITY
    assert get_instrument_kind(etf_db, "SOXX") is InstrumentType.ETF


# ---------------------------------------------------------------------------
# 7. End-to-end SOXX canary
# ---------------------------------------------------------------------------


# Representative-but-stylized SOXX snapshot. Numbers are approximations of
# real public data; weights sum reasonably and the top names are accurate to
# the actual fund composition. The point is to exercise the pipeline shape,
# not to assert against live data.


# ---------------------------------------------------------------------------
# 8. Credential redaction (security regression)
#
# _fmp_get sends the FMP key as a ?apikey=<key> URL query param. On a network
# error, requests/urllib3 embed the fully-resolved URL — key included — in the
# exception string (e.g. "Max retries exceeded with url: /stable/...?apikey=KEY").
# That string must never reach the raised RuntimeError / logs unredacted.
# GEMINI.md: never log a URL that may contain credentials.
# ---------------------------------------------------------------------------
