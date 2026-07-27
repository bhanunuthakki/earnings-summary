"""Tests for the LLM artifact TTL policy + expired-artifact drain.

The schema has `expires_at` since migration 0035 but no upsert ever set it,
and `drain_dirty` only picked up dirty=1 rows. Together that meant cached
LLM outputs (bear_case, company_description, ...) lived forever absent an
explicit mark_dirty.

This wires:
  * `default_expires_at(purpose)` — per-purpose TTL lookup
  * upsert applies the default when the caller didn't pass `expires_at`
  * drain_dirty picks up artifacts where `expires_at < now`
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm_artifact_store import (  # noqa: E402
    _DEFAULT_TTL_DAYS,
    UpsertRequest,
    default_expires_at,
    drain_dirty,
    upsert,
)


def _make_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            scope TEXT NOT NULL DEFAULT 'ticker',
            purpose TEXT NOT NULL,
            fiscal_period TEXT,
            content_md TEXT,
            content_json TEXT,
            input_sha256 TEXT NOT NULL,
            output_sha256 TEXT,
            model TEXT,
            prompt_version TEXT NOT NULL DEFAULT 'v1',
            generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            superseded_by_id INTEGER,
            dirty INTEGER NOT NULL DEFAULT 0,
            dirty_reason TEXT,
            source_doc_ids TEXT,
            parent_artifact_ids TEXT,
            llm_call_id INTEGER
        )
        """
    )
    conn.commit()
    conn.close()


def test_default_expires_at_for_known_purpose() -> None:
    """bear_case has a 30d TTL; the helper returns now+30d."""
    now = datetime(2026, 5, 26, tzinfo=UTC)
    exp = default_expires_at("bear_case", now=now)
    assert exp == now + timedelta(days=_DEFAULT_TTL_DAYS["bear_case"])


def test_default_expires_at_unknown_purpose_returns_none() -> None:
    """Purposes without a TTL policy don't auto-expire."""
    assert default_expires_at("undefined_purpose") is None


def test_upsert_applies_default_ttl_when_caller_doesnt_set_one(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _make_db(db)
    new_id, _hit = upsert(
        UpsertRequest(
            ticker="GOOG",
            purpose="bear_case",
            content_md="content",
            cache_inputs=["seed"],
        ),
        db_path=db,
    )
    assert new_id is not None

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT expires_at FROM llm_artifacts WHERE id = ?",
            (new_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] is not None  # default TTL applied


def test_upsert_honors_explicit_expires_at_over_default(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _make_db(db)
    explicit = datetime(2030, 1, 1, tzinfo=UTC)
    new_id, _ = upsert(
        UpsertRequest(
            ticker="META",
            purpose="bear_case",
            content_md="content",
            cache_inputs=["seed"],
            expires_at=explicit,
        ),
        db_path=db,
    )
    assert new_id is not None

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT expires_at FROM llm_artifacts WHERE id = ?",
            (new_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == explicit.isoformat()


def test_drain_picks_up_expired_artifacts(tmp_path: Path) -> None:
    """An artifact past its expires_at is treated as drainable even when
    dirty=0. This is the soft-TTL chain that keeps LLM outputs from
    living forever absent an explicit mark_dirty."""
    db = tmp_path / "portfolio.db"
    _make_db(db)
    past = datetime(2024, 1, 1, tzinfo=UTC)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            INSERT INTO llm_artifacts (ticker, scope, purpose, input_sha256,
                                       prompt_version, expires_at, dirty)
            VALUES ('NU', 'ticker', 'bear_case', 'sha', 'v1', ?, 0)
            """,
            (past.isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()

    items = drain_dirty(db_path=db, now=datetime(2026, 5, 26, tzinfo=UTC))
    assert len(items) == 1
    assert items[0].ticker == "NU"
    assert items[0].dirty is False  # picked up via TTL, not flag


def test_drain_skips_unexpired_clean_artifact(tmp_path: Path) -> None:
    """An artifact whose expires_at is still in the future and dirty=0 must
    NOT be in the drain queue."""
    db = tmp_path / "portfolio.db"
    _make_db(db)
    future = datetime(2099, 1, 1, tzinfo=UTC)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            INSERT INTO llm_artifacts (ticker, scope, purpose, input_sha256,
                                       prompt_version, expires_at, dirty)
            VALUES ('AMZN', 'ticker', 'bear_case', 'sha', 'v1', ?, 0)
            """,
            (future.isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()

    items = drain_dirty(db_path=db, now=datetime(2026, 5, 26, tzinfo=UTC))
    assert items == []


def test_drain_still_picks_up_dirty_with_null_expires_at(tmp_path: Path) -> None:
    """Backward-compat: a dirty artifact with no expires_at (legacy row) is
    still drainable."""
    db = tmp_path / "portfolio.db"
    _make_db(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            INSERT INTO llm_artifacts (ticker, scope, purpose, input_sha256,
                                       prompt_version, dirty)
            VALUES ('WIX', 'ticker', 'bear_case', 'sha', 'v1', 1)
            """
        )
        conn.commit()
    finally:
        conn.close()

    items = drain_dirty(db_path=db, now=datetime(2026, 5, 26, tzinfo=UTC))
    assert len(items) == 1
    assert items[0].dirty is True
