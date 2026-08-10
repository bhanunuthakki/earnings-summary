"""Tests for the DCF-eligible ticker universe (``src/dcf/universe.py``) and its
two batch-driver consumers — the guarantee that evaluation-list names are
first-class DCF citizens alongside portfolio names.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import build_all_redesigned_dcf  # noqa: E402
import refresh_dcf  # noqa: E402

from dcf.universe import dcf_universe  # noqa: E402


def _make_repo(tmp_path: Path, rows: list[tuple[str, str] | tuple[str, str, str]]) -> Path:
    """A repo_root whose ``data/portfolio.db`` is seeded with (ticker, list_type) rows."""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(tmp_path / "data" / "portfolio.db")
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY,
            user_id INTEGER DEFAULT 1,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            list_type TEXT NOT NULL,
            archived_at TIMESTAMP,
            UNIQUE(user_id, ticker)
        );
        """
    )
    for row in rows:
        ticker, list_type = row[:2]
        archived_at = row[2] if len(row) == 3 else None
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type, archived_at) VALUES (?, ?, ?, ?)",
            (ticker, ticker, list_type, archived_at),
        )
    conn.commit()
    conn.close()
    return tmp_path


def test_universe_includes_portfolio_and_evaluation(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, [("NU", "portfolio"), ("UBER", "evaluation"), ("V", "evaluation")])
    assert dcf_universe(repo) == ["NU", "UBER", "V"]


def test_universe_excludes_non_briefed_lists(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        [("NU", "portfolio"), ("ABC", "watchlist"), ("SPY", "etf"), ("XYZ", "none")],
    )
    assert dcf_universe(repo) == ["NU"]


def test_universe_excludes_archived_briefed_names(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        [("NU", "portfolio"), ("DASH", "evaluation", "2026-08-09T12:00:00")],
    )
    assert dcf_universe(repo) == ["NU"]


def test_universe_uppercases_and_dedupes(tmp_path: Path) -> None:
    # UNIQUE(user_id, ticker) is case-sensitive in SQLite, so 'uber'/'UBER' both
    # insert; UPPER() + the set collapse them to a single canonical ticker.
    repo = _make_repo(tmp_path, [("uber", "evaluation"), ("UBER", "evaluation")])
    assert dcf_universe(repo) == ["UBER"]


def test_universe_empty_without_db(tmp_path: Path) -> None:
    assert dcf_universe(tmp_path) == []


def test_build_all_default_tickers_ignores_orphan_workbooks(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, [("NU", "portfolio"), ("UBER", "evaluation")])
    (repo / "dcf").mkdir(parents=True, exist_ok=True)
    (repo / "dcf" / "AMZN.xlsx").write_bytes(b"")  # legacy workbook, not tracked in the DB
    (repo / "dcf" / "_helper.xlsx").write_bytes(b"")  # helper file — skipped
    (repo / "dcf" / "META_redesign.xlsx").write_bytes(b"")  # sample file — skipped
    assert build_all_redesigned_dcf.default_tickers(repo) == ["NU", "UBER"]


def test_refresh_all_named_includes_eval_without_seeded_wacc(tmp_path: Path) -> None:
    # The redesigned path computes WACC, so an eval name with no holdings JSON and
    # no seeded `wacc` must still land in the --all-named set.
    repo = _make_repo(tmp_path, [("NU", "portfolio"), ("UBER", "evaluation")])
    assert refresh_dcf.dcf_maintained_universe(repo) == ["NU", "UBER"]


def test_refresh_all_named_ignores_legacy_wacc_holdings(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, [("UBER", "evaluation")])
    holdings = repo / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True, exist_ok=True)
    # META is not in tracked_companies but carries a legacy seeded wacc — the union
    # keeps it so the change is non-regressive for hand-seeded names.
    (holdings / "META.json").write_text('{"wacc": 0.09}', encoding="utf-8")
    assert refresh_dcf.dcf_maintained_universe(repo) == ["UBER"]
