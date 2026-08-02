"""Migration 0271 — thesis_materiality judgment columns on disclosure_events.

Mirrors the test_alembic_financial_facts_supersedes_index harness: stamp the
parent, run the one migration against a purpose-built schema, assert both
directions. The migration must be idempotent (re-runnable when columns
already exist) and skip cleanly when the table is absent (partial schemas).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0271_disclosure_thesis_materiality"
PARENT = "0270_financial_facts_supersedes_index"
INDEX = "ix_disclosure_events_thesis_materiality"
COLUMNS = (
    "thesis_materiality",
    "thesis_materiality_rationale",
    "thesis_materiality_judged_at",
)

_EVENTS_SQL = (
    "CREATE TABLE disclosure_events ("
    "id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, event_type TEXT NOT NULL, "
    "subject TEXT NOT NULL, verdict TEXT NOT NULL DEFAULT 'unclassified', "
    "status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL)"
)


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _stamp_parent(path: Path, *, table_sql: str) -> Config:
    with sqlite3.connect(path) as conn:
        if table_sql:
            conn.executescript(table_sql)
    config = _config(path)
    command.stamp(config, PARENT)
    return config


def _column_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info('disclosure_events')")}


def test_0271_adds_reversible_judgment_columns_and_index(tmp_path: Path) -> None:
    path = tmp_path / "thesis-materiality.db"
    config = _stamp_parent(path, table_sql=_EVENTS_SQL)

    command.upgrade(config, REVISION)
    names = _column_names(path)
    assert set(COLUMNS) <= names
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)
        index_cols = [row[2] for row in conn.execute(f"PRAGMA index_info('{INDEX}')")]
        assert index_cols == ["ticker", "thesis_materiality"]
        # NULL means "not yet judged": a fresh row must not satisfy the
        # elevation predicate any surface gates on.
        conn.execute(
            "INSERT INTO disclosure_events (ticker, event_type, subject, created_at) "
            "VALUES ('NU', 'item_added', 's', '2026-08-01T00:00:00')"
        )
        elevated = conn.execute(
            "SELECT COUNT(*) FROM disclosure_events "
            "WHERE thesis_materiality = 'restricts_measurement'"
        ).fetchone()[0]
        assert elevated == 0

    command.downgrade(config, PARENT)
    names_after = _column_names(path)
    assert not (set(COLUMNS) & names_after)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (PARENT,)
        assert conn.execute(f"PRAGMA index_info('{INDEX}')").fetchall() == []


def test_0271_is_idempotent_when_columns_preexist(tmp_path: Path) -> None:
    path = tmp_path / "thesis-materiality-idempotent.db"
    config = _stamp_parent(
        path,
        table_sql=(
            _EVENTS_SQL.rstrip(")")
            + ", thesis_materiality TEXT, thesis_materiality_rationale TEXT, "
            "thesis_materiality_judged_at TEXT)"
        ),
    )
    command.upgrade(config, REVISION)
    assert set(COLUMNS) <= _column_names(path)


def test_0271_partial_schema_may_omit_disclosure_events(tmp_path: Path) -> None:
    path = tmp_path / "missing-disclosure-events.db"
    config = _stamp_parent(path, table_sql="")
    command.upgrade(config, REVISION)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)
    command.downgrade(config, PARENT)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (PARENT,)
