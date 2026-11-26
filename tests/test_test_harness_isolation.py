"""Regression guards for collection-time database and migration isolation."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from alembic.config import Config

from alembic import command
from schema_compat import expected_head

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _config(db_path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


def test_collection_database_override_is_restored_before_tests() -> None:
    """Collection isolation must not change runtime environment semantics."""
    configured = os.environ.get("EARNINGS_SUMMARY_DB_PATH", "")

    assert "earnings-summary-pytest-" not in configured


def test_default_database_binding_is_private_without_an_env_override(tmp_path: Path) -> None:
    import db

    assert Path(db.DB_PATH).parent == tmp_path / "default-db"


def test_importing_db_does_not_create_the_configured_database(tmp_path: Path) -> None:
    configured = tmp_path / "import-only.db"
    environment = dict(os.environ)
    environment["EARNINGS_SUMMARY_DB_PATH"] = os.fspath(configured)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (os.fspath(PROJECT_ROOT / "src"), environment.get("PYTHONPATH", "")))
    )

    subprocess.run(
        [sys.executable, "-c", "import db"],
        check=True,
        env=environment,
        cwd=PROJECT_ROOT,
    )

    assert not configured.exists()


def test_active_graph_upgrades_to_expected_head(tmp_path: Path) -> None:
    active_db = tmp_path / "active.db"
    explicitly_active = _config(active_db)
    command.upgrade(explicitly_active, "head")
    with sqlite3.connect(active_db) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            expected_head(),
        )
