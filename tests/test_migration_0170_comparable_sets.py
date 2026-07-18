"""Round-trip test for 0170 -- comparable_sets / comparable_set_members /
comp_set_metrics_daily (docs/design/comparable_sets_bottoms_up.md §6).

Built with the real chain (init_db + alembic), like the 0160 metrics-engine
schema test, so this exercises the actual `sa.inspect(bind).get_table_names()`
existing-table guard against a real schema rather than a hand-rolled
approximation.
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

PRIOR_HEAD = "0167_segment_10q_disambiguate_budget"
NEW_HEAD = "0170_comparable_sets"

_NEW_TABLES = ("comparable_sets", "comparable_set_members", "comp_set_metrics_daily")


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def prior_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("comparable_sets_schema_tmpl") / "at_0167.db"
    import db as dbmod

    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, PRIOR_HEAD)
    return db


@pytest.fixture
def db_at_prior(prior_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "comparable_sets_schema.db"
    shutil.copy(prior_template, db)
    return db


def test_prior_head_lacks_new_tables(db_at_prior: Path) -> None:
    conn = sqlite3.connect(str(db_at_prior))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in _NEW_TABLES:
        assert t not in tables
    conn.close()


def test_upgrade_creates_new_tables(db_at_prior: Path) -> None:
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in _NEW_TABLES:
        assert t in tables
    conn.close()


def test_upgrade_is_idempotent_on_a_rerun(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.upgrade(cfg, NEW_HEAD)  # must not raise
    conn = sqlite3.connect(str(db_at_prior))
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for t in _NEW_TABLES:
        assert tables.count(t) == 1
    conn.close()


def test_comp_set_metrics_daily_unique_constraint(db_at_prior: Path) -> None:
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    row = (
        "comparable_set",
        "NU_1",
        "2026-07-17",
        "pe_ttm",
        "median",
        12.5,
        10,
        9,
        0.9,
        1,
        "{}",
        "2026-07-17T00:00:00",
    )
    conn.execute(
        "INSERT INTO comp_set_metrics_daily (scope_type, scope_key, as_of_date, metric, "
        "stat_type, value, n_members, n_valid, coverage_pct, method_version, method_flags, "
        "computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        row,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO comp_set_metrics_daily (scope_type, scope_key, as_of_date, metric, "
            "stat_type, value, n_members, n_valid, coverage_pct, method_version, method_flags, "
            "computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    conn.close()


def test_comparable_set_members_pk_allows_reopen_on_new_day(db_at_prior: Path) -> None:
    """Same (comparable_set_id, member_ticker) can have two rows as long as
    valid_from differs — the mechanism freeze_comparable_set relies on to
    close-then-reopen a member across a membership change."""
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    conn.execute(
        "INSERT INTO comparable_sets (comparable_set_id, ticker, method_version, resolved_at, "
        "metric_class) VALUES ('NU_1', 'NU', 1, '2026-07-17T00:00:00', 'financial')"
    )
    conn.execute(
        "INSERT INTO comparable_set_members (comparable_set_id, member_ticker, "
        "membership_reason, context_only, valid_from, valid_to) "
        "VALUES ('NU_1', 'SOFI', 'industry_seed', 0, '2026-07-01', '2026-07-17')"
    )
    conn.execute(
        "INSERT INTO comparable_set_members (comparable_set_id, member_ticker, "
        "membership_reason, context_only, valid_from, valid_to) "
        "VALUES ('NU_1', 'SOFI', 'llm_ratified', 0, '2026-07-17', NULL)"
    )
    conn.commit()
    rows = conn.execute(
        "SELECT valid_from, valid_to FROM comparable_set_members WHERE member_ticker = 'SOFI' "
        "ORDER BY valid_from"
    ).fetchall()
    assert len(rows) == 2
    conn.close()


def test_downgrade_drops_new_tables(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in _NEW_TABLES:
        assert t not in tables
    conn.close()
