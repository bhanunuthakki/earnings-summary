"""Round-trip test for 0181 — fact_overrides.locator (Phase C provenance
click-through, docs/design/provenance_clickthrough.md §1.3/§3.2's 8-K row).

Mirrors test_alembic_fact_overrides.py's pattern (stamp the prior head against
an empty DB, run just this one migration) rather than the full init_db chain --
fact_overrides already exists at the prior head (0111), so this is a pure
additive-column test, same shape as test_migration_0178_comp_set_metrics_locator.py.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Stamping straight to PRIOR_HEAD (unlike upgrading through it) never RUNS any
# migration SQL, so it wouldn't actually create fact_overrides -- stamp to the
# revision just BEFORE that table existed (0111's own down_revision, same
# baseline test_alembic_fact_overrides.py/test_provenance_overrides.py use)
# and upgrade forward, so 0111 through 0180 (including fact_overrides itself)
# genuinely run.
STAMP_BASE = "0110_pass_decision_source"
PRIOR_HEAD = "0180_sector_benchmark_proposal_budget"
NEW_HEAD = "0181_fact_overrides_locator"

_TABLE = "fact_overrides"
_COLUMN = "locator"

_INSERT = (
    "INSERT INTO fact_overrides "
    "(user_id, ticker, period_end, fiscal_period_type, fact_kind, fact_key, action, "
    " source_doc_type, status, created_by, created_at) "
    "VALUES ('bhanu', 'GOOG', '2025-12-31', 'Q4', 'segment', 'product', 'replace', "
    " 'sec_8k', 'active', 'test', '2026-07-18')"
)


def _config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _seed(db_path: Path) -> Config:
    cfg = _config(db_path)
    command.stamp(cfg, STAMP_BASE)
    command.upgrade(cfg, PRIOR_HEAD)
    return cfg


def _columns(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({_TABLE})").fetchall()}
    finally:
        conn.close()


def test_prior_head_lacks_locator_column(tmp_path: Path) -> None:
    db = tmp_path / "prior.db"
    _seed(db)
    cols = _columns(db)
    assert cols  # fact_overrides already exists at 0111+
    assert _COLUMN not in cols


def test_upgrade_adds_locator_column(tmp_path: Path) -> None:
    db = tmp_path / "up.db"
    cfg = _seed(db)
    command.upgrade(cfg, NEW_HEAD)
    assert _COLUMN in _columns(db)


def test_upgrade_is_idempotent_on_a_rerun(tmp_path: Path) -> None:
    db = tmp_path / "idem.db"
    cfg = _seed(db)
    command.upgrade(cfg, NEW_HEAD)
    command.upgrade(cfg, NEW_HEAD)  # must not raise
    assert _COLUMN in _columns(db)


def test_existing_rows_survive_upgrade_with_null_locator(tmp_path: Path) -> None:
    db = tmp_path / "survive.db"
    cfg = _seed(db)
    conn = sqlite3.connect(str(db))
    conn.execute(_INSERT)
    conn.commit()
    conn.close()

    command.upgrade(cfg, NEW_HEAD)

    conn = sqlite3.connect(str(db))
    row = conn.execute(f"SELECT fact_key, {_COLUMN} FROM fact_overrides").fetchone()
    conn.close()
    assert row == ("product", None)


def test_downgrade_drops_locator_column(tmp_path: Path) -> None:
    db = tmp_path / "down.db"
    cfg = _seed(db)
    command.upgrade(cfg, NEW_HEAD)
    assert _COLUMN in _columns(db)
    command.downgrade(cfg, PRIOR_HEAD)
    assert _COLUMN not in _columns(db)
