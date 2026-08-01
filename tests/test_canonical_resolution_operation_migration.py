# pyright: reportPrivateUsage=false
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parents[1]


def _load_migration() -> ModuleType:
    path = ROOT / "alembic" / "versions" / "0266_canonical_resolution_operation_ledger.py"
    spec = importlib.util.spec_from_file_location("migration_0266_for_test", path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load migration 0266")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0266_writer_reservation_closes_downgrade_empty_check_race(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canonical-operation-ledger-lock.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE canonical_resolution_operation_ledger (operation_id TEXT PRIMARY KEY)"
        )
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    migration = _load_migration()

    with engine.connect() as owner, owner.begin():
        migration._acquire_writer_lock(owner)
        contender = sqlite3.connect(database, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                contender.execute("BEGIN IMMEDIATE")
        finally:
            contender.close()
