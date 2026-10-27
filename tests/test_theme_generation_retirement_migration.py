"""Retired theme configuration leaves retained purposes and user state intact."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

PARENT = "0066_merge_retirement_main"
HEAD = "0067_retire_theme_generation"
TABLES = ("llm_budgets", "llm_budget_alerts", "model_eval_verdicts")


def _seed(conn: sqlite3.Connection) -> None:
    for purpose in ("theme_seed_cluster", "theme_synthesis", "transcript_summary"):
        conn.execute(
            "INSERT INTO llm_budget_alerts "
            "(purpose,month,threshold_pct,alerted_at,spend_at_alert_usd) "
            "VALUES (?,'2026-10',0.8,'2026-10-27',1)",
            (purpose,),
        )
        conn.execute(
            "INSERT INTO model_eval_verdicts "
            "(purpose,candidate,incumbent,verdict,run_id,recorded_at) "
            "VALUES (?,'fixture','fixture','pass','fixture-run','2026-10-27')",
            (purpose,),
        )


def test_theme_configuration_retirement_is_exact(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "themes.db", target=PARENT)
    with sqlite3.connect(database) as conn:
        _seed(conn)
        retained = {
            table: conn.execute(
                f"SELECT * FROM {table} WHERE purpose NOT IN ('theme_seed_cluster','theme_synthesis') ORDER BY id"
            ).fetchall()
            for table in TABLES
        }
    migrated_db(database, upgrade_existing=True, target=HEAD)
    with sqlite3.connect(database) as conn:
        for table in TABLES:
            assert conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == retained[table]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("extension", ["foreign_key", "trigger"])
def test_unknown_theme_configuration_dependency_stops_before_mutation(
    tmp_path: Path, migrated_db: Callable[..., Path], extension: str
) -> None:
    database = migrated_db(tmp_path / "themes.db", target=PARENT)
    with sqlite3.connect(database) as conn:
        _seed(conn)
        if extension == "foreign_key":
            conn.execute(
                "CREATE TABLE unknown_consumer (budget_id INTEGER REFERENCES llm_budgets(id))"
            )
        else:
            conn.execute(
                "CREATE TRIGGER unknown_consumer AFTER DELETE ON model_eval_verdicts "
                "BEGIN DELETE FROM llm_budget_alerts; END"
            )
        before = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in TABLES
        }
    with pytest.raises(RuntimeError, match="unsupported"):
        migrated_db(database, upgrade_existing=True, target=HEAD)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [(PARENT,)]
        assert {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in TABLES
        } == before
