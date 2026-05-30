"""End-to-end tests for the material-news trigger's LLM classification pass.

Mocks ``triggers.material_news.call_llm`` (and stubs ``load_thesis_anchor``)
so the suite runs against a local SQLite fixture without hitting any real API.
Both names are reachable ONLY through ``scan()``; build_alert / should_fire /
draft_actions are deterministic and touch neither, so only the scan tests patch.

Material news is an LLM-DEPENDENT trigger (like earnings_tone, the inverse of
kpi_inflection): the materiality judgment IS the LLM call, with no deterministic
fallback. The key contracts verified here:

  * scan() emits a candidate ONLY for stories scored >= the relevance floor —
    immaterial stories never become candidates (the veto lives in scan)
  * scan() degrades to [] (never raises, never fabricates) when the LLM fails
  * the batch classification is cached — a second scan over the same news is a
    cache hit (no second LLM call)
  * build_alert is deterministic from the stored classification — NO LLM call
  * draft_actions emits thesis_update + earnings_prep_append only (material news
    is informational: no bear_append, no sizing_update)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from alerts.store import compute_signature_sha
from triggers import MaterialNewsTrigger, UserStateContext
from triggers.base import Cadence, Trigger, TriggerCandidate

# ---------------------------------------------------------------------------
# Schema fixture — news table (the trigger's contract) + llm_artifacts (cache)
# ---------------------------------------------------------------------------


def _create_schema(conn: sqlite3.Connection) -> None:
    """Create the news table the trigger reads + the artifact-store cache table.

    No migration creates ``news`` in the repo today (news is WebSearch-driven
    free markdown); this fixture stands in for the structured per-story table
    the trigger's column contract expects, mirroring how the earnings_tone test
    fixtures the transcripts tables.
    """
    _ = conn.execute(
        "CREATE TABLE news ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "ticker TEXT NOT NULL, "
        + "headline TEXT NOT NULL, "
        + "url TEXT NOT NULL, "
        + "published_at TEXT NOT NULL, "
        + "snippet TEXT"
        + ")"
    )
    _ = conn.execute(
        "CREATE TABLE llm_artifacts ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "ticker TEXT, "
        + "scope TEXT NOT NULL DEFAULT 'ticker', "
        + "purpose TEXT NOT NULL, "
        + "fiscal_period TEXT, "
        + "content_md TEXT, "
        + "content_json TEXT, "
        + "input_sha256 TEXT NOT NULL, "
        + "output_sha256 TEXT, "
        + "model TEXT, "
        + "prompt_version TEXT NOT NULL DEFAULT 'v1', "
        + "generated_at TEXT NOT NULL, "
        + "expires_at TEXT, "
        + "superseded_by_id INTEGER, "
        + "dirty INTEGER NOT NULL DEFAULT 0, "
        + "dirty_reason TEXT, "
        + "source_doc_ids TEXT, "
        + "parent_artifact_ids TEXT, "
        + "llm_call_id INTEGER"
        + ")"
    )


def _insert_news(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    headline: str,
    url: str,
    published_at: datetime,
    snippet: str | None = None,
) -> int:
    """Insert one news row, return its id. ``published_at`` is stored in the
    ``'YYYY-MM-DD HH:MM:SS'`` shape the trigger's recency filter compares against."""
    cur = conn.execute(
        "INSERT INTO news (ticker, headline, url, published_at, snippet) "
        + "VALUES (?, ?, ?, ?, ?)",
        (ticker, headline, url, published_at.strftime("%Y-%m-%d %H:%M:%S"), snippet),
    )
    conn.commit()
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _seed_three_recent(conn: sqlite3.Connection, *, ticker: str = "BN") -> list[int]:
    """Three recent stories (1h/2h/3h old), most-recent first. Returns their ids."""
    now = datetime.now(UTC).replace(tzinfo=None)
    return [
        _insert_news(
            conn,
            ticker=ticker,
            headline="Acme acquires Beta for $2B",
            url="https://news.example/1",
            published_at=now - timedelta(hours=1),
            snippet="A major acquisition reshaping the segment.",
        ),
        _insert_news(
            conn,
            ticker=ticker,
            headline="CEO sells routine shares under 10b5-1 plan",
            url="https://news.example/2",
            published_at=now - timedelta(hours=2),
        ),
        _insert_news(
            conn,
            ticker=ticker,
            headline="Regulator opens probe into core unit",
            url="https://news.example/3",
            published_at=now - timedelta(hours=3),
            snippet="Antitrust scrutiny of the flagship business.",
        ),
    ]


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create the test DB (news + llm_artifacts) and route DB_PATH consumers to it."""
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    try:
        _create_schema(conn)
        # WAL lets the artifact-store's separate write connection coexist with
        # the test's read connection without tripping a sqlite file lock.
        _ = conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("db.DB_PATH", str(path))
    return path


@pytest.fixture
def fixture_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a connection on the fixture DB (committed inserts visible to the cache)."""
    conn = sqlite3.connect(str(db_path))
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# LLM mock + stub + payload helpers
# ---------------------------------------------------------------------------


