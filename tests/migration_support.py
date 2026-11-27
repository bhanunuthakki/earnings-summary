"""Shared builders for migration-backed test setup."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]


def build_filing_xbrl_ledger_history(
    path: Path,
    *,
    target: str = "head",
) -> Path:
    """Build an active migrated database through ``target``."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, target)
    return path
