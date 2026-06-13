"""P5.3 discovery pipelines: the factor screens (TTM math over synthetic
FMP caches), the adjacency miner (word-boundary full-phrase matching — the
KLA/"kirKLAnd's" substring trap must NOT reproduce), the candidates store
(status survives re-runs), the CLI aggregation, and migration 0081.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from discovery.adjacency import (
    _doc_index,  # pyright: ignore[reportPrivateUsage]
    _phrase_in_doc,  # pyright: ignore[reportPrivateUsage]
    build_lexicon,
    mine_adjacency,
    normalize_phrase,
)
from discovery.screens import load_ticker_metrics, run_screens
from discovery.store import (
    get_candidate,
    list_candidates,
    list_signals,
    set_status,
    upsert_candidate,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import run_discovery  # noqa: E402

_DDL = """
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    list_type TEXT NOT NULL
);
CREATE TABLE news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    headline TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    snippet TEXT,
    source TEXT,
    source_feed TEXT DEFAULT 'fmp_stock_news',
    fetched_at TEXT NOT NULL DEFAULT '2026-06-11 00:00:00'
);
CREATE TABLE discovery_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    name TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    score FLOAT NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL,
    score_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CONSTRAINT ck_discovery_candidates_status
        CHECK (status IN ('new', 'queued', 'building', 'built', 'dismissed')),
    CONSTRAINT ck_discovery_candidates_evidence_json CHECK (json_valid(evidence_json)),
    CONSTRAINT ck_discovery_candidates_score_json
        CHECK (score_json IS NULL OR json_valid(score_json)),
    CONSTRAINT uq_discovery_candidates_user_ticker UNIQUE (user_id, ticker)
);
CREATE TABLE discovery_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    signal_class TEXT NOT NULL,
    source_key TEXT NOT NULL,
    weight FLOAT NOT NULL DEFAULT 1.0,
    raw_strength FLOAT NOT NULL DEFAULT 1.0,
    observed_at TEXT NOT NULL,
    detail TEXT,
    meta_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CONSTRAINT uq_discovery_signals_identity
        UNIQUE (user_id, ticker, signal_class, source_key)
);
CREATE TABLE discovery_sources (
    source_key TEXT PRIMARY KEY,
    signal_class TEXT NOT NULL,
    display_name TEXT NOT NULL,
    base_weight FLOAT NOT NULL DEFAULT 1.0,
    tier TEXT NOT NULL DEFAULT 'structural',
    style_tags TEXT,
    cik TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    last_calibrated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# The structural source weights the seed migration (0096) lands; mirrored in
# the fixture so the end-to-end test scores against real, non-unit weights.
_STRUCTURAL_SOURCES = [
    ("quality_compounder", "screen", "Quality compounder", 1.0),
    ("fcf_value", "screen", "FCF value", 0.9),
    ("growth_inflection", "screen", "Growth inflection", 1.0),
    ("watchlist", "adjacency", "Watchlist", 1.0),
    ("transcript", "adjacency", "Transcript", 0.7),
    ("news", "adjacency", "News", 0.5),
]

# Quarterly income rows newest-first: (date, revenue, grossProfit, operatingIncome).
_GOODCO_INCOME = [
    ("2026-03-31", 130, 60, 20),
    ("2025-12-31", 120, 55, 18),
    ("2025-09-30", 115, 52, 17),
    ("2025-06-30", 110, 50, 16),
    ("2025-03-31", 100, 45, 14),
    ("2024-12-31", 95, 42, 13),
    ("2024-09-30", 90, 40, 12),
    ("2024-06-30", 86, 38, 11),
    ("2024-03-31", 84, 37, 10),
]


def _write_fmp(
    fmp_dir: Path,
    ticker: str,
    *,
    roic_q: float,
    fcf_q: float,
    nd: float,
    mcap: float,
    income: list[tuple[str, int, int, int]],
    active: bool = True,
) -> None:
    fmp_dir.mkdir(parents=True, exist_ok=True)
    (fmp_dir / f"{ticker}_profile.json").write_text(
        json.dumps(
            [
                {
                    "symbol": ticker,
                    "companyName": f"{ticker.title()} Co",
                    "sector": "Technology",
                    "industry": "Software",
                    "marketCap": mcap,
                    "isActivelyTrading": active,
                }
            ]
        ),
        encoding="utf-8",
    )
    km = [
        {
            "date": d,
            "returnOnInvestedCapital": roic_q,
            "freeCashFlowYield": fcf_q,
            "netDebtToEBITDA": nd,
            "marketCap": mcap,
        }
        for d, _r, _g, _o in income[:6]
    ]
    (fmp_dir / f"{ticker}_key_metrics_quarterly.json").write_text(json.dumps(km), encoding="utf-8")
    inc = [
        {"date": d, "revenue": r, "grossProfit": g, "operatingIncome": o} for d, r, g, o in income
    ]
    (fmp_dir / f"{ticker}_income_statement_quarterly.json").write_text(
        json.dumps(inc), encoding="utf-8"
    )


def _scale_income(factor: float) -> list[tuple[str, int, int, int]]:
    return [(d, int(r * factor), int(g * factor), int(o * factor)) for d, r, g, o in _GOODCO_INCOME]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A synthetic repo root: DB + FMP caches + holdings + transcripts."""
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    companies = [
        ("AAA", "Alpha Holdings", "portfolio"),
        ("GOODCO", "Goodco Inc.", "index_member"),
        ("VALCO", "Valco Corp", "index_member"),
        ("BADCO", "Badco Corp", "index_member"),
        ("MRVL", "Marvell Technology", "index_member"),
        ("KLAC", "KLA Corp", "index_member"),
        ("RIO", "Rio Tinto", "index_member"),
        ("GHOST", "Ghostco Corp", "index_member"),
        ("STALE", "Staleco Inc.", "index_member"),
    ]
    conn.executemany(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type)"
        " VALUES ('bhanu', ?, ?, ?)",
        companies,
    )
    conn.execute(
        "INSERT INTO news (ticker, headline, url, published_at, snippet) VALUES"
        " ('AAA', 'Alpha and Valco announce partnership', 'https://x', '2026-06-01 00:00:00',"
        " 'Valco Corp deal')"
    )
    conn.executemany(
        "INSERT INTO discovery_sources (source_key, signal_class, display_name, base_weight,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, 'seed', 'seed')",
        _STRUCTURAL_SOURCES,
    )
    conn.commit()
    conn.close()

    fmp = tmp_path / "data" / "historical" / "fmp"
    # GOODCO: passes all three screens (ROIC 20% TTM, FCF yield 8%, ND/EBITDA
    # 1.0x TTM, rev YoY +30% accelerating, GM ~46%, OM ~15%).
    _write_fmp(fmp, "GOODCO", roic_q=0.05, fcf_q=0.02, nd=4.0, mcap=5e9, income=_GOODCO_INCOME)
    # VALCO: fcf_value only (ROIC 10% TTM, FCF 6%, rev +30% but GM/OM scale the
    # same so growth_inflection passes too? No: VALCO income halves margins).
    valco_income = [(d, r, int(g * 0.5), int(o * 0.4)) for d, r, g, o in _scale_income(1.0)]
    _write_fmp(fmp, "VALCO", roic_q=0.025, fcf_q=0.015, nd=2.0, mcap=3e9, income=valco_income)
    # BADCO: screens out on every rule.
    badco_income = [(d, int(r * 0.9**i), 5, 1) for i, (d, r, _g, _o) in enumerate(_GOODCO_INCOME)]
    _write_fmp(fmp, "BADCO", roic_q=0.01, fcf_q=0.002, nd=20.0, mcap=4e8, income=badco_income)
    # GHOST: numbers that WOULD pass, but the listing is dead — both
    # pipelines must drop it (it also sits on AAA's watchlist).
    _write_fmp(
        fmp, "GHOST", roic_q=0.05, fcf_q=0.02, nd=4.0, mcap=5e9, income=_GOODCO_INCOME, active=False
    )
    # STALE: still flagged trading but the cache froze in 2017 — the
    # freshness gate must drop it.
    stale_income = [
        (d.replace("2026", "2017").replace("2025", "2016").replace("2024", "2015"), r, g, o)
        for d, r, g, o in _GOODCO_INCOME
    ]
    _write_fmp(fmp, "STALE", roic_q=0.05, fcf_q=0.02, nd=4.0, mcap=5e9, income=stale_income)
    # MRVL/KLAC/RIO: no caches — adjacency-only names.

    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "AAA.json").write_text(
        json.dumps(
            {
                "ticker": "AAA",
                "competitive_watchlist": ["Marvell (the ASIC rival)", "KLA", "Ghostco"],
            }
        ),
        encoding="utf-8",
    )

    transcripts = tmp_path / "transcripts" / "processed"
    transcripts.mkdir(parents=True)
    (transcripts / "AAA_Q1_2026.txt").write_text(
        "We partner with Marvell on silicon. Marvell shipped early. "
        "Customers compare us with marvell constantly, and MARVELL keeps pace. "
        "Meanwhile kirkland's retail results were fine and bravo brio reopened. "
        "The rio grande facility expanded.",
        encoding="utf-8",
    )
    (transcripts / "AAA_Q4_2025.txt").write_text(
        "A quiet quarter. Nothing else to report.", encoding="utf-8"
    )
    return tmp_path


# ----------------------------------------------------------------------------
# screens
# ----------------------------------------------------------------------------


def test_metrics_ttm_math(repo: Path) -> None:
    m = load_ticker_metrics(repo / "data" / "historical" / "fmp", "GOODCO", "Goodco Inc.")
    assert m.roic_ttm == pytest.approx(0.20)
    assert m.fcf_yield_ttm == pytest.approx(0.08)
    assert m.nd_to_ebitda_ttm == pytest.approx(1.0)
    assert m.rev_yoy == pytest.approx(0.30)
    assert m.rev_yoy_prior == pytest.approx(100 / 84 - 1)
    assert m.gross_margin_ttm == pytest.approx(217 / 475)
    assert m.op_margin_ttm == pytest.approx(71 / 475)
    assert m.market_cap == 5e9
    # Missing cache → all-None bundle, never an exception.
    empty = load_ticker_metrics(repo / "data" / "historical" / "fmp", "MRVL", "Marvell")
    assert empty.roic_ttm is None
    assert empty.rev_yoy is None


def test_screens_pass_and_fail(repo: Path) -> None:
    from datetime import date

    hits = run_screens(
        repo / "data" / "portfolio.db",
        repo / "data" / "historical" / "fmp",
        as_of=date(2026, 6, 11),
    )
    by_ticker: dict[str, set[str]] = {}
    for h in hits:
        by_ticker.setdefault(h.ticker, set()).add(h.screen)
    assert by_ticker["GOODCO"] == {"quality_compounder", "fcf_value", "growth_inflection"}
    assert by_ticker["VALCO"] == {"fcf_value"}
    assert "BADCO" not in by_ticker
    assert "AAA" not in by_ticker  # portfolio names are not screened
    assert "GHOST" not in by_ticker  # delisted profiles never screen in
    assert "STALE" not in by_ticker  # frozen 2017 caches fail the freshness gate
    detail = next(h.detail for h in hits if h.ticker == "GOODCO" and h.screen == "fcf_value")
    assert "FCF yield 8.0%" in detail
    assert "mcap $5.0B" in detail


# ----------------------------------------------------------------------------
# adjacency
# ----------------------------------------------------------------------------


def test_normalize_phrase() -> None:
    assert normalize_phrase("Blackstone Inc.") == ("blackstone",)
    assert normalize_phrase("Expedia Group") == ("expedia",)
    assert normalize_phrase("KLA Corp") == ("kla",)
    assert normalize_phrase("Rio Tinto") == ("rio", "tinto")
    assert normalize_phrase("Inc.") == ("inc",)  # degenerate keeps one token


def test_adjacency_word_boundary_matching(repo: Path) -> None:
    hits = mine_adjacency(
        repo, repo / "data" / "portfolio.db", fmp_dir=repo / "data" / "historical" / "fmp"
    )
    # The dead listing never surfaces even though AAA's watchlist names it.
    assert all(h.ticker != "GHOST" for h in hits)
    by_key = {(h.ticker, h.source) for h in hits}
    # Marvell: 4 word-boundary mentions in AAA's calls + the watchlist entry.
    assert ("MRVL", "transcript") in by_key
    assert ("MRVL", "watchlist") in by_key
    # The substring traps must NOT fire: "kirkland's" is not KLA, "bravo
    # brio"/"rio grande" are not Rio Tinto.
    assert ("KLAC", "transcript") not in by_key
    assert all(t != "RIO" for t, _s in by_key)
    # Short intentional watchlist entries DO resolve ("KLA" -> KLA Corp).
    assert ("KLAC", "watchlist") in by_key
    # News: Valco named in AAA's headline/snippet.
    assert ("VALCO", "news") in by_key
    mrvl_transcript = next(h for h in hits if h.ticker == "MRVL" and h.source == "transcript")
    assert "4x" in mrvl_transcript.detail
    assert mrvl_transcript.holding == "AAA"


def test_adjacency_min_mentions_threshold(repo: Path) -> None:
    hits = mine_adjacency(repo, repo / "data" / "portfolio.db", min_transcript_mentions=5)
    assert all(not (h.ticker == "MRVL" and h.source == "transcript") for h in hits)


def test_alt_token_requires_capitalization(repo: Path) -> None:
    lex = build_lexicon(repo / "data" / "portfolio.db")
    mrvl = next(p for p in lex if p.ticker == "MRVL")
    assert mrvl.alt_token == "marvell"  # unique, long, non-generic first token
    klac = next(p for p in lex if p.ticker == "KLAC")
    assert klac.alt_token is None  # single-token names have no alt
    rio = next(p for p in lex if p.ticker == "RIO")
    assert rio.alt_token is None  # "rio" is too short
    lower = _doc_index("we rely on marvell parts in production")
    upper = _doc_index("We rely on Marvell parts in production")
    assert not _phrase_in_doc(mrvl, lower, min_single=5)  # prose stays prose
    assert _phrase_in_doc(mrvl, upper, min_single=5)


def test_lexicon_is_index_members_only(repo: Path) -> None:
    lex = build_lexicon(repo / "data" / "portfolio.db")
    tickers = {p.ticker for p in lex}
    assert "AAA" not in tickers  # holdings are not discoverable candidates
    assert {"GOODCO", "VALCO", "MRVL", "KLAC", "RIO"} <= tickers


# ----------------------------------------------------------------------------
# store
# ----------------------------------------------------------------------------


def test_store_upsert_preserves_status(repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    row = upsert_candidate(
        ticker="goodco",
        name="Goodco Inc.",
        score=3.0,
        evidence=[{"source": "screen:fcf_value", "detail": "x"}],
        db_path=db,
    )
    assert row.ticker == "GOODCO"
    assert row.status == "new"

    moved = set_status(row.id, "dismissed", db_path=db)
    assert moved is not None and moved.status == "dismissed"

    again = upsert_candidate(
        ticker="GOODCO", name="Goodco Inc.", score=4.0, evidence=[], db_path=db
    )
    assert again.id == row.id
    assert again.score == 4.0
    assert again.status == "dismissed"  # the owner's no survives re-runs

    assert list_candidates(db_path=db) == []  # dismissed is out of the live inbox
    assert [c.ticker for c in list_candidates(status="dismissed", db_path=db)] == ["GOODCO"]
    fetched = get_candidate(row.id, db_path=db)
    assert fetched is not None and fetched.evidence == []
    with pytest.raises(ValueError, match="status"):
        set_status(row.id, "bogus", db_path=db)


# ----------------------------------------------------------------------------
# CLI aggregation end-to-end
# ----------------------------------------------------------------------------


def test_discover_end_to_end(repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    results = run_discovery.discover(repo)
    scores = {t: s for t, s, _n in results}
    # Weighted, not counted: GOODCO = quality(1.0)+fcf(0.9)+growth(1.0) = 2.9;
    # MRVL = watchlist(1.0)+transcript(0.7) = 1.7 (decay ~1.0 same-day).
    assert scores["GOODCO"] == pytest.approx(2.9, abs=0.01)
    assert scores["MRVL"] == pytest.approx(1.7, abs=0.01)
    assert scores["GOODCO"] > scores["MRVL"]
    # The raised entry bar (ENTRY_THRESHOLD 1.5) keeps weak singletons OUT:
    # VALCO = fcf(0.9)+news(0.5) = 1.4 and KLAC = watchlist(1.0) = 1.0 never
    # enter the queue as new names.
    assert "VALCO" not in scores
    assert "KLAC" not in scores
    assert "BADCO" not in scores
    assert "GHOST" not in scores
    assert "STALE" not in scores

    rows = list_candidates(db_path=db)
    by_ticker = {c.ticker: c for c in rows}
    assert set(by_ticker) == {"GOODCO", "MRVL"}
    # score_json carries the per-class breakdown the panel peeks.
    why = by_ticker["GOODCO"].score_json
    assert why is not None and why["terms"] == {"screen": pytest.approx(2.9, abs=0.01)}
    sources = {str(e.get("source")) for e in by_ticker["MRVL"].evidence}
    assert sources == {"adjacency:watchlist", "adjacency:transcript"}

    # The typed signals landed (3 screen rows for GOODCO, 2 adjacency for MRVL).
    goodco_sig = list_signals("GOODCO", db_path=db)
    assert {s.source_key for s in goodco_sig} == {
        "quality_compounder",
        "fcf_value",
        "growth_inflection",
    }
    assert all(s.signal_class == "screen" for s in goodco_sig)
    mrvl_sig = list_signals("MRVL", db_path=db)
    assert {s.source_key for s in mrvl_sig} == {"watchlist", "transcript"}

    # Re-run refreshes without duplicating evidence OR signal rows.
    run_discovery.discover(repo)
    again = list_candidates(db_path=db)
    assert len(again) == len(rows)
    assert len(list_signals("GOODCO", db_path=db)) == 3
    mrvl = next(c for c in again if c.ticker == "MRVL")
    assert len(mrvl.evidence) == 2


def test_discover_existing_below_threshold_is_refreshed(repo: Path) -> None:
    """A NEW weak name stays out, but a name already IN the queue is always
    refreshed (its score/lifecycle stays current even below the entry bar)."""
    db = repo / "data" / "portfolio.db"
    # Seed KLAC (watchlist-only, score ~1.0 < 1.5) as a pre-existing candidate.
    upsert_candidate(ticker="KLAC", name="KLA Corp", score=9.0, evidence=[], db_path=db)
    run_discovery.discover(repo)
    klac = next(c for c in list_candidates(db_path=db) if c.ticker == "KLAC")
    assert klac.score == pytest.approx(1.0, abs=0.01)  # refreshed down, not skipped
    assert klac.score_json is not None


def test_discover_skip_flags(repo: Path) -> None:
    results = run_discovery.discover(repo, include_adjacency=False)
    tickers = {t for t, _s, _n in results}
    assert "GOODCO" in tickers
    assert "MRVL" not in tickers


# ----------------------------------------------------------------------------
# migration 0081
# ----------------------------------------------------------------------------


def test_migration_creates_table(tmp_path: Path) -> None:
    db = tmp_path / "mig.db"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, "0080_viewspec_compile_budget")
    # Pinned (not "head") so later migrations don't break this assertion.
    command.upgrade(cfg, "0081_discovery_candidates")
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
        "0081_discovery_candidates"
    )
    conn.execute(
        "INSERT INTO discovery_candidates (ticker, evidence_json, first_seen_at,"
        " last_seen_at, updated_at) VALUES ('X', '[]', 'a', 'a', 'a')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO discovery_candidates (ticker, status, evidence_json,"
            " first_seen_at, last_seen_at, updated_at)"
            " VALUES ('Y', 'nope', '[]', 'a', 'a', 'a')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO discovery_candidates (ticker, evidence_json, first_seen_at,"
            " last_seen_at, updated_at) VALUES ('Z', 'not json', 'a', 'a', 'a')"
        )
    conn.close()


def test_migration_discovery_scoring_tables(tmp_path: Path) -> None:
    """0095 (discovery_signals) + 0097 (discovery_sources seed) on top of the
    live head. 0096 (score_json) no-ops without discovery_candidates, fine here."""
    db = tmp_path / "mig2.db"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, "0095_signals")
    command.upgrade(cfg, "0098_discovery_sources")
    conn = sqlite3.connect(db)
    try:
        # The roster seeded from §5's two tiers: 3 screens + 3 adjacency + 33 funds.
        by_class = dict(
            conn.execute(
                "SELECT signal_class, count(*) FROM discovery_sources GROUP BY signal_class"
            ).fetchall()
        )
        assert by_class == {"screen": 3, "adjacency": 3, "investor_13f": 33}
        # Appaloosa's CIK is the one verified seed; the rest resolve in the miner.
        cik = conn.execute(
            "SELECT cik FROM discovery_sources WHERE source_key = 'appaloosa'"
        ).fetchone()[0]
        assert cik == "0001656456"
        # discovery_signals identity is UNIQUE per (user, ticker, class, source).
        conn.execute(
            "INSERT INTO discovery_signals (user_id, ticker, signal_class, source_key,"
            " observed_at, created_at, updated_at)"
            " VALUES ('bhanu', 'X', 'screen', 'fcf_value', 'a', 'a', 'a')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO discovery_signals (user_id, ticker, signal_class, source_key,"
                " observed_at, created_at, updated_at)"
                " VALUES ('bhanu', 'X', 'screen', 'fcf_value', 'b', 'b', 'b')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO discovery_signals (user_id, ticker, signal_class, source_key,"
                " observed_at, meta_json, created_at, updated_at)"
                " VALUES ('bhanu', 'Y', 'screen', 's', 'a', 'not json', 'a', 'a')"
            )
    finally:
        conn.close()