def _anchor_stub(*_args: object, **_kwargs: object) -> str:
    """Stand-in for load_thesis_anchor — hermetic, no real-repo holdings read.

    Passed to ``monkeypatch.setattr`` in scan tests; the empty string exercises
    the prompt's generic-framing fallback. Scan is the only path that loads the
    anchor, so non-scan tests don't need it.
    """
    return ""


class _StatefulLLM:
    """Tracks call count + returns canned responses in sequence.

    The last entry is reused once the queue is exhausted so a test that sets up
    one response doesn't index-out-of-bounds on an accidental re-call (itself
    useful signal — see the cache-hit test that asserts call_count stays 1).
    """

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.call_count = 0
        self.prompts: list[str] = []

    def __call__(self, prompt: str, **_kwargs: object) -> str:
        self.prompts.append(prompt)
        idx = min(self.call_count, len(self.responses) - 1)
        self.call_count += 1
        return self.responses[idx]


def _classification_payload(scores: list[tuple[int, float, str]]) -> str:
    """Build a canned JSON-array response matching the prompt's contract."""
    return json.dumps(
        [{"news_index": idx, "relevance": rel, "why_material": why} for idx, rel, why in scores]
    )


def _make_candidate(
    *,
    ticker: str = "BN",
    news_id: int = 1,
    headline: str = "Acme acquires Beta for $2B",
    url: str = "https://news.example/1",
    published_at: str = "2026-05-29 10:00:00",
    relevance: float = 0.82,
    why_material: str = "Material M&A reshapes the segment",
) -> TriggerCandidate:
    """Build a candidate directly (bypassing scan) for unit-testing the
    deterministic build_alert / draft_actions / should_fire paths."""
    return TriggerCandidate(
        ticker=ticker,
        kind="material_news",
        key=f"{ticker}:news:{news_id}",
        evidence={
            "news_id": news_id,
            "headline": headline,
            "url": url,
            "published_at": published_at,
            "relevance_score": relevance,
            "why_material": why_material,
        },
        computed_at=datetime.now(UTC).replace(tzinfo=None),
    )


def _empty_state() -> UserStateContext:
    return UserStateContext(
        registered_kpis=[], sizing_intents=[], recent_dismissed_signatures=set()
    )


def _patch_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub load_thesis_anchor for a scan test (hermetic, deterministic)."""
    monkeypatch.setattr("triggers.material_news.load_thesis_anchor", _anchor_stub)


# ---------------------------------------------------------------------------
# Protocol shape
# ---------------------------------------------------------------------------


def test_is_trigger_protocol_instance() -> None:
    trig = MaterialNewsTrigger()
    assert isinstance(trig, Trigger)
    assert trig.kind == "material_news"
    assert trig.cadence == Cadence.ON_NEWS


def test_signature_key_evidence_is_news_id_only() -> None:
    cand = _make_candidate(news_id=42)
    assert dict(MaterialNewsTrigger().signature_key_evidence(cand)) == {"news_id": 42}


# ---------------------------------------------------------------------------
# scan() — classification, veto, degradation, cache
# ---------------------------------------------------------------------------


def test_scan_emits_candidates_only_for_material_stories(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 recent stories, LLM scores 2 of them >= 0.6 → exactly 2 candidates,
    each carrying relevance + why_material in evidence."""
    ids = _seed_three_recent(fixture_db)
    payload = _classification_payload(
        [
            (0, 0.90, "Major M&A reshapes the segment"),
            (1, 0.20, "Routine 10b5-1 insider sale, immaterial"),
            (2, 0.75, "Antitrust probe threatens the core unit"),
        ]
    )
    mock = _StatefulLLM([payload])
    monkeypatch.setattr("triggers.material_news.call_llm", mock)
    _patch_anchor(monkeypatch)

    candidates = MaterialNewsTrigger().scan("BN", fixture_db)

    assert mock.call_count == 1  # ONE batched call for the whole ticker
    assert len(candidates) == 2
    keys = sorted(c.key for c in candidates)
    assert keys == [f"BN:news:{ids[0]}", f"BN:news:{ids[2]}"]
    for cand in candidates:
        assert cand.ticker == "BN"
        assert cand.kind == "material_news"
        assert cand.evidence["relevance_score"] >= 0.6
        assert isinstance(cand.evidence["why_material"], str)
        assert cand.evidence["why_material"]
        assert cand.evidence["url"].startswith("https://")


