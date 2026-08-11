"""Migration contract for immutable earnings-surprise observations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0007_add_earnings_surprise_observations"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _observation_values() -> tuple[object, ...]:
    digest = "a" * 64
    return (
        digest,
        f"earnings-surprise-observation:{digest}",
        "WIX",
        "2026-08-06",
        "2.1",
        "2.3",
        None,
        None,
        "9.52",
        None,
        None,
        None,
        "fmp_calendar",
        "https://example.com/source",
        "2026-08-06T20:00:00+00:00",
        "data/surprise/WIX_surprises.json",
        0,
        "{}",
        "b" * 64,
        "{}",
        "c" * 64,
        "2026-08-06T20:01:00+00:00",
    )


def test_upgrade_creates_immutable_ledgers_and_projection_lineage(tmp_path: Path) -> None:
    path = tmp_path / "earnings-observations.db"
    config = _config(path)
    command.upgrade(config, "head")

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            REVISION,
        )
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "earnings_surprise_observations",
            "earnings_surprise_quarantine",
        } <= tables
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(earnings_surprises)")
        }
        assert "source_observation_id" in columns
        connection.execute(
            """
            INSERT INTO earnings_surprise_observations VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            _observation_values(),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="observations are immutable"):
            connection.execute("UPDATE earnings_surprise_observations SET source_name='changed'")
        with pytest.raises(sqlite3.IntegrityError, match="observations are immutable"):
            connection.execute("DELETE FROM earnings_surprise_observations")
        with pytest.raises(sqlite3.IntegrityError, match="requires source observation"):
            connection.execute(
                """
                INSERT INTO earnings_surprises (
                    ticker,release_date,source_name,fetched_at,source_observation_id
                ) VALUES ('NU','2026-08-06','fmp_calendar',
                          '2026-08-06T20:00:00+00:00',NULL)
                """
            )
        connection.execute(
            """
            INSERT INTO earnings_surprises (
                ticker,release_date,source_name,fetched_at,source_observation_id
            ) VALUES (?,?,?,?,?)
            """,
            (
                "WIX",
                "2026-08-06",
                "fmp_calendar",
                "2026-08-06T20:00:00+00:00",
                "a" * 64,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="requires source observation"):
            connection.execute("UPDATE earnings_surprises SET source_observation_id=?", ("d" * 64,))


def test_downgrade_removes_only_earnings_governance_surface(tmp_path: Path) -> None:
    path = tmp_path / "earnings-observations-downgrade.db"
    config = _config(path)
    command.upgrade(config, "head")
    command.downgrade(config, "0006_add_ask_proposal_approval")

    with sqlite3.connect(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "earnings_surprise_observations" not in tables
        assert "earnings_surprise_quarantine" not in tables
        assert "earnings_surprises" in tables
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(earnings_surprises)")
        }
        assert "source_observation_id" not in columns
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
