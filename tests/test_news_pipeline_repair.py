"""The 2026-07-19 news-pipeline repair (Workstream A2).

The daily stage_0_news kill discarded the entire (LLM-billed) haul because the
dispatcher persisted once at the very end — the news table froze at 2026-07-03
while ~$14/day of websearch-LLM output was thrown away, and the follow-on diet
scoring (correctly wired) never executed once. Four seams, hermetic:

  * incremental persist — a ticker's rows land as its future resolves, so a
    later ticker's failure (or a stage kill) loses only in-flight work;
  * diet scoring order — runs after the primary sweep persists, BEFORE the
    additive feeds (whose slowness is what starved it to 0 calls ever);
  * websearch scope gate — under `auto`, only portfolio names may burn the
    WebSearch+LLM fallback (the ungated sweep billed ~90 names/day);
  * the data_feed_stale dead-man (migration 0183) — a full-book sweep leaving
    the table stale fires ONE book-level alert per day, deduped by signature.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

import execution.fetch_fmp_news as fmpnews
import execution.fetch_news as fetch_news
from alembic import command
from news.store import NewsRow

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _no_additive_rows(*_a: object, **_k: object) -> list[NewsRow]:
    return []


@pytest.fixture(autouse=True)
def _hermetic_additive_feeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_news.edgarnews, "fetch_edgar_news_for_ticker", _no_additive_rows)
    monkeypatch.setattr(fetch_news.yfgrades, "fetch_grades_for_ticker", _no_additive_rows)
    # yf_news joined the additive feeds 2026-07-25 (it replaced the paid
    # WebSearch+LLM path); unstubbed it would reach Yahoo from the test suite.
    monkeypatch.setattr(fetch_news.yfnews, "fetch_news_for_ticker", _no_additive_rows)
    monkeypatch.setattr(fetch_news, "check_s1_watch", lambda *_a, **_k: [])


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def news_db(tmp_path: Path) -> Path:
    """DB with the real `news` table (0065 slice, matching the dispatcher suite)."""
    db = tmp_path / "news_repair.db"
    cfg = _build_config(db)
    command.stamp(cfg, "0064_queued_actions")
    command.upgrade(cfg, "0065_news")
    conn = sqlite3.connect(str(db))
    try:
        # Deliberately minimal 0065 contract fixture, not a production
        # versioned database.
        conn.execute("DROP TABLE alembic_version")
        conn.commit()
    finally:
        conn.close()
    return db


@pytest.fixture
def head_db(tmp_path: Path) -> Path:
    """Full at-head DB — the dead-man test needs the alerts table + the 0183
    widened trigger-kind CHECK."""
    db = tmp_path / "head.db"
    import db as dbmod

    saved = (dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR)
    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, "head")
    dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR = saved
    return db


def _row(ticker: str, url: str, published_at: str = "2026-01-15 10:00:00") -> NewsRow:
    return NewsRow(
        ticker=ticker,
        headline=f"story {url}",
        url=url,
        published_at=published_at,
        source_feed="fmp_stock_news",
    )


def _persisted(db: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT ticker, url FROM news ORDER BY ticker, url").fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------ incremental persist


def test_earlier_tickers_rows_survive_a_later_ticker_failure(
    news_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stage-kill shape: GOOD's rows must be in the table even though BAD's
    collection blows up — under the old collect-then-persist-once shape the
    whole haul died together."""

    def fmp(ticker: str, **_k: object) -> fmpnews.FmpNewsResult:
        if ticker == "BAD":
            raise RuntimeError("collector exploded")
        return fmpnews.FmpNewsResult(
            ticker, 200, [{"x": 1}], [_row(ticker, f"https://n/{ticker}")], None
        )

    monkeypatch.setattr(fetch_news.fmpnews, "FMP_API_KEY", "key")
    monkeypatch.setattr(fetch_news.fmpnews, "fetch_news_for_ticker", fmp)
    monkeypatch.setattr(fetch_news, "score_unscored_signals", lambda *_a, **_k: {})

    rc = fetch_news.run(["GOOD", "BAD"], source="fmp", db_path=str(news_db), days=2, limit=10)
    assert rc == 0
    assert _persisted(news_db) == [("GOOD", "https://n/GOOD")]


