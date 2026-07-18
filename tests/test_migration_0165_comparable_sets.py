"""Round-trip tests for ``0165_comparable_sets`` -- the bottoms-up comparable-sets
program's schema (comparable_sets, comparable_set_members, comp_set_metrics_daily).

Built with the real chain (init_db + alembic), like the 0142/0160 migration tests,
so the new tables are proven against the actual production database shape. Also
proves the migration runs twice cleanly (idempotent guard) and that the schema
round-trips through a downgrade.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

PRIOR_HEAD = "0164_tracked_companies_accounting_standard"
NEW_HEAD = "0165_comparable_sets"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def prior_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("comparable_sets_tmpl") / "at_0164.db"
    import db as dbmod

    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, PRIOR_HEAD)
    return db


@pytest.fixture
def db_at_prior(prior_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "comparable_sets.db"
    shutil.copy(prior_template, db)
    return db


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_prior_head_lacks_new_tables(db_at_prior: Path) -> None:
    conn = sqlite3.connect(str(db_at_prior))
    tables = _tables(conn)
    assert "comparable_sets" not in tables
    assert "comparable_set_members" not in tables
    assert "comp_set_metrics_daily" not in tables
    conn.close()


def test_upgrade_creates_new_schema(db_at_prior: Path) -> None:
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    tables = _tables(conn)
    assert "comparable_sets" in tables
    assert "comparable_set_members" in tables
    assert "comp_set_metrics_daily" in tables

    set_cols = {r[1] for r in conn.execute("PRAGMA table_info(comparable_sets)").fetchall()}
    assert set_cols == {
        "comparable_set_id",
        "ticker",
        "method_version",
        "resolved_at",
        "metric_class",
        "method_flags",
        "source_summary",
    }
    member_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(comparable_set_members)").fetchall()
    }
    assert member_cols == {
        "comparable_set_id",
        "member_ticker",
        "membership_reason",
        "context_only",
        "valid_from",
        "valid_to",
    }
    metric_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(comp_set_metrics_daily)").fetchall()
    }
    assert metric_cols == {
        "id",
        "scope_type",
        "scope_key",
        "as_of_date",
        "metric",
        "stat_type",
        "value",
        "n_members",
        "n_valid",
        "coverage_pct",
        "method_version",
        "method_flags",
        "computed_at",
    }
    conn.close()


def test_upgrade_is_idempotent_on_a_rerun(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.upgrade(cfg, NEW_HEAD)  # must not raise
    conn = sqlite3.connect(str(db_at_prior))
    tables = list(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
    names = [t[0] for t in tables]
    assert names.count("comparable_sets") == 1
    assert names.count("comparable_set_members") == 1
    assert names.count("comp_set_metrics_daily") == 1
    conn.close()


def test_no_real_foreign_key_on_comparable_set_members(db_at_prior: Path) -> None:
    """FK-poisoning invariant: comparable_set_members.comparable_set_id must have
    no real FK -- a real FK would fail every child insert under a test fixture
    stamped at an earlier revision, since ``db.open_conn`` runs
    ``PRAGMA foreign_keys=ON``. See docs/design/comparable_sets_bottoms_up.md
    section 14."""
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    conn.execute("PRAGMA foreign_keys=ON")
    fks = conn.execute("PRAGMA foreign_key_list(comparable_set_members)").fetchall()
    assert fks == []
    # An insert referencing a comparable_set_id that doesn't exist in
    # comparable_sets must succeed (no FK enforcement) -- proves the deviation
    # from the doc's literal DDL actually took effect.
    conn.execute(
        "INSERT INTO comparable_set_members "
        "(comparable_set_id, member_ticker, membership_reason, context_only, valid_from) "
        "VALUES ('NOPE_1', 'ZZZ', 'industry_seed', 0, '2026-07-17')"
    )
    conn.commit()
    conn.close()


def test_check_constraints_reject_invalid_values(db_at_prior: Path) -> None:
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    conn.execute(
        "INSERT INTO comparable_sets "
        "(comparable_set_id, ticker, method_version, resolved_at, metric_class) "
        "VALUES ('NU_1', 'NU', 1, '2026-07-17', 'operating')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO comparable_sets "
            "(comparable_set_id, ticker, method_version, resolved_at, metric_class) "
            "VALUES ('NU_2', 'NU', 2, '2026-07-17', 'not_a_real_class')"
        )
    conn.close()


def test_downgrade_removes_new_schema(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    tables = _tables(conn)
    assert "comparable_sets" not in tables
    assert "comparable_set_members" not in tables
    assert "comp_set_metrics_daily" not in tables
    conn.close()
