"""ETF published-data sources (directives/etf_data.md).

Covers, network-free throughout (fixtures + monkeypatching):
- N-PORT: fund-map resolution, primary-doc parsing (weights → decimal
  fractions, country, identifier tickers, rank), the schema-drift halt
  (NportParseError + raw-XML dump), accession selection, series matching.
- Vanguard adapter: page parsing (percent → decimal, asOfDate, next link),
  pagination + rank assembly in fetch().
- Issuer registry: unmapped ticker, adapter exception → soft None.
- Ingest orchestration: spine upsert w/ country, already-done idempotency,
  characteristics read-modify-write merge, price fallback statuses.
- Migration 0144 round-trip on the 0044 substrate.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from etf_sources import ingest as etf_ingest  # noqa: E402
from etf_sources import nport, vanguard  # noqa: E402
from etf_sources.issuer_registry import (  # noqa: E402
    IssuerCharacteristics,
    IssuerData,
    fetch_issuer_data,
)
from etf_sources.nport import (  # noqa: E402
    FundRef,
    NportParseError,
    _recent_nport_accessions,
    _resolve_fund_from_payload,
    parse_nport,
)
from instrument_store import get_etf_holdings, get_etf_profile, upsert_etf_profile  # noqa: E402
from models.instruments import EtfHolding, EtfProfile  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


NPORT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/nport">
  <headerData><filerInfo><seriesClassInfo>
    <seriesId>S000068000</seriesId><classId>C000217000</classId>
  </seriesClassInfo></filerInfo></headerData>
  <formData>
    <genInfo>
      <regName>Test ETF Trust</regName>
      <seriesName>Test Intl Small Cap Value ETF</seriesName>
      <seriesId>S000068000</seriesId>
      <repPdEnd>2026-08-31</repPdEnd>
      <repPdDate>2026-05-31</repPdDate>
    </genInfo>
    <invstOrSecs>
      <invstOrSec>
        <name>Alpha Industries PLC</name>
        <lei>XXX</lei>
        <title>Alpha Industries PLC</title>
        <cusip>000000000</cusip>
        <identifiers><isin value="GB0000000001"/><ticker value="ALFA"/></identifiers>
        <balance>1000</balance>
        <valUSD>250000.50</valUSD>
        <pctVal>2.5</pctVal>
        <invCountry>GB</invCountry>
      </invstOrSec>
      <invstOrSec>
        <name>Beta Holdings KK</name>
        <title>Beta Holdings KK</title>
        <identifiers><isin value="JP0000000002"/></identifiers>
        <balance>500</balance>
        <valUSD>750000</valUSD>
        <pctVal>7.5</pctVal>
        <invCountry>JP</invCountry>
      </invstOrSec>
      <invstOrSec>
        <name>CASH &amp; EQUIVALENTS</name>
        <valUSD>10000</valUSD>
        <pctVal>0.1</pctVal>
      </invstOrSec>
    </invstOrSecs>
  </formData>
</edgarSubmission>
"""

FUND_MAP = {
    "fields": ["cik", "seriesId", "classId", "symbol"],
    "data": [
        ["0001111111", "S000099999", "C000111111", "OTHR"],
        ["0002222222", "S000068000", "C000217000", "TEST"],
    ],
}

SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["NPORT-P", "10-K", "NPORT-P"],
            "accessionNumber": ["0001-26-000002", "0001-26-000001", "0001-26-000003"],
            "filingDate": ["2026-06-25", "2026-06-01", "2026-06-28"],
            "primaryDocument": ["primary_doc.xml", "tenk.htm", "primary_doc.xml"],
        }
    }
}

VANGUARD_PAGE = {
    "size": 3,
    "asOfDate": "2026-05-31",
    "next": "https://example.test/page2",
    "fund": {
        "entity": [
            {
                "longName": "Taiwan Semiconductor Manufacturing Co. Ltd.",
                "shortName": "TSMC",
                "sharesHeld": "336692988",
                "marketValue": 24944013049.51,
                "ticker": "2330",
                "isin": "TW0002330008",
                "percentWeight": "14.64",
            },
            {
                "longName": "Tencent Holdings Ltd.",
                "sharesHeld": "100",
                "marketValue": 5.0,
                "ticker": "700",
                "percentWeight": "2.74",
            },
        ]
    },
}