def test_diet_scoring_runs_after_primary_persist_before_additive(
    news_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []

    def fmp(ticker: str, **_k: object) -> fmpnews.FmpNewsResult:
        order.append(f"collect:{ticker}")
        return fmpnews.FmpNewsResult(
            ticker, 200, [{"x": 1}], [_row(ticker, f"https://n/{ticker}")], None
        )

    def edgar(_t: str, **_k: object) -> list[NewsRow]:
        order.append("additive:edgar")
        return []

    def diet(_db: object) -> dict[str, int]:
        order.append("diet")
        return {}

    monkeypatch.setattr(fetch_news.fmpnews, "FMP_API_KEY", "key")
    monkeypatch.setattr(fetch_news.fmpnews, "fetch_news_for_ticker", fmp)
    monkeypatch.setattr(fetch_news.edgarnews, "fetch_edgar_news_for_ticker", edgar)
    monkeypatch.setattr(fetch_news, "score_unscored_signals", diet)

    fetch_news.run(["NU"], source="fmp", db_path=str(news_db), days=2, limit=10)
    assert order == ["collect:NU", "diet", "additive:edgar"]


# ------------------------------------------------------------ websearch scope gate


class _WsRecorder:
    def __init__(self) -> None:
        self.tickers: list[str] = []

    def __call__(self, ticker: str, *, news_days: int, db_path: object) -> list[NewsRow]:
        self.tickers.append(ticker)
        return []


def test_auto_fallback_gated_to_portfolio_names(
    news_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = fmpnews.FmpNewsResult("X", 200, {"Error Message": "Limit Reach"}, [], None)
    monkeypatch.setattr(fetch_news.fmpnews, "FMP_API_KEY", "key")
    monkeypatch.setattr(fetch_news.fmpnews, "fetch_news_for_ticker", lambda *_a, **_k: refused)
    monkeypatch.setattr(fetch_news, "portfolio_tickers", lambda _db: frozenset({"NU"}))
    monkeypatch.setattr(fetch_news, "score_unscored_signals", lambda *_a, **_k: {})
    ws = _WsRecorder()
    monkeypatch.setattr(fetch_news, "fetch_websearch_news_for_ticker", ws)

    fetch_news.run(["NU", "AAON"], source="auto", db_path=str(news_db), days=2, limit=10)
    assert ws.tickers == ["NU"]  # AAON refused too, but is not fallback-eligible


def test_websearch_scope_all_restores_ungated_fallback(
    news_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = fmpnews.FmpNewsResult("X", 200, {"Error Message": "Limit Reach"}, [], None)
    monkeypatch.setattr(fetch_news.fmpnews, "FMP_API_KEY", "key")
    monkeypatch.setattr(fetch_news.fmpnews, "fetch_news_for_ticker", lambda *_a, **_k: refused)
    monkeypatch.setattr(fetch_news, "score_unscored_signals", lambda *_a, **_k: {})
    ws = _WsRecorder()
    monkeypatch.setattr(fetch_news, "fetch_websearch_news_for_ticker", ws)

    fetch_news.run(
        ["NU", "AAON"],
        source="auto",
        db_path=str(news_db),
        days=2,
        limit=10,
        websearch_scope="all",
    )
    assert sorted(ws.tickers) == ["AAON", "NU"]


# ------------------------------------------------------------------- dead-man alert


def _alert_count(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE trigger_kind='data_feed_stale'"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def test_deadman_fires_once_on_a_stale_table(head_db: Path) -> None:
    conn = sqlite3.connect(str(head_db))
    try:
        from news.store import upsert_news_rows

        upsert_news_rows(conn, [_row("NU", "https://n/old", published_at="2026-01-01 10:00:00")])
    finally:
        conn.close()

    fetch_news._fire_deadman_if_stale(str(head_db), tickers_n=10, inserted_total=0)
    assert _alert_count(head_db) == 1
    # Same day, same signature — deduped.
    fetch_news._fire_deadman_if_stale(str(head_db), tickers_n=10, inserted_total=0)
    assert _alert_count(head_db) == 1
    # The alert is book-level (the 0171 'PORTFOLIO' sentinel convention).
    conn = sqlite3.connect(str(head_db))
    try:
        tick = conn.execute(
            "SELECT ticker FROM alerts WHERE trigger_kind='data_feed_stale'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert tick == "PORTFOLIO"


def test_deadman_quiet_on_fresh_table_and_targeted_runs(head_db: Path) -> None:
    from datetime import UTC, datetime

    conn = sqlite3.connect(str(head_db))
    try:
        from news.store import upsert_news_rows

        today = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d 09:00:00")
        upsert_news_rows(conn, [_row("NU", "https://n/fresh", published_at=today)])
    finally:
        conn.close()

    fetch_news._fire_deadman_if_stale(str(head_db), tickers_n=10, inserted_total=1)
    assert _alert_count(head_db) == 0  # fresh table — quiet
    fetch_news._fire_deadman_if_stale(str(head_db), tickers_n=1, inserted_total=0)
    assert _alert_count(head_db) == 0  # targeted run — never judges feed health
