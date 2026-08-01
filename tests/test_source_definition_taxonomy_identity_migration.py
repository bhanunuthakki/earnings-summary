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
    path = ROOT / "alembic" / "versions" / "0267_source_definition_taxonomy_identity.py"
    spec = importlib.util.spec_from_file_location("migration_0267_for_test", path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load migration 0267")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_0267_writer_reservation_closes_component_empty_check_race(
    tmp_path: Path,
    direction: str,
) -> None:
    database = tmp_path / f"taxonomy-identity-{direction}-lock.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE source_taxonomy_components (component_id TEXT PRIMARY KEY)")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    migration = _load_migration()

    with engine.connect() as owner, owner.begin():
        migration._acquire_writer_lock(owner)
        contender = sqlite3.connect(database, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                contender.execute("INSERT INTO source_taxonomy_components VALUES ('racing-writer')")
        finally:
            contender.close()
