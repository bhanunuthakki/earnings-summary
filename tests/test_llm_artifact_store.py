"""Tests for src/llm_artifact_store.py and the migration 0035 schema."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llm_artifact_store import (
    UpsertRequest,
    artifact_is_fresh,
    compute_input_sha256,
    drain_dirty,
    history,
    mark_dirty,
    read_artifact,
    read_current,
    upsert,
)


def _create_schema(conn: sqlite3.Connection) -> None:
    """Mirror migrations 0034 + 0035 inline for self-contained tests."""
    conn.executescript(
        """
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            called_at DATETIME NOT NULL,
            purpose VARCHAR(64), ticker VARCHAR(16), scope VARCHAR(64),
            model VARCHAR(64) NOT NULL,
            prompt_sha256 VARCHAR(64) NOT NULL,
            response_sha256 VARCHAR(64),
            prompt_chars INTEGER NOT NULL, response_chars INTEGER,
            input_tokens INTEGER, cache_creation_input_tokens INTEGER,
            cache_read_input_tokens INTEGER, output_tokens INTEGER,
            elapsed_ms INTEGER NOT NULL,
            cost_estimate_usd FLOAT,
            cache_hit BOOLEAN NOT NULL DEFAULT 0,
            fallback_used VARCHAR(16),
            artifact_id INTEGER,
            error TEXT,
            run_id VARCHAR(64)
        );
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16),
            scope VARCHAR(64) NOT NULL DEFAULT 'ticker',
            purpose VARCHAR(64) NOT NULL,
            fiscal_period VARCHAR(10),
            content_md TEXT,
            content_json TEXT,
            input_sha256 VARCHAR(64) NOT NULL,
            output_sha256 VARCHAR(64),
            model VARCHAR(64),
            prompt_version VARCHAR(32) NOT NULL DEFAULT 'v1',
            generated_at DATETIME NOT NULL,
            expires_at DATETIME,
            superseded_by_id INTEGER REFERENCES llm_artifacts(id),
            dirty BOOLEAN NOT NULL DEFAULT 0,
            dirty_reason VARCHAR(128),
            source_doc_ids TEXT,
            parent_artifact_ids TEXT,
            llm_call_id INTEGER REFERENCES llm_calls(id)
        );
        """
    )
    conn.commit()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "artifacts.db"
    conn = sqlite3.connect(str(path))
    try:
        _create_schema(conn)
    finally:
        conn.close()
    return path


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------


def test_compute_input_sha256_deterministic() -> None:
    assert compute_input_sha256(
        prompt_version="v1", cache_inputs=["a", b"b"]
    ) == compute_input_sha256(prompt_version="v1", cache_inputs=["a", b"b"])


def test_compute_input_sha256_changes_on_input_order() -> None:
    a = compute_input_sha256(prompt_version="v1", cache_inputs=["a", "b"])
    b = compute_input_sha256(prompt_version="v1", cache_inputs=["b", "a"])
    assert a != b


def test_compute_input_sha256_changes_on_prompt_version() -> None:
    a = compute_input_sha256(prompt_version="v1", cache_inputs=["a"])
    b = compute_input_sha256(prompt_version="v2", cache_inputs=["a"])
    assert a != b


# ---------------------------------------------------------------------------
# upsert — happy path + cache hits + supersession
# ---------------------------------------------------------------------------


def test_upsert_creates_row_when_missing(db: Path) -> None:
    aid, hit = upsert(
        UpsertRequest(
            ticker="GOOG",
            purpose="bear_case",
            content_md="# bear",
            cache_inputs=["thesis-v1", "transcripts-q1"],
        ),
        db_path=db,
    )
    assert aid is not None and aid > 0
    assert hit is False

    art = read_current(ticker="GOOG", purpose="bear_case", db_path=db)
    assert art is not None
    assert art.content_md == "# bear"


def test_artifact_freshness_rejects_dirty_and_expired_rows(db: Path) -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    artifact_id, _ = upsert(
        UpsertRequest(
            ticker="GOOG",
            purpose="bear_case",
            content_md="# bear",
            cache_inputs=["freshness"],
            expires_at=now + timedelta(hours=1),
        ),
        db_path=db,
    )
    artifact = read_artifact(artifact_id or 0, db_path=db)
    assert artifact is not None
    assert artifact_is_fresh(artifact, now=now) is True
    assert artifact_is_fresh(artifact, now=now + timedelta(hours=2)) is False

    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE llm_artifacts SET dirty = 1 WHERE id = ?", (artifact.id,))
    conn.commit()
    conn.close()
    dirty = read_artifact(artifact.id, db_path=db)
    assert dirty is not None
    assert artifact_is_fresh(dirty, now=now) is False


def test_upsert_returns_cache_hit_on_same_inputs(db: Path) -> None:
    req = UpsertRequest(
        ticker="GOOG",
        purpose="bear_case",
        content_md="# bear",
        cache_inputs=["thesis-v1"],
    )
    aid1, hit1 = upsert(req, db_path=db)
    aid2, hit2 = upsert(req, db_path=db)
    assert aid1 == aid2
    assert hit1 is False
    assert hit2 is True  # second call hits cache


def test_upsert_supersedes_on_input_change(db: Path) -> None:
    aid1, _ = upsert(
        UpsertRequest(
            ticker="GOOG",
            purpose="bear_case",
            content_md="v1",
            cache_inputs=["thesis-v1"],
        ),
        db_path=db,
    )
    aid2, _ = upsert(
        UpsertRequest(
            ticker="GOOG",
            purpose="bear_case",
            content_md="v2",
            cache_inputs=["thesis-v2"],
        ),
        db_path=db,
    )
    assert aid1 != aid2

    # Current row is the new one
    current = read_current(ticker="GOOG", purpose="bear_case", db_path=db)
    assert current is not None
    assert current.id == aid2
    assert current.content_md == "v2"

    # Prior row exists in history with superseded_by_id set
    hist = history(ticker="GOOG", purpose="bear_case", db_path=db)
    assert len(hist) == 2
    assert hist[0].id == aid2
    assert hist[1].id == aid1
    assert hist[1].superseded_by_id == aid2

    historical = read_artifact(aid1, db_path=db) if aid1 is not None else None
    assert historical is not None
    assert historical.id == aid1
    assert historical.content_md == "v1"
    assert historical.superseded_by_id == aid2


def test_upsert_supersedes_when_dirty_even_if_inputs_match(db: Path) -> None:
    """A dirty=1 row must NOT serve a cache hit; it gets superseded so the
    re-run produces a fresh row with dirty=0."""
    req = UpsertRequest(
        ticker="GOOG",
        purpose="bear_case",
        content_md="v1",
        cache_inputs=["thesis-v1"],
    )
    aid1, _ = upsert(req, db_path=db)

    # Flip dirty via mark_dirty
    n = mark_dirty(ticker="GOOG", purposes=["bear_case"], reason="test", db_path=db)
    assert n == 1

    # Re-run with same inputs — should supersede, not hit cache
    aid2, hit = upsert(req, db_path=db)
    assert aid2 != aid1
    assert hit is False

    current = read_current(ticker="GOOG", purpose="bear_case", db_path=db)
    assert current is not None
    assert current.dirty is False


# ---------------------------------------------------------------------------
# JSON content + provenance metadata
# ---------------------------------------------------------------------------


def test_upsert_roundtrips_json_content(db: Path) -> None:
    payload = {"failure_modes": [{"hypothesis": "x", "evidence_in_data": "y"}]}
    upsert(
        UpsertRequest(
            ticker="GOOG",
            purpose="bear_case",
            content_json=payload,
            cache_inputs=["k"],
        ),
        db_path=db,
    )
    art = read_current(ticker="GOOG", purpose="bear_case", db_path=db)
    assert art is not None
    assert art.content_json == payload


def test_upsert_stores_source_doc_ids(db: Path) -> None:
    upsert(
        UpsertRequest(
            ticker="GOOG",
            purpose="company_description",
            content_md="x",
            cache_inputs=["k"],
            source_doc_ids=[1, 2, 3],
            parent_artifact_ids=[10, 20],
        ),
        db_path=db,
    )
    art = read_current(ticker="GOOG", purpose="company_description", db_path=db)
    assert art is not None
    assert art.source_doc_ids == [1, 2, 3]
    assert art.parent_artifact_ids == [10, 20]


# ---------------------------------------------------------------------------
# read_current uniqueness across fiscal_period
# ---------------------------------------------------------------------------


def test_read_current_partitions_by_fiscal_period(db: Path) -> None:
    upsert(
        UpsertRequest(
            ticker="GOOG",
            purpose="earnings_summary",
            fiscal_period="2025-12-31",
            content_md="2025 Q4",
            cache_inputs=["k1"],
        ),
        db_path=db,
    )
    upsert(
        UpsertRequest(
            ticker="GOOG",
            purpose="earnings_summary",
            fiscal_period="2026-03-31",
            content_md="2026 Q1",
            cache_inputs=["k2"],
        ),
        db_path=db,
    )

    q4 = read_current(
        ticker="GOOG", purpose="earnings_summary", fiscal_period="2025-12-31", db_path=db
    )
    q1 = read_current(
        ticker="GOOG", purpose="earnings_summary", fiscal_period="2026-03-31", db_path=db
    )
    assert q4 is not None and q4.content_md == "2025 Q4"
    assert q1 is not None and q1.content_md == "2026 Q1"


# ---------------------------------------------------------------------------
# Dirty propagation + drain
# ---------------------------------------------------------------------------


def test_mark_dirty_only_touches_matching_rows(db: Path) -> None:
    for ticker, purpose in [("GOOG", "bear_case"), ("META", "bear_case"), ("GOOG", "qa_topics")]:
        upsert(
            UpsertRequest(
                ticker=ticker,
                purpose=purpose,
                content_md="x",
                cache_inputs=[f"{ticker}-{purpose}"],
            ),
            db_path=db,
        )

    n = mark_dirty(ticker="GOOG", purposes=["bear_case"], reason="fact_q3", db_path=db)
    assert n == 1

    goog_bear = read_current(ticker="GOOG", purpose="bear_case", db_path=db)
    meta_bear = read_current(ticker="META", purpose="bear_case", db_path=db)
    goog_qa = read_current(ticker="GOOG", purpose="qa_topics", db_path=db)
    assert goog_bear is not None and goog_bear.dirty is True
    assert meta_bear is not None and meta_bear.dirty is False
    assert goog_qa is not None and goog_qa.dirty is False


def test_drain_dirty_returns_only_dirty(db: Path) -> None:
    for ticker in ["GOOG", "META", "NU"]:
        upsert(
            UpsertRequest(
                ticker=ticker,
                purpose="bear_case",
                content_md="x",
                cache_inputs=[ticker],
            ),
            db_path=db,
        )
    mark_dirty(ticker="GOOG", purposes=["bear_case"], reason="r", db_path=db)
    mark_dirty(ticker="NU", purposes=["bear_case"], reason="r", db_path=db)

    drain = drain_dirty(db_path=db)
    drained_tickers = sorted(a.ticker for a in drain if a.ticker is not None)
    assert drained_tickers == ["GOOG", "NU"]


# ---------------------------------------------------------------------------
# Resilience — missing DB / missing table
# ---------------------------------------------------------------------------


def test_read_current_returns_none_when_db_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no_such.db"
    assert read_current(ticker="X", purpose="y", db_path=missing) is None


def test_upsert_returns_none_when_db_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no_such.db"
    aid, hit = upsert(
        UpsertRequest(ticker="X", purpose="y", content_md="z", cache_inputs=["k"]),
        db_path=missing,
    )
    assert aid is None
    assert hit is False


def test_mark_dirty_returns_zero_when_db_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no_such.db"
    assert mark_dirty(ticker="X", purposes=["y"], reason="r", db_path=missing) == 0


def test_drain_dirty_returns_empty_when_db_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no_such.db"
    assert drain_dirty(db_path=missing) == []


def _drifted_db(tmp_path: Path) -> Path:
    """A DB where llm_artifacts EXISTS but predates several columns (no
    fiscal_period, no superseded_by_id, ...). Mimics a legacy DB or a stale
    hand-rolled test fixture — the reads' WHERE clauses reference columns that
    aren't there."""
    path = tmp_path / "drifted.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE llm_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT, purpose TEXT, scope TEXT, content_md TEXT,
                generated_at TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_read_current_degrades_on_drifted_schema(tmp_path: Path) -> None:
    """Regression: a present-but-drifted llm_artifacts (missing fiscal_period)
    must degrade to None, not raise OperationalError and 500 the render path
    (the portfolio-risk panel crash). _open guards a missing *table*; this
    guards a missing *column*."""
    path = _drifted_db(tmp_path)
    assert (
        read_current(ticker=None, purpose="thesis_collision", scope="portfolio", db_path=path)
        is None
    )


def test_history_and_drain_degrade_on_drifted_schema(tmp_path: Path) -> None:
    path = _drifted_db(tmp_path)
    assert history(ticker="X", purpose="bear_case", db_path=path) == []
    assert drain_dirty(db_path=path) == []
