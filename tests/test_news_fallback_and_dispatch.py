"""Tests for the WebSearch+Opus fallback feed and the news dispatcher (PR5).

Hermetic — no live LLM/web/FMP. Covers (plan §6.6, §6.7):

  * structure_recent_news_json — parses a JSON array; retries once on a parse
    miss then degrades to []; degrades when the web call raises; strips fences.
  * fetch_websearch_news_for_ticker — maps items to NewsRows tagged
    ``websearch_opus``; DROPS items missing / with an unparseable date (never
    fabricates a timestamp); converts ET->UTC; a same-day re-run is an
    llm_artifacts cache hit (no second Opus call).
  * fmp_refused — the table-driven refusal predicate (401/402/403/429/5xx and the
    HTTP-200-non-array gotcha refuse; a genuine 200+[] does not).
  * dispatcher auto routing — falls back only on refusal, never on 200+[]; FMP
    rows persist without a fallback; --source websearch never calls FMP, and
    --source fmp never calls WebSearch even on refusal.
  * additive feeds (EDGAR + yfinance grades) — run for every source policy,
    skip flags opt out, failures degrade without touching the policy rows, and
    cross-feed duplicates (ticker + normalized headline + date) are dropped.
    An autouse fixture stubs both to [] so every dispatcher test stays hermetic.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

import execution.fetch_fmp_news as fmpnews
import execution.fetch_news as fetch_news
import execution.fetch_news_websearch as websearch
import llm_client
from alembic import command
from execution.fetch_news import fmp_refused
from llm_client import structure_recent_news_json
from news.store import NewsRow
from pipeline.row_validation import RowValidationDriftError

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _no_additive_rows(*_a: object, **_k: object) -> list[NewsRow]:
    return []


@pytest.fixture(autouse=True)
def _hermetic_additive_feeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dispatcher runs the additive EDGAR + grades + yf-news feeds by
    default; stub all THREE to [] so no dispatcher test ever touches sec.gov /
    Yahoo. Additive-specific tests re-patch with their own recorders.

    yf_news joined 2026-07-25 when it replaced the paid WebSearch+LLM path as
    the default journalism source — an unstubbed live feed here made every
    exact-row assertion in this file fail against the network."""
    monkeypatch.setattr(fetch_news.edgarnews, "fetch_edgar_news_for_ticker", _no_additive_rows)
    monkeypatch.setattr(fetch_news.yfgrades, "fetch_grades_for_ticker", _no_additive_rows)
    monkeypatch.setattr(fetch_news.yfnews, "fetch_news_for_ticker", _no_additive_rows)


_LLM_ARTIFACTS_DDL = (
    "CREATE TABLE llm_artifacts ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "ticker TEXT, scope TEXT NOT NULL DEFAULT 'ticker', purpose TEXT NOT NULL, "
    "fiscal_period TEXT, content_md TEXT, content_json TEXT, "
    "input_sha256 TEXT NOT NULL, output_sha256 TEXT, model TEXT, "
    "prompt_version TEXT NOT NULL DEFAULT 'v1', generated_at TEXT NOT NULL, "
    "expires_at TEXT, superseded_by_id INTEGER, dirty INTEGER NOT NULL DEFAULT 0, "
    "dirty_reason TEXT, source_doc_ids TEXT, parent_artifact_ids TEXT, llm_call_id INTEGER)"
)


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def news_db(tmp_path: Path) -> Path:
    """DB with the real `news` table (migration) + llm_artifacts (for the cache)."""
    db = tmp_path / "news_dispatch.db"
    cfg = _build_config(db)
    command.stamp(cfg, "0064_queued_actions")
    command.upgrade(cfg, "0065_news")
    conn = sqlite3.connect(str(db))
    try:
        _ = conn.execute(_LLM_ARTIFACTS_DDL)
        # Deliberately minimal 0065 + cache contract fixture, not a production
        # versioned DB; guarded writers enforce the local tables exercised here.
        conn.execute("DROP TABLE alembic_version")
        conn.commit()
    finally:
        conn.close()
    return db


