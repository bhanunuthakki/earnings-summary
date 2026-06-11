"""An explicit ``--db-path`` must reach EVERY writer, including the ones that
resolve their DB from the ``db.DB_PATH`` global (the LLM call ledger).

Regression for the symptom seen running a trigger/news CLI from a worktree
against the prod DB: alerts/news wrote to the override, but the LLM cost ledger
fell back to ``db.DB_PATH`` (the worktree's empty DB) -> "no such table:
llm_calls". The CLIs now sync the global via ``db.set_db_path``.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import fetch_news  # noqa: E402
import run_triggers  # noqa: E402

import db  # noqa: E402


@pytest.fixture
def restore_db_globals() -> Iterator[None]:
    orig = (db.DB_PATH, db.DATA_DIR, db.FMP_DIR, db.PROJECT_ROOT)
    try:
        yield
    finally:
        db.DB_PATH, db.DATA_DIR, db.FMP_DIR, db.PROJECT_ROOT = orig


def test_set_db_path_repoints_data_globals_but_not_project_root(
    restore_db_globals: None, tmp_path: Path
) -> None:
    project_root_before = db.PROJECT_ROOT
    target = tmp_path / "data" / "portfolio.db"
    db.set_db_path(target)
    assert str(target) == db.DB_PATH
    assert str(tmp_path / "data") == db.DATA_DIR
    assert str(tmp_path / "data" / "historical" / "fmp") == db.FMP_DIR
    # PROJECT_ROOT stays the running checkout (code/holdings resolve from it).
    assert project_root_before == db.PROJECT_ROOT


def test_run_triggers_resolve_syncs_global(restore_db_globals: None, tmp_path: Path) -> None:
    """``resolve_db_path`` with an explicit override syncs ``db.DB_PATH`` so the
    LLM ledger and the alert store agree on one DB."""
    override = tmp_path / "portfolio.db"
    sqlite3.connect(str(override)).close()  # must exist (driver is a write path)
    resolved = run_triggers.resolve_db_path(override)
    assert resolved == override
    assert str(override) == db.DB_PATH


def test_run_triggers_resolve_no_override_leaves_global(restore_db_globals: None) -> None:
    before = db.DB_PATH
    # No override -> resolves from db.DB_PATH; must not mutate it. The default DB
    # may not exist in a worktree, so tolerate the FileNotFoundError guard.
    with contextlib.suppress(FileNotFoundError):
        run_triggers.resolve_db_path(None)
    assert before == db.DB_PATH


def test_fetch_news_main_syncs_global(
    restore_db_globals: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fetch_news`` syncs the global before the WebSearch+Opus path runs, so its
    LLM ledger writes to the override DB."""
    override = tmp_path / "portfolio.db"
    sqlite3.connect(str(override)).close()
    captured: dict[str, str] = {}

    def _fake_run(
        tickers: list[str], *, source: str, db_path: str, days: int, limit: int, **_: object
    ) -> int:
        captured["db_path"] = db_path
        captured["global"] = db.DB_PATH
        return 0

    monkeypatch.setattr(fetch_news, "run", _fake_run)
    rc = fetch_news.main(["--source", "fmp", "--tickers", "NU", "--db-path", str(override)])
    assert rc == 0
    assert str(override) == captured["db_path"]
    assert str(override) == captured["global"]  # synced before run()
