"""Round-trip + enforcement tests for ``0072_kpi_reporting_cadence``.

Proves the migration adds ``kpi_definitions.reporting_cadence`` as a NOT-NULL
column defaulting to ``'quarterly'`` (every existing row backfills), enforces the
``ReportingCadence`` vocabulary via a DB-level CHECK, and that downgrade removes
the column. Mirrors the stamp-at-prior-head pattern of the other migration tests:
create a minimal pre-0072 ``kpi_definitions``, stamp at 0071, run only 0072.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0071_new_table_check_constraints"
NEW_HEAD = "0072_kpi_reporting_cadence"

_KPI_DEFINITIONS_DDL = """
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR NOT NULL,
    name TEXT NOT NULL,
    unit VARCHAR NOT NULL,
    primary_source VARCHAR NOT NULL,
    fallback_source VARCHAR,
    ir_url VARCHAR,
    threshold_tier VARCHAR,
    threshold_low FLOAT,
    threshold_high FLOAT,
    notes TEXT
)
"""


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _columns(conn: sqlite3.Connection) -> dict[str, tuple[str, int, object]]:
    return {
        str(r[1]): (str(r[2]), int(r[3]), r[4])
        for r in conn.execute("PRAGMA table_info(kpi_definitions)").fetchall()
    }


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Tmp DB with a pre-0072 kpi_definitions holding one existing row, stamped at 0071."""
    db = tmp_path / "m0072.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_KPI_DEFINITIONS_DDL)
        conn.execute(
            "INSERT INTO kpi_definitions (ticker, name, unit, primary_source) "
            "VALUES ('NU', 'Capital adequacy ratio (CET1 / Basel III total)', 'percent', 'ir_doc')"
        )
        conn.commit()
    finally:
        conn.close()
    command.stamp(_build_config(db), PRIOR_HEAD)
    return db


def test_upgrade_adds_defaulted_checked_column(db_path: Path) -> None:
    """After 0072: column exists, NOT NULL, existing row backfills to 'quarterly',
    in-vocab values persist, out-of-vocab is rejected by the DB."""
    command.upgrade(_build_config(db_path), NEW_HEAD)
    conn = sqlite3.connect(str(db_path))
    try:
        cols = _columns(conn)
        assert "reporting_cadence" in cols
        _type, notnull, default = cols["reporting_cadence"]
        assert notnull == 1  # NOT NULL
        assert "quarterly" in str(default)  # constant default
        # The pre-existing row backfilled to the default.
        existing = conn.execute(
            "SELECT reporting_cadence FROM kpi_definitions WHERE ticker = 'NU'"
        ).fetchone()
        assert existing[0] == "quarterly"
        # Canonical vocabulary passes.
        for cadence in ("quarterly", "annual", "ttm"):
            conn.execute(
                "INSERT INTO kpi_definitions (ticker, name, unit, primary_source, reporting_cadence) "
                "VALUES (?, ?, 'percent', 'ir_doc', ?)",
                (f"T_{cadence}", f"k_{cadence}", cadence),
            )
        conn.commit()
        # Out-of-vocabulary is rejected by the CHECK.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO kpi_definitions (ticker, name, unit, primary_source, reporting_cadence) "
                "VALUES ('X', 'k_bogus', 'percent', 'ir_doc', 'monthly')"
            )
    finally:
        conn.close()


def test_upgrade_is_idempotent(db_path: Path) -> None:
    """Re-running the upgrade is a no-op (the _has_column guard)."""
    cfg = _build_config(db_path)
    command.upgrade(cfg, NEW_HEAD)
    # A second stamp-down + up exercises the guard without error.
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, NEW_HEAD)
    conn = sqlite3.connect(str(db_path))
    try:
        assert "reporting_cadence" in _columns(conn)
    finally:
        conn.close()


def test_downgrade_removes_column(db_path: Path) -> None:
    """Downgrade to 0071 drops the column."""
    cfg = _build_config(db_path)
    command.upgrade(cfg, NEW_HEAD)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db_path))
    try:
        assert "reporting_cadence" not in _columns(conn)
    finally:
        conn.close()
