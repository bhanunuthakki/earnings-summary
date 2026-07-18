"""Round-trip tests for 0165..0167 -- the segment quarterly framework's
additive provenance columns on segment_dimensions/segment_periods (Phase 1,
docs/design/segment_quarterly_framework.md §4) + the
segment_10q_period_disambiguate llm_budgets seed row.

Built against the real migration chain (init_db + alembic), like the 0160
metrics-engine test, so these migrations run against the actual production
segment_dimensions/segment_periods table shapes -- including the 0057
triggers segment_dimensions carries, which op.add_column (never
batch_alter_table) must leave untouched.
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
NEW_HEAD = "0167_segment_10q_disambiguate_budget"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def prior_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("segment_quarterly_prov_tmpl") / "at_0164.db"
    import db as dbmod

    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, PRIOR_HEAD)
    return db


@pytest.fixture
def db_at_prior(prior_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "segment_quarterly_prov.db"
    shutil.copy(prior_template, db)
    return db


def _triggers(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='segment_dimensions'"
        )
    }


def test_prior_head_lacks_new_columns(db_at_prior: Path) -> None:
    conn = sqlite3.connect(str(db_at_prior))
    dim_cols = {r[1] for r in conn.execute("PRAGMA table_info(segment_dimensions)").fetchall()}
    assert "disclosure_status" not in dim_cols
    assert "supersedes_id" not in dim_cols
    period_cols = {r[1] for r in conn.execute("PRAGMA table_info(segment_periods)").fetchall()}
    assert "period_basis" not in period_cols
    conn.close()


def test_upgrade_adds_columns_and_preserves_triggers(db_at_prior: Path) -> None:
    conn = sqlite3.connect(str(db_at_prior))
    triggers_before = _triggers(conn)
    assert "trg_dirty_segment_dimensions_ins" in triggers_before
    assert "trg_artifacts_dirty_on_segment_dimensions_insert" in triggers_before
    conn.close()

    command.upgrade(_build_config(db_at_prior), NEW_HEAD)

    conn = sqlite3.connect(str(db_at_prior))
    dim_cols = {r[1] for r in conn.execute("PRAGMA table_info(segment_dimensions)").fetchall()}
    for col in (
        "disclosure_status",
        "method_version",
        "confidence",
        "extracted_by",
        "locator",
        "derived_from",
        "supersedes_id",
    ):
        assert col in dim_cols, col

    period_cols = {r[1] for r in conn.execute("PRAGMA table_info(segment_periods)").fetchall()}
    for col in ("period_basis", "raw_period_label", "method_version"):
        assert col in period_cols, col

    # op.add_column (never batch_alter_table) must leave the 0057 triggers
    # untouched -- the whole point of the §4.4 rule.
    triggers_after = _triggers(conn)
    assert triggers_after == triggers_before
    conn.close()


def test_upgrade_is_idempotent_on_a_rerun(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.upgrade(cfg, NEW_HEAD)  # must not raise
    conn = sqlite3.connect(str(db_at_prior))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(segment_dimensions)").fetchall()]
    assert cols.count("disclosure_status") == 1
    conn.close()


def test_existing_rows_default_and_backfill_method_version(db_at_prior: Path) -> None:
    conn = sqlite3.connect(str(db_at_prior))
    conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size) "
        "VALUES ('AMZN', 'fmp', 'fmp_segment_product', 'x.json', '0'*64, "
        "CURRENT_TIMESTAMP, 'ok', 1)"
    )
    doc_id = conn.execute("SELECT id FROM documents WHERE ticker='AMZN'").fetchone()[0]
    conn.execute(
        "INSERT INTO segment_periods (ticker, period_end, fiscal_period_type, "
        "source_doc_id, unit) VALUES ('AMZN', '2024-12-31', 'FY', ?, 'actual')",
        (doc_id,),
    )
    period_id = conn.execute("SELECT id FROM segment_periods WHERE ticker='AMZN'").fetchone()[0]
    conn.execute(
        "INSERT INTO segment_dimensions (period_id, dim_type, dim_name, value, metric) "
        "VALUES (?, 'product', 'AWS', 100, 'revenue_by_product')",
        (period_id,),
    )
    conn.commit()
    conn.close()

    command.upgrade(_build_config(db_at_prior), NEW_HEAD)

    conn = sqlite3.connect(str(db_at_prior))
    row = conn.execute(
        "SELECT disclosure_status, confidence, method_version FROM segment_dimensions "
        "WHERE dim_name = 'AWS'"
    ).fetchone()
    assert row[0] == "reported"
    assert row[1] == 1.0
    assert row[2] == "fmp_segment_v1"  # backfilled: revenue_by_product -> fmp_segment_v1
    conn.close()


def test_llm_budgets_seed_row(db_at_prior: Path) -> None:
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    row = conn.execute(
        "SELECT monthly_cap_usd, hard_block, on_exceed FROM llm_budgets "
        "WHERE purpose = 'segment_10q_period_disambiguate'"
    ).fetchone()
    assert row is not None
    assert row[0] == 10.0
    assert row[1] == 0
    assert row[2] == "skip"
    conn.close()


def test_downgrade_drops_columns_and_budget_row(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    dim_cols = {r[1] for r in conn.execute("PRAGMA table_info(segment_dimensions)").fetchall()}
    assert "disclosure_status" not in dim_cols
    period_cols = {r[1] for r in conn.execute("PRAGMA table_info(segment_periods)").fetchall()}
    assert "period_basis" not in period_cols
    row = conn.execute(
        "SELECT 1 FROM llm_budgets WHERE purpose = 'segment_10q_period_disambiguate'"
    ).fetchone()
    assert row is None
    conn.close()
