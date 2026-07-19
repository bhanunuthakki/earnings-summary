"""Append-only risk-snapshot history (A5, program review 2026-07-19).

The single-row 0105 upsert made "exposure vs 30 days ago" structurally
unanswerable, and its persist gate accepted any partially-loaded tracker
response — the sole prod row was all-NULL. Three seams:

  * write_snapshot appends every capture to portfolio_risk_snapshot_history
    (0185) while the single-row latest-view keeps its upsert contract;
  * read_history returns newest-first with a since floor, [] pre-0185;
  * _persist_risk_snapshot requires the performance AND positioning sections
    — a degenerate response can no longer clobber the last good snapshot.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from integrations.portfolio_tracker_client import PortfolioAnalytics
from portfolio_risk_snapshot_store import (
    RiskSnapshot,
    read_history,
    read_latest_snapshot,
    write_snapshot,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def head_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("risk_hist_tmpl") / "at_head.db"
    import db as dbmod

    saved = (dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR)
    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, "head")
    dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR = saved
    return db


@pytest.fixture
def head_db(head_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "risk_hist.db"
    shutil.copy(head_template, db)
    return db


def test_every_write_appends_history_while_latest_view_upserts(head_db: Path) -> None:
    assert write_snapshot(RiskSnapshot(beta=1.1, spy_beta=1.3), db_path=head_db) is True
    assert write_snapshot(RiskSnapshot(beta=1.5, spy_beta=1.6), db_path=head_db) is True
    latest = read_latest_snapshot(db_path=head_db)
    assert latest is not None and latest.beta == pytest.approx(1.5)
    conn = sqlite3.connect(str(head_db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM portfolio_risk_snapshots").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM portfolio_risk_snapshot_history").fetchone()[0] == 2
        )
    finally:
        conn.close()
    hist = read_history(db_path=head_db)
    assert [h.beta for h in hist] == [pytest.approx(1.5), pytest.approx(1.1)]  # newest first


def test_read_history_since_floor_and_pre_0185_degrade(head_db: Path, tmp_path: Path) -> None:
    write_snapshot(RiskSnapshot(beta=1.0), db_path=head_db)
    assert read_history(since="2099-01-01", db_path=head_db) == []
    assert read_history(since="2000-01-01", db_path=head_db) != []
    # Pre-0185 shape: drop the history table — reader degrades to [], writer
    # still lands the latest-view.
    conn = sqlite3.connect(str(head_db))
    conn.execute("DROP TABLE portfolio_risk_snapshot_history")
    conn.commit()
    conn.close()
    assert read_history(db_path=head_db) == []
    assert write_snapshot(RiskSnapshot(beta=2.0), db_path=head_db) is True
    latest = read_latest_snapshot(db_path=head_db)
    assert latest is not None and latest.beta == pytest.approx(2.0)


def test_persist_gate_rejects_degenerate_tracker_response(head_db: Path) -> None:
    """`available=True` with only one section loaded must NOT clobber the last
    good snapshot (the all-NULL prod row's exact failure shape)."""
    import pipeline.portfolio_panel as pp

    write_snapshot(RiskSnapshot(beta=1.1, spy_beta=1.3), db_path=head_db)
    degenerate = PortfolioAnalytics(
        available=True, api_url="http://x", performance=None, positioning=None
    )
    pp._persist_risk_snapshot(degenerate, head_db)  # pyright: ignore[reportPrivateUsage]
    latest = read_latest_snapshot(db_path=head_db)
    assert latest is not None
    assert latest.beta == pytest.approx(1.1)  # untouched — no NULL clobber
