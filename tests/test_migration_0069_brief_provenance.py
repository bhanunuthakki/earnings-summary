"""Migration 0069 — recreate brief_provenance_log so the live writer persists.

The writer (execution/build_artifacts.py::_log_brief_provenance) was a guarded
no-op in prod because the table had been dropped in 0031. 0069 brings it back.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PRIOR = "0059_kpi_facts_restatement"

# The columns build_artifacts._log_brief_provenance INSERTs into. The recreated
# table must carry all of them or the writer would fail at runtime.
_WRITER_COLUMNS = {
    "ticker",
    "generation_date",
    "sources_used",
    "sections_status",
    "trigger",
    "artifact_path",
}


def _cfg(db: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    return cfg


def _columns(db: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_0069_recreates_table_for_the_writer(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    cfg = _cfg(db)
    command.stamp(cfg, _PRIOR)
    command.upgrade(cfg, "head")
    cols = _columns(db, "brief_provenance_log")
    assert cols, "brief_provenance_log should exist at head"
    assert _WRITER_COLUMNS <= cols  # the writer's INSERT will succeed


def test_0069_downgrade_roundtrips(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    cfg = _cfg(db)
    command.stamp(cfg, _PRIOR)
    command.upgrade(cfg, "head")
    assert _columns(db, "brief_provenance_log")
    command.downgrade(cfg, "0068_substrate_constraints")
    assert not _columns(db, "brief_provenance_log")  # gone after downgrading past 0069
