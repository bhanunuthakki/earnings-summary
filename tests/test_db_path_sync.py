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
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import analyze_filing_intelligence  # noqa: E402
import extract_commitments_from_transcript  # noqa: E402
import fetch_news  # noqa: E402
import pressure_test_thesis  # noqa: E402
import process_report_comments  # noqa: E402
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


def test_extract_commitments_main_syncs_global(
    restore_db_globals: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``extract_commitments_from_transcript`` syncs the global at the top of
    main() so the ``--auto`` path's ``saydo_commitment_extract`` cost rows write
    to the explicit --db, not the process-default portfolio.db.

    Exercised through ``--list-pending`` (no LLM call, so no real cost): the sync
    is unconditional and precedes the mode branch, so capturing ``db.DB_PATH``
    here proves the state the governed ``call_llm`` in ``--auto`` would see."""
    override = tmp_path / "portfolio.db"
    sqlite3.connect(str(override)).close()  # open_db is a real connect
    captured: dict[str, str] = {}

    def _fake_list_pending(conn: object, ticker: str | None) -> list[dict[str, object]]:
        captured["global"] = db.DB_PATH
        return []

    monkeypatch.setattr(extract_commitments_from_transcript, "_list_pending", _fake_list_pending)
    rc = extract_commitments_from_transcript.main(["--list-pending", "--db", str(override)])
    assert rc == 0
    assert str(override) == captured["global"]  # synced before the DB/LLM work
    assert str(override) == db.DB_PATH


# --- --repo-root family: same ledger sync, keyed off <repo-root>/data/portfolio.db ---
#
# These CLIs take --repo-root (not --db-path) and make governed LLM calls, but
# historically never synced db.DB_PATH — so a run against another checkout's
# data/ (e.g. --repo-root <MAIN> from a worktree) misdirected the llm_calls cost
# rows to the process-default DB. Each now calls db.set_db_path at the top of
# main(). Four sibling --repo-root scripts (extract_risk_factors / _footnotes /
# _exec_comp, canonicalize_segments) already synced the globals manually.


def test_analyze_filing_intelligence_main_syncs_global(
    restore_db_globals: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synced from --repo-root before the governed ``strategic_analysis`` call
    (short-circuited here via a skipped result, which returns 0 after sync)."""
    captured: dict[str, str] = {}

    def _fake_analyze(
        ticker: str, repo_root: Path, *, fiscal_year: int | None = None, refresh: bool = False
    ) -> object:
        captured["global"] = db.DB_PATH
        return SimpleNamespace(skipped_reason="test-short-circuit")

    monkeypatch.setattr(analyze_filing_intelligence, "analyze_for_ticker", _fake_analyze)
    rc = analyze_filing_intelligence.main(["--ticker", "NU", "--repo-root", str(tmp_path)])
    assert rc == 0
    expected = str(tmp_path / "data" / "portfolio.db")
    assert expected == captured["global"]  # synced before the governed call
    assert expected == db.DB_PATH


def test_pressure_test_thesis_main_syncs_global(
    restore_db_globals: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synced from --repo-root before the governed ``pressure_test_thesis`` call
    (short-circuited here via a missing thesis, which returns 1 after sync)."""
    captured: dict[str, str] = {}

    def _fake_resolve(cli_thesis: str | None, repo_root: Path, ticker: str) -> str | None:
        captured["global"] = db.DB_PATH
        return None  # no thesis -> main returns 1, after the sync

    monkeypatch.setattr(pressure_test_thesis, "_resolve_thesis", _fake_resolve)
    rc = pressure_test_thesis.main(["--ticker", "NU", "--repo-root", str(tmp_path)])
    assert rc == 1
    expected = str((tmp_path / "data" / "portfolio.db").resolve())
    assert expected == captured["global"]  # synced before the governed call
    assert expected == db.DB_PATH


def test_process_report_comments_main_syncs_global(
    restore_db_globals: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synced from --repo-root before the per-comment governed routers run
    (short-circuited here via no report on disk, which returns 0 after sync)."""
    captured: dict[str, str] = {}

    def _fake_resolve_latest(repo_root: Path, ticker: str) -> object:
        captured["global"] = db.DB_PATH
        return None  # no report -> ticker skipped, main returns 0

    monkeypatch.setattr(
        process_report_comments, "_resolve_latest_report_date", _fake_resolve_latest
    )
    rc = process_report_comments.main(["--ticker", "NU", "--repo-root", str(tmp_path)])
    assert rc == 0
    expected = str((tmp_path / "data" / "portfolio.db").resolve())
    assert expected == captured["global"]  # synced before the governed routers
    assert expected == db.DB_PATH
