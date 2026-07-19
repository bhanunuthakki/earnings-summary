"""Round-trip test for 0172 — comp_set_metrics_daily.locator (Phase 2
provenance column, docs/design/comparable_sets_bottoms_up.md §5-§7 + the
per-#911 locator rule).

Built with the real chain (init_db + alembic), like the 0170 test, so this
exercises the actual ``sa.inspect`` table/column guards against a real
schema.
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

PRIOR_HEAD = "0174_behavior_distill_budget"
NEW_HEAD = "0178_comp_set_metrics_locator"

_TABLE = "comp_set_metrics_daily"
_COLUMN = "locator"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _columns(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({_TABLE})")}
    finally:
        conn.close()


@pytest.fixture(scope="module")
def prior_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("csmd_locator_tmpl") / "at_0171.db"
    import db as dbmod

    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, PRIOR_HEAD)
    return db


@pytest.fixture
def db_at_prior(prior_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "csmd_locator.db"
    shutil.copy(prior_template, db)
    return db


def test_prior_head_lacks_locator_column(db_at_prior: Path) -> None:
    cols = _columns(db_at_prior)
    assert cols  # table itself exists at 0170+
    assert _COLUMN not in cols


def test_upgrade_adds_locator_column(db_at_prior: Path) -> None:
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    assert _COLUMN in _columns(db_at_prior)


def test_upgrade_is_idempotent_on_a_rerun(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.upgrade(cfg, NEW_HEAD)  # must not raise
    assert _COLUMN in _columns(db_at_prior)


def test_existing_rows_survive_upgrade_with_null_locator(db_at_prior: Path) -> None:
    conn = sqlite3.connect(str(db_at_prior))
    conn.execute(
        f"INSERT INTO {_TABLE} (scope_type, scope_key, as_of_date, metric, stat_type, "
        "value, n_members, n_valid, coverage_pct, method_version, method_flags, computed_at) "
        "VALUES ('comparable_set', 'NU_1', '2026-07-17', 'pe_ttm', 'median', 12.5, 10, 9, "
        "0.9, 1, '{}', '2026-07-17T00:00:00')"
    )
    conn.commit()
    conn.close()
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    row = conn.execute(f"SELECT value, {_COLUMN} FROM {_TABLE}").fetchone()
    conn.close()
    assert row == (12.5, None)


def test_downgrade_drops_locator_column(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.downgrade(cfg, PRIOR_HEAD)
    assert _COLUMN not in _columns(db_at_prior)
