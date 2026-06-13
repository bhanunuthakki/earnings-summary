"""EDGAR 13F-HR miner (src/discovery/thirteenf.py) + its writer
(execution/fetch_13f.py) + the run_discovery investor-lane integration.

Pins the Discovery rule's investor leg: parse the INFORMATION TABLE, diff vs
the prior quarter (new/add/trim/exit), resolve a holding to a universe ticker
(CUSIP then issuer-name), route untracked→signal / tracked→news, and — folded
through run_discovery's single scorer — surface a corroborated investor-only
name (clamped) while a lone fund's buy stays below the entry bar. Network is
monkeypatched throughout; no live SEC calls.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from discovery import thirteenf
from discovery.scoring import INVESTOR_ONLY_CAP
from discovery.store import list_candidates, list_signals
from discovery.thirteenf import (
    Position,
    UniverseResolver,
    build_universe_resolver,
    diff_positions,
    mine_manager,
    parse_info_table,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import fetch_13f  # noqa: E402
import run_discovery  # noqa: E402

# An INFORMATION TABLE with a namespace prefix + a nested shares amount + a
# multi-row issuer (two share classes of one CUSIP) — the shapes the parser
# must survive.
_INFO_TABLE_XML = """<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>ADVANCED MICRO DEVICES INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>007903107</cusip>
    <value>45039402</value>
    <shrsOrPrnAmt><sshPrnamt>221400</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>ACME ROBOTICS CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>000111222</cusip>
    <value>30000000</value>
    <shrsOrPrnAmt><sshPrnamt>100000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>ACME ROBOTICS CORP</nameOfIssuer>
    <titleOfClass>COM CL B</titleOfClass>
    <cusip>000111222</cusip>
    <value>10000000</value>
    <shrsOrPrnAmt><sshPrnamt>40000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
</informationTable>"""


# ----------------------------------------------------------------------------
# parsing + diff
# ----------------------------------------------------------------------------


def test_parse_info_table_namespaced_and_nested() -> None:
    positions = parse_info_table(_INFO_TABLE_XML)
    by_cusip = {p.cusip: p for p in positions}
    # Two raw rows for ACME's one CUSIP are parsed separately (aggregation is a
    # later step), AMD once.
    assert by_cusip["007903107"].name == "ADVANCED MICRO DEVICES INC"
    assert by_cusip["007903107"].shares == 221400
    assert by_cusip["007903107"].value == 45039402
    assert len([p for p in positions if p.cusip == "000111222"]) == 2


def test_parse_info_table_bad_xml_is_empty() -> None:
    assert parse_info_table("<not valid") == []
    assert parse_info_table("") == []


def test_diff_classifies_moves() -> None:
    prior = [
        Position("AAA", "Alpha", value=100.0, shares=100.0),
        Position("BBB", "Beta", value=100.0, shares=100.0),  # will be trimmed
        Position("CCC", "Gamma", value=100.0, shares=100.0),  # will be exited
    ]
    current = [
        Position("AAA", "Alpha", value=150.0, shares=150.0),  # add
        Position("BBB", "Beta", value=50.0, shares=50.0),  # trim
        Position("DDD", "Delta", value=200.0, shares=200.0),  # new
    ]
    by_cusip = {m.cusip: m for m in diff_positions(current, prior)}
    assert by_cusip["AAA"].action == "add"
    assert by_cusip["BBB"].action == "trim"
    assert by_cusip["CCC"].action == "exit"
    assert by_cusip["DDD"].action == "new"
    # pct_of_book is share of the CURRENT total (150+50+200 = 400).
    assert by_cusip["DDD"].pct_of_book == pytest.approx(0.5)
    assert by_cusip["CCC"].pct_of_book == 0.0  # exits carry no current weight


def test_diff_aggregates_multi_class_issuer() -> None:
    positions = parse_info_table(_INFO_TABLE_XML)
    moves = {m.cusip: m for m in diff_positions(positions, [])}
    # ACME's two share-class rows collapse to one new position (40M of 85M book).
    assert moves["000111222"].action == "new"
    assert moves["000111222"].pct_of_book == pytest.approx(40_000_000 / 85_039_402, abs=1e-4)


# ----------------------------------------------------------------------------
# universe resolver (CUSIP then issuer-name)
# ----------------------------------------------------------------------------


def test_resolver_cusip_then_name(tmp_path: Path) -> None:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tracked_companies (user_id TEXT, ticker TEXT, name TEXT, list_type TEXT)"
    )
    conn.executemany(
        "INSERT INTO tracked_companies VALUES ('bhanu', ?, ?, ?)",
        [
            ("AMD", "Advanced Micro Devices", "index_member"),  # discoverable
            ("ACME", "Acme Robotics", "index_member"),
            ("NVDA", "NVIDIA Corp", "portfolio"),  # tracked (active book)
        ],
    )
    conn.commit()
    conn.close()
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    (fmp / "AMD_profile.json").write_text(
        json.dumps(
            [{"symbol": "AMD", "companyName": "Advanced Micro Devices", "cusip": "007903107"}]
        ),
        encoding="utf-8",
    )
    resolver = build_universe_resolver(db, fmp)
    # CUSIP hit (AMD), untracked.
    assert resolver.resolve("007903107", "WHATEVER") == ("AMD", False)
    # Name fallback (ACME has no profile cache → no CUSIP).
    assert resolver.resolve("999999999", "Acme Robotics Inc.") == ("ACME", False)
    # Tracked name routes tracked=True.
    assert resolver.resolve("888888888", "NVIDIA Corp") == ("NVDA", True)
    # Unknown name/cusip → None (not in our universe).
    assert resolver.resolve("123", "Mystery Holdings") is None


# ----------------------------------------------------------------------------
# mine_manager with a monkeypatched SEC client
# ----------------------------------------------------------------------------


def _fake_sec(
    monkeypatch: pytest.MonkeyPatch, *, info_xml: str, prior_xml: str | None = None
) -> None:
    submissions = {
        "filings": {
            "recent": {
                "form": ["13F-HR", "8-K", "13F-HR"],
                "accessionNumber": ["0001-26-000002", "0001-26-000001", "0001-26-000000"],
                "filingDate": ["2026-05-15", "2026-04-01", "2026-02-14"],
            }
        }
    }
    index_cur = {
        "directory": {"item": [{"name": "primary_doc.xml"}, {"name": "Form13FInfoTable.xml"}]}
    }

    def fake_get(url: str, *, user_agent: str, as_json: bool) -> object | str | None:
        if "submissions" in url:
            return submissions
        if url.endswith("index.json"):
            return index_cur
        if "000002" in url:  # current quarter info table
            return info_xml
        if "000000" in url and prior_xml is not None:  # prior quarter
            return prior_xml
        return ""

    monkeypatch.setattr(thirteenf, "_sec_get", fake_get)


def test_mine_manager_emits_new_buys(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_sec(monkeypatch, info_xml=_INFO_TABLE_XML)
    resolver = UniverseResolver(by_cusip={"007903107": ("AMD", False), "000111222": ("ACME", True)})
    hits = mine_manager("0001656456", "appaloosa", resolver=resolver)
    by_ticker = {h.ticker: h for h in hits}
    assert set(by_ticker) == {"AMD", "ACME"}  # both are new buys (no prior)
    assert by_ticker["AMD"].action == "new"
    assert by_ticker["AMD"].tracked is False
    assert by_ticker["ACME"].tracked is True  # routed to the news lane
    assert by_ticker["AMD"].source_key == "appaloosa"
    assert by_ticker["AMD"].observed_at == "2026-05-15"
    assert "NEW" in by_ticker["AMD"].detail


# ----------------------------------------------------------------------------
# persistence + the run_discovery scorer integration
# ----------------------------------------------------------------------------

_DDL = """
CREATE TABLE tracked_companies (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL
  DEFAULT 'bhanu', ticker TEXT NOT NULL, name TEXT NOT NULL, list_type TEXT NOT NULL);