ETF_DDL = """
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER DEFAULT 1,
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    list_type TEXT NOT NULL,
    instrument_type VARCHAR,
    archived_at TIMESTAMP,
    UNIQUE(user_id, ticker)
);
CREATE TABLE etf_profile (
    ticker TEXT PRIMARY KEY,
    name TEXT, issuer TEXT, expense_ratio REAL, aum_usd_m REAL,
    inception_date TEXT, asset_class TEXT, benchmark_index TEXT,
    domicile TEXT, listed_exchange TEXT, distribution_yield REAL,
    description TEXT, sector_label TEXT, nav REAL, price REAL,
    premium_discount_pct REAL,
    pe_ratio REAL, pb_ratio REAL, weighted_avg_mktcap_usd_m REAL,
    characteristics_as_of TEXT, characteristics_source TEXT,
    source TEXT NOT NULL DEFAULT 'fmp',
    profile_fetched_at TIMESTAMP NOT NULL
);
CREATE TABLE etf_holdings (
    ticker TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    constituent_ticker TEXT,
    name TEXT, weight_pct REAL, shares_held REAL, market_value_usd REAL,
    sector TEXT, asset_class TEXT, rank_position INTEGER, country TEXT,
    source TEXT NOT NULL DEFAULT 'fmp',
    fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, as_of_date, constituent_ticker)
);
"""