def _news_rows(db_path: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT ticker, url, source_feed FROM news ORDER BY url").fetchall()
    finally:
        conn.close()


def _anchor_stub(*_a: object, **_k: object) -> str:
    return ""


# ===========================================================================
# structure_recent_news_json (the Opus/web generator)
# ===========================================================================


class _WebStub:
    """Canned call_llm_with_web returning responses in sequence (tracks calls)."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0

    def __call__(self, prompt: str, **_kwargs: object) -> str:
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[idx]


def _json_items() -> str:
    return json.dumps(
        [
            {
                "headline": "Acme acquires Beta",
                "url": "https://news.example/1",
                "published_at": "2026-01-15 13:30:00",
                "published_tz": "UTC",
                "snippet": "A major acquisition.",
                "source": "Reuters",
            }
        ]
    )


def test_structure_returns_parsed_array(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _WebStub([_json_items()])
    monkeypatch.setattr(llm_client, "call_llm_with_web", stub)
    out = structure_recent_news_json("AAPL", news_days=2)
    assert stub.calls == 1
    assert len(out) == 1
    item = out[0]
    assert isinstance(item, dict)
    assert item["headline"] == "Acme acquires Beta"


def test_structure_retries_once_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _WebStub(["not json at all", _json_items()])
    monkeypatch.setattr(llm_client, "call_llm_with_web", stub)
    out = structure_recent_news_json("AAPL")
    assert stub.calls == 2
    assert len(out) == 1


def test_structure_degrades_on_bad_json_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _WebStub(["nope", "still nope"])
    monkeypatch.setattr(llm_client, "call_llm_with_web", stub)
    assert structure_recent_news_json("AAPL") == []
    assert stub.calls == 2


def test_structure_degrades_when_call_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("web tools unavailable")

    monkeypatch.setattr(llm_client, "call_llm_with_web", _boom)
    assert structure_recent_news_json("AAPL") == []


def test_structure_strips_json_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    fenced = "```json\n" + _json_items() + "\n```"
    monkeypatch.setattr(llm_client, "call_llm_with_web", _WebStub([fenced]))
    out = structure_recent_news_json("AAPL")
    assert len(out) == 1


# ===========================================================================
# fetch_websearch_news_for_ticker (map / date-drop / ET->UTC / cache)
# ===========================================================================


class _StructStub:
    """Canned structure_recent_news_json returning item lists (tracks calls)."""

    def __init__(self, responses: list[list[object]]) -> None:
        self.responses = responses
        self.calls = 0

    def __call__(self, ticker: str, **_kwargs: object) -> list[object]:
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[idx]


def test_websearch_maps_and_tags_source_feed(
    news_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(websearch, "load_thesis_anchor", _anchor_stub)
    items: list[object] = [
        {
            "headline": "Acme acquires Beta",
            "url": "https://news.example/1",
            "published_at": "2026-01-15 13:30:00",
            "published_tz": "UTC",
            "snippet": "deal",
            "source": "Reuters",
        }
    ]
    monkeypatch.setattr(websearch, "structure_recent_news_json", _StructStub([items]))
    rows = websearch.fetch_websearch_news_for_ticker("AAPL", news_days=2, db_path=str(news_db))
    assert len(rows) == 1
    assert rows[0].source_feed == "websearch_opus"
    assert rows[0].url == "https://news.example/1"
    assert rows[0].published_at == "2026-01-15 13:30:00"
    assert rows[0].source == "Reuters"


def test_websearch_batch_drift_halts_before_cache_write(
    news_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(websearch, "load_thesis_anchor", _anchor_stub)
    items: list[object] = [
        {
            "headline": f"story {index}",
            "url": f"https://news.example/{index}",
            "published_at": "2026-06-12 12:00:00",
            "published_tz": "UTC",
        }
        for index in range(7)
    ]
    items.extend([{"renamed_headline": "drift"}] * 3)
    monkeypatch.setattr(websearch, "structure_recent_news_json", _StructStub([items]))
    writes: list[object] = []

    def _record_cache_write(*_args: object, **_kwargs: object) -> None:
        writes.append(object())

    monkeypatch.setattr(websearch, "_write_cache", _record_cache_write)

    with pytest.raises(RowValidationDriftError, match="3/10"):
        websearch.fetch_websearch_news_for_ticker("AAPL", db_path=str(news_db))
    assert writes == []


def test_websearch_drops_items_without_determinable_date(
    news_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(websearch, "load_thesis_anchor", _anchor_stub)
    items: list[object] = [
        {
            "headline": "dated",
            "url": "https://news.example/1",
            "published_at": "2026-01-15 13:30:00",
            "published_tz": "UTC",
        },
        {"headline": "no date field", "url": "https://news.example/2"},
        {
            "headline": "unparseable date",
            "url": "https://news.example/3",
            "published_at": "sometime last week",
            "published_tz": "UTC",
        },
        {
            "headline": "unknown zone",
            "url": "https://news.example/4",
            "published_at": "2026-01-15 13:30:00",
            "published_tz": "PST",
        },
    ]
    monkeypatch.setattr(websearch, "structure_recent_news_json", _StructStub([items]))
    rows = websearch.fetch_websearch_news_for_ticker("AAPL", news_days=2, db_path=str(news_db))
    # Only the determinable-UTC story survives; the rest are dropped, never faked.
    assert [r.url for r in rows] == ["https://news.example/1"]


def test_websearch_converts_et_to_utc(news_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(websearch, "load_thesis_anchor", _anchor_stub)
    items: list[object] = [
        {
            "headline": "H",
            "url": "https://news.example/1",
            "published_at": "2026-01-15 08:30:00",
            "published_tz": "ET",
        }
    ]
    monkeypatch.setattr(websearch, "structure_recent_news_json", _StructStub([items]))
    rows = websearch.fetch_websearch_news_for_ticker("AAPL", news_days=2, db_path=str(news_db))
    assert rows[0].published_at == "2026-01-15 13:30:00"  # 08:30 EST -> 13:30 UTC


def test_websearch_cache_hit_skips_second_call(
    news_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(websearch, "load_thesis_anchor", _anchor_stub)
    items: list[object] = [
        {
            "headline": "H",
            "url": "https://news.example/1",
            "published_at": "2026-01-15 13:30:00",
            "published_tz": "UTC",
        }
    ]
    stub = _StructStub([items])
    monkeypatch.setattr(websearch, "structure_recent_news_json", stub)

    r1 = websearch.fetch_websearch_news_for_ticker("AAPL", news_days=2, db_path=str(news_db))
    r2 = websearch.fetch_websearch_news_for_ticker("AAPL", news_days=2, db_path=str(news_db))
    assert stub.calls == 1, "second same-day run should hit the llm_artifacts cache"
    assert [r.url for r in r1] == [r.url for r in r2]


# ===========================================================================
# fmp_refused — the refusal predicate (table-driven)
# ===========================================================================


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, [{"x": 1}], False),  # populated array -> not refused
        (200, [], False),  # genuine no-news -> NOT refused (no costly fallback)
        (200, {"Error Message": "Limit Reach"}, True),  # 200 + dict body -> refused
        (200, None, True),  # 200 + null body -> refused
        (401, {"Error Message": "Invalid API KEY"}, True),
        (402, None, True),
        (403, [], True),  # status refuses even with an array body
        (429, [], True),
        (500, [], True),
        (503, None, True),
        (0, None, True),  # network failure -> refused (non-list branch)
    ],
)
def test_fmp_refused(status: int, body: object, expected: bool) -> None:
    assert fmp_refused(status, body) is expected


# ===========================================================================
# dispatcher auto routing + source isolation
# ===========================================================================


def _fmp_result(
    status: int, body: object, rows: list[NewsRow] | None = None
) -> fmpnews.FmpNewsResult:
    return fmpnews.FmpNewsResult("AAPL", status, body, rows if rows is not None else [], None)


class _FmpStub:
    def __init__(self, result: fmpnews.FmpNewsResult) -> None:
        self.result = result
        self.calls = 0

    def __call__(self, *_a: object, **_k: object) -> fmpnews.FmpNewsResult:
        self.calls += 1
        return self.result


class _WsRecorder:
    def __init__(self, rows: list[NewsRow] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.calls = 0

    def __call__(self, ticker: str, *, news_days: int, db_path: object) -> list[NewsRow]:
        self.calls += 1
        return self.rows


def _ws_row() -> NewsRow:
    return NewsRow(
        ticker="AAPL",
        headline="websearch story",
        url="https://news.example/ws1",
        published_at="2026-01-15 10:00:00",
        source_feed="websearch_opus",
    )


def _fmp_row() -> NewsRow:
    return NewsRow(
        ticker="AAPL",
        headline="fmp story",
        url="https://news.example/fmp1",
        published_at="2026-01-15 10:00:00",
        source_feed="fmp_stock_news",
    )


def test_auto_falls_back_when_fmp_refused(news_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_news.fmpnews, "FMP_API_KEY", "key")
    monkeypatch.setattr(
        fetch_news.fmpnews,
        "fetch_news_for_ticker",
        _FmpStub(_fmp_result(200, {"Error Message": "Limit Reach"})),
    )
    ws = _WsRecorder([_ws_row()])
    monkeypatch.setattr(fetch_news, "fetch_websearch_news_for_ticker", ws)

    # websearch_scope="all": this test exercises the refusal ROUTING, not the
    # portfolio scope gate (covered in test_news_pipeline_repair.py); the
    # fixture DB has no tracked_companies so the default 'portfolio' scope
    # would correctly gate the fallback off.
    rc = fetch_news.run(
        ["AAPL"], source="auto", db_path=str(news_db), days=2, limit=50, websearch_scope="all"
    )
    assert rc == 0
    assert ws.calls == 1
    assert _news_rows(news_db) == [("AAPL", "https://news.example/ws1", "websearch_opus")]


def test_auto_no_fallback_on_empty_array(news_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_news.fmpnews, "FMP_API_KEY", "key")
    monkeypatch.setattr(fetch_news.fmpnews, "fetch_news_for_ticker", _FmpStub(_fmp_result(200, [])))
    ws = _WsRecorder([_ws_row()])
    monkeypatch.setattr(fetch_news, "fetch_websearch_news_for_ticker", ws)

    rc = fetch_news.run(["AAPL"], source="auto", db_path=str(news_db), days=2, limit=50)
    assert rc == 0
    assert ws.calls == 0  # 200 + [] is no-news, not refusal
    assert _news_rows(news_db) == []


def test_auto_persists_fmp_rows_without_fallback(
    news_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch_news.fmpnews, "FMP_API_KEY", "key")
    monkeypatch.setattr(
        fetch_news.fmpnews,
        "fetch_news_for_ticker",
        _FmpStub(_fmp_result(200, [{"x": 1}], [_fmp_row()])),
    )
    ws = _WsRecorder([_ws_row()])
    monkeypatch.setattr(fetch_news, "fetch_websearch_news_for_ticker", ws)

    fetch_news.run(["AAPL"], source="auto", db_path=str(news_db), days=2, limit=50)
    assert ws.calls == 0
    assert _news_rows(news_db) == [("AAPL", "https://news.example/fmp1", "fmp_stock_news")]


def test_source_websearch_never_calls_fmp(news_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fmp = _FmpStub(_fmp_result(200, []))
    monkeypatch.setattr(fetch_news.fmpnews, "fetch_news_for_ticker", fmp)
    monkeypatch.setattr(fetch_news, "fetch_websearch_news_for_ticker", _WsRecorder([_ws_row()]))

    fetch_news.run(["AAPL"], source="websearch", db_path=str(news_db), days=2, limit=50)
    assert fmp.calls == 0
    assert _news_rows(news_db) == [("AAPL", "https://news.example/ws1", "websearch_opus")]


def test_source_fmp_never_calls_websearch_even_on_refusal(
    news_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch_news.fmpnews, "FMP_API_KEY", "key")
    monkeypatch.setattr(
        fetch_news.fmpnews,
        "fetch_news_for_ticker",
        _FmpStub(_fmp_result(401, {"Error Message": "Invalid API KEY"})),
    )
    ws = _WsRecorder([_ws_row()])
    monkeypatch.setattr(fetch_news, "fetch_websearch_news_for_ticker", ws)

    fetch_news.run(["AAPL"], source="fmp", db_path=str(news_db), days=2, limit=50)
    assert ws.calls == 0  # fmp-only: no fallback even when FMP refuses


# ===========================================================================
# additive feeds (EDGAR + grades): supplement, never replace
# ===========================================================================


class _AdditiveRecorder:
    """Canned per-ticker additive fetcher (records calls, optionally raises)."""

    def __init__(self, rows: list[NewsRow] | None = None, error: Exception | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls = 0

    def __call__(self, ticker: str, *, days: int) -> list[NewsRow]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.rows


def _edgar_row(headline: str = "8-K 2.01, 9.01: completed acquisition — Acme") -> NewsRow:
    return NewsRow(
        ticker="AAPL",
        headline=headline,
        url="https://www.sec.gov/Archives/edgar/data/1/1/doc.htm",
        published_at="2026-01-15 21:00:00",
        source="SEC EDGAR",
        source_feed="edgar_8k",
    )


def _grade_row() -> NewsRow:
    return NewsRow(
        ticker="AAPL",
        headline="Wedbush upgrades AAPL to Outperform",
        url="https://finance.yahoo.com/quote/AAPL/analysis#grade-1",
        published_at="2026-01-15 14:00:00",
        source="Wedbush",
        source_feed="yf_grades",
    )


def _patch_primary_fmp_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_news.fmpnews, "FMP_API_KEY", "key")
    monkeypatch.setattr(
        fetch_news.fmpnews,
        "fetch_news_for_ticker",
        _FmpStub(_fmp_result(200, [{"x": 1}], [_fmp_row()])),
    )
    monkeypatch.setattr(fetch_news, "fetch_websearch_news_for_ticker", _WsRecorder([]))


def test_additive_rows_persist_alongside_policy_rows(
    news_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_primary_fmp_ok(monkeypatch)
    edgar = _AdditiveRecorder([_edgar_row()])
    grades = _AdditiveRecorder([_grade_row()])
    monkeypatch.setattr(fetch_news.edgarnews, "fetch_edgar_news_for_ticker", edgar)
    monkeypatch.setattr(fetch_news.yfgrades, "fetch_grades_for_ticker", grades)

    rc = fetch_news.run(["AAPL"], source="auto", db_path=str(news_db), days=2, limit=50)
    assert rc == 0
    assert (edgar.calls, grades.calls) == (1, 1)
    assert _news_rows(news_db) == [
        ("AAPL", "https://finance.yahoo.com/quote/AAPL/analysis#grade-1", "yf_grades"),
        ("AAPL", "https://news.example/fmp1", "fmp_stock_news"),
        ("AAPL", "https://www.sec.gov/Archives/edgar/data/1/1/doc.htm", "edgar_8k"),
    ]


def test_additive_duplicate_of_policy_story_is_dropped(
    news_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same ticker + normalized headline + date as the FMP row (urls differ) ->
    the additive copy never lands; the policy row does."""
    _patch_primary_fmp_ok(monkeypatch)
    dup = _edgar_row(headline="FMP story")  # normalizes equal to 'fmp story', same date
    monkeypatch.setattr(
        fetch_news.edgarnews, "fetch_edgar_news_for_ticker", _AdditiveRecorder([dup])
    )

    fetch_news.run(["AAPL"], source="auto", db_path=str(news_db), days=2, limit=50)
    assert _news_rows(news_db) == [("AAPL", "https://news.example/fmp1", "fmp_stock_news")]


def test_additive_failure_degrades_without_touching_policy_rows(
    news_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_primary_fmp_ok(monkeypatch)
    monkeypatch.setattr(
        fetch_news.edgarnews,
        "fetch_edgar_news_for_ticker",
        _AdditiveRecorder(error=RuntimeError("sec.gov down")),
    )
    monkeypatch.setattr(
        fetch_news.yfgrades,
        "fetch_grades_for_ticker",
        _AdditiveRecorder(error=RuntimeError("yahoo down")),
    )

    rc = fetch_news.run(["AAPL"], source="auto", db_path=str(news_db), days=2, limit=50)
    assert rc == 0  # additive hiccups never fail the dispatch
    assert _news_rows(news_db) == [("AAPL", "https://news.example/fmp1", "fmp_stock_news")]


def test_skip_flags_disable_each_additive_feed(
    news_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_primary_fmp_ok(monkeypatch)
    edgar = _AdditiveRecorder([_edgar_row()])
    grades = _AdditiveRecorder([_grade_row()])
    monkeypatch.setattr(fetch_news.edgarnews, "fetch_edgar_news_for_ticker", edgar)
    monkeypatch.setattr(fetch_news.yfgrades, "fetch_grades_for_ticker", grades)

    fetch_news.run(
        ["AAPL"],
        source="auto",
        db_path=str(news_db),
        days=2,
        limit=50,
        skip_edgar=True,
        skip_grades=True,
    )
    assert (edgar.calls, grades.calls) == (0, 0)
    assert _news_rows(news_db) == [("AAPL", "https://news.example/fmp1", "fmp_stock_news")]


def test_additive_feeds_run_under_websearch_policy_too(
    news_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Additive means policy-independent: even with --source websearch (the
    FMP-fully-cut-off setting) the EDGAR + grades legs still contribute."""
    monkeypatch.setattr(fetch_news, "fetch_websearch_news_for_ticker", _WsRecorder([_ws_row()]))
    edgar = _AdditiveRecorder([_edgar_row()])
    monkeypatch.setattr(fetch_news.edgarnews, "fetch_edgar_news_for_ticker", edgar)

    fetch_news.run(["AAPL"], source="websearch", db_path=str(news_db), days=2, limit=50)
    assert edgar.calls == 1
    assert _news_rows(news_db) == [
        ("AAPL", "https://news.example/ws1", "websearch_opus"),
        ("AAPL", "https://www.sec.gov/Archives/edgar/data/1/1/doc.htm", "edgar_8k"),
    ]