CREATE TABLE news (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, headline TEXT NOT NULL,
  url TEXT NOT NULL, published_at TEXT NOT NULL, snippet TEXT, source TEXT,
  source_feed TEXT DEFAULT 'fmp_stock_news', fetched_at TEXT NOT NULL DEFAULT '2026-06-13 00:00:00',
  UNIQUE (ticker, url));
CREATE TABLE discovery_candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL
  DEFAULT 'bhanu', ticker TEXT NOT NULL, name TEXT, status TEXT NOT NULL DEFAULT 'new',
  score FLOAT NOT NULL DEFAULT 0, evidence_json TEXT NOT NULL, score_json TEXT,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CONSTRAINT ck_score_json CHECK (score_json IS NULL OR json_valid(score_json)),
  CONSTRAINT uq UNIQUE (user_id, ticker));
CREATE TABLE discovery_signals (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL
  DEFAULT 'bhanu', ticker TEXT NOT NULL, signal_class TEXT NOT NULL, source_key TEXT NOT NULL,
  weight FLOAT NOT NULL DEFAULT 1.0, raw_strength FLOAT NOT NULL DEFAULT 1.0, observed_at TEXT NOT NULL,
  detail TEXT, meta_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CONSTRAINT uq_sig UNIQUE (user_id, ticker, signal_class, source_key));
