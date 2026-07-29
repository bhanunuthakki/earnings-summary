from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0242_filing_xbrl_extraction_dispositions"
PARENT = "0241_source_fact_publication_ledger"
BASE_REVISION = "0213_decision_draft_provider_id"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _base_database(path: Path) -> Config:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    config = _config(path)
    command.stamp(config, BASE_REVISION)
    return config


def test_0242_installs_one_append_only_disposition_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "filing-xbrl-dispositions.db"
    config = _base_database(path)
    command.upgrade(config, REVISION)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "filing_xbrl_extraction_dispositions",
            "filing_xbrl_extraction_disposition_seals",
        } <= tables
        triggers = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        assert {
            "trg_filing_xbrl_dispositions_exact",
            "trg_filing_xbrl_dispositions_published_anchor",
            "trg_filing_xbrl_dispositions_duplicate_primary",
            "trg_filing_xbrl_dispositions_unsealed",
            "trg_filing_xbrl_disposition_seals_exact",
            "trg_filing_xbrl_extraction_dispositions_append_only",
            ("trg_filing_xbrl_extraction_disposition_seals_append_only"),
        } <= triggers
        indexes = {
            str(row[1])
            for row in conn.execute("PRAGMA index_list(filing_xbrl_extraction_dispositions)")
        }
        assert "ix_filing_xbrl_dispositions_run_order" in indexes
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    command.downgrade(config, PARENT)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (PARENT,)
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name LIKE "
            "'filing_xbrl_extraction_disposition%'"
        ).fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
