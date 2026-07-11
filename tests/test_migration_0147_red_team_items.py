"""Round-trip tests for ``0147_red_team_items`` — the monthly red-team engine's
persisted-item table (monthly_red_team.md Phase 2, PR5).
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

PRIOR_HEAD = "0146_positioning_budgets"
NEW_HEAD = "0147_red_team_items"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def prior_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One DB built to the real pre-0147 head via init_db + alembic, shared
    (read-only) across the module and copied per test."""
    db = tmp_path_factory.mktemp("red_team_items_tmpl") / "at_0146.db"
    import db as dbmod

    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, PRIOR_HEAD)
    return db


@pytest.fixture
def db_at_prior(prior_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "red_team_items.db"
    shutil.copy(prior_template, db)
    return db


def _table_exists(db_path: Path, table: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def test_red_team_items_absent_before_upgrade(db_at_prior: Path) -> None:
    assert not _table_exists(db_at_prior, "red_team_items")


def test_upgrade_creates_red_team_items_table(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    assert _table_exists(db_at_prior, "red_team_items")

    conn = sqlite3.connect(str(db_at_prior))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(red_team_items)")}
        assert cols == {
            "id",
            "run_key",
            "ticker",
            "lens",
            "kind",
            "attack_md",
            "question_md",
            "proposed_change_md",
            "severity",
            "status",
            "defer_count",
            "response_md",
            "responded_at",
            "created_at",
        }
    finally:
        conn.close()


def test_upgrade_is_idempotent_on_a_rerun(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    # A second upgrade pass over an already-upgraded DB (the sa.inspect guard)
    # must not raise "table already exists".
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, NEW_HEAD)
    assert _table_exists(db_at_prior, "red_team_items")


def test_round_trip_insert_and_read(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)

    conn = sqlite3.connect(str(db_at_prior))
    try:
        conn.execute(
            "INSERT INTO red_team_items "
            "(run_key, ticker, lens, kind, attack_md, question_md, "
            " proposed_change_md, severity, status, defer_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "red_team_2026_08",
                "NU",
                "fx_translation",
                "per_name",
                "attack",
                "question?",
                "proposed change",
                "med",
                "open",
                0,
                "2026-08-01T10:00:00",
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT ticker, status, defer_count FROM red_team_items WHERE run_key = ?",
            ("red_team_2026_08",),
        ).fetchone()
        assert row == ("NU", "open", 0)
        # status/defer_count server_default apply on omitted insert too.
        conn.execute(
            "INSERT INTO red_team_items "
            "(run_key, ticker, lens, kind, attack_md, question_md, "
            " proposed_change_md, severity, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "red_team_2026_08",
                None,
                "factor_block",
                "cross_book",
                "attack",
                "question?",
                "proposed change",
                "high",
                "2026-08-01T10:00:01",
            ),
        )
        conn.commit()
        row2 = conn.execute(
            "SELECT ticker, status, defer_count FROM red_team_items WHERE lens = ?",
            ("factor_block",),
        ).fetchone()
        assert row2 == (None, "open", 0)
    finally:
        conn.close()


def test_downgrade_drops_table(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    assert _table_exists(db_at_prior, "red_team_items")
    command.downgrade(cfg, PRIOR_HEAD)
    assert not _table_exists(db_at_prior, "red_team_items")
