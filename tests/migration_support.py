"""Shared builders for deliberate historical migration test setup."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]


def build_filing_xbrl_ledger_history(
    path: Path,
    *,
    target: str = "head",
) -> Path:
    """Build legacy fact tables and replay the filing-ledger migration chain."""
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
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, target)
    return path
