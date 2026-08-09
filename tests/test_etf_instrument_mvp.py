"""Cross-asset MVP: ETF instrument end-to-end.

Covers the layers introduced in migration 0044 + the ETF brief pipeline:
- etf_profile / etf_holdings schema roundtrip
- instrument_store API (kind discriminator, upsert, get, list-by-kind)
- fetch_etf_data parsers (FMP shape coercion)
- ingest_from_payloads / ingest_from_cache idempotency
- ETF section builders (profile, holdings) including MISSING_DATA path
- ETF markdown renderer
- Equity ingest paths unaffected — sanity test
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution.fetch_etf_data import (  # noqa: E402
    ingest_from_cache,
    ingest_from_payloads,
    parse_etf_holdings,
    parse_etf_info,
)
from instrument_store import (  # noqa: E402
    get_etf_holdings,
    get_etf_profile,
    get_instrument_kind,
    list_instruments_by_kind,
    upsert_etf_holdings,
    upsert_etf_profile,
)
from models.companies import InstrumentType  # noqa: E402
from models.instruments import EtfHolding, EtfProfile  # noqa: E402
from report.etf_models import EtfBriefSpec  # noqa: E402
from report.models import SectionStatus  # noqa: E402
from report.renderers.etf_markdown import render as render_etf_markdown  # noqa: E402
from report.sections.etf_holdings import build_etf_brief  # noqa: E402

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


def test_parse_etf_info_unwraps_array_payload() -> None:
    payload = [
        {
            "symbol": "SOXX",
            "name": "iShares Semiconductor ETF",
            "etfCompany": "BlackRock",
            "expenseRatio": 0.35,  # FMP returns as percent-form string sometimes
            "aum": 14_500_000_000,
            "inceptionDate": "2001-07-13",
            "assetClass": "equity",
            "etfIndex": "ICE Semiconductor Index",
            "exchange": "NASDAQ",
            "domicile": "US",
            "yield": 0.85,
            "description": "Tracks US semis.",
            "sector": "Semiconductors",
        }
    ]
    p = parse_etf_info(payload, "SOXX", datetime(2026, 5, 25))
    assert p.name == "iShares Semiconductor ETF"
    assert p.issuer == "BlackRock"
    assert p.expense_ratio == pytest.approx(0.0035)  # 0.35% → 0.0035
    assert p.aum_usd_m == pytest.approx(14_500.0)  # 14.5e9 → 14.5e3 M
    assert p.inception_date == date(2001, 7, 13)
    assert p.distribution_yield == pytest.approx(0.0085)


def test_parse_etf_info_handles_empty_payload() -> None:
    p = parse_etf_info([], "ZZZ", datetime(2026, 5, 25))
    assert p.ticker == "ZZZ"
    assert p.name is None
    assert p.issuer is None


def test_parse_etf_info_accepts_single_object_payload() -> None:
    payload = {"symbol": "SOXX", "name": "iShares Semiconductor ETF"}
    p = parse_etf_info(payload, "SOXX", datetime(2026, 5, 25))
    assert p.name == "iShares Semiconductor ETF"


def test_parse_etf_holdings_normalizes_weights() -> None:
    payload = [
        {"asset": "NVDA", "name": "NVIDIA Corp", "weightPercentage": 9.42, "sector": "Semis"},
        {"asset": "AVGO", "name": "Broadcom Inc", "weightPercentage": 8.51, "sector": "Semis"},
    ]
    rows = parse_etf_holdings(payload, "SOXX", date(2026, 5, 25), datetime(2026, 5, 25))
    assert len(rows) == 2
    assert rows[0].constituent_ticker == "NVDA"
    assert rows[0].weight_pct == pytest.approx(0.0942)
    assert rows[0].rank_position == 1
    assert rows[1].rank_position == 2


def test_parse_etf_holdings_tolerates_non_dict_items() -> None:
    payload = [
        {"asset": "NVDA", "weightPercentage": 9.42},
        "junk",  # noisy element
        None,
        {"asset": "AVGO", "weightPercentage": 8.51},
    ]
    rows = parse_etf_holdings(payload, "SOXX", date(2026, 5, 25), datetime(2026, 5, 25))
    assert len(rows) == 2
    assert [r.constituent_ticker for r in rows] == ["NVDA", "AVGO"]


def test_parse_etf_holdings_handles_cash_sleeve_with_null_ticker() -> None:
    payload = [
        {"asset": None, "name": "CASH & EQUIVALENTS", "weightPercentage": 0.42},
    ]
    rows = parse_etf_holdings(payload, "SOXX", date(2026, 5, 25), datetime(2026, 5, 25))
    assert len(rows) == 1
    assert rows[0].constituent_ticker is None
    assert rows[0].name == "CASH & EQUIVALENTS"


# ---------------------------------------------------------------------------
# 4. Ingest orchestrators — idempotency
# ---------------------------------------------------------------------------


def test_ingest_from_payloads_persists_profile_and_holdings(
    etf_db: sqlite3.Connection,
) -> None:
    info = [{"symbol": "SOXX", "name": "iShares Semiconductor ETF", "etfCompany": "BlackRock"}]
    holdings = [
        {"asset": "NVDA", "name": "NVIDIA", "weightPercentage": 9.42, "sector": "Semis"},
        {"asset": "AVGO", "name": "Broadcom", "weightPercentage": 8.51, "sector": "Semis"},
    ]
    profile, n = ingest_from_payloads(
        etf_db,
        "SOXX",
        info_payload=info,
        holdings_payload=holdings,
        as_of_date=date(2026, 5, 25),
        fetched_at=datetime(2026, 5, 25, 12, 0),
    )
    assert profile.name == "iShares Semiconductor ETF"
    assert n == 2
    assert get_etf_profile(etf_db, "SOXX") is not None
    assert len(get_etf_holdings(etf_db, "SOXX")) == 2


def test_ingest_from_payloads_is_idempotent(etf_db: sqlite3.Connection) -> None:
    info = [{"symbol": "SOXX", "name": "iShares Semiconductor ETF"}]
    holdings = [{"asset": "NVDA", "weightPercentage": 9.42}]
    for _ in range(3):
        ingest_from_payloads(
            etf_db,
            "SOXX",
            info_payload=info,
            holdings_payload=holdings,
            as_of_date=date(2026, 5, 25),
            fetched_at=datetime(2026, 5, 25, 12, 0),
        )
    profile_count = etf_db.execute("SELECT COUNT(*) AS n FROM etf_profile").fetchone()["n"]
    holdings_count = etf_db.execute("SELECT COUNT(*) AS n FROM etf_holdings").fetchone()["n"]
    assert profile_count == 1
    assert holdings_count == 1


def test_ingest_from_cache_reads_disk_fixtures(etf_db: sqlite3.Connection, tmp_path: Path) -> None:
    fmp_dir = tmp_path / "fmp"
    fmp_dir.mkdir()
    (fmp_dir / "SOXX_etf_info.json").write_text(
        json.dumps([{"symbol": "SOXX", "name": "iShares Semiconductor ETF"}]),
        encoding="utf-8",
    )
    (fmp_dir / "SOXX_etf_holdings.json").write_text(
        json.dumps([{"asset": "NVDA", "weightPercentage": 9.42}]),
        encoding="utf-8",
    )
    profile, n = ingest_from_cache(etf_db, "SOXX", fmp_dir=fmp_dir, as_of_date=date(2026, 5, 25))
    assert profile.name == "iShares Semiconductor ETF"
    assert n == 1


def test_ingest_from_cache_raises_when_files_missing(
    etf_db: sqlite3.Connection, tmp_path: Path
) -> None:
    fmp_dir = tmp_path / "fmp"
    fmp_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        ingest_from_cache(etf_db, "SOXX", fmp_dir=fmp_dir)


# ---------------------------------------------------------------------------
# 5. Section builders + dispatch
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo_root_with_db(etf_db: sqlite3.Connection, tmp_path: Path) -> Path:
    """Promote the etf_db to a repo-shaped tmp tree with data/portfolio.db."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Replicate the schema by closing the fixture conn and copying the file.
    etf_db.commit()
    src_path = Path(etf_db.execute("PRAGMA database_list").fetchone()["file"])
    target = data_dir / "portfolio.db"
    target.write_bytes(src_path.read_bytes())
    return tmp_path


