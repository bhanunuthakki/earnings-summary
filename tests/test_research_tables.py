"""Phase-1 W1-1: the research_tasks / research_proposals / research_hot_flags spine."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"
_TABLES = ("research_tasks", "research_proposals", "research_hot_flags")


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "ledger.db", stamp=PRIOR_HEAD)


def _tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_research_tables_exist(db_path: Path) -> None:
    names = _tables(db_path)
    for table in _TABLES:
        assert table in names


def test_no_foreign_keys(db_path: Path) -> None:
    # code-level RI only — an FK to a table absent in a partial fixture schema
    # poisons inserts under PRAGMA foreign_keys=ON (the 0118 posture).
    conn = sqlite3.connect(str(db_path))
    try:
        for table in _TABLES:
            assert conn.execute(f"PRAGMA foreign_key_list({table})").fetchall() == []
    finally:
        conn.close()


def test_downgrade_to_0119_drops_them(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(
        tmp_path / "research-archived.db",
        stamp=PRIOR_HEAD,
        archived=True,
    )
    command.downgrade(_cfg(db_path), "0119_ledger_synthesis_budget")
    names = _tables(db_path)
    for table in _TABLES:
        assert table not in names


def test_research_tasks_legacy_cost_and_metadata_storage_columns_present(db_path: Path) -> None:
    """B7 repurposes these two columns (dead since 0120 — never read/written
    by any code path until this PR) as the triage's stated cost estimate and
    a JSON meta blob (session_prompt/packeted_at/unanswered_weeks) — see
    research.proposals' module docstring. This locks the columns' presence
    and NULL-ability so a future migration can't silently drop them."""
    conn = sqlite3.connect(str(db_path))
    try:
        cols = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(research_tasks)")}
    finally:
        conn.close()
    assert "cost_usd" in cols and cols["cost_usd"] == 0  # nullable
    assert "run_id" in cols and cols["run_id"] == 0  # nullable


def test_status_and_provenance_defaults(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO research_tasks (claim, created_at, updated_at) VALUES ('x', '2026-06-29', '2026-06-29')"
        )
        conn.execute(
            "INSERT INTO research_proposals (kind, title, created_at, updated_at) "
            "VALUES ('memo', 't', '2026-06-29', '2026-06-29')"
        )
        conn.commit()
        assert conn.execute("SELECT status FROM research_tasks").fetchone()[0] == "proposed"
        status, provenance = conn.execute(
            "SELECT status, provenance FROM research_proposals"
        ).fetchone()
        assert status == "pending"
        assert provenance == "derived"
    finally:
        conn.close()
