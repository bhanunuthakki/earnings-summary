from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.test_report_canonical_financials import database as database
from tests.test_report_canonical_financials import seed_table


def test_financial_candidate_migration_preserves_rows_and_guards(
    database: sqlite3.Connection, monkeypatch: object
) -> None:
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from pytest import MonkeyPatch
    from sqlalchemy import create_engine

    assert isinstance(monkeypatch, MonkeyPatch)
    facts = seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD")])
    assert facts
    database.commit()
    path = database.execute("PRAGMA database_list").fetchone()[2]
    before = database.execute(
        "SELECT * FROM canonical_fact_candidate_dispositions ORDER BY candidate_disposition_id"
    ).fetchall()
    assert before
    triggers_before = {
        row[0]
        for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND instr(sql,'canonical_fact_candidate_dispositions')>0"
        )
    }
    migration_path = (
        Path(__file__).parents[1] / "alembic/versions/0053_reviewed_financial_derivations.py"
    )
    spec = importlib.util.spec_from_file_location("financial_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.downgrade()
        migration.upgrade()
    assert (
        database.execute(
            "SELECT * FROM canonical_fact_candidate_dispositions ORDER BY candidate_disposition_id"
        ).fetchall()
        == before
    )
    assert {
        row[0]
        for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND instr(sql,'canonical_fact_candidate_dispositions')>0"
        )
    } == triggers_before
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []
    assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
