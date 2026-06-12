"""Unit tests for src/pipeline/tier_runner.py — tier-aware due-list math.

Covers the daily/weekly/monthly cadence rules across P1/P2/P3 tiers and the
lens-regen variant that joins through llm_artifacts. Uses a sandboxed
portfolio.db built per-test so suites don't share state.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.tier_runner import (  # noqa: E402
    tickers_due_for_lens_regen,
    tickers_due_for_refresh,
    tier_coverage_summary,
)


def _bootstrap_schema(db_path: Path, *, with_artifacts: bool = True) -> None:
    """Create the minimal schema slice tier_runner reads against."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER DEFAULT 1,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            list_type TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            archived_at TIMESTAMP,
            last_built_at TIMESTAMP,
            processing_tier TEXT NOT NULL DEFAULT 'P3',
            UNIQUE(user_id, ticker)
        );
        """
    )
    if with_artifacts:
        conn.executescript(
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
                generated_at TIMESTAMP NOT NULL,
                expires_at TIMESTAMP,
                superseded_by_id INTEGER,
                dirty INTEGER NOT NULL DEFAULT 0,
                dirty_reason TEXT,
                source_doc_ids TEXT,
                parent_artifact_ids TEXT,
                llm_call_id INTEGER
            );
            """
        )
    conn.commit()
    conn.close()


