"""Round-trip test for ``0137_provenance_freshness``.

Mirrors the repo's alembic-test pattern: hand-create minimal pre-0137 tables,
stamp the prior head, run the one migration. Proves:

- dcf_runs gains is_latest/superseded_at/superseded_by_id; existing rows read
  is_latest=1
- the whole-ticker unique index is replaced by a PARTIAL one that admits history
  (many is_latest=0 rows) but forbids two current runs for one ticker
- the over_under CHECK survives (no table rebuild)
- decisions gains the basis_* columns
- v_decision_freshness classifies basis_status: unknown / fresh / superseded_minor
  / superseded_material, with a correctly-signed drift
- downgrade refuses when versioned history exists, else cleans up
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0136_prompt_ab"
HEAD = "0137_provenance_freshness"

_PRE_DDL = """
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR NOT NULL,
    valuation_date VARCHAR(10) NOT NULL,
    segment_name VARCHAR,
    npv_per_share NUMERIC(24, 6),
    live_price NUMERIC(24, 6),
    over_under_pct FLOAT,
    created_at DATETIME NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    CONSTRAINT ck_dcf_runs_over_under_ratio CHECK (
        over_under_pct IS NULL OR live_price IS NULL OR npv_per_share IS NULL
        OR live_price <= 0 OR npv_per_share <= 0
        OR ABS(over_under_pct - (1.0 * live_price / npv_per_share - 1.0)) <= 0.005)
);
CREATE UNIQUE INDEX uq_dcf_runs_ticker ON dcf_runs (ticker);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    outcome_label VARCHAR(16),
    made_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL
);
"""


def _config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "m.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_PRE_DDL)
        conn.execute(
            "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share) "
            "VALUES ('RBRK', '2026-06-01', 91.0)"
        )
        conn.commit()
    finally:
        conn.close()
    cfg = _config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    return db


def test_dcf_versioned_columns_and_partial_index(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(dcf_runs)").fetchall()}
        assert {"is_latest", "superseded_at", "superseded_by_id"} <= cols
        # existing row defaulted to current
        assert conn.execute("SELECT is_latest FROM dcf_runs WHERE ticker='RBRK'").fetchone()[0] == 1
        # old whole-ticker unique index is gone; the partial one is present
        idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "uq_dcf_runs_ticker" not in idx
        part = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_dcf_runs_latest'"
        ).fetchone()
        assert part is not None and "WHERE" in str(part[0]).upper()
    finally:
        conn.close()


def test_partial_index_admits_history_but_forbids_two_current(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        # A superseded prior version alongside the current one is allowed
        conn.execute(
            "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, is_latest, superseded_at) "
            "VALUES ('RBRK', '2026-05-01', 88.0, 0, '2026-06-01T00:00:00')"
        )
        conn.commit()
        # A SECOND current (is_latest=1) run for the same ticker is refused
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, is_latest) "
                "VALUES ('RBRK', '2026-07-02', 66.45, 1)"
            )
            conn.commit()
    finally:
        conn.close()


def test_over_under_check_survives(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        # over_under_pct wildly inconsistent with live/npv must still be refused
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, live_price, "
                "over_under_pct, is_latest) VALUES ('AAA', '2026-07-02', 100.0, 50.0, 5.0, 1)"
            )
            conn.commit()
    finally:
        conn.close()


def test_decision_basis_columns(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        assert {
            "basis_kind",
            "basis_ref_id",
            "basis_value",
            "basis_as_of",
            "basis_meta_json",
        } <= cols
    finally:
        conn.close()


def test_view_classifies_basis_status(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        cur_id = conn.execute("SELECT id FROM dcf_runs WHERE ticker='RBRK'").fetchone()[0]
        # current RBRK run is npv 91 → correct it to 66.45 as a NEW current version
        conn.execute("UPDATE dcf_runs SET is_latest=0, superseded_at='x' WHERE id=?", (cur_id,))
        conn.execute(
            "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, is_latest) "
            "VALUES ('RBRK', '2026-07-02', 66.45, 1)"
        )
        rows = [
            # (basis_kind, basis_value, basis_as_of, expected_status)
            ("dcf", 91.0, "2026-05-25", "superseded_material"),  # -27% drift
            ("dcf", 66.45, "2026-07-02", "fresh"),  # equals current as-of
            ("dcf", 64.0, "2026-06-01", "superseded_minor"),  # +3.8% drift
            (None, None, None, "unknown"),  # no basis recorded
        ]
        for kind, val, asof, _ in rows:
            conn.execute(
                "INSERT INTO decisions (ticker, recommendation_kind, made_at, created_at, "
                "basis_kind, basis_value, basis_as_of) VALUES ('RBRK','hold','2026-07-01',"
                "'2026-07-01',?,?,?)",
                (kind, val, asof),
            )
        conn.commit()
        got = {
            (r["basis_value"], r["basis_status"]): r
            for r in conn.execute(
                "SELECT basis_value, basis_status, valuation_superseded, basis_drift_pct "
                "FROM v_decision_freshness"
            ).fetchall()
        }
        statuses = {r["basis_status"] for r in got.values()}
        assert statuses == {"superseded_material", "fresh", "superseded_minor", "unknown"}
        # the $91 basis: superseded, negative drift (fair value fell)
        material = next(r for r in got.values() if r["basis_status"] == "superseded_material")
        assert material["valuation_superseded"] == 1
        assert material["basis_drift_pct"] < 0
    finally:
        conn.close()


def test_downgrade_refuses_with_history_else_clean(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    cfg = _config(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, is_latest, superseded_at) "
            "VALUES ('RBRK', '2026-05-01', 80.0, 0, '2026-06-01')"
        )
        conn.commit()
    finally:
        conn.close()
    # history exists → downgrade must refuse (ticker uniqueness would be violated)
    with pytest.raises(RuntimeError):
        command.downgrade(cfg, PRIOR_HEAD)
    # remove history → downgrade cleans up
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM dcf_runs WHERE is_latest=0")
        conn.commit()
    finally:
        conn.close()
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(dcf_runs)").fetchall()}
        assert "is_latest" not in cols
        dcols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        assert "basis_kind" not in dcols
        views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
        assert "v_decision_freshness" not in views
    finally:
        conn.close()
