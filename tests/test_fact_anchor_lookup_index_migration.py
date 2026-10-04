from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from tests import test_resolution_selected_observation_index as selected_index

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0258_fact_anchor_run_lookup_index"
PARENT = "0257_embedding_candidate_governance"
TABLE = "fact_reported_observation_anchors_v2"
INDEX = "ix_fact_reported_anchors_v2_extraction_observation"
TRIGGER = "trg_test_fact_anchor_insert"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_0258_adds_reversible_covering_extraction_run_lookup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fact-anchor-index.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            f"""
            CREATE TABLE {TABLE} (
                observation_id TEXT PRIMARY KEY,
                extraction_run_id TEXT NOT NULL
            );
            CREATE TRIGGER {TRIGGER}
            AFTER INSERT ON {TABLE}
            BEGIN
                SELECT 1;
            END;
            INSERT INTO {TABLE} VALUES ('observation-2', 'run-1');
            INSERT INTO {TABLE} VALUES ('observation-1', 'run-1');
            """
        )
        conn.commit()
    finally:
        conn.close()

    config = _config(path)
    command.stamp(config, PARENT)
    command.upgrade(config, REVISION)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)
        assert conn.execute(f"PRAGMA index_info('{INDEX}')").fetchall() == [
            (0, 1, "extraction_run_id"),
            (1, 0, "observation_id"),
        ]
        plan = " ".join(
            str(row[3])
            for row in conn.execute(
                f"EXPLAIN QUERY PLAN SELECT observation_id FROM {TABLE} "
                "WHERE extraction_run_id=? ORDER BY observation_id",
                ("run-1",),
            )
        )
        assert f"COVERING INDEX {INDEX}" in plan
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name=?",
            (TRIGGER,),
        ).fetchone() == (1,)
    finally:
        conn.close()

    command.downgrade(config, PARENT)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (PARENT,)
        assert conn.execute(f"PRAGMA index_info('{INDEX}')").fetchall() == []
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name=?",
            (TRIGGER,),
        ).fetchone() == (1,)
        assert conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone() == (2,)
    finally:
        conn.close()


def test_index_upgrade_and_downgrade_preserve_ledger_and_current_selection(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "round-trip.db", target=selected_index.PARENT)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        selected_index.seed_resolution_ledger(conn)
        ledger = conn.execute(
            "SELECT * FROM observation_resolution_revisions ORDER BY revision"
        ).fetchall()
        candidates = conn.execute(
            "SELECT * FROM observation_resolution_candidates ORDER BY resolution_id,observation_id"
        ).fetchall()
        selected = conn.execute("SELECT * FROM v_observation_resolution_current").fetchall()
        assert len(ledger) == 2
        assert len(selected) == 1
        assert selected[0][0] == "resolution-2"
        assert selected_index.INDEX not in selected_index.resolution_indexes(conn)
        for target in (selected_index.REVISION, selected_index.PARENT):
            if target == selected_index.REVISION:
                command.upgrade(_config(path), target)
            else:
                command.downgrade(_config(path), target)
            assert (selected_index.INDEX in selected_index.resolution_indexes(conn)) == (
                target == selected_index.REVISION
            )
            assert (
                conn.execute(
                    "SELECT * FROM observation_resolution_revisions ORDER BY revision"
                ).fetchall()
                == ledger
            )
            assert (
                conn.execute(
                    "SELECT * FROM observation_resolution_candidates ORDER BY resolution_id,observation_id"
                ).fetchall()
                == candidates
            )
            assert (
                conn.execute("SELECT * FROM v_observation_resolution_current").fetchall()
                == selected
            )
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(
                    "DELETE FROM observation_resolution_revisions WHERE resolution_id='resolution-1'"
                )
            conn.rollback()
    finally:
        conn.close()


def test_0050_kpi_successor_index_round_trip_preserves_rows_and_triggers(tmp_path: Path) -> None:
    path = tmp_path / "kpi-successor-index.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            "CREATE TABLE kpi_facts(id INTEGER PRIMARY KEY,supersedes_id INTEGER);"
            "INSERT INTO kpi_facts VALUES (1,NULL),(2,1),(3,1);"
            "CREATE TRIGGER preserve_kpi_rows BEFORE DELETE ON kpi_facts "
            "BEGIN SELECT RAISE(ABORT,'append-only'); END;"
        )
        conn.commit()
        rows = conn.execute("SELECT * FROM kpi_facts ORDER BY id").fetchall()
    finally:
        conn.close()
    config = _config(path)
    parent = "0049_resolution_selected_observation_index"
    revision = "0050_kpi_fact_supersedes_lookup_index"
    index = "ix_kpi_facts_supersedes_id"
    command.stamp(config, parent)
    for target in (revision, parent, revision):
        if target == revision:
            command.upgrade(config, target)
        else:
            command.downgrade(config, target)
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("SELECT * FROM kpi_facts ORDER BY id").fetchall() == rows
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (target,)
            indexes = {
                str(item[1]): int(item[2]) for item in conn.execute("PRAGMA index_list(kpi_facts)")
            }
            assert (index in indexes) == (target == revision)
            if target == revision:
                assert indexes[index] == 0  # Multiple corrections remain representable.
                assert conn.execute(f"PRAGMA index_info('{index}')").fetchall() == [
                    (0, 1, "supersedes_id")
                ]
                plan = " ".join(
                    str(row[3])
                    for row in conn.execute(
                        "EXPLAIN QUERY PLAN SELECT id FROM kpi_facts WHERE supersedes_id=1"
                    )
                )
                assert index in plan
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("DELETE FROM kpi_facts WHERE id=1")
            conn.rollback()
            assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        finally:
            conn.close()


def test_0050_current_schema_index_preserves_resolution_ledger(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    parent = "0049_resolution_selected_observation_index"
    revision = "0050_kpi_fact_supersedes_lookup_index"
    path = migrated_db(tmp_path / "kpi-index-ledger.db", target=parent)
    conn = sqlite3.connect(path)
    try:
        selected_index.seed_resolution_ledger(conn)
        tables = (
            "observation_resolution_revisions",
            "observation_resolution_candidates",
            "v_observation_resolution_current",
        )
        before = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables
        }
        schema = conn.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','view','trigger') "
            "ORDER BY type,name"
        ).fetchall()
        command.upgrade(_config(path), revision)
        assert {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables
        } == before
        assert (
            conn.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','view','trigger') "
                "ORDER BY type,name"
            ).fetchall()
            == schema
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA index_info('ix_kpi_facts_supersedes_id')").fetchall() == [
            (0, 12, "supersedes_id")
        ]
    finally:
        conn.close()