def _seed_ticker(
    db_path: Path,
    ticker: str,
    tier: str,
    *,
    last_built_at: str | None = None,
    list_type: str = "portfolio",
    archived_at: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO tracked_companies
            (user_id, ticker, name, list_type, processing_tier, last_built_at, archived_at)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        """,
        (ticker, f"{ticker} Co", list_type, tier, last_built_at, archived_at),
    )
    conn.commit()
    conn.close()


def _seed_lens_artifact(
    db_path: Path,
    ticker: str,
    lens_name: str,
    *,
    generated_at: str,
    superseded: bool = False,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO llm_artifacts
            (ticker, scope, purpose, input_sha256, output_sha256, prompt_version,
             generated_at, superseded_by_id)
        VALUES (?, 'ticker', ?, 'sha', 'sha', 'v1', ?, ?)
        """,
        (
            ticker,
            f"lens:{lens_name}",
            generated_at,
            1 if superseded else None,
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """Sandboxed repo root with the minimal data/portfolio.db schema."""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    _bootstrap_schema(tmp_path / "data" / "portfolio.db")
    return tmp_path


@pytest.fixture()
def now() -> datetime:
    return datetime(2026, 5, 25, 12, 0, 0)


# --- tickers_due_for_refresh: tier × cadence matrix --------------------------


def test_p1_always_due_on_daily_cadence(repo_root: Path, now: datetime) -> None:
    """P1 fires every daily tick regardless of how recently it was built."""
    fresh = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    _seed_ticker(repo_root / "data" / "portfolio.db", "GOOG", "P1", last_built_at=fresh)
    due = tickers_due_for_refresh(repo_root, "daily", now=now)
    assert "GOOG" in due


def test_p2_skipped_on_daily_when_fresh(repo_root: Path, now: datetime) -> None:
    """P2 with a 3-day-old build is still within the 7d daily-skip window."""
    three_days = (now - timedelta(days=3)).isoformat(timespec="seconds")
    _seed_ticker(
        repo_root / "data" / "portfolio.db",
        "ABNB",
        "P2",
        last_built_at=three_days,
        list_type="watchlist",
    )
    assert "ABNB" not in tickers_due_for_refresh(repo_root, "daily", now=now)


def test_p2_due_on_daily_when_stale(repo_root: Path, now: datetime) -> None:
    """P2 last built 10 days ago — past the 7-day daily threshold → due."""
    ten_days = (now - timedelta(days=10)).isoformat(timespec="seconds")
    _seed_ticker(
        repo_root / "data" / "portfolio.db",
        "ABNB",
        "P2",
        last_built_at=ten_days,
        list_type="watchlist",
    )
    assert "ABNB" in tickers_due_for_refresh(repo_root, "daily", now=now)


def test_p3_skipped_on_daily_when_within_30d(repo_root: Path, now: datetime) -> None:
    """P3 with 20-day-old build → within 30d daily threshold → skipped."""
    twenty_days = (now - timedelta(days=20)).isoformat(timespec="seconds")
    _seed_ticker(
        repo_root / "data" / "portfolio.db",
        "MSFT",
        "P3",
        last_built_at=twenty_days,
        list_type="index_member",
    )
    assert "MSFT" not in tickers_due_for_refresh(repo_root, "daily", now=now)


def test_p3_due_on_daily_when_over_30d(repo_root: Path, now: datetime) -> None:
    """P3 with 40-day-old build → past 30d threshold → due."""
    forty_days = (now - timedelta(days=40)).isoformat(timespec="seconds")
    _seed_ticker(
        repo_root / "data" / "portfolio.db",
        "MSFT",
        "P3",
        last_built_at=forty_days,
        list_type="index_member",
    )
    assert "MSFT" in tickers_due_for_refresh(repo_root, "daily", now=now)


def test_never_built_ticker_always_due(repo_root: Path, now: datetime) -> None:
    """A ticker with last_built_at NULL is due regardless of tier."""
    _seed_ticker(repo_root / "data" / "portfolio.db", "NEW1", "P3", last_built_at=None)
    _seed_ticker(repo_root / "data" / "portfolio.db", "NEW2", "P2", last_built_at=None)
    due = tickers_due_for_refresh(repo_root, "daily", now=now)
    assert {"NEW1", "NEW2"}.issubset(due)


def test_archived_excluded(repo_root: Path, now: datetime) -> None:
    """Archived tickers are excluded even if they would otherwise be due."""
    archived_at = "2026-04-01"
    _seed_ticker(
        repo_root / "data" / "portfolio.db",
        "OLDIE",
        "P1",
        last_built_at=None,
        archived_at=archived_at,
    )
    assert "OLDIE" not in tickers_due_for_refresh(repo_root, "daily", now=now)


def test_weekly_cadence_p2_threshold_30d(repo_root: Path, now: datetime) -> None:
    """Weekly cron tick: P2 threshold is 30d (monthly catch-up), not 7d."""
    fifteen_days = (now - timedelta(days=15)).isoformat(timespec="seconds")
    _seed_ticker(
        repo_root / "data" / "portfolio.db",
        "ABNB",
        "P2",
        last_built_at=fifteen_days,
    )
    # Within 30d → skipped on weekly cadence (even though 15d > daily's 7d).
    assert "ABNB" not in tickers_due_for_refresh(repo_root, "weekly", now=now)


def test_weekly_cadence_p1_threshold_7d(repo_root: Path, now: datetime) -> None:
    """P1 on weekly cadence: due iff > 7d. Mirror of the daily P2 rule."""
    fresh = (now - timedelta(days=3)).isoformat(timespec="seconds")
    stale = (now - timedelta(days=10)).isoformat(timespec="seconds")
    _seed_ticker(repo_root / "data" / "portfolio.db", "GOOG", "P1", last_built_at=fresh)
    _seed_ticker(repo_root / "data" / "portfolio.db", "META", "P1", last_built_at=stale)
    due = tickers_due_for_refresh(repo_root, "weekly", now=now)
    assert "GOOG" not in due
    assert "META" in due


def test_monthly_cadence_p3_threshold_365d(repo_root: Path, now: datetime) -> None:
    """Monthly cron: P3 threshold 365d. A 100-day-old P3 is NOT due."""
    hundred_days = (now - timedelta(days=100)).isoformat(timespec="seconds")
    _seed_ticker(
        repo_root / "data" / "portfolio.db",
        "MSFT",
        "P3",
        last_built_at=hundred_days,
    )
    assert "MSFT" not in tickers_due_for_refresh(repo_root, "monthly", now=now)


# --- tickers_due_for_lens_regen: joins llm_artifacts -------------------------


def test_lens_regen_due_when_no_artifact(repo_root: Path, now: datetime) -> None:
    """A tracked ticker with no cached lens artifact is always due."""
    fresh = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    _seed_ticker(repo_root / "data" / "portfolio.db", "META", "P1", last_built_at=fresh)
    due = tickers_due_for_lens_regen(repo_root, "five_min_reread", "daily", now=now)
    assert "META" in due


def test_lens_regen_skipped_when_recent(repo_root: Path, now: datetime) -> None:
    """P2 ticker with a fresh lens (3d old) → skipped on weekly cadence
    (P2 weekly threshold is 30d)."""
    fresh = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    _seed_ticker(
        repo_root / "data" / "portfolio.db",
        "ABNB",
        "P2",
        last_built_at=fresh,
    )
    three_days_ago = (now - timedelta(days=3)).isoformat(timespec="seconds")
    _seed_lens_artifact(
        repo_root / "data" / "portfolio.db",
        "ABNB",
        "five_min_reread",
        generated_at=three_days_ago,
    )
    due = tickers_due_for_lens_regen(repo_root, "five_min_reread", "weekly", now=now)
    assert "ABNB" not in due


def test_lens_regen_ignores_superseded(repo_root: Path, now: datetime) -> None:
    """Superseded artifact rows are ignored (the current row decides freshness)."""
    fresh = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    _seed_ticker(
        repo_root / "data" / "portfolio.db",
        "TICK",
        "P2",
        last_built_at=fresh,
        list_type="watchlist",
    )
    very_old = (now - timedelta(days=60)).isoformat(timespec="seconds")
    recent = (now - timedelta(days=2)).isoformat(timespec="seconds")
    # Superseded old row + current fresh row → should be skipped on daily (P2 cadence 7d)
    _seed_lens_artifact(
        repo_root / "data" / "portfolio.db",
        "TICK",
        "five_min_reread",
        generated_at=very_old,
        superseded=True,
    )
    _seed_lens_artifact(
        repo_root / "data" / "portfolio.db",
        "TICK",
        "five_min_reread",
        generated_at=recent,
    )
    due = tickers_due_for_lens_regen(repo_root, "five_min_reread", "daily", now=now)
    assert "TICK" not in due


# --- tier_coverage_summary ---------------------------------------------------


def test_coverage_counts_tiers_and_freshness(repo_root: Path, now: datetime) -> None:
    """Mixed-tier portfolio: counts roll up correctly into the dashboard strip."""
    db = repo_root / "data" / "portfolio.db"
    # Two P1, one fresh today (< 24h) one stale (built yesterday)
    today = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    yesterday = (now - timedelta(days=2)).isoformat(timespec="seconds")
    _seed_ticker(db, "GOOG", "P1", last_built_at=today)
    _seed_ticker(db, "META", "P1", last_built_at=yesterday)
    # One P2 fresh, one P2 stale
    _seed_ticker(db, "ABNB", "P2", last_built_at=today, list_type="watchlist")
    eight_days_ago = (now - timedelta(days=8)).isoformat(timespec="seconds")
    _seed_ticker(db, "DDOG", "P2", last_built_at=eight_days_ago, list_type="watchlist")
    # One P3 fresh
    _seed_ticker(db, "VOO", "P3", last_built_at=today, list_type="index_member")

    cov = tier_coverage_summary(repo_root, now=now)
    assert cov["P1"]["total"] == 2
    assert cov["P1"]["fresh"] == 1
    assert cov["P1"]["stale"] == 1
    assert cov["P2"]["total"] == 2
    assert cov["P2"]["fresh"] == 1
    assert cov["P2"]["stale"] == 1
    assert cov["P3"]["total"] == 1
    assert cov["P3"]["fresh"] == 1


def test_coverage_handles_missing_db(tmp_path: Path) -> None:
    """No DB file → zero-counts dict (dashboard renders nothing)."""
    cov = tier_coverage_summary(tmp_path)
    assert cov == {
        "P1": {"fresh": 0, "stale": 0, "total": 0},
        "P2": {"fresh": 0, "stale": 0, "total": 0},
        "P3": {"fresh": 0, "stale": 0, "total": 0},
    }


def test_missing_processing_tier_column_returns_empty(tmp_path: Path) -> None:
    """If the migration hasn't applied yet, helpers return empty lists rather
    than crashing — important for fresh-clone setups."""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            list_type TEXT NOT NULL,
            archived_at TIMESTAMP,
            last_built_at TIMESTAMP
        );
        """
    )
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type) VALUES (?, ?, ?)",
        ("GOOG", "Alphabet", "portfolio"),
    )
    conn.commit()
    conn.close()
    assert tickers_due_for_refresh(tmp_path, "daily") == []
    assert tickers_due_for_lens_regen(tmp_path, "five_min_reread", "daily") == []


def test_idempotency_repeated_call(repo_root: Path, now: datetime) -> None:
    """Calling tickers_due_for_refresh twice with no DB change is stable
    and does not mutate state."""
    fresh = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    _seed_ticker(repo_root / "data" / "portfolio.db", "GOOG", "P1", last_built_at=fresh)
    first = tickers_due_for_refresh(repo_root, "daily", now=now)
    second = tickers_due_for_refresh(repo_root, "daily", now=now)
    assert first == second == ["GOOG"]
