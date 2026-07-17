"""Round-trip tests for 0153..0157 -- the bottoms-up metrics engine's schema
(formula_definitions, metric_computation_attempts, kpi_facts.formula_id/
formula_version, tracked_companies.business_model_class/accounting_standard).

Built with the real chain (init_db + alembic), like the 0142 migration test,
so these migrations run against the actual production tracked_companies/
kpi_facts table shapes rather than a hand-rolled approximation -- the whole
point being to prove the anonymous list_type CHECK on tracked_companies
survives both the upgrade AND the downgrade (the 0073 landmine this program
had to route around, see 0156/0157's docstrings).
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

PRIOR_HEAD = "0152_v_thesis_status_stub_substring"
NEW_HEAD = "0157_tracked_companies_accounting_standard"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def prior_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("metrics_engine_schema_tmpl") / "at_0152.db"
    import db as dbmod

    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, PRIOR_HEAD)
    return db


@pytest.fixture
def db_at_prior(prior_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "metrics_engine_schema.db"
    shutil.copy(prior_template, db)
    return db


def _list_type_check_sql(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'tracked_companies'"
    ).fetchone()
    return str(row[0])


def test_prior_head_lacks_new_tables_and_columns(db_at_prior: Path) -> None:
    conn = sqlite3.connect(str(db_at_prior))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "formula_definitions" not in tables
    assert "metric_computation_attempts" not in tables
    kpi_cols = {r[1] for r in conn.execute("PRAGMA table_info(kpi_facts)").fetchall()}
    assert "formula_id" not in kpi_cols
    tc_cols = {r[1] for r in conn.execute("PRAGMA table_info(tracked_companies)").fetchall()}
    assert "business_model_class" not in tc_cols
    assert "accounting_standard" not in tc_cols
    conn.close()


def test_upgrade_creates_new_schema_and_preserves_list_type_check(db_at_prior: Path) -> None:
    conn = sqlite3.connect(str(db_at_prior))
    check_before = _list_type_check_sql(conn)
    conn.close()

    command.upgrade(_build_config(db_at_prior), NEW_HEAD)

    conn = sqlite3.connect(str(db_at_prior))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "formula_definitions" in tables
    assert "metric_computation_attempts" in tables

    kpi_cols = {r[1] for r in conn.execute("PRAGMA table_info(kpi_facts)").fetchall()}
    assert "formula_id" in kpi_cols
    assert "formula_version" in kpi_cols

    tc_cols = {r[1] for r in conn.execute("PRAGMA table_info(tracked_companies)").fetchall()}
    assert "business_model_class" in tc_cols
    assert "accounting_standard" in tc_cols

    # The 0073 landmine check: the anonymous list_type CHECK must be
    # byte-identical after two ADD COLUMN passes.
    check_after = _list_type_check_sql(conn)
    assert "list_type IN" in check_after
    assert check_before.split("list_type")[1].split(",")[0] == check_after.split("list_type")[1].split(",")[0]
    conn.close()


def test_upgrade_is_idempotent_on_a_rerun(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.upgrade(cfg, NEW_HEAD)  # must not raise
    conn = sqlite3.connect(str(db_at_prior))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tracked_companies)").fetchall()]
    assert cols.count("business_model_class") == 1
    assert cols.count("accounting_standard") == 1
    conn.close()


def test_business_model_class_check_rejects_invalid_value(db_at_prior: Path) -> None:
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type) "
        "VALUES ('bhanu', 'ZZZ', 'Test Co', 'watchlist')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE tracked_companies SET business_model_class = 'not_a_real_class' WHERE ticker = 'ZZZ'"
        )
    conn.close()


def test_seed_data_lands_on_nu_and_bn(db_at_prior: Path) -> None:
    conn = sqlite3.connect(str(db_at_prior))
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type) "
        "VALUES ('bhanu', 'NU', 'Nu Holdings', 'portfolio')"
    )
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type) "
        "VALUES ('bhanu', 'BN', 'Brookfield', 'portfolio')"
    )
    conn.commit()
    conn.close()

    command.upgrade(_build_config(db_at_prior), NEW_HEAD)

    conn = sqlite3.connect(str(db_at_prior))
    nu = conn.execute(
        "SELECT business_model_class, accounting_standard FROM tracked_companies WHERE ticker = 'NU'"
    ).fetchone()
    assert nu == ("bank", "ifrs")
    bn = conn.execute(
        "SELECT business_model_class, accounting_standard FROM tracked_companies WHERE ticker = 'BN'"
    ).fetchone()
    assert bn == ("holdco", "ifrs")
    conn.close()


def test_downgrade_removes_new_schema_and_preserves_list_type_check(db_at_prior: Path) -> None:
    conn = sqlite3.connect(str(db_at_prior))
    check_before = _list_type_check_sql(conn)
    conn.close()

    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.downgrade(cfg, PRIOR_HEAD)

    conn = sqlite3.connect(str(db_at_prior))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "formula_definitions" not in tables
    assert "metric_computation_attempts" not in tables
    kpi_cols = {r[1] for r in conn.execute("PRAGMA table_info(kpi_facts)").fetchall()}
    assert "formula_id" not in kpi_cols
    assert "formula_version" not in kpi_cols
    tc_cols = {r[1] for r in conn.execute("PRAGMA table_info(tracked_companies)").fetchall()}
    assert "business_model_class" not in tc_cols
    assert "accounting_standard" not in tc_cols

    check_after = _list_type_check_sql(conn)
    assert check_before.split("list_type")[1].split(",")[0] == check_after.split("list_type")[1].split(",")[0]
    conn.close()
