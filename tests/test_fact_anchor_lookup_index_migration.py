from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

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
