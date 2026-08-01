"""End-to-end tests for the material-news trigger's LLM classification pass.

Mocks ``triggers.material_news.call_llm`` (and stubs ``load_thesis_anchor``)
so the suite runs against a local SQLite fixture without hitting any real API.
Both names are reachable ONLY through ``scan()``; build_alert / should_fire /
draft_actions are deterministic and touch neither, so only the scan tests patch.

Material news is an LLM-DEPENDENT trigger (like earnings_tone, the inverse of
kpi_inflection): the materiality judgment IS the LLM call, with no deterministic
fallback. The key contracts verified here:

  * scan() ALWAYS returns [] — the lane is pull-only (owner ruling
    2026-07-31: news never alerts; the catch-up is the pre-earnings brief).
    Its product is the news_events store: a row per story scored >= the
    relevance floor AND classified as a new PRIMARY event —
    commentary/opinion/recap stories AND results-class earnings coverage
    never reach the store, whatever they scored
  * scan() degrades to a no-op (never raises, never fabricates) when the LLM
    fails
  * the batch classification is cached — a second scan over the same news is a
    cache hit (no second LLM call) and a store no-op
  * should_fire / build_alert / draft_actions stay Protocol-compliant and
    deterministic (unreachable in production — no candidates are emitted)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    # The trigger's product store (alembic 0262) — scan() persists qualifying
    # primary events here and emits no candidates (owner ruling 2026-07-31).
    _ = conn.execute(
        "CREATE TABLE news_events ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "ticker TEXT NOT NULL, "
        + "news_id INTEGER NOT NULL UNIQUE, "
        + "headline TEXT NOT NULL, "
        + "url TEXT NOT NULL, "
        + "published_at TEXT NOT NULL, "
        + "event_key TEXT NOT NULL DEFAULT '', "
        + "event_type TEXT NOT NULL, "
        + "relevance REAL NOT NULL, "
        + "why_material TEXT NOT NULL DEFAULT '', "
        + "classified_at TEXT NOT NULL"
        + ")"
    )


def _noted(conn: sqlite3.Connection) -> list[tuple[int, str, str, float]]:
    """The persisted news_events rows, ordered by news_id — the assertion
    surface for every scan test: (news_id, event_key, event_type, relevance)."""
    return [
        (int(r[0]), str(r[1]), str(r[2]), float(r[3]))
        for r in conn.execute(
            "SELECT news_id, event_key, event_type, relevance FROM news_events ORDER BY news_id"
        ).fetchall()
    ]


def _note_event(
    conn: sqlite3.Connection,
    *,
    ticker: str = "BN",
    news_id: int,
    event_key: str,
    classified_at: datetime,
) -> None:
    """Pre-insert one news_events row (the cross-day guard's lookback set)."""
    _ = conn.execute(
        "INSERT INTO news_events(ticker, news_id, headline, url, published_at, "
        "event_key, event_type, relevance, why_material, classified_at) "
        "VALUES (?, ?, 'prior coverage', 'https://news.example/prior', ?, ?, "
        "'primary', 0.9, 'prior', ?)",
        (
            ticker,
            news_id,
            classified_at.strftime("%Y-%m-%d %H:%M:%S"),
            event_key,
            classified_at.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()


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


def _classification_payload(
    scores: list[tuple[int, float, str]],
    *,
    event_types: dict[int, str] | None = None,
    event_keys: dict[int, str] | None = None,
) -> str:
    """Build a canned JSON-array response matching the v4 prompt contract.

    Every entry carries an ``event_type`` ("primary" unless overridden via
    ``event_types``) and an ``event_key`` (per-index unique unless overridden
    via ``event_keys``, so stories don't cluster together by accident — tests
    of the clustering behavior override with a shared key)."""
    types = event_types or {}
    keys = event_keys or {}
    return json.dumps(
        [
            {
                "news_index": idx,
                "event_type": types.get(idx, "primary"),
                "event_key": keys.get(idx, f"event_{idx}"),
                "relevance": rel,
                "why_material": why,
            }
            for idx, rel, why in scores
        ]
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
    event_type: str | None = "primary",
) -> TriggerCandidate:
    """Build a candidate directly (bypassing scan) for unit-testing the
    deterministic build_alert / draft_actions / should_fire paths.
    ``event_type=None`` omits the field (the pre-v3 evidence shape)."""
    evidence: dict[str, object] = {
        "news_id": news_id,
        "headline": headline,
        "url": url,
        "published_at": published_at,
        "relevance_score": relevance,
        "why_material": why_material,
    }
    if event_type is not None:
        evidence["event_type"] = event_type
    return TriggerCandidate(
        ticker=ticker,
        kind="material_news",
        key=f"{ticker}:news:{news_id}",
        evidence=evidence,
        computed_at=datetime.now(UTC).replace(tzinfo=None),
    )


def _empty_state() -> UserStateContext:
    return UserStateContext(
        registered_kpis=[], sizing_intents=[], recent_dismissed_signatures=set()
    )


def _patch_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the anchor loaders for a scan test (hermetic, deterministic). Only
    the thesis anchor returns content; bear/IR return empty so the composed
    anchor equals the thesis — the historical shape these scan tests assert."""
    monkeypatch.setattr("triggers.material_news.load_thesis_anchor", _anchor_stub)
    monkeypatch.setattr("triggers.material_news.load_bear_anchor", lambda *_a, **_k: "")
    monkeypatch.setattr("triggers.material_news.load_ir_anchor", lambda *_a, **_k: "")


def test_load_anchor_composes_thesis_bear_and_ir(monkeypatch: pytest.MonkeyPatch) -> None:
    """The materiality anchor is the composed 3-block (thesis + bear case + IR
    narrative), not just the thesis — so a story that validates a named bear
    hypothesis or undercuts management's own IR framing can be judged material.
    All three blocks must reach the anchor that frames the classification."""
    monkeypatch.setattr("triggers.material_news.load_thesis_anchor", lambda *_a, **_k: "THESIS-XYZ")
    monkeypatch.setattr("triggers.material_news.load_bear_anchor", lambda *_a, **_k: "BEAR-XYZ")
    monkeypatch.setattr("triggers.material_news.load_ir_anchor", lambda *_a, **_k: "IR-XYZ")

    anchor = MaterialNewsTrigger()._load_anchor("BN")

    assert "THESIS-XYZ" in anchor
    assert "BEAR-XYZ" in anchor
    assert "IR-XYZ" in anchor


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


def test_scan_notes_material_primary_events_and_emits_no_candidates(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 recent stories, 2 score >= the 0.65 floor → scan returns [] (the
    pull-only contract: nothing pending, ever) and persists exactly those 2
    to news_events with their classification fields."""
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

    assert MaterialNewsTrigger().scan("BN", fixture_db) == []

    assert mock.call_count == 1  # ONE batched call for the whole ticker
    noted = _noted(fixture_db)
    assert [(n, t) for n, _k, t, _r in noted] == [(ids[0], "primary"), (ids[2], "primary")]
    assert all(r >= 0.65 for _n, _k, _t, r in noted)


def test_scan_vetoes_commentary_regardless_of_score(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE v3 signal-quality contract: an opinion piece or earnings recap about
    a thesis-relevant TOPIC is never noted, even scored 0.9+ — topical
    relevance without new primary information is noise (owner ruling
    2026-07-30, the LMND CFO think-piece / MSFT earnings-recap alerts)."""
    ids = _seed_three_recent(fixture_db)
    payload = _classification_payload(
        [
            (0, 0.95, "CFO think-piece bears on the profitability debate"),
            (1, 0.90, "Recap of the earnings move"),
            (2, 0.85, "Regulator opens probe into core unit"),
        ],
        event_types={0: "commentary", 1: "commentary", 2: "primary"},
    )
    monkeypatch.setattr("triggers.material_news.call_llm", _StatefulLLM([payload]))
    _patch_anchor(monkeypatch)

    assert MaterialNewsTrigger().scan("BN", fixture_db) == []
    assert [n for n, _k, _t, _r in _noted(fixture_db)] == [ids[2]]


def test_scan_missing_event_type_fails_closed(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry without a recognizable event_type (schema drift back to the v2
    shape) is treated as commentary — excluded — never as a note-anyway
    fallback to the old noisy behavior."""
    _ = _seed_three_recent(fixture_db)
    payload = json.dumps(
        [{"news_index": 0, "relevance": 0.92, "why_material": "high score, no event_type"}]
    )
    monkeypatch.setattr("triggers.material_news.call_llm", _StatefulLLM([payload]))
    _patch_anchor(monkeypatch)

    assert MaterialNewsTrigger().scan("BN", fixture_db) == []
    assert _noted(fixture_db) == []


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
    parses, and the event is noted."""
    ids = _seed_three_recent(fixture_db)
    good = _classification_payload([(0, 0.91, "Material M&A")])
    mock = _StatefulLLM(["Here you go: { not json", good])
    monkeypatch.setattr("triggers.material_news.call_llm", mock)
    _patch_anchor(monkeypatch)

    assert MaterialNewsTrigger().scan("BN", fixture_db) == []

    assert mock.call_count == 2
    assert "previous response was not valid JSON" in mock.prompts[1]
    assert [n for n, _k, _t, _r in _noted(fixture_db)] == [ids[0]]


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
    """The veto: when every story scores below the floor, nothing is noted
    even though the LLM responded cleanly."""
    _ = _seed_three_recent(fixture_db)
    payload = _classification_payload(
        [(0, 0.10, "noise"), (1, 0.30, "routine"), (2, 0.60, "near-miss, still below 0.65")]
    )
    mock = _StatefulLLM([payload])
    monkeypatch.setattr("triggers.material_news.call_llm", mock)
    _patch_anchor(monkeypatch)

    assert MaterialNewsTrigger().scan("BN", fixture_db) == []
    assert mock.call_count == 1
    assert _noted(fixture_db) == []


def test_scan_cache_hit_skips_second_llm_call(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second scan over the same news batch is an artifact-store cache hit —
    call_llm must not run again, and the store stays one-row (INSERT OR
    IGNORE + the recent-event_key guard make the rerun a no-op)."""
    ids = _seed_three_recent(fixture_db)
    payload = _classification_payload([(0, 0.90, "Material M&A")])
    mock = _StatefulLLM([payload])
    monkeypatch.setattr("triggers.material_news.call_llm", mock)
    _patch_anchor(monkeypatch)

    trig = MaterialNewsTrigger()
    assert trig.scan("BN", fixture_db) == []
    assert mock.call_count == 1
    assert [n for n, _k, _t, _r in _noted(fixture_db)] == [ids[0]]

    assert trig.scan("BN", fixture_db) == []
    assert mock.call_count == 1, "cache miss — call_llm was re-invoked"
    assert [n for n, _k, _t, _r in _noted(fixture_db)] == [ids[0]]


# ---------------------------------------------------------------------------
# scan() — event-level dedup (v4)
# ---------------------------------------------------------------------------


def test_scan_clusters_same_event_to_single_row(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE v4 dedup contract: three outlets covering the same real-world event
    (identical event_key, all primary, all above the floor) collapse to exactly
    ONE noted row — the highest-relevance story (prod 2026-07-30: three
    Brookfield/NextEra alerts, four META capex alerts, one per outlet)."""
    ids = _seed_three_recent(fixture_db)
    payload = _classification_payload(
        [
            (0, 0.85, "Acquisition, outlet A"),
            (1, 0.92, "Acquisition, outlet B with deal terms"),
            (2, 0.75, "Acquisition, outlet C"),
        ],
        event_keys={0: "acme_beta_deal", 1: "acme_beta_deal", 2: "acme_beta_deal"},
    )
    monkeypatch.setattr("triggers.material_news.call_llm", _StatefulLLM([payload]))
    _patch_anchor(monkeypatch)

    assert MaterialNewsTrigger().scan("BN", fixture_db) == []
    assert _noted(fixture_db) == [(ids[1], "acme_beta_deal", "primary", 0.92)]


def test_scan_empty_event_key_never_clusters(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unclustered entries (event_key "" or the field missing entirely — e.g. a
    schema drift back to the v3 shape) all note individually: losing the field
    degrades dedup, never coverage."""
    ids = _seed_three_recent(fixture_db)
    payload = json.dumps(
        [
            {
                "news_index": 0,
                "event_type": "primary",
                "event_key": "",
                "relevance": 0.90,
                "why_material": "M&A",
            },
            {
                "news_index": 1,
                "event_type": "primary",
                "relevance": 0.85,
                "why_material": "no event_key field",
            },
            {
                "news_index": 2,
                "event_type": "primary",
                "event_key": "",
                "relevance": 0.80,
                "why_material": "probe",
            },
        ]
    )
    monkeypatch.setattr("triggers.material_news.call_llm", _StatefulLLM([payload]))
    _patch_anchor(monkeypatch)

    assert MaterialNewsTrigger().scan("BN", fixture_db) == []
    noted = _noted(fixture_db)
    assert [n for n, _k, _t, _r in noted] == sorted(ids)
    assert all(k == "" for _n, k, _t, _r in noted)


def test_scan_cross_day_guard_suppresses_recently_noted_event(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A story whose event_key was already noted for the SAME ticker within the
    last 72h is skipped — day-2 coverage arrives with fresh news ids, which
    UNIQUE(news_id) can't see. The guard is ticker-scoped: another ticker's
    row on the same key doesn't suppress."""
    now = datetime.now(UTC).replace(tzinfo=None)
    ids = _seed_three_recent(fixture_db)
    _note_event(
        fixture_db,
        ticker="BN",
        news_id=901,
        event_key="acme_beta_deal",
        classified_at=now - timedelta(hours=10),
    )
    _note_event(
        fixture_db,
        ticker="OTHER",
        news_id=902,
        event_key="core_unit_probe",
        classified_at=now - timedelta(hours=10),
    )
    payload = _classification_payload(
        [
            (0, 0.90, "Day-2 coverage of the acquisition"),
            (1, 0.20, "Routine insider sale"),
            (2, 0.80, "Regulator opens probe"),
        ],
        event_keys={0: "acme_beta_deal", 2: "core_unit_probe"},
    )
    monkeypatch.setattr("triggers.material_news.call_llm", _StatefulLLM([payload]))
    _patch_anchor(monkeypatch)

    assert MaterialNewsTrigger().scan("BN", fixture_db) == []
    fresh = [n for n, _k, _t, _r in _noted(fixture_db) if n not in (901, 902)]
    assert fresh == [ids[2]]


def test_scan_cross_day_guard_releases_after_window(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A noted row on the same event_key OLDER than the 72h window no longer
    suppresses — a genuine re-development of an old event notes again."""
    now = datetime.now(UTC).replace(tzinfo=None)
    ids = _seed_three_recent(fixture_db)
    _note_event(
        fixture_db,
        ticker="BN",
        news_id=901,
        event_key="acme_beta_deal",
        classified_at=now - timedelta(hours=100),
    )
    payload = _classification_payload(
        [(0, 0.90, "The acquisition develops further")],
        event_keys={0: "acme_beta_deal"},
    )
    monkeypatch.setattr("triggers.material_news.call_llm", _StatefulLLM([payload]))
    _patch_anchor(monkeypatch)

    assert MaterialNewsTrigger().scan("BN", fixture_db) == []
    fresh = [n for n, _k, _t, _r in _noted(fixture_db) if n != 901]
    assert fresh == [ids[0]]


# ---------------------------------------------------------------------------
# should_fire — defensive relevance re-check
# ---------------------------------------------------------------------------


def test_should_fire_respects_relevance_floor() -> None:
    trig = MaterialNewsTrigger()
    state = _empty_state()
    assert trig.should_fire(_make_candidate(relevance=0.65), state) is True
    assert trig.should_fire(_make_candidate(relevance=0.95), state) is True
    assert trig.should_fire(_make_candidate(relevance=0.64), state) is False


def test_should_fire_event_type_gate() -> None:
    """Commentary AND results are blocked at fire time — only primary events
    alert (results-day coverage belongs to the earnings machinery, owner
    ruling 2026-07-31). A pre-v3 candidate (no event_type in evidence) passes
    on relevance alone — it was built mid-flight under the old contract."""
    trig = MaterialNewsTrigger()
    state = _empty_state()
    assert (
        trig.should_fire(_make_candidate(relevance=0.95, event_type="commentary"), state) is False
    )
    assert trig.should_fire(_make_candidate(relevance=0.95, event_type="results"), state) is False
    assert trig.should_fire(_make_candidate(relevance=0.95, event_type="primary"), state) is True
    assert trig.should_fire(_make_candidate(relevance=0.95, event_type=None), state) is True


def test_scan_vetoes_results_class(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A results-class story never fires from the news lane, however large the
    surprise — the 2026-07-31 backtest showed every results-class fire was
    redundant with the post-ER readout / earnings_tone coverage."""
    ids = _seed_three_recent(fixture_db)
    payload = _classification_payload(
        [
            (0, 0.95, "Blowout quarter, huge beat"),
            (1, 0.90, "8-K results of operations"),
            (2, 0.85, "Regulator opens probe into core unit"),
        ],
        event_types={0: "results", 1: "results", 2: "primary"},
    )
    monkeypatch.setattr("triggers.material_news.call_llm", _StatefulLLM([payload]))
    _patch_anchor(monkeypatch)

    assert MaterialNewsTrigger().scan("BN", fixture_db) == []
    assert [n for n, _k, _t, _r in _noted(fixture_db)] == [ids[2]]


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
# draft_actions — none (the alert is the deliverable)
# ---------------------------------------------------------------------------


def test_draft_actions_emits_nothing() -> None:
    """The templated thesis_update/earnings_prep pair was ruled noise
    (2026-07-30): it restated the headline, doubled the approve/dismiss
    surface, and pre-generated work the owner takes deliberately through the
    ledger/ask flows. Material news alerts settle at the alert level."""
    cand = _make_candidate(
        news_id=7,
        headline="Acme acquires Beta for $2B",
        url="https://news.example/7",
        why_material="Reshapes the competitive landscape",
    )
    alert = MaterialNewsTrigger().build_alert(cand, None)
    assert MaterialNewsTrigger().draft_actions(alert, cand) == []


# ---------------------------------------------------------------------------
# Full pipeline integration
# ---------------------------------------------------------------------------


def test_full_pipeline_integration_smoke(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BN story scored material walks the whole production path: scan
    classifies it, notes it to news_events with every classification field,
    and hands the driver NOTHING — the pull-only lane end-to-end."""
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
        [(0, 0.88, "A record fund close expands fee-bearing capital")],
        event_keys={0: "bn_flagship_fund_close"},
    )
    mock = _StatefulLLM([payload])
    monkeypatch.setattr("triggers.material_news.call_llm", mock)
    _patch_anchor(monkeypatch)

    assert MaterialNewsTrigger().scan("BN", fixture_db) == []

    row = fixture_db.execute(
        "SELECT ticker, news_id, headline, url, event_key, event_type, relevance, why_material "
        "FROM news_events"
    ).fetchone()
    assert row is not None
    assert row[0] == "BN"
    assert row[1] == news_id
    assert row[2] == "Brookfield closes $12B flagship infrastructure fund"
    assert row[3] == "https://news.example/bn1"
    assert row[4] == "bn_flagship_fund_close"
    assert row[5] == "primary"
    assert row[6] == 0.88
    assert "fee-bearing capital" in row[7]
