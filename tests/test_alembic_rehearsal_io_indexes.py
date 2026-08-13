"""Rehearsal fact rehydration must use bounded, covering SQLite lookups."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from alembic.config import Config

from alembic import command
from provenance.financial_fact_resolution import DOCUMENT_FACT_REHYDRATION_SQL

ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0009_add_ir_approval_store"
HEAD = "0010_add_rehearsal_io_indexes"
FACT_INDEX = "ix_financial_facts_source_doc_id_id"
LINK_INDEX = "ix_fact_observation_revisions_source_fact_observation"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _index_columns(conn: sqlite3.Connection, index_name: str) -> list[str]:
    return [str(row[2]) for row in conn.execute(f"PRAGMA index_info({index_name})")]


def _upgrade_increment(config: Config, target: str) -> None:
    """Invoke only the requested incremental revision on a cached 0009 clone."""

    upgrade = getattr(command, "upgrade")
    upgrade(config, target)


def _seed_representative_facts(path: Path, *, fact_count: int) -> None:
    with sqlite3.connect(path) as conn:
        # This disposable clone measures the read plan only. Bypass the write
        # admission trigger instead of fabricating a complete evidence ledger;
        # the production tables, migration, indexes, and query remain exact.
        conn.execute("DROP TRIGGER trg_financial_facts_observation_insert")
        conn.executemany(
            "INSERT INTO financial_facts("
            "id,ticker,period_end,fiscal_period_type,line_item,value,unit,source_doc_id"
            ") VALUES (?,?,?,'Q2',?,1,'USD',?)",
            (
                (row_id, "RBRK", "2026-07-31", f"metric:{row_id}", 17)
                for row_id in range(1, fact_count + 1)
            ),
        )
        conn.executemany(
            "INSERT INTO fact_observation_revisions("
            "fact_table,fact_row_id,fact_revision,observation_id,logical_key,"
            "source_document_id,source_tier,captured_at"
            ") VALUES (?,?,1,?,?,?,'fmp_normalized','2026-08-12T00:00:00+00:00')",
            (
                (
                    "financial_facts",
                    row_id,
                    f"observation:{row_id}",
                    f"financial-fact:{row_id}",
                    17,
                )
                for row_id in range(1, fact_count + 1, 2)
            ),
        )


def _assert_search_uses(plan: list[str], index_name: str) -> None:
    matching = [detail for detail in plan if index_name in detail]
    assert matching, plan
    assert all("SEARCH" in detail and "SCAN" not in detail for detail in matching), plan


def test_upgrade_adds_exact_rehydration_indexes_and_downgrade_removes_them(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = migrated_db(tmp_path / "rehearsal-io.db", target=PRIOR_HEAD)
    config = _config(path)

    _upgrade_increment(config, HEAD)
    with sqlite3.connect(path) as conn:
        assert _index_columns(conn, FACT_INDEX) == ["source_doc_id", "id"]
        assert _index_columns(conn, LINK_INDEX) == [
            "source_document_id",
            "fact_table",
            "fact_row_id",
            "observation_id",
        ]

    command.downgrade(config, PRIOR_HEAD)
    with sqlite3.connect(path) as conn:
        assert _index_columns(conn, FACT_INDEX) == []
        assert _index_columns(conn, LINK_INDEX) == []


def test_rehydration_query_uses_both_indexes_without_table_scan(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = migrated_db(tmp_path / "rehydration-plan.db", target=PRIOR_HEAD)
    _seed_representative_facts(path, fact_count=3_300)
    _upgrade_increment(_config(path), HEAD)

    with sqlite3.connect(path) as conn:
        plan = [
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN " + DOCUMENT_FACT_REHYDRATION_SQL,
                (17, 17),
            )
        ]
        _assert_search_uses(plan, FACT_INDEX)
        _assert_search_uses(plan, LINK_INDEX)
        assert not any("SCAN fact" in detail or "SCAN link" in detail for detail in plan), plan

        vm_steps = 0

        def count_vm_steps() -> int:
            nonlocal vm_steps
            vm_steps += 1
            return 0

        conn.set_progress_handler(count_vm_steps, 1)
        rows = conn.execute(DOCUMENT_FACT_REHYDRATION_SQL, (17, 17)).fetchall()
        conn.set_progress_handler(None, 0)

    assert len(rows) == 3_300
    assert vm_steps < 250_000
