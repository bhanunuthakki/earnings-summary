"""Round-trip test for 0168 -- the new segment_quarterly_coverage table
(Phase 2, docs/design/segment_quarterly_framework.md §4.3).

Built against the real migration chain (init_db + alembic), like the
0165..0167 test, so this exercises the actual create-table/drop-table path,
not just a synthetic schema fixture.
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
NEW_HEAD = "0168_segment_quarterly_coverage"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def prior_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("segment_qcov_tmpl") / "at_0167.db"
    import db as dbmod

    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, PRIOR_HEAD)
    return db


@pytest.fixture
def db_at_prior(prior_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "segment_qcov.db"
    shutil.copy(prior_template, db)
    return db


def test_prior_head_lacks_table(db_at_prior: Path) -> None:
    conn = sqlite3.connect(str(db_at_prior))
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='segment_quarterly_coverage'"
    ).fetchone()
    assert row is None
    conn.close()


def test_upgrade_creates_table_with_expected_columns(db_at_prior: Path) -> None:
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(segment_quarterly_coverage)").fetchall()}
    for col in (
        "id",
        "ticker",
        "period_end",
        "fiscal_period_type",
        "dim_type",
        "dim_name",
        "status",
        "reason_code",
        "source_doc_id",
        "method_version",
        "checked_at",
    ):
        assert col in cols, col
    conn.close()


def test_upgrade_is_idempotent_on_a_rerun(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.upgrade(cfg, NEW_HEAD)  # must not raise
    conn = sqlite3.connect(str(db_at_prior))
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='segment_quarterly_coverage'"
    ).fetchone()
    assert row is not None
    conn.close()


def test_unique_constraint_rejects_duplicate_non_null_key(db_at_prior: Path) -> None:
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    conn.execute(
        "INSERT INTO segment_quarterly_coverage "
        "(ticker, period_end, fiscal_period_type, dim_type, dim_name, status, "
        " reason_code, source_doc_id, method_version, checked_at) "
        "VALUES ('AMZN', '2025-12-31', 'Q4', 'business_unit', 'AWS', 'not_computable', "
        " 'missing_prior_anchor_for_subtraction', NULL, 'segment_q4_derive_v1', CURRENT_TIMESTAMP)"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO segment_quarterly_coverage "
            "(ticker, period_end, fiscal_period_type, dim_type, dim_name, status, "
            " reason_code, source_doc_id, method_version, checked_at) "
            "VALUES ('AMZN', '2025-12-31', 'Q4', 'business_unit', 'AWS', 'tolerance_breach', "
            " 'negative_derived_value', NULL, 'segment_q4_derive_v1', CURRENT_TIMESTAMP)"
        )
    conn.close()


def test_downgrade_drops_table(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='segment_quarterly_coverage'"
    ).fetchone()
    assert row is None
    conn.close()