def test_build_etf_brief_missing_when_profile_absent(repo_root_with_db: Path) -> None:
    brief = build_etf_brief("UNKNOWN_ETF", repo_root_with_db)
    assert brief.profile.status is SectionStatus.MISSING_DATA
    assert brief.holdings.status is SectionStatus.MISSING_DATA


def test_build_etf_brief_ok_after_ingest(etf_db: sqlite3.Connection, tmp_path: Path) -> None:
    info = [
        {
            "symbol": "SOXX",
            "name": "iShares Semiconductor ETF",
            "etfCompany": "BlackRock",
            "expenseRatio": 0.35,
        }
    ]
    holdings = [
        {"asset": "NVDA", "name": "NVIDIA", "weightPercentage": 9.42, "sector": "Semis"},
        {"asset": "AVGO", "name": "Broadcom", "weightPercentage": 8.51, "sector": "Semis"},
        {"asset": "AMD", "name": "AMD", "weightPercentage": 6.21, "sector": "Semis"},
    ]
    ingest_from_payloads(
        etf_db,
        "SOXX",
        info_payload=info,
        holdings_payload=holdings,
        as_of_date=date(2026, 5, 25),
        fetched_at=datetime(2026, 5, 25, 12, 0),
    )

    # Materialize the DB to a repo-shaped tmp dir so build_etf_brief can find it.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = data_dir / "portfolio.db"
    etf_db.commit()
    src_file = Path(etf_db.execute("PRAGMA database_list").fetchone()["file"])
    target.write_bytes(src_file.read_bytes())

    brief = build_etf_brief("SOXX", tmp_path)
    assert brief.profile.status is SectionStatus.OK
    assert brief.profile.name == "iShares Semiconductor ETF"
    assert brief.holdings.status is SectionStatus.OK
    assert brief.holdings.total_constituents == 3
    assert brief.holdings.top_holdings[0].constituent_ticker == "NVDA"
    # Sector breakdown should sum the three Semis weights.
    assert len(brief.holdings.sector_breakdown) == 1
    assert brief.holdings.sector_breakdown[0].sector == "Semis"
    assert brief.holdings.sector_breakdown[0].weight_pct == pytest.approx(0.0942 + 0.0851 + 0.0621)


