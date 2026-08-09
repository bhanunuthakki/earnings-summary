"""Upgrade and integrity tests for normalized DCF input provenance."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HEAD = "0251_dcf_run_inputs"
PRIOR_HEAD = "0250_immutable_transcript_versions"


def _migration() -> ModuleType:
    path = PROJECT_ROOT / "alembic" / "versions_archived" / "0251_dcf_run_inputs.py"
    spec = importlib.util.spec_from_file_location("migration_0251_dcf_run_inputs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == HEAD
    assert module.down_revision == PRIOR_HEAD
    return module


def _run_migration(db_path: Path, operation: str) -> None:
    module = _migration()
    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as connection:
        setattr(module, "op", Operations(MigrationContext.configure(connection)))
        migration_fn = getattr(module, operation)
        assert isinstance(migration_fn, Callable)
        migration_fn()
    engine.dispose()


def test_upgrade_backfills_normalized_rows_and_enforces_immutability(tmp_path: Path) -> None:
    db_path = tmp_path / "provenance.db"
    detail = {
        "sources": [
            {
                "role": "owner_assumptions",
                "path": "data/dcf_assumptions/META.json",
                "sha256": "a" * 64,
                "bytes": 321,
                "observed_at": "2026-07-28T12:00:00+00:00",
            }
        ],
        "market_price": {
            "price": 700.0,
            "observed_at": "2026-07-28T12:01:00+00:00",
            "source": "fmp_quote",
        },
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE dcf_runs (id INTEGER PRIMARY KEY, ticker TEXT, provenance_json TEXT)"
        )
        conn.execute(
            "INSERT INTO dcf_runs VALUES (1, 'META', ?)",
            (json.dumps(detail),),
        )
    _run_migration(db_path, "upgrade")

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT role, locator, sha256, byte_size, observed_at FROM dcf_run_inputs ORDER BY role"
        ).fetchall()
        assert rows == [
            (
                "market_price",
                "fmp_quote",
                None,
                None,
                "2026-07-28T12:01:00+00:00",
            ),
            (
                "owner_assumptions",
                "data/dcf_assumptions/META.json",
                "a" * 64,
                321,
                "2026-07-28T12:00:00+00:00",
            ),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE dcf_run_inputs SET locator='other' WHERE id=1")
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            conn.execute("DELETE FROM dcf_run_inputs WHERE id=1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dcf_run_inputs "
                "(dcf_run_id, role, locator, sha256, detail_json) "
                "VALUES (1, 'bad', 'bad', 'ABC', '{}')"
            )

    _run_migration(db_path, "downgrade")
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='dcf_run_inputs'"
            ).fetchone()[0]
            == 0
        )


def test_upgrade_accepts_legacy_dcf_runs_without_provenance_column(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE dcf_runs (id INTEGER PRIMARY KEY, ticker TEXT)")
        conn.execute("INSERT INTO dcf_runs VALUES (7, 'NU')")
    _run_migration(db_path, "upgrade")

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_run_inputs").fetchone()[0] == 0
        foreign_keys = conn.execute("PRAGMA foreign_key_list(dcf_run_inputs)").fetchall()
        assert foreign_keys[0][2:5] == ("dcf_runs", "dcf_run_id", "id")
