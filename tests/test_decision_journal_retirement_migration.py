"""The forward view change preserves explicit decision records and foreign keys."""

from __future__ import annotations

import ast
import importlib.util
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

ROOT = Path(__file__).resolve().parents[1]


def _migration() -> ModuleType:
    path = ROOT / "alembic/versions/0062_explicit_decision_journal.py"
    spec = importlib.util.spec_from_file_location("decision_journal_retirement", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _predecessor_view() -> str:
    tree = ast.parse((ROOT / "alembic/versions/0001_initial_schema.py").read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("CREATE VIEW IF NOT EXISTS v_decision_journal ")
        ):
            return node.value
    raise AssertionError("predecessor owner missing")


def _apply(database: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{database}")
    try:
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            _migration().upgrade()
    finally:
        engine.dispose()


def test_retained_view_preserves_populated_decision_series(
    tmp_path: Path, migrated_db: Callable[[Path], Path]
) -> None:
    database = migrated_db(tmp_path / "journal.db")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DROP VIEW v_decision_journal")
        connection.execute(_predecessor_view())
        connection.execute(
            "INSERT INTO decisions (ticker,recommendation_kind,recommendation_value,conviction,decided_by,made_at,created_at,user_action_kind,outcome_label,outcome_pct,process_quality) VALUES ('SYNTH','position',1.0,'medium','owner','2026-01-01','2026-01-01','hold','gain',12.5,'sound')"
        )
        before = connection.execute("SELECT * FROM decisions").fetchall()
    _apply(database)
    _apply(database)  # A repeated upgrade does not reintroduce optional joins.
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM decisions").fetchall() == before
        row = connection.execute(
            "SELECT ticker,user_action_kind,outcome_label,outcome_pct,process_quality FROM v_decision_journal"
        ).fetchone()
        assert row == ("SYNTH", "hold", "gain", 12.5, "sound")
        assert connection.execute("SELECT COUNT(*) FROM v_decision_journal").fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='v_decision_journal'"
        ).fetchone()
        assert sql is not None
        assert not any(
            name in sql[0]
            for name in (
                "coach_pings",
                "advisor_memos",
                "decision_nudges",
                "owner_profile_facts",
                "stance_scores",
                "decision_drafts",
            )
        )


def test_retained_view_rejects_unknown_predecessor_without_replacing_it(
    tmp_path: Path, migrated_db: Callable[[Path], Path]
) -> None:
    database = migrated_db(tmp_path / "journal.db")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP VIEW v_decision_journal")
        connection.execute("CREATE VIEW v_decision_journal AS SELECT 1 AS owner_custom_field")
    with pytest.raises(RuntimeError, match="predecessor owner"):
        _apply(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM v_decision_journal").fetchall() == [(1,)]
