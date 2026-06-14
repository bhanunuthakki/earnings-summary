"""Turn-level ask caches (src/ask/turn_cache.py, L14) + their wiring into
``ask.grounding``. Native prompt caching is unreachable through ``claude -p``
(see the module docstring), so these reuse the stable, question-independent work
across turns. Nothing here calls an LLM — the corpus + gather caches are pure
file/DB reuse, and the autouse ``_clear_ask_turn_caches`` fixture (conftest)
resets module state around every test."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from ask import grounding, turn_cache
from ask.turn_cache import _LruTtlCache  # pyright: ignore[reportPrivateUsage]

# ---------------------------------------------------------------------------
# _LruTtlCache primitive
# ---------------------------------------------------------------------------


def test_lru_evicts_least_recently_used() -> None:
    cache: _LruTtlCache[str, int] = _LruTtlCache(max_entries=2, ttl_seconds=0)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1  # touch a → b is now the LRU
    cache.put("c", 3)  # evicts b
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_ttl_expires_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(turn_cache.time, "monotonic", lambda: clock[0])
    cache: _LruTtlCache[str, str] = _LruTtlCache(max_entries=8, ttl_seconds=10)
    cache.put("k", "v")
    assert cache.get("k") == "v"
    clock[0] = 1011.0  # +11s, past the 10s TTL
    assert cache.get("k") is None


def test_hits_and_misses_counted() -> None:
    cache: _LruTtlCache[str, int] = _LruTtlCache(max_entries=4, ttl_seconds=0)
    cache.put("k", 1)
    cache.get("k")  # hit
    cache.get("nope")  # miss
    assert cache.hits == 1
    assert cache.misses == 1


# ---------------------------------------------------------------------------
# Corpus (content) cache — question-independent file parse reuse
# ---------------------------------------------------------------------------


def test_cached_file_parse_memoizes_by_mtime(tmp_path: Path) -> None:
    path = tmp_path / "doc.txt"
    path.write_text("hello", encoding="utf-8")
    calls: list[Path] = []

    def builder(p: Path) -> str:
        calls.append(p)
        return p.read_text(encoding="utf-8")

    assert turn_cache.cached_file_parse(path, builder) == "hello"
    assert turn_cache.cached_file_parse(path, builder) == "hello"
    assert len(calls) == 1  # the second read was served from cache

    # A changed file (new mtime) is a fresh key → the builder re-runs.
    path.write_text("world", encoding="utf-8")
    future = path.stat().st_mtime + 10
    os.utime(path, (future, future))
    assert turn_cache.cached_file_parse(path, builder) == "world"
    assert len(calls) == 2


def test_cached_file_parse_never_caches_none(tmp_path: Path) -> None:
    path = tmp_path / "doc.txt"
    path.write_text("x", encoding="utf-8")
    calls: list[int] = []

    def builder(_p: Path) -> str | None:
        calls.append(1)
        return None  # unparseable

    assert turn_cache.cached_file_parse(path, builder) is None
    assert turn_cache.cached_file_parse(path, builder) is None
    assert len(calls) == 2  # None is never cached → re-run, as the pre-cache code re-read
    # A missing file returns None WITHOUT calling the builder.
    assert turn_cache.cached_file_parse(tmp_path / "absent.txt", builder) is None
    assert len(calls) == 2


def test_cached_file_parse_caches_empty_result(tmp_path: Path) -> None:
    path = tmp_path / "doc.txt"
    path.write_text("x", encoding="utf-8")
    calls: list[int] = []

    def builder(_p: Path) -> list[str]:
        calls.append(1)
        return []  # valid but empty

    assert turn_cache.cached_file_parse(path, builder) == []
    assert turn_cache.cached_file_parse(path, builder) == []
    assert len(calls) == 1  # an empty-but-valid parse is a cache hit, not re-read


# ---------------------------------------------------------------------------
# Route + gather public surface
# ---------------------------------------------------------------------------


def test_normalize_question_folds_case_and_whitespace() -> None:
    assert turn_cache.normalize_question("  Trim   NU?  ") == "trim nu?"
    assert turn_cache.normalize_question("Trim NU?") == turn_cache.normalize_question("trim  nu?")


def test_route_cache_roundtrip_and_clear() -> None:
    assert turn_cache.get_route("trim nu?") is None
    turn_cache.put_route("trim nu?", ("holdings", "dcf"))
    assert turn_cache.get_route("trim nu?") == ("holdings", "dcf")
    turn_cache.put_route("", ("holdings",))  # empty key is a no-op
    assert turn_cache.get_route("") is None
    turn_cache.clear_all()
    assert turn_cache.get_route("trim nu?") is None


def test_gather_memo_returns_isolated_copy() -> None:
    key = turn_cache.gather_key(
        cache_key="sid:1",
        repo_root=Path("/r"),
        scope_tickers=["nu"],
        norm_question="x",
        db_token=1,
    )
    turn_cache.put_gather(key, ["a", "b"])
    first = turn_cache.get_gather(key)
    assert first == ["a", "b"]
    first.append("mutated")  # mutating the returned list must not poison the cache
    assert turn_cache.get_gather(key) == ["a", "b"]


def test_gather_key_normalizes_scope_order_and_case() -> None:
    a = turn_cache.gather_key(
        cache_key="s",
        repo_root=Path("/r"),
        scope_tickers=["NU", "meli"],
        norm_question="q",
        db_token=1,
    )
    b = turn_cache.gather_key(
        cache_key="s",
        repo_root=Path("/r"),
        scope_tickers=["MELI", "nu"],
        norm_question="q",
        db_token=1,
    )
    assert a == b  # scope order + case do not change identity
    c = turn_cache.gather_key(
        cache_key="s", repo_root=Path("/r"), scope_tickers=["NU"], norm_question="q", db_token=2
    )
    assert a != c  # a different db token DOES (freshness)


# ---------------------------------------------------------------------------
# Wiring into ask.grounding — corpus reuse + the gather memo
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, source_type TEXT,
    doc_type TEXT, file_path TEXT, sha256 TEXT, fetched_at TIMESTAMP,
    fetch_status TEXT, source_url TEXT, source_quality_tier TEXT);
CREATE TABLE transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER, ticker TEXT,
    call_date TIMESTAMP, fiscal_period_type TEXT, period_end TIMESTAMP);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, period_end TIMESTAMP,
    fiscal_period_type TEXT, line_item TEXT, value TEXT,
    unit TEXT DEFAULT 'actual', source_doc_id INTEGER);
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, name TEXT, unit TEXT DEFAULT 'actual');
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, period_end TIMESTAMP,
    fiscal_period_type TEXT, kpi_definition_id INTEGER, value TEXT,
    unit TEXT DEFAULT 'actual', source_doc_id INTEGER);
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT DEFAULT 'bhanu',
    ticker TEXT, name TEXT, list_type TEXT);
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "data" / "historical" / "fmp").mkdir(parents=True)
    (tmp_path / "transcripts" / "processed").mkdir(parents=True)
    filing_rel = "data/historical/fmp/TST_form_10k_2025.json"
    transcript_rel = "transcripts/processed/TST_Q1_2026.txt"
    (tmp_path / filing_rel).write_text(
        '{"symbol":"TST","period":"FY","year":2025,'
        '"Credit": [{"NPL": ["NPL formation and margin trends improved this quarter."]}]}',
        encoding="utf-8",
    )
    (tmp_path / transcript_rel).write_text(
        "CFO: Margin trajectory and NPL formation improved across every cohort and geography.\n"
        "CEO: We expect operating leverage to build through the year as deposit volumes scale.\n",
        encoding="utf-8",
    )
    db = tmp_path / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status) VALUES"
        " (2,'TST','fmp','fmp_10k_json',?, 'b','2026-01-05 10:00:00','ok')",
        (filing_rel,),
    )
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status) VALUES"
        " (3,'TST','transcript_audio','earnings_call_transcript',?, 'c','2026-04-05 10:00:00','ok')",
        (transcript_rel,),
    )
    conn.execute(
        "INSERT INTO transcripts (document_id, ticker, fiscal_period_type, period_end)"
        " VALUES (3,'TST','Q1','2026-03-31')"
    )
    conn.commit()
    conn.close()
    return tmp_path


def _gather(repo: Path, *, cache_key: str | None) -> list[grounding.EvidenceItem]:
    return grounding.gather_evidence(
        "How did margin and NPL formation trend this quarter?",
        repo_root=repo,
        db_path=repo / "data" / "portfolio.db",
        scope_tickers=["TST"],
        cache_key=cache_key,
    )


def test_corpus_cache_reuses_file_parse_across_turns(repo: Path) -> None:
    # Two turns with the same scope re-score the SAME filing + transcript; the
    # parse is cached by (path, mtime), so the second turn is corpus-cache hits.
    first = _gather(repo, cache_key=None)
    assert first, "expected filing/transcript evidence"
    before = turn_cache.stats()["corpus"]["hits"]
    second = _gather(repo, cache_key=None)
    after = turn_cache.stats()["corpus"]["hits"]
    assert after > before  # the second turn served the parse from cache
    # Behavior is unchanged: identical evidence text either way.
    assert [e.text for e in first] == [e.text for e in second]
    # cache_key=None never engages the gather memo.
    assert turn_cache.stats()["gather"]["hits"] == 0


def test_gather_memo_skips_retrieval_on_exact_repeat(repo: Path) -> None:
    first = _gather(repo, cache_key="sid:1")
    assert turn_cache.stats()["gather"] == {"hits": 0, "misses": 1}
    second = _gather(repo, cache_key="sid:1")
    assert turn_cache.stats()["gather"]["hits"] == 1
    assert [e.text for e in first] == [e.text for e in second]


def test_gather_memo_busts_on_db_write(repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    _gather(repo, cache_key="sid:1")
    _gather(repo, cache_key="sid:1")
    assert turn_cache.stats()["gather"]["hits"] == 1
    # A DB write (mtime change) invalidates the memo — the next turn re-retrieves.
    future = db.stat().st_mtime + 10
    os.utime(db, (future, future))
    _gather(repo, cache_key="sid:1")
    assert turn_cache.stats()["gather"]["misses"] == 2


def test_gather_memo_isolated_per_thread(repo: Path) -> None:
    _gather(repo, cache_key="sid:1")
    # A different thread key is a different memo — no cross-conversation leak.
    _gather(repo, cache_key="sid:2")
    assert turn_cache.stats()["gather"]["hits"] == 0
    assert turn_cache.stats()["gather"]["misses"] == 2


# ---------------------------------------------------------------------------
# Engine cache-key derivation (the per-thread identity feeding the memo)
# ---------------------------------------------------------------------------


def test_engine_turn_cache_key_derivation() -> None:
    from datetime import date

    from ask.context import ContextPack
    from ask.engine import AskTurn
    from ask.engine import _turn_cache_key as key  # pyright: ignore[reportPrivateUsage]

    # A server-side session id wins (the Ask tab).
    assert key(AskTurn(text="x", session_id="abc"), ContextPack(scope="portfolio")) == "sid:abc"
    # Report drawer with no session → ticker:report_date.
    drawer = ContextPack(scope="ticker", ticker="NU", report_date=date(2026, 3, 31))
    assert key(AskTurn(text="x"), drawer) == "rep:NU:2026-03-31"
    # A first-turn portfolio call with no session yet disables the memo.
    assert key(AskTurn(text="x"), ContextPack(scope="portfolio")) is None