def test_etf_markdown_renderer_emits_profile_and_holdings() -> None:
    from report.etf_models import (
        EtfHoldingRow,
        EtfHoldingsSection,
        EtfProfileSection,
        EtfSectorBreakdownRow,
    )

    brief = EtfBriefSpec(
        ticker="SOXX",
        generation_date=date(2026, 5, 25),
        repo_root="/tmp",
        profile=EtfProfileSection(
            status=SectionStatus.OK,
            ticker="SOXX",
            name="iShares Semiconductor ETF",
            issuer="BlackRock",
            expense_ratio=0.0035,
            aum_usd_m=14_500.0,
            benchmark_index="ICE Semiconductor Index",
        ),
        holdings=EtfHoldingsSection(
            status=SectionStatus.OK,
            as_of_date=date(2026, 5, 25),
            total_constituents=2,
            top_holdings=[
                EtfHoldingRow(
                    rank=1,
                    constituent_ticker="NVDA",
                    name="NVIDIA",
                    weight_pct=0.0942,
                    sector="Semis",
                ),
            ],
            sector_breakdown=[EtfSectorBreakdownRow(sector="Semis", weight_pct=0.0942)],
        ),
    )
    md = render_etf_markdown(brief)
    assert "# SOXX — ETF Brief" in md
    assert "iShares Semiconductor ETF" in md
    assert "35 bps" in md  # expense ratio formatted
    assert "$14.50B" in md  # AUM rolled up to billions
    assert "NVDA" in md
    assert "9.42%" in md  # weight rendered as percent
    assert "## §5 Holdings" in md


def test_etf_markdown_renderer_emits_missing_block_when_section_missing() -> None:
    from report.etf_models import (
        EtfHoldingsSection,
        EtfProfileSection,
    )
    from report.models import MissingReason

    brief = EtfBriefSpec(
        ticker="SOXX",
        generation_date=date(2026, 5, 25),
        repo_root="/tmp",
        profile=EtfProfileSection(
            status=SectionStatus.MISSING_DATA,
            ticker="SOXX",
            missing=MissingReason(
                stage="INGEST(fmp_etf)",
                fix_command="python execution/fetch_etf_data.py --ticker SOXX",
            ),
        ),
        holdings=EtfHoldingsSection(status=SectionStatus.MISSING_DATA),
    )
    md = render_etf_markdown(brief)
    assert "Missing data" in md
    assert "INGEST(fmp_etf)" in md


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
_SOXX_INFO_FIXTURE = [
    {
        "symbol": "SOXX",
        "name": "iShares Semiconductor ETF",
        "etfCompany": "BlackRock",
        "expenseRatio": 0.35,
        "aum": 14_500_000_000,
        "inceptionDate": "2001-07-13",
        "assetClass": "equity",
        "etfIndex": "ICE Semiconductor Index",
        "exchange": "NASDAQ",
        "domicile": "US",
        "yield": 0.85,
        "description": (
            "iShares Semiconductor ETF seeks to track the investment results "
            "of an index composed of US-listed equities in the semiconductor sector."
        ),
        "sector": "Semiconductors",
    }
]


