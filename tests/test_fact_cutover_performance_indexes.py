"""Bounded fact cutover lookups must not scan the complete evidence ledger."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0225_financial_fact_resolution_cutover"
HEAD = "0226_fact_cutover_performance_indexes"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _index_columns(conn: sqlite3.Connection, index_name: str) -> list[str]:
    return [str(row[2]) for row in conn.execute(f"PRAGMA index_info({index_name})").fetchall()]


def test_upgrade_adds_exact_evidence_anchor_lookup_indexes(tmp_path: Path) -> None:
    path = tmp_path / "fact-cutover-indexes.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE evidence_extraction_runs (
                extraction_run_id TEXT PRIMARY KEY,
                document_version_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                completed_at DATETIME
            );
            CREATE TABLE evidence_nodes (
                node_id TEXT PRIMARY KEY,
                extraction_run_id TEXT NOT NULL,
                node_kind TEXT NOT NULL,
                revision INTEGER NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    config = _config(path)
    command.stamp(config, PRIOR_HEAD)
    command.upgrade(config, HEAD)

    conn = sqlite3.connect(path)
    try:
        assert _index_columns(conn, "ix_evidence_runs_document_outcome") == [
            "document_version_id",
            "outcome",
            "completed_at",
            "extraction_run_id",
        ]
        assert _index_columns(conn, "ix_evidence_nodes_extraction_kind") == [
            "extraction_run_id",
            "node_kind",
            "revision",
            "node_id",
        ]
    finally:
        conn.close()

    command.downgrade(config, PRIOR_HEAD)
    conn = sqlite3.connect(path)
    try:
        assert _index_columns(conn, "ix_evidence_runs_document_outcome") == []
        assert _index_columns(conn, "ix_evidence_nodes_extraction_kind") == []
    finally:
        conn.close()