CREATE TABLE discovery_sources (source_key TEXT PRIMARY KEY, signal_class TEXT NOT NULL,
  display_name TEXT NOT NULL, base_weight FLOAT NOT NULL DEFAULT 1.0, tier TEXT NOT NULL
  DEFAULT 'structural', style_tags TEXT, cik TEXT, active INTEGER NOT NULL DEFAULT 1,
  last_calibrated_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    conn.executemany(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type) VALUES ('bhanu', ?, ?, ?)",
        [
            ("SOLO", "Solo Corp", "index_member"),
            ("DUET", "Duet Inc", "index_member"),
            ("NVDA", "NVIDIA Corp", "portfolio"),
        ],
    )
    conn.executemany(
        "INSERT INTO discovery_sources (source_key, signal_class, display_name, base_weight,"
        " tier, style_tags, cik, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'seed', 'seed')",
        [
            ("appaloosa", "investor_13f", "Appaloosa", 0.85, "multi_cycle", "hedge", "0001656456"),
            ("whale_rock", "investor_13f", "Whale Rock", 0.9, "crossover", "hedge", "0001000001"),
        ],
    )
    conn.commit()
    conn.close()
    return tmp_path


def _hit(ticker: str, source_key: str, *, tracked: bool, pct: float) -> thirteenf.InvestorHit:
    return thirteenf.InvestorHit(
        ticker=ticker,
        name=f"{ticker} Co",
        tracked=tracked,
        source_key=source_key,
        action="new",
        pct_of_book=pct,
        observed_at="2026-05-15",
        detail=f"NEW {pct * 100:.1f}% position (2026-05-15)",
        filing_url="https://www.sec.gov/Archives/edgar/data/1/x/",
    )


def test_persist_hits_routes_untracked_and_tracked(repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    hits = [
        _hit("SOLO", "appaloosa", tracked=False, pct=0.05),  # untracked → signal
        _hit("NVDA", "whale_rock", tracked=True, pct=0.04),  # tracked → news
    ]
    summary = fetch_13f.persist_hits(hits, db_path=db)
    assert summary["signals"] == 1
    assert summary["tracked_news"] == 1
    sigs = list_signals("SOLO", db_path=db)
    assert len(sigs) == 1 and sigs[0].source_key == "appaloosa"
    assert sigs[0].meta["action"] == "new"
    conn = sqlite3.connect(db)
    news = conn.execute("SELECT ticker, source_feed FROM news").fetchall()
    conn.close()
    assert news == [("NVDA", "edgar_13f")]


def test_persist_hits_replaces_investor_class(repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    fetch_13f.persist_hits([_hit("SOLO", "appaloosa", tracked=False, pct=0.05)], db_path=db)
    # A later run where the manager no longer holds SOLO removes its signal.
    fetch_13f.persist_hits([_hit("DUET", "appaloosa", tracked=False, pct=0.05)], db_path=db)
    assert list_signals("SOLO", db_path=db) == []
    assert len(list_signals("DUET", db_path=db)) == 1


def test_corroborated_investor_only_name_surfaces_clamped(repo: Path) -> None:
    """Two funds' new buys on an untracked name → it ENTERS the queue (clears
    the entry bar) but is clamped (can't top it on investor weight alone)."""
    db = repo / "data" / "portfolio.db"
    fetch_13f.persist_hits(
        [
            _hit("DUET", "appaloosa", tracked=False, pct=0.05),
            _hit("DUET", "whale_rock", tracked=False, pct=0.05),
        ],
        db_path=db,
    )
    # No FMP caches / holdings → no screen or adjacency hits; DUET is investor-only.
    run_discovery.discover(repo, include_adjacency=False)
    cands = {c.ticker: c for c in list_candidates(db_path=db)}
    assert "DUET" in cands  # corroborated investor-only name surfaced
    assert cands["DUET"].score == pytest.approx(INVESTOR_ONLY_CAP)  # clamped
    why = cands["DUET"].score_json
    assert why is not None and why["clamped"] is True
    assert cands["DUET"].name == "Duet Inc"  # name filled from the universe


def test_single_fund_investor_only_stays_below_the_bar(repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    fetch_13f.persist_hits([_hit("SOLO", "appaloosa", tracked=False, pct=0.03)], db_path=db)
    run_discovery.discover(repo, include_adjacency=False)
    # A lone fund's modest buy doesn't clear the entry threshold → no candidate.
    assert all(c.ticker != "SOLO" for c in list_candidates(db_path=db))


def test_run_no_active_managers_is_noop(tmp_path: Path) -> None:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    conn.commit()
    conn.close()
    summary = fetch_13f.run(tmp_path)
    assert summary == {"signals": 0, "untracked_names": 0, "tracked_news": 0, "managers": 0}


def test_run_mines_and_persists(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_mine(
        db_path: Path,
        fmp_dir: Path,
        *,
        sources: list[tuple[str, str]],
        user_agent: str = thirteenf.DEFAULT_USER_AGENT,
    ) -> list[thirteenf.InvestorHit]:
        captured["sources"] = sources
        return [_hit("SOLO", "appaloosa", tracked=False, pct=0.05)]

    monkeypatch.setattr(fetch_13f, "mine_13f", fake_mine)
    summary = fetch_13f.run(repo)
    assert summary["managers"] == 2  # appaloosa + whale_rock both have CIKs
    assert summary["signals"] == 1
    assert ("appaloosa", "0001656456") in captured["sources"]  # type: ignore[operator]