_SOXX_HOLDINGS_FIXTURE = [
    {"asset": "NVDA", "name": "NVIDIA Corp", "weightPercentage": 9.42, "sector": "Semiconductors"},
    {"asset": "AVGO", "name": "Broadcom Inc", "weightPercentage": 8.51, "sector": "Semiconductors"},
    {
        "asset": "AMD",
        "name": "Advanced Micro Devices",
        "weightPercentage": 7.21,
        "sector": "Semiconductors",
    },
    {
        "asset": "TSM",
        "name": "Taiwan Semiconductor",
        "weightPercentage": 6.05,
        "sector": "Semiconductors",
    },
    {"asset": "INTC", "name": "Intel Corp", "weightPercentage": 5.88, "sector": "Semiconductors"},
    {"asset": "QCOM", "name": "Qualcomm Inc", "weightPercentage": 4.92, "sector": "Semiconductors"},
    {
        "asset": "MU",
        "name": "Micron Technology",
        "weightPercentage": 4.10,
        "sector": "Semiconductors",
    },
    {
        "asset": "AMAT",
        "name": "Applied Materials",
        "weightPercentage": 4.02,
        "sector": "Semiconductors",
    },
    {"asset": "LRCX", "name": "Lam Research", "weightPercentage": 3.65, "sector": "Semiconductors"},
    {"asset": "KLAC", "name": "KLA Corp", "weightPercentage": 3.22, "sector": "Semiconductors"},
    {
        "asset": "ASML",
        "name": "ASML Holding",
        "weightPercentage": 3.10,
        "sector": "Semiconductor Equipment",
    },
    {
        "asset": "MRVL",
        "name": "Marvell Technology",
        "weightPercentage": 2.41,
        "sector": "Semiconductors",
    },
]


