"""Empty supersession schemas are reversible; retained correction lineage is not."""

# Dates below are fictional test data.
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command


@pytest.mark.parametrize("retained", [False, True])
def test_supersession_downgrade_preserves_retained_history(
    tmp_path: Path, migrated_db: Callable[..., Path], retained: bool
) -> None:
    database = migrated_db(tmp_path / "supersession.db", target="0044_position_entry_supersession")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO position_entries(id,user_id,ticker,source,exit_date,created_at,updated_at) "
            "VALUES(1,'fixture','WIX','manual','2035-05-15','2035-06-20','2035-06-20')"
        )
        if retained:
            conn.execute(
                "INSERT INTO position_entries(id,user_id,ticker,source,created_at,updated_at) "
                "VALUES(2,'fixture','WIX','manual','2035-06-20','2035-06-20')"
            )
            conn.execute("UPDATE position_entries SET superseded_by_entry_id=1 WHERE id=2")
    if retained:
        with pytest.raises(RuntimeError, match="Retained lifecycle correction lineage"):
            command.downgrade(config, "0043_etf_profile_field_evidence")
        with sqlite3.connect(database) as conn:
            assert conn.execute(
                "SELECT superseded_by_entry_id FROM position_entries WHERE id=2"
            ).fetchone() == (1,)
    else:
        command.downgrade(config, "0043_etf_profile_field_evidence")
        with sqlite3.connect(database) as conn:
            assert "superseded_by_entry_id" not in {
                row[1] for row in conn.execute("PRAGMA table_info(position_entries)")
            }
            assert conn.execute("SELECT count(*) FROM position_entries").fetchone() == (1,)
        command.upgrade(config, "0044_position_entry_supersession")
        with sqlite3.connect(database) as conn:
            assert conn.execute(
                "SELECT superseded_by_entry_id FROM position_entries WHERE id=1"
            ).fetchone() == (None,)
