"""Tests for the fact-change → LLM artifact dirty chain.

`mark_artifacts_dirty_for_fact_change` is the canonical hook called from
(a) the daily worker before each rebuild and (b) the SEC silent-staleness
detector. It wraps `mark_dirty` with the FACT_DEPENDENT_PURPOSES tuple so
every call site agrees on which artifacts to invalidate when upstream
facts move.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm_artifact_store import (  # noqa: E402
    FACT_DEPENDENT_PURPOSES,
    mark_artifacts_dirty_for_fact_change,
)


def _build_artifacts_schema(db_path: Path) -> sqlite3.Connection:
    """Minimal llm_artifacts schema sufficient for mark_dirty."""
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
    return conn


def _seed_artifact(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    purpose: str,
    superseded: bool = False,
) -> int:
    cur = conn.execute(
        "INSERT INTO llm_artifacts "
        "(ticker, scope, purpose, input_sha256, prompt_version, superseded_by_id) "
        "VALUES (?, 'ticker', ?, ?, 'v1', ?)",
        (ticker, purpose, f"sha-{ticker}-{purpose}", 99 if superseded else None),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def test_marks_all_fact_dependent_purposes_dirty(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    conn = _build_artifacts_schema(db)
    try:
        for purpose in FACT_DEPENDENT_PURPOSES:
            _seed_artifact(conn, ticker="META", purpose=purpose)
    finally:
        conn.close()

    flipped = mark_artifacts_dirty_for_fact_change(
        ticker="META", reason="brief_rebuild", db_path=db
    )
    assert flipped == len(FACT_DEPENDENT_PURPOSES)

    conn = sqlite3.connect(str(db))
    try:
        dirty_count = conn.execute(
            "SELECT COUNT(*) FROM llm_artifacts WHERE ticker = 'META' AND dirty = 1"
        ).fetchone()[0]
        reasons = {
            r[0]
            for r in conn.execute(
                "SELECT dirty_reason FROM llm_artifacts WHERE dirty = 1"
            ).fetchall()
        }
    finally:
        conn.close()
    assert dirty_count == len(FACT_DEPENDENT_PURPOSES)
    assert reasons == {"brief_rebuild"}


def test_does_not_touch_other_tickers(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    conn = _build_artifacts_schema(db)
    try:
        _seed_artifact(conn, ticker="META", purpose="bear_case")
        _seed_artifact(conn, ticker="GOOG", purpose="bear_case")
    finally:
        conn.close()

    flipped = mark_artifacts_dirty_for_fact_change(
        ticker="META", reason="rebuild", db_path=db
    )
    assert flipped == 1

    conn = sqlite3.connect(str(db))
    try:
        goog_dirty = conn.execute(
            "SELECT dirty FROM llm_artifacts WHERE ticker = 'GOOG'"
        ).fetchone()
    finally:
        conn.close()
    assert goog_dirty[0] == 0


def test_skips_superseded_rows(tmp_path: Path) -> None:
    """Already-superseded artifacts must not be touched — they're history,
    not the current cache."""
    db = tmp_path / "portfolio.db"
    conn = _build_artifacts_schema(db)
    try:
        _seed_artifact(conn, ticker="NU", purpose="bear_case", superseded=True)
        _seed_artifact(conn, ticker="NU", purpose="bear_case")
    finally:
        conn.close()

    flipped = mark_artifacts_dirty_for_fact_change(
        ticker="NU", reason="rebuild", db_path=db
    )
    # Only the live (non-superseded) row should flip.
    assert flipped == 1


def test_skips_already_dirty(tmp_path: Path) -> None:
    """If the artifact is already dirty, mark_dirty should not double-flip
    (idempotent)."""
    db = tmp_path / "portfolio.db"
    conn = _build_artifacts_schema(db)
    try:
        rid = _seed_artifact(conn, ticker="AMZN", purpose="bear_case")
        conn.execute(
            "UPDATE llm_artifacts SET dirty = 1, dirty_reason = 'prior' WHERE id = ?",
            (rid,),
        )
        conn.commit()
    finally:
        conn.close()

    flipped = mark_artifacts_dirty_for_fact_change(
        ticker="AMZN", reason="rebuild", db_path=db
    )
    assert flipped == 0  # already dirty


def test_returns_zero_on_missing_table(tmp_path: Path) -> None:
    """Synthetic env without migration 0035 — must not crash."""
    db = tmp_path / "portfolio.db"
    sqlite3.connect(str(db)).close()  # create empty DB
    flipped = mark_artifacts_dirty_for_fact_change(
        ticker="META", reason="rebuild", db_path=db
    )
    assert flipped == 0
