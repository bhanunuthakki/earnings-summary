# pyright: reportPrivateUsage=false
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest
from alembic.config import Config
from sqlalchemy import create_engine

from alembic import command

ROOT = Path(__file__).resolve().parents[1]


def _load_migration() -> ModuleType:
    path = ROOT / "alembic" / "versions" / "0268_latest_governed_population_operation_ledger.py"
    spec = importlib.util.spec_from_file_location("migration_0268_for_test", path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load migration 0268")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_0269_migration() -> ModuleType:
    path = ROOT / "alembic" / "versions" / "0269_latest_governed_population_receipt_v2.py"
    spec = importlib.util.spec_from_file_location("migration_0269_for_test", path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load migration 0269")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def test_0268_writer_reservation_closes_downgrade_empty_check_race(
    tmp_path: Path,
) -> None:
    database = tmp_path / "latest-population-ledger-lock.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE latest_governed_population_operation_ledger "
            "(operation_id TEXT PRIMARY KEY)"
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


def test_0269_ledger_rejects_insert_or_replace_without_recursive_triggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_0269_migration()
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE latest_governed_population_operation_ledger_v2 ("
        "operation_id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE,receipt_sha256 TEXT UNIQUE)"
    )

    class _Operation:
        @staticmethod
        def execute(statement: str) -> None:
            conn.execute(statement)

    monkeypatch.setattr(migration, "op", _Operation())
    migration._create_v2_immutable_triggers()
    values = ("operation-1", "idempotency-1", "a" * 64)
    conn.execute(
        "INSERT INTO latest_governed_population_operation_ledger_v2 VALUES (?,?,?)",
        values,
    )
    assert conn.execute("PRAGMA recursive_triggers").fetchone() == (0,)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "INSERT OR REPLACE INTO latest_governed_population_operation_ledger_v2 VALUES (?,?,?)",
            values,
        )


def test_0269_upgrades_an_applied_v1_ledger_and_accepts_v2_receipts(tmp_path: Path) -> None:
    database = tmp_path / "latest-population-v1-to-v2.db"
    config = _config(database)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE database_runtime_identity ("
            "singleton INTEGER PRIMARY KEY,database_instance_id TEXT NOT NULL UNIQUE)"
        )
        conn.execute(
            "INSERT INTO database_runtime_identity VALUES (1,?)",
            ("database-instance:" + "0" * 32,),
        )
    command.stamp(config, "0267_source_definition_taxonomy_identity")
    command.upgrade(config, "0268_latest_governed_population_operation_ledger")

    with sqlite3.connect(database) as conn:
        instance_id = str(
            conn.execute("SELECT database_instance_id FROM database_runtime_identity").fetchone()[0]
        )

        def insert_receipt(version: str, marker: str) -> None:
            operation_id = "latest-governed-population-operation:" + marker * 64
            fields = {
                "schema_version": version,
                "operation_id": operation_id,
                "database_instance_id": instance_id,
                "eligibility_artifact_sha256": marker * 64,
                "registry_artifact_sha256": marker * 64,
                "admission_sha256": marker * 64,
                "request_sha256": marker * 64,
                "result_sha256": marker * 64,
                "receipt_sha256": marker * 64,
            }
            conn.execute(
                "INSERT INTO latest_governed_population_operation_ledger ("
                "operation_id,idempotency_key,database_instance_id,"
                "eligibility_artifact_sha256,registry_artifact_sha256,admission_sha256,"
                "request_sha256,result_sha256,receipt_sha256,receipt_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    operation_id,
                    instance_id,
                    marker * 64,
                    marker * 64,
                    marker * 64,
                    marker * 64,
                    marker * 64,
                    marker * 64,
                    json.dumps(fields, sort_keys=True, separators=(",", ":")),
                ),
            )

        insert_receipt("latest-governed-population-receipt/v1", "a")
        conn.commit()

    command.upgrade(config, "0269_latest_governed_population_receipt_v2")
    with sqlite3.connect(database) as conn:
        assert {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name="
                "'latest_governed_population_operation_ledger'"
            )
        } == {
            "trg_latest_governed_population_operation_ledger_identity_immutable",
            "trg_latest_governed_population_operation_ledger_immutable_delete",
            "trg_latest_governed_population_operation_ledger_immutable_update",
        }
        assert conn.execute(
            "SELECT json_extract(receipt_json,'$.schema_version') "
            "FROM latest_governed_population_operation_ledger"
        ).fetchall() == [("latest-governed-population-receipt/v1",)]
        assert {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name="
                "'latest_governed_population_operation_ledger_v2'"
            )
        } == {
            "trg_latest_governed_population_operation_ledger_v2_identity_immutable",
            "trg_latest_governed_population_operation_ledger_v2_immutable_delete",
            "trg_latest_governed_population_operation_ledger_v2_immutable_update",
        }

    with pytest.raises(RuntimeError, match="forward-only after population evidence"):
        command.downgrade(config, "0268_latest_governed_population_operation_ledger")

    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0269_latest_governed_population_receipt_v2",
        )
        instance_id = str(
            conn.execute("SELECT database_instance_id FROM database_runtime_identity").fetchone()[0]
        )
        marker = "b"
        operation_id = "latest-governed-population-operation:" + marker * 64
        fields = {
            "schema_version": "latest-governed-population-receipt/v2",
            "operation_id": operation_id,
            "database_instance_id": instance_id,
            "eligibility_artifact_sha256": marker * 64,
            "registry_artifact_sha256": marker * 64,
            "admission_sha256": marker * 64,
            "request_sha256": marker * 64,
            "result_sha256": marker * 64,
            "receipt_sha256": marker * 64,
        }
        conn.execute(
            "INSERT INTO latest_governed_population_operation_ledger_v2 "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                operation_id,
                operation_id,
                instance_id,
                marker * 64,
                marker * 64,
                marker * 64,
                marker * 64,
                marker * 64,
                marker * 64,
                json.dumps(fields, sort_keys=True, separators=(",", ":")),
            ),
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM latest_governed_population_operation_ledger"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM latest_governed_population_operation_ledger_v2"
        ).fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE latest_governed_population_operation_ledger_v2 "
                "SET result_sha256=? WHERE operation_id=?",
                ("c" * 64, operation_id),
            )