def test_scan_no_recent_news_returns_empty(fixture_db: sqlite3.Connection) -> None:
    """A story older than the recency window is not classified or fired.

    scan short-circuits on the empty-recent-news set before reaching the LLM or
    the anchor, so no patching is needed.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    _ = _insert_news(
        fixture_db,
        ticker="BN",
        headline="Stale story from two days ago",
        url="https://news.example/old",
        published_at=now - timedelta(hours=48),
    )
    assert MaterialNewsTrigger().scan("BN", fixture_db) == []


def test_scan_missing_news_table_returns_empty(fixture_db: sqlite3.Connection) -> None:
    """No news table → [] (the current production reality), never a raise."""
    _ = fixture_db.execute("DROP TABLE news")
    fixture_db.commit()
    assert MaterialNewsTrigger().scan("BN", fixture_db) == []


def test_scan_degrades_to_empty_when_llm_raises(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE key LLM-dependent contract: a failed classification yields no
    alerts (not a raise, not fabricated materiality)."""
    _ = _seed_three_recent(fixture_db)

    def _boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr("triggers.material_news.call_llm", _boom)
    _patch_anchor(monkeypatch)
    assert MaterialNewsTrigger().scan("BN", fixture_db) == []


def test_scan_retries_once_on_malformed_json_then_succeeds(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First response is non-JSON; the retry (carrying the JSON-only preamble)
    parses, and the candidate lands."""
    ids = _seed_three_recent(fixture_db)
    good = _classification_payload([(0, 0.91, "Material M&A")])
    mock = _StatefulLLM(["Here you go: { not json", good])
    monkeypatch.setattr("triggers.material_news.call_llm", mock)
    _patch_anchor(monkeypatch)

    candidates = MaterialNewsTrigger().scan("BN", fixture_db)

    assert mock.call_count == 2
    assert "previous response was not valid JSON" in mock.prompts[1]
    assert [c.key for c in candidates] == [f"BN:news:{ids[0]}"]


def test_scan_degrades_to_empty_on_malformed_json_twice(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both attempts return non-array text → degrade to [] (no raise)."""
    _ = _seed_three_recent(fixture_db)
    mock = _StatefulLLM(["not json", "still not json"])
    monkeypatch.setattr("triggers.material_news.call_llm", mock)
    _patch_anchor(monkeypatch)

    assert MaterialNewsTrigger().scan("BN", fixture_db) == []
    assert mock.call_count == 2


def test_scan_all_below_threshold_returns_empty(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The veto: when every story scores below the floor, no candidates are
    emitted even though the LLM responded cleanly."""
    _ = _seed_three_recent(fixture_db)
    payload = _classification_payload(
        [(0, 0.10, "noise"), (1, 0.30, "routine"), (2, 0.55, "near-miss, still below")]
    )
    mock = _StatefulLLM([payload])
    monkeypatch.setattr("triggers.material_news.call_llm", mock)
    _patch_anchor(monkeypatch)

    assert MaterialNewsTrigger().scan("BN", fixture_db) == []
    assert mock.call_count == 1


def test_scan_cache_hit_skips_second_llm_call(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second scan over the same news batch is an artifact-store cache hit —
    call_llm must not run again."""
    _ = _seed_three_recent(fixture_db)
    payload = _classification_payload([(0, 0.90, "Material M&A")])
    mock = _StatefulLLM([payload])
    monkeypatch.setattr("triggers.material_news.call_llm", mock)
    _patch_anchor(monkeypatch)

    trig = MaterialNewsTrigger()
    first = trig.scan("BN", fixture_db)
    assert mock.call_count == 1

    second = trig.scan("BN", fixture_db)
    assert mock.call_count == 1, "cache miss — call_llm was re-invoked"
    assert [c.key for c in first] == [c.key for c in second]


# ---------------------------------------------------------------------------
# should_fire — defensive relevance re-check
# ---------------------------------------------------------------------------


def test_should_fire_respects_relevance_floor() -> None:
    trig = MaterialNewsTrigger()
    state = _empty_state()
    assert trig.should_fire(_make_candidate(relevance=0.60), state) is True
    assert trig.should_fire(_make_candidate(relevance=0.95), state) is True
    assert trig.should_fire(_make_candidate(relevance=0.59), state) is False


# ---------------------------------------------------------------------------
# build_alert — deterministic, no LLM
# ---------------------------------------------------------------------------


def test_build_alert_is_deterministic_and_calls_no_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_alert composes the memo from stored evidence with NO LLM call.

    A mock is installed purely to assert it is never invoked.
    """
    mock = _StatefulLLM([_classification_payload([])])
    monkeypatch.setattr("triggers.material_news.call_llm", mock)

    cand = _make_candidate(
        headline="Acme acquires Beta for $2B",
        relevance=0.82,
        why_material="Material M&A reshapes the segment",
    )
    alert = MaterialNewsTrigger().build_alert(cand, None)

    assert mock.call_count == 0  # build_alert issues NO LLM call
    assert alert.trigger_kind == "material_news"
    assert alert.ticker == "BN"
    memo = alert.memo_text
    assert memo is not None
    assert "Acme acquires Beta for $2B" in memo
    assert "82%" in memo

    expected_sig = compute_signature_sha("material_news", "BN", {"news_id": 1})
    assert alert.signature_sha == expected_sig

    evidence = json.loads(alert.evidence_json)
    assert evidence["news_id"] == 1
    assert evidence["url"] == "https://news.example/1"
    assert evidence["relevance_score"] == 0.82
    assert evidence["why_material"] == "Material M&A reshapes the segment"


def test_build_alert_memo_handles_empty_reason() -> None:
    """An empty why_material still produces a clean memo (no dangling 'because')."""
    cand = _make_candidate(why_material="")
    memo = MaterialNewsTrigger().build_alert(cand, None).memo_text
    assert memo is not None
    assert "because" not in memo
    assert "82%" in memo


# ---------------------------------------------------------------------------
# draft_actions — informational only
# ---------------------------------------------------------------------------


def test_draft_actions_thesis_update_and_earnings_prep_only() -> None:
    cand = _make_candidate(
        news_id=7,
        headline="Acme acquires Beta for $2B",
        url="https://news.example/7",
        why_material="Reshapes the competitive landscape",
    )
    alert = MaterialNewsTrigger().build_alert(cand, None)
    actions = MaterialNewsTrigger().draft_actions(alert, cand)

    kinds = sorted(a.action_kind for a in actions)
    assert kinds == ["earnings_prep_append", "thesis_update"]
    # Material news is informational — never direction-bearing actions.
    assert "bear_append" not in kinds
    assert "sizing_update" not in kinds

    by_kind: dict[str, dict[str, Any]] = {a.action_kind: dict(a.payload) for a in actions}
    thesis = by_kind["thesis_update"]
    assert thesis["news_id"] == 7
    assert thesis["news_url"] == "https://news.example/7"
    assert "Acme acquires Beta for $2B" in thesis["body"]
    assert "Reshapes the competitive landscape" in thesis["body"]

    prep = by_kind["earnings_prep_append"]
    assert prep["news_id"] == 7
    assert "Acme acquires Beta for $2B" in prep["body"]


# ---------------------------------------------------------------------------
# Full pipeline integration
# ---------------------------------------------------------------------------


def test_full_pipeline_integration_smoke(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BN story scored material walks scan → should_fire → build_alert →
    draft_actions end-to-end."""
    now = datetime.now(UTC).replace(tzinfo=None)
    news_id = _insert_news(
        fixture_db,
        ticker="BN",
        headline="Brookfield closes $12B flagship infrastructure fund",
        url="https://news.example/bn1",
        published_at=now - timedelta(hours=2),
        snippet="Largest close to date, expanding fee-bearing capital.",
    )
    payload = _classification_payload(
        [(0, 0.88, "A record fund close expands fee-bearing capital")]
    )
    mock = _StatefulLLM([payload])
    monkeypatch.setattr("triggers.material_news.call_llm", mock)
    _patch_anchor(monkeypatch)

    trig = MaterialNewsTrigger()
    candidates = trig.scan("BN", fixture_db)
    assert len(candidates) == 1

    assert trig.should_fire(candidates[0], _empty_state()) is True

    alert = trig.build_alert(candidates[0], None)
    assert alert.ticker == "BN"
    assert alert.trigger_kind == "material_news"
    memo = alert.memo_text
    assert memo is not None
    assert "Brookfield closes $12B flagship infrastructure fund" in memo
    assert alert.signature_sha == compute_signature_sha("material_news", "BN", {"news_id": news_id})

    actions = trig.draft_actions(alert, candidates[0])
    assert sorted(a.action_kind for a in actions) == [
        "earnings_prep_append",
        "thesis_update",
    ]
