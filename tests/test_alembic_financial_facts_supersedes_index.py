from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0270_financial_facts_supersedes_index"
PARENT = "0269_latest_governed_population_receipt_v2"
INDEX = "ix_0270_financial_facts_supersedes_id"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _stamp_parent(path: Path, *, table_sql: str) -> Config:
    with sqlite3.connect(path) as conn:
        if table_sql:
            conn.executescript(table_sql)
    config = _config(path)
    command.stamp(config, PARENT)
    return config


def test_0270_adds_reversible_leading_fk_lookup_index(tmp_path: Path) -> None:
    path = tmp_path / "financial-facts-index.db"
    config = _stamp_parent(
        path,
        table_sql=(
            "CREATE TABLE financial_facts ("
            "id INTEGER PRIMARY KEY, ticker TEXT, "
            "supersedes_id INTEGER REFERENCES financial_facts(id))"
        ),
    )

    command.upgrade(config, REVISION)

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)
        assert conn.execute(f"PRAGMA index_info('{INDEX}')").fetchall() == [(0, 2, "supersedes_id")]
        index_row = next(
            row for row in conn.execute("PRAGMA index_list('financial_facts')") if row[1] == INDEX
        )
        assert index_row[2] == 0
        assert index_row[4] == 0
        plan = tuple(
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM financial_facts WHERE supersedes_id=?",
                (1,),
            )
        )
        assert any(f"COVERING INDEX {INDEX}" in step for step in plan)
        assert not any(step == "SCAN financial_facts" for step in plan)

    command.downgrade(config, PARENT)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (PARENT,)
        assert conn.execute(f"PRAGMA index_info('{INDEX}')").fetchall() == []
        assert conn.execute("PRAGMA foreign_key_list('financial_facts')").fetchone()[2:] == (
            "financial_facts",
            "supersedes_id",
            "id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        )


def test_0270_refuses_missing_financial_facts_table(tmp_path: Path) -> None:
    path = tmp_path / "missing-financial-facts.db"
    config = _stamp_parent(path, table_sql="")

    with pytest.raises(RuntimeError, match="financial_facts table is missing"):
        command.upgrade(config, REVISION)


def test_0270_refuses_missing_supersedes_column(tmp_path: Path) -> None:
    path = tmp_path / "missing-supersedes.db"
    config = _stamp_parent(
        path,
        table_sql="CREATE TABLE financial_facts (id INTEGER PRIMARY KEY)",
    )

    with pytest.raises(RuntimeError, match="supersedes_id column is missing"):
        command.upgrade(config, REVISION)


def test_0270_refuses_missing_self_fk_contract(tmp_path: Path) -> None:
    path = tmp_path / "missing-self-fk.db"
    config = _stamp_parent(
        path,
        table_sql=("CREATE TABLE financial_facts (id INTEGER PRIMARY KEY, supersedes_id INTEGER)"),
    )

    with pytest.raises(RuntimeError, match="self-FK is missing"):
        command.upgrade(config, REVISION)


@pytest.mark.parametrize(
    "table_sql",
    [
        (
            "CREATE TABLE financial_facts ("
            "id INTEGER PRIMARY KEY, supersedes_id INTEGER "
            "REFERENCES financial_facts(id) ON DELETE CASCADE)"
        ),
        (
            "CREATE TABLE financial_facts ("
            "id INTEGER PRIMARY KEY, scope_id INTEGER, supersedes_id INTEGER, "
            "UNIQUE(id, scope_id), "
            "FOREIGN KEY(supersedes_id, scope_id) "
            "REFERENCES financial_facts(id, scope_id))"
        ),
    ],
    ids=["cascade", "composite"],
)
def test_0270_refuses_non_exact_self_fk_semantics(
    tmp_path: Path,
    table_sql: str,
) -> None:
    path = tmp_path / "drifted-self-fk.db"
    config = _stamp_parent(path, table_sql=table_sql)

    with pytest.raises(RuntimeError, match="exact single-column self-FK"):
        command.upgrade(config, REVISION)


def test_0270_refuses_preexisting_migration_owned_index_name(tmp_path: Path) -> None:
    path = tmp_path / "conflicting-index.db"
    config = _stamp_parent(
        path,
        table_sql=(
            "CREATE TABLE financial_facts ("
            "id INTEGER PRIMARY KEY, ticker TEXT, "
            "supersedes_id INTEGER REFERENCES financial_facts(id));"
            f"CREATE INDEX {INDEX} ON financial_facts(ticker)"
        ),
    )

    with pytest.raises(RuntimeError, match="migration-owned index name already exists"):
        command.upgrade(config, REVISION)
