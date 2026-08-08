"""Regression guards for collection-time database and migration isolation."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _config(db_path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


def test_default_database_is_process_isolated_before_collection() -> None:
    """Collection must resolve the default DB outside the shared checkout."""
    configured = Path(os.environ["EARNINGS_SUMMARY_DB_PATH"])

    assert configured.name == f"portfolio-{os.getpid()}.db"
    assert configured.parent.name.startswith("earnings-summary-pytest-")


def test_importing_db_does_not_create_the_configured_database(tmp_path: Path) -> None:
    configured = tmp_path / "import-only.db"
    environment = dict(os.environ)
    environment["EARNINGS_SUMMARY_DB_PATH"] = os.fspath(configured)

    subprocess.run(
        [sys.executable, "-c", "import db"],
        check=True,
        env=environment,
        cwd=PROJECT_ROOT,
    )

    assert not configured.exists()


def test_explicit_history_uses_archive_while_head_uses_active_graph(tmp_path: Path) -> None:
    archive = PROJECT_ROOT / "alembic" / "versions_archived"
    historical_db = tmp_path / "historical.db"
    historical = _config(historical_db)

    command.stamp(historical, "0273_post_earnings_readout_budget")

    assert historical.get_main_option("version_locations") == str(archive)
    with sqlite3.connect(historical_db) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0273_post_earnings_readout_budget",
        )

    active_db = tmp_path / "active.db"
    active = _config(active_db)
    command.stamp(active, "head")

    assert active.get_main_option("version_locations", "") == ""
    with sqlite3.connect(active_db) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0003_restore_baseline_defaults",
        )
