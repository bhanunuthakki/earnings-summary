"""Round-trip + enforcement tests for ``0076_dcf_runs_over_under_check``.

The migration adds a named CHECK to ``dcf_runs`` pinning the stored
``over_under_pct`` to the documented decimal-ratio convention
((live - fair) / fair, migration 0024) whenever the row carries both a
positive live_price and a positive npv_per_share. It is the DB-level layer of
the #368 fix: the Python chokepoint (src/dcf/persist.derive_over_under) is the
only in-repo producer, and this CHECK extends the guarantee to raw SQL.

Built with the real chain (``init_db()`` + ``alembic upgrade 0075``) like the
0073 test, so the batch_alter recreation runs against the actual production
table shape (legacy columns, uq_dcf_runs_ticker, the ix_* indexes) rather than
a hand-rolled approximation that could drift.
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

PRIOR_HEAD = "0075_locator_schema"
NEW_HEAD = "0076_dcf_runs_over_under_check"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def prior_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One DB built to the real pre-0076 head via init_db + alembic, shared
    (read-only) across the module and copied per test."""
    db = tmp_path_factory.mktemp("over_under_check_tmpl") / "at_0075.db"
    import db as dbmod

    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, PRIOR_HEAD)
    return db


@pytest.fixture
def db_at_prior(prior_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "over_under_check.db"
    shutil.copy(prior_template, db)
    return db


def _insert(
    conn: sqlite3.Connection,
    ticker: str,
    live: float | None,
    npv_ps: float,
    ou: float | None,
) -> None:
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, horizon_years, wacc, terminal_growth,"
        " npv, npv_per_share, shares_outstanding, currency, live_price, over_under_pct,"
        " revenue_growths_json, fcf_margin, assumption_snapshot_json)"
        " VALUES (?, '2026-06-10', 5, 0.1, 0, 1000.0, ?, 5e7, 'USD', ?, ?, '[]', 0, '{}')",
        (ticker, npv_ps, live, ou),
    )


def test_upgrade_keeps_rows_and_enforces_check(db_at_prior: Path) -> None:
    conn = sqlite3.connect(str(db_at_prior))
    _insert(conn, "AAA", 10.0, 20.0, -0.5)  # consistent ratio
    _insert(conn, "BBB", None, 20.0, None)  # no live price (legacy shape)
    _insert(conn, "CCC", 10.0, 20.0, None)  # NULL over_under with fields present
    conn.commit()
    conn.close()

    command.upgrade(_build_config(db_at_prior), NEW_HEAD)

    conn = sqlite3.connect(str(db_at_prior))
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 3
    # The batch recreation must preserve the upsert key + query indexes.
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='dcf_runs'"
        )
    }
    assert "uq_dcf_runs_ticker" in names
    with pytest.raises(sqlite3.IntegrityError, match="ck_dcf_runs_over_under_ratio"):
        _insert(conn, "DDD", 12.29, 22.10, 79.82)  # the NU incident shape
    _insert(conn, "EEE", 12.29, 22.10, -0.4439)  # consistent ratio -> accepted
    conn.commit()
    conn.close()


def test_downgrade_drops_the_check(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    _insert(conn, "DDD", 12.29, 22.10, 79.82)  # constraint gone -> accepted again
    conn.commit()
    conn.close()
