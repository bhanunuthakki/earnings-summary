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


def test_graph_selection_follows_stamped_database_and_explicit_locations(tmp_path: Path) -> None:
    archive = PROJECT_ROOT / "alembic" / "versions_archived"
    active = PROJECT_ROOT / "alembic" / "versions"
    historical_db = tmp_path / "historical.db"
    historical = _config(historical_db)

    command.stamp(historical, "0273_post_earnings_readout_budget")

    assert historical.get_main_option("version_locations", "") == ""
    with sqlite3.connect(historical_db) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0273_post_earnings_readout_budget",
        )

    # Historical test fixtures commonly rebuild Config between the stamp and
    # upgrade. ``head`` must follow the revision already stored in the DB,
    # otherwise the active squashed baseline runs against handcrafted legacy
    # tables and either duplicates them or fails on missing old columns.
    upgrade = getattr(command, "upgrade")
    upgrade(_config(historical_db), "head")

    assert historical.get_main_option("version_locations", "") == ""
    with sqlite3.connect(historical_db) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0010_add_rehearsal_io_indexes",
        )

    active_db = tmp_path / "active.db"
    explicitly_active = _config(active_db)
    explicitly_active.set_main_option("version_locations", str(active))
    upgrade(explicitly_active, "head")
    with sqlite3.connect(active_db) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0010_add_rehearsal_io_indexes",
        )

    explicitly_archived_db = tmp_path / "explicitly-archived.db"
    explicitly_archived = _config(explicitly_archived_db)
    explicitly_archived.set_main_option("version_locations", str(archive))
    command.stamp(explicitly_archived, "head")
    upgrade(explicitly_archived, "head")

    assert explicitly_archived.get_main_option("version_locations") == str(archive)
    with sqlite3.connect(explicitly_archived_db) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0273_post_earnings_readout_budget",
        )
