"""Unit tests for daily_fetch_and_brief's material-change + cadence gates.

Covers the B+C gating logic added on top of the brief_dirty queue.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from daily_fetch_and_brief import _check_skip_gates, _compute_brief_hash  # noqa: E402


def _bootstrap_min_schema(db_path: Path) -> None:
    """Create just the tables / columns the gate logic queries."""
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
            brief_dirty BOOLEAN DEFAULT 0,
            last_built_at TIMESTAMP,
            last_brief_hash TEXT,
            UNIQUE(user_id, ticker)
        );
        CREATE TABLE financial_facts (ticker TEXT, period_end TEXT, line_item TEXT);
        CREATE TABLE kpi_facts (ticker TEXT, period_end TEXT, kpi_name TEXT);
        CREATE TABLE transcripts (ticker TEXT, period_end TEXT);
        CREATE TABLE management_commitments (ticker TEXT, period_made TEXT);
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """Set up a sandboxed repo_root with min schema + holdings dir."""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "micro_thesis" / "holdings").mkdir(parents=True, exist_ok=True)
    _bootstrap_min_schema(tmp_path / "data" / "portfolio.db")
    return tmp_path


def _seed(repo_root: Path, ticker: str, list_type: str, **extras) -> None:
    conn = sqlite3.connect(str(repo_root / "data" / "portfolio.db"))
    cols = "user_id, ticker, name, list_type"
    placeholders = "?, ?, ?, ?"
    vals: list[object] = [1, ticker, f"{ticker} Co", list_type]
    for k, v in extras.items():
        cols += f", {k}"
        placeholders += ", ?"
        vals.append(v)
    conn.execute(f"INSERT INTO tracked_companies ({cols}) VALUES ({placeholders})", vals)
    conn.commit()
    conn.close()


def test_compute_brief_hash_is_stable_for_empty_data(repo_root: Path) -> None:
    """Two calls with no DB rows changing produce the same hash."""
    _seed(repo_root, "GOOG", "portfolio")
    conn = sqlite3.connect(str(repo_root / "data" / "portfolio.db"))
    try:
        h1 = _compute_brief_hash(conn, "GOOG", repo_root)
        h2 = _compute_brief_hash(conn, "GOOG", repo_root)
        assert h1 == h2
    finally:
        conn.close()


def test_compute_brief_hash_changes_when_facts_change(repo_root: Path) -> None:
    """Adding a financial_fact row flips the hash."""
    _seed(repo_root, "GOOG", "portfolio")
    conn = sqlite3.connect(str(repo_root / "data" / "portfolio.db"))
    try:
        h1 = _compute_brief_hash(conn, "GOOG", repo_root)
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, line_item) VALUES (?, ?, ?)",
            ("GOOG", "2026-03-31", "revenue"),
        )
        conn.commit()
        h2 = _compute_brief_hash(conn, "GOOG", repo_root)
        assert h1 != h2
    finally:
        conn.close()


def test_compute_brief_hash_changes_when_holdings_json_edited(repo_root: Path) -> None:
    """User edits to micro_thesis/holdings/<T>.json (thesis tweak) trip the hash."""
    _seed(repo_root, "GOOG", "portfolio")
    holdings = repo_root / "micro_thesis" / "holdings" / "GOOG.json"
    holdings.write_text('{"name": "Alphabet"}', encoding="utf-8")
    conn = sqlite3.connect(str(repo_root / "data" / "portfolio.db"))
    try:
        h1 = _compute_brief_hash(conn, "GOOG", repo_root)
        holdings.write_text('{"name": "Alphabet", "thesis": "new"}', encoding="utf-8")
        h2 = _compute_brief_hash(conn, "GOOG", repo_root)
        assert h1 != h2
    finally:
        conn.close()


def test_gate_no_material_change_skips_recent_build(repo_root: Path) -> None:
    """Portfolio ticker with matching hash + recent build → skip."""
    now = datetime.now()
    recent = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(repo_root / "data" / "portfolio.db"))
    conn.row_factory = sqlite3.Row
    try:
        _seed(repo_root, "GOOG", "portfolio")
        current_hash = _compute_brief_hash(conn, "GOOG", repo_root)
        conn.execute(
            "UPDATE tracked_companies SET last_built_at = ?, last_brief_hash = ? WHERE ticker = 'GOOG'",
            (recent, current_hash),
        )
        conn.commit()
        skip, reason, _ = _check_skip_gates(
            conn, "GOOG", repo_root,
            force=False, eval_cadence_days=7, no_change_ttl_days=7,
        )
        assert skip
        assert reason is not None and "no_material_change" in reason
    finally:
        conn.close()


def test_gate_force_bypasses_skip(repo_root: Path) -> None:
    """--force flag rebuilds even with recent + matching hash."""
    now = datetime.now()
    recent = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(repo_root / "data" / "portfolio.db"))
    conn.row_factory = sqlite3.Row
    try:
        _seed(repo_root, "GOOG", "portfolio")
        current_hash = _compute_brief_hash(conn, "GOOG", repo_root)
        conn.execute(
            "UPDATE tracked_companies SET last_built_at = ?, last_brief_hash = ? WHERE ticker = 'GOOG'",
            (recent, current_hash),
        )
        conn.commit()
        skip, _reason, _ = _check_skip_gates(
            conn, "GOOG", repo_root,
            force=True, eval_cadence_days=7, no_change_ttl_days=7,
        )
        assert not skip
    finally:
        conn.close()


def test_gate_evaluation_cadence_skips_when_recent(repo_root: Path) -> None:
    """Evaluation ticker built 2 days ago → skip even if hash differs (cadence wins)."""
    two_days_ago = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(repo_root / "data" / "portfolio.db"))
    conn.row_factory = sqlite3.Row
    try:
        _seed(repo_root, "ABNB", "evaluation",
              last_built_at=two_days_ago, last_brief_hash="stale_hash")
        skip, reason, _ = _check_skip_gates(
            conn, "ABNB", repo_root,
            force=False, eval_cadence_days=7, no_change_ttl_days=7,
        )
        assert skip
        assert reason is not None and "evaluation_cadence" in reason
    finally:
        conn.close()


def test_gate_evaluation_cadence_does_not_skip_when_old(repo_root: Path) -> None:
    """Evaluation ticker built 10 days ago → rebuild (past cadence)."""
    ten_days_ago = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(repo_root / "data" / "portfolio.db"))
    conn.row_factory = sqlite3.Row
    try:
        _seed(repo_root, "ABNB", "evaluation",
              last_built_at=ten_days_ago, last_brief_hash="stale_hash")
        # Hash will differ from "stale_hash" because we compute it fresh
        skip, _reason, _ = _check_skip_gates(
            conn, "ABNB", repo_root,
            force=False, eval_cadence_days=7, no_change_ttl_days=7,
        )
        assert not skip
    finally:
        conn.close()


def test_gate_portfolio_with_no_history_does_not_skip(repo_root: Path) -> None:
    """Portfolio ticker that's never been built → always rebuild."""
    conn = sqlite3.connect(str(repo_root / "data" / "portfolio.db"))
    conn.row_factory = sqlite3.Row
    try:
        _seed(repo_root, "GOOG", "portfolio")
        skip, _reason, _ = _check_skip_gates(
            conn, "GOOG", repo_root,
            force=False, eval_cadence_days=7, no_change_ttl_days=7,
        )
        assert not skip
    finally:
        conn.close()