def _materialize_db(etf_db: sqlite3.Connection, tmp_path: Path) -> Path:
    """Promote the in-memory fixture conn to a real file under repo_root/data/."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    etf_db.commit()
    src_path = Path(etf_db.execute("PRAGMA database_list").fetchone()["file"])
    target = data_dir / "portfolio.db"
    target.write_bytes(src_path.read_bytes())
    return tmp_path


def test_soxx_end_to_end_ingest_then_render(etf_db: sqlite3.Connection, tmp_path: Path) -> None:
    """The full ETF MVP arc: seed registry, ingest from fixtures, render brief.

    Mirrors what happens on a real onboard: SOXX added with instrument_type='etf',
    fetcher runs against cached JSON, brief renders as Markdown.
    """
    # 1. Register SOXX as a tracked ETF.
    _seed_tc(etf_db, "SOXX", "etf", list_type="portfolio")

    # 2. Drop fixture JSONs that the cached-mode fetcher will read.
    fmp_dir = tmp_path / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)
    (fmp_dir / "SOXX_etf_info.json").write_text(json.dumps(_SOXX_INFO_FIXTURE), encoding="utf-8")
    (fmp_dir / "SOXX_etf_holdings.json").write_text(
        json.dumps(_SOXX_HOLDINGS_FIXTURE), encoding="utf-8"
    )

    # 3. Ingest from the fixtures.
    profile, n = ingest_from_cache(etf_db, "SOXX", fmp_dir=fmp_dir, as_of_date=date(2026, 5, 25))
    assert profile.issuer == "BlackRock"
    assert n == 12

    # 4. Materialize the DB and build the brief.
    repo_root = _materialize_db(etf_db, tmp_path)
    brief = build_etf_brief("SOXX", repo_root)
    assert brief.profile.status is SectionStatus.OK
    assert brief.holdings.status is SectionStatus.OK
    assert brief.holdings.total_constituents == 12
    assert brief.holdings.top_holdings[0].constituent_ticker == "NVDA"
    assert brief.holdings.top_holdings[0].weight_pct == pytest.approx(0.0942)

    # Sector breakdown rolls up — we expect 2 distinct sectors (Semiconductors,
    # Semiconductor Equipment) ordered by total weight desc.
    sectors = [row.sector for row in brief.holdings.sector_breakdown]
    assert sectors == ["Semiconductors", "Semiconductor Equipment"]

    # 5. Render the markdown and sanity-check structural anchors.
    md = render_etf_markdown(brief)
    assert "SOXX" in md
    assert "iShares Semiconductor ETF" in md
    assert "BlackRock" in md
    assert "35 bps" in md
    assert "## §1 ETF Profile" in md
    assert "## §5 Holdings" in md
    # Sector breakdown table appears.
    assert "Semiconductor Equipment" in md


def test_build_artifacts_dispatches_to_etf_path(
    etf_db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`execution/build_artifacts._build_one` must route ETF tickers to the
    ETF-shaped builder (Markdown only) and equity tickers to the existing
    equity pipeline (unchanged)."""
    _seed_tc(etf_db, "SOXX", "etf", list_type="portfolio")
    fmp_dir = tmp_path / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)
    (fmp_dir / "SOXX_etf_info.json").write_text(json.dumps(_SOXX_INFO_FIXTURE), encoding="utf-8")
    (fmp_dir / "SOXX_etf_holdings.json").write_text(
        json.dumps(_SOXX_HOLDINGS_FIXTURE), encoding="utf-8"
    )
    ingest_from_cache(etf_db, "SOXX", fmp_dir=fmp_dir, as_of_date=date(2026, 5, 25))
    repo_root = _materialize_db(etf_db, tmp_path)

    # Import here so the conftest's path setup wins.
    sys.path.insert(0, str(PROJECT_ROOT))
    from execution.build_artifacts import _build_one  # pyright: ignore[reportPrivateUsage]

    result = _build_one(
        "SOXX",
        repo_root,
        enable_llm=False,
        news_days=7,
        news_cache_ttl_days=7,
        refresh_news=False,
    )
    assert result["instrument_kind"] == "etf"
    md_path = Path(cast("str", result["report_md"]))
    assert md_path.exists()
    md = md_path.read_text(encoding="utf-8")
    assert "iShares Semiconductor ETF" in md
    assert "## §5 Holdings" in md


# ---------------------------------------------------------------------------
# 8. Credential redaction (security regression)
#
# _fmp_get sends the FMP key as a ?apikey=<key> URL query param. On a network
# error, requests/urllib3 embed the fully-resolved URL — key included — in the
# exception string (e.g. "Max retries exceeded with url: /stable/...?apikey=KEY").
# That string must never reach the raised RuntimeError / logs unredacted.
# GEMINI.md: never log a URL that may contain credentials.
# ---------------------------------------------------------------------------


def test_fmp_get_does_not_leak_api_key_on_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RequestException whose string embeds the resolved ``?apikey=<key>`` URL
    must not surface the key in the RuntimeError ``_fmp_get`` raises, nor in any
    exception chained onto it."""
    from execution.fetch_etf_data import _fmp_get  # pyright: ignore[reportPrivateUsage]
    from net.client import HttpCallError, HttpErrorKind

    secret = "FMPKEY_SUPERSECRET_9f8e7d6c5b4a"

    def _leaky_client(url: str, **kwargs: object) -> object:
        key = kwargs.get("api_key", "")
        raise HttpCallError(
            kind=HttpErrorKind.NETWORK,
            message=f"connection failed for {url}?apikey={key}",
            retryable=True,
        )

    monkeypatch.setattr("execution.fetch_etf_data.FMP_CLIENT.get_url_json", _leaky_client)

    with pytest.raises(RuntimeError) as excinfo:
        _fmp_get(secret, "SOXX", "etf/info")

    raised = excinfo.value
    # The secret must not appear anywhere reachable from the raised exception:
    # not the message, and not a chained __cause__/__context__ whose own str()
    # would otherwise re-expose the unredacted URL.
    leak_surface = " ".join(str(p) for p in (raised, raised.__cause__, raised.__context__))
    assert secret not in leak_surface
    # Positively confirm the redactor ran (key masked, not merely truncated off).
    assert "apikey=***" in str(raised)
    # `from None`: no original RequestException is chained onto the RuntimeError.
    assert raised.__cause__ is None