@pytest.fixture()
def etf_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(tmp_path / "etf.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(ETF_DDL)
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# N-PORT — resolution
# ---------------------------------------------------------------------------


def test_resolve_fund_from_payload() -> None:
    ref = _resolve_fund_from_payload(FUND_MAP, "test")
    assert ref == FundRef(ticker="TEST", cik=2222222, series_id="S000068000", class_id="C000217000")


def test_resolve_fund_missing_symbol_is_none() -> None:
    assert _resolve_fund_from_payload(FUND_MAP, "NOPE") is None


def test_resolve_fund_malformed_payload_is_none() -> None:
    assert _resolve_fund_from_payload({"data": "junk"}, "TEST") is None
    assert _resolve_fund_from_payload(None, "TEST") is None


def test_recent_nport_accessions_sorted_and_filtered() -> None:
    hits = _recent_nport_accessions(SUBMISSIONS)
    assert [a for _, a, _ in hits] == ["0001-26-000003", "0001-26-000002"]
    assert all(doc == "primary_doc.xml" for _, _, doc in hits)


# ---------------------------------------------------------------------------
# N-PORT — parsing
# ---------------------------------------------------------------------------


def test_parse_nport_full_report() -> None:
    report = parse_nport(NPORT_XML, "test", accession="0001-26-000003")
    assert report.series_id == "S000068000"
    assert report.rep_period_date == date(2026, 5, 31)
    assert len(report.holdings) == 3
    # Sorted by weight desc → Beta (7.5%) first, rank assigned.
    top = report.holdings[0]
    assert top.name == "Beta Holdings KK"
    assert top.weight_pct == pytest.approx(0.075)  # 7.5 pct → decimal fraction
    assert top.country == "JP"
    assert top.constituent_ticker is None  # no ticker identifier disclosed
    assert top.rank_position == 1
    second = report.holdings[1]
    assert second.constituent_ticker == "ALFA"
    assert second.country == "GB"
    assert second.shares_held == 1000
    assert second.market_value_usd == pytest.approx(250000.50)
    cash = report.holdings[2]
    assert cash.constituent_ticker is None
    assert cash.weight_pct == pytest.approx(0.001)
    assert all(h.source == "nport" for h in report.holdings)
    assert all(h.ticker == "TEST" for h in report.holdings)


def test_parse_nport_unparseable_xml_raises() -> None:
    with pytest.raises(NportParseError, match="unparseable"):
        parse_nport("<not-closed", "TEST")


def test_parse_nport_missing_geninfo_raises() -> None:
    with pytest.raises(NportParseError, match="genInfo"):
        parse_nport("<root><other/></root>", "TEST")


def test_parse_nport_no_holdings_raises() -> None:
    xml = (
        "<edgarSubmission><formData><genInfo><seriesId>S1</seriesId>"
        "<repPdDate>2026-05-31</repPdDate></genInfo></formData></edgarSubmission>"
    )
    with pytest.raises(NportParseError, match="invstOrSec"):
        parse_nport(xml, "TEST")


def test_fetch_latest_report_matches_series_and_dumps_on_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = FundRef(ticker="TEST", cik=2222222, series_id="S000068000", class_id="C000217000")

    responses: dict[str, object] = {
        nport.EDGAR_SUBMISSIONS_URL.format(cik=ref.cik): SUBMISSIONS,
        nport.EDGAR_FILE_URL.format(
            cik_int=ref.cik, acc="000126000003", name="primary_doc.xml"
        ): NPORT_XML,
    }

    def fake_get(url: str, *, user_agent: str, as_json: bool) -> object | str | None:
        return responses.get(url)

    monkeypatch.setattr(nport, "_sec_get", fake_get)
    report = nport.fetch_latest_report(ref, tmp_dir=tmp_path)
    assert report is not None and report.accession == "0001-26-000003"

    # Drift: the newest accession returns junk XML → halt + dump.
    responses[
        nport.EDGAR_FILE_URL.format(cik_int=ref.cik, acc="000126000003", name="primary_doc.xml")
    ] = "<html>WAF page</html>"
    with pytest.raises(NportParseError, match="dumped"):
        nport.fetch_latest_report(ref, tmp_dir=tmp_path)
    assert (tmp_path / "TEST_000126000003.xml").exists()


def test_fetch_latest_report_wrong_series_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = FundRef(ticker="TEST", cik=2222222, series_id="S000012345", class_id="C1")

    def fake_get(url: str, *, user_agent: str, as_json: bool) -> object | str | None:
        if url.endswith(".json"):
            return SUBMISSIONS
        return NPORT_XML  # a valid doc, but for series S000068000

    monkeypatch.setattr(nport, "_sec_get", fake_get)
    assert nport.fetch_latest_report(ref, tmp_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# Vanguard adapter
# ---------------------------------------------------------------------------


def test_vanguard_parse_holdings_page() -> None:
    ts = datetime(2026, 7, 10, 12, 0, 0)
    rows, as_of, next_url = vanguard.parse_holdings_page(VANGUARD_PAGE, "vwo", ts)
    assert as_of == date(2026, 5, 31)
    assert next_url == "https://example.test/page2"
    assert len(rows) == 2
    tsmc = rows[0]
    assert tsmc.ticker == "VWO"
    assert tsmc.constituent_ticker == "2330"
    assert tsmc.weight_pct == pytest.approx(0.1464)
    assert tsmc.shares_held == pytest.approx(336692988.0)
    assert tsmc.as_of_date == date(2026, 5, 31)
    assert tsmc.source == "issuer:vanguard"


def test_vanguard_parse_holdings_page_malformed() -> None:
    ts = datetime(2026, 7, 10)
    assert vanguard.parse_holdings_page(None, "VWO", ts) == ([], None, None)
    assert vanguard.parse_holdings_page({"fund": "junk"}, "VWO", ts) == ([], None, None)


def test_vanguard_fetch_paginates_and_ranks(monkeypatch: pytest.MonkeyPatch) -> None:
    page2 = {
        "asOfDate": "2026-05-31",
        "fund": {
            "entity": [
                {"longName": "Third Co", "ticker": "THRD", "percentWeight": "20.00"},
            ]
        },
    }
    profile = {"fundProfile": {"longName": "Test EM ETF", "expenseRatio": "0.0600"}}

    def fake_get_json(session: object, url: str) -> object | None:
        if url.endswith("/portfolio-holding/stock"):
            return VANGUARD_PAGE
        if url == "https://example.test/page2":
            return page2
        if url.endswith("/profile"):
            return profile
        return None

    monkeypatch.setattr(vanguard, "_get_json", fake_get_json)
    data = vanguard.fetch("VWO")
    assert data is not None
    assert [h.constituent_ticker for h in data.holdings] == ["THRD", "2330", "700"]
    assert [h.rank_position for h in data.holdings] == [1, 2, 3]
    assert data.holdings_as_of == date(2026, 5, 31)
    assert data.characteristics is not None
    assert data.characteristics.expense_ratio == pytest.approx(0.0006)  # 0.06% → decimal
    assert data.characteristics.issuer == "Vanguard"


# ---------------------------------------------------------------------------
# Issuer registry
# ---------------------------------------------------------------------------


def test_registry_unmapped_ticker_is_none() -> None:
    assert fetch_issuer_data("AVDV") is None  # Avantis: no adapter yet, by design


def test_registry_adapter_exception_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    import etf_sources.issuer_registry as reg

    def boom(ticker: str) -> IssuerData | None:
        raise RuntimeError("page redesigned")

    monkeypatch.setattr(reg, "_adapters", lambda: {"vanguard": boom})
    assert fetch_issuer_data("VWO") is None


# ---------------------------------------------------------------------------
# Ingest orchestration
# ---------------------------------------------------------------------------


def _report(as_of: date = date(2026, 5, 31)) -> nport.NportReport:
    parsed = parse_nport(NPORT_XML, "TEST", accession="acc-1")
    return nport.NportReport(
        series_id=parsed.series_id,
        rep_period_date=as_of,
        holdings=[h.model_copy(update={"as_of_date": as_of}) for h in parsed.holdings],
        accession="acc-1",
        n_rows_skipped=0,
    )


def test_refresh_published_data_spine_and_idempotency(
    etf_db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(etf_ingest.nport, "fetch_holdings", lambda t, **kw: _report())
    monkeypatch.setattr(etf_ingest, "fetch_issuer_data", lambda t: None)
    monkeypatch.setattr(etf_ingest, "fetch_proxy_series", lambda t, period: [])

    result = etf_ingest.refresh_published_data(etf_db, "TEST", tmp_path)
    assert result.nport_status == "ingested"
    assert result.nport_rows == 3
    assert result.issuer_status == "unavailable"
    assert result.price_status == "failed"  # no closes anywhere, fetch returned []

    rows = get_etf_holdings(etf_db, "TEST")
    assert len(rows) == 3
    assert rows[0].country == "JP"
    assert rows[0].source == "nport"

    # Second run: same report already on file → explicit already_done.
    result2 = etf_ingest.refresh_published_data(etf_db, "TEST", tmp_path)
    assert result2.nport_status == "already_done"


def test_refresh_published_data_issuer_overlay_and_prices(
    etf_db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = IssuerData(
        source="issuer:vanguard",
        holdings=[
            EtfHolding(
                ticker="TEST",
                as_of_date=date(2026, 6, 30),
                constituent_ticker="FRSH",
                name="Fresh Co",
                weight_pct=0.05,
                rank_position=1,
                source="issuer:vanguard",
                fetched_at=datetime(2026, 7, 10),
            )
        ],
        holdings_as_of=date(2026, 6, 30),
        characteristics=IssuerCharacteristics(
            source="issuer:vanguard", expense_ratio=0.0006, name="Test EM ETF", issuer="Vanguard"
        ),
    )
    monkeypatch.setattr(etf_ingest.nport, "fetch_holdings", lambda t, **kw: None)
    monkeypatch.setattr(etf_ingest, "fetch_issuer_data", lambda t: overlay)
    monkeypatch.setattr(
        etf_ingest, "fetch_proxy_series", lambda t, period: [(date(2026, 7, 9), 100.0)]
    )

    result = etf_ingest.refresh_published_data(etf_db, "TEST", tmp_path)
    assert result.nport_status == "unavailable"
    assert result.issuer_status == "ingested"
    assert result.issuer_rows == 1
    assert result.characteristics_applied is True
    assert result.price_status == "fetched"
    assert (tmp_path / "data" / "factor_proxies" / "TEST.json").exists()

    profile = get_etf_profile(etf_db, "TEST")
    assert profile is not None
    assert profile.expense_ratio == pytest.approx(0.0006)
    assert profile.characteristics_source == "issuer:vanguard"

    # Newest snapshot wins on read.
    rows = get_etf_holdings(etf_db, "TEST")
    assert [h.constituent_ticker for h in rows] == ["FRSH"]


def test_apply_characteristics_merges_without_blanking(etf_db: sqlite3.Connection) -> None:
    upsert_etf_profile(
        etf_db,
        EtfProfile(
            ticker="TEST",
            name="Existing Name",
            issuer="Existing Issuer",
            expense_ratio=0.0035,
            pe_ratio=17.0,
            source="fmp",
            profile_fetched_at=datetime(2026, 1, 1),
        ),
    )
    etf_ingest.apply_characteristics(
        etf_db,
        "TEST",
        IssuerCharacteristics(source="issuer:vanguard", pb_ratio=1.4, as_of=date(2026, 6, 30)),
    )
    merged = get_etf_profile(etf_db, "TEST")
    assert merged is not None
    assert merged.name == "Existing Name"  # not blanked by a sparse overlay
    assert merged.expense_ratio == pytest.approx(0.0035)  # overlay had none → kept
    assert merged.pe_ratio == pytest.approx(17.0)  # kept
    assert merged.pb_ratio == pytest.approx(1.4)  # merged in
    assert merged.characteristics_as_of == date(2026, 6, 30)
    assert merged.characteristics_source == "issuer:vanguard"


def test_refresh_skips_price_fetch_when_closes_exist(
    etf_db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from factor_proxies import store_proxy_series

    store_proxy_series(tmp_path, "TEST", [(date(2026, 7, 8), 99.0), (date(2026, 7, 9), 100.0)])
    monkeypatch.setattr(etf_ingest.nport, "fetch_holdings", lambda t, **kw: None)
    monkeypatch.setattr(etf_ingest, "fetch_issuer_data", lambda t: None)

    def no_fetch(t: str, period: str) -> list[tuple[date, float]]:
        raise AssertionError("must not fetch when closes are on file")

    monkeypatch.setattr(etf_ingest, "fetch_proxy_series", no_fetch)
    result = etf_ingest.refresh_published_data(etf_db, "TEST", tmp_path)
    assert result.price_status == "present"
    assert result.price_rows == 2


# ---------------------------------------------------------------------------
# Migration 0144 round-trip
# ---------------------------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_migration_0144_roundtrip(tmp_path: Path) -> None:
    from alembic.config import Config

    from alembic import command

    db = tmp_path / "m.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(ETF_DDL)
    # Strip the new columns to simulate the 0044-era substrate.
    conn.executescript(
        """
        ALTER TABLE etf_holdings DROP COLUMN country;
        ALTER TABLE etf_profile DROP COLUMN pe_ratio;
        ALTER TABLE etf_profile DROP COLUMN pb_ratio;
        ALTER TABLE etf_profile DROP COLUMN weighted_avg_mktcap_usd_m;
        ALTER TABLE etf_profile DROP COLUMN characteristics_as_of;
        ALTER TABLE etf_profile DROP COLUMN characteristics_source;
        """
    )
    conn.commit()
    conn.close()

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, "0143_v_thesis_status_view")
    command.upgrade(cfg, "0144_etf_published_data")

    conn = sqlite3.connect(str(db))
    try:
        assert "country" in _columns(conn, "etf_holdings")
        assert {"pe_ratio", "pb_ratio", "characteristics_source"} <= _columns(conn, "etf_profile")
    finally:
        conn.close()

    # Idempotent re-upgrade + clean downgrade.
    command.stamp(cfg, "0143_v_thesis_status_view")
    command.upgrade(cfg, "0144_etf_published_data")
    command.downgrade(cfg, "0143_v_thesis_status_view")
    conn = sqlite3.connect(str(db))
    try:
        assert "country" not in _columns(conn, "etf_holdings")
        assert "pe_ratio" not in _columns(conn, "etf_profile")
    finally:
        conn.close()


def test_migration_0144_noops_without_etf_tables(tmp_path: Path) -> None:
    from alembic.config import Config

    from alembic import command

    db = tmp_path / "bare.db"
    sqlite3.connect(str(db)).close()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, "0143_v_thesis_status_view")
    command.upgrade(cfg, "0144_etf_published_data")  # must not raise
