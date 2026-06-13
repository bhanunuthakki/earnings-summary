"""execution/recalibrate_investor_weights.py — the 13F weight-recalibration loop
+ the investor_calibration ledger (alembic 0100).

Pins the snapshot → measure → recalibrate chain: a new buy is recorded once at
its surfacing price, measured against a realized forward return once its horizon
elapses, and a fund's base_weight is nudged by its measured hit-rate only once
it has enough decided buys. Prices are injected — no FMP cache needed.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from discovery.sources import get_source

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import recalibrate_investor_weights as rw  # noqa: E402

_DDL = """
CREATE TABLE discovery_signals (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL
  DEFAULT 'bhanu', ticker TEXT NOT NULL, signal_class TEXT NOT NULL, source_key TEXT NOT NULL,
  weight FLOAT NOT NULL DEFAULT 1.0, raw_strength FLOAT NOT NULL DEFAULT 1.0, observed_at TEXT NOT NULL,
  detail TEXT, meta_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CONSTRAINT uq_sig UNIQUE (user_id, ticker, signal_class, source_key));
CREATE TABLE discovery_sources (source_key TEXT PRIMARY KEY, signal_class TEXT NOT NULL,
  display_name TEXT NOT NULL, base_weight FLOAT NOT NULL DEFAULT 1.0, tier TEXT NOT NULL
  DEFAULT 'crossover', style_tags TEXT, cik TEXT, active INTEGER NOT NULL DEFAULT 1,
  last_calibrated_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE investor_calibration (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL
  DEFAULT 'bhanu', source_key TEXT NOT NULL, ticker TEXT NOT NULL, observed_at TEXT NOT NULL,
  action TEXT, entry_price FLOAT, entry_recorded_at TEXT NOT NULL, horizon_days INTEGER NOT NULL
  DEFAULT 180, measured_at TEXT, exit_price FLOAT, forward_return FLOAT, outcome TEXT,
  CONSTRAINT ck_out CHECK (outcome IS NULL OR outcome IN ('win','loss','flat','no_price')),
  CONSTRAINT uq_cal UNIQUE (user_id, source_key, ticker, observed_at));
"""


def _series(*points: tuple[str, float]) -> list[tuple[date, float]]:
    return [(date.fromisoformat(d), v) for d, v in points]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "data" / "portfolio.db"
    p.parent.mkdir(parents=True)
    conn = sqlite3.connect(p)
    conn.executescript(_DDL)
    conn.executemany(
        "INSERT INTO discovery_sources (source_key, signal_class, display_name, base_weight,"
        " created_at, updated_at) VALUES (?, 'investor_13f', ?, ?, 's', 's')",
        [("appaloosa", "Appaloosa", 0.5), ("whale_rock", "Whale Rock", 0.5)],
    )
    conn.commit()
    conn.close()
    return p


# ----------------------------------------------------------------------------
# price lookup
# ----------------------------------------------------------------------------


def test_price_on_or_after() -> None:
    closes = _series(("2026-05-15", 100.0), ("2026-05-18", 110.0), ("2026-11-12", 130.0))
    assert rw.price_on_or_after(closes, date(2026, 5, 15)) == 100.0
    assert rw.price_on_or_after(closes, date(2026, 5, 16)) == 110.0  # next available
    assert rw.price_on_or_after(closes, date(2027, 1, 1)) is None  # beyond the series


# ----------------------------------------------------------------------------
# step 1: snapshot
# ----------------------------------------------------------------------------


def _seed_signal(db: Path, ticker: str, source_key: str, observed_at: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO discovery_signals (ticker, signal_class, source_key, observed_at, meta_json,"
        " created_at, updated_at) VALUES (?, 'investor_13f', ?, ?, '{\"action\": \"new\"}',"
        " '2026-06-13T00:00:00', '2026-06-13T00:00:00')",
        (ticker, source_key, observed_at),
    )
    conn.commit()
    conn.close()


def test_snapshot_records_buys_once(db: Path) -> None:
    _seed_signal(db, "ACME", "appaloosa", "2026-05-15")

    def loader(_t: str) -> list[tuple[date, float]]:
        return _series(("2026-05-15", 100.0))

    assert rw.snapshot_new_buys(db, loader=loader) == 1
    assert rw.snapshot_new_buys(db, loader=loader) == 0  # idempotent
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT entry_price, action, outcome FROM investor_calibration WHERE ticker='ACME'"
    ).fetchone()
    conn.close()
    assert row == (100.0, "new", None)


# ----------------------------------------------------------------------------
# step 2: measure
# ----------------------------------------------------------------------------


def _seed_ledger(
    db: Path, source_key: str, ticker: str, observed_at: str, entry: float | None
) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO investor_calibration (source_key, ticker, observed_at, entry_price,"
        " entry_recorded_at, horizon_days) VALUES (?, ?, ?, ?, 's', 180)",
        (source_key, ticker, observed_at, entry),
    )
    conn.commit()
    conn.close()


def test_measure_due_scores_outcomes(db: Path) -> None:
    _seed_ledger(db, "appaloosa", "WIN", "2026-01-01", 100.0)  # +30% by horizon
    _seed_ledger(db, "appaloosa", "LOSE", "2026-01-01", 100.0)  # -20%
    _seed_ledger(db, "appaloosa", "DRY", "2026-01-01", None)  # no entry price
    _seed_ledger(db, "appaloosa", "YOUNG", "2026-06-01", 100.0)  # horizon not elapsed

    prices = {
        "WIN": _series(("2026-06-30", 130.0)),  # ~180d after 2026-01-01
        "LOSE": _series(("2026-06-30", 80.0)),
        "DRY": _series(("2026-06-30", 50.0)),
        "YOUNG": _series(("2026-12-01", 200.0)),
    }
    n = rw.measure_due(db, loader=lambda t: prices[t], as_of=date(2026, 7, 15))
    assert n == 3  # WIN, LOSE, DRY are due; YOUNG is not
    conn = sqlite3.connect(db)
    got = dict(conn.execute("SELECT ticker, outcome FROM investor_calibration").fetchall())
    conn.close()
    assert got["WIN"] == "win"
    assert got["LOSE"] == "loss"
    assert got["DRY"] == "no_price"
    assert got["YOUNG"] is None  # untouched — not yet due


# ----------------------------------------------------------------------------
# step 3: recalibrate
# ----------------------------------------------------------------------------


def _seed_outcomes(db: Path, source_key: str, wins: int, losses: int) -> None:
    conn = sqlite3.connect(db)
    for i in range(wins):
        conn.execute(
            "INSERT INTO investor_calibration (source_key, ticker, observed_at, entry_recorded_at,"
            " outcome) VALUES (?, ?, '2026-01-01', 's', 'win')",
            (source_key, f"W{i}"),
        )
    for i in range(losses):
        conn.execute(
            "INSERT INTO investor_calibration (source_key, ticker, observed_at, entry_recorded_at,"
            " outcome) VALUES (?, ?, '2026-01-01', 's', 'loss')",
            (source_key, f"L{i}"),
        )
    conn.commit()
    conn.close()


def test_recalibrate_moves_weight_by_hit_rate(db: Path) -> None:
    _seed_outcomes(db, "appaloosa", wins=10, losses=0)  # perfect → weight up
    _seed_outcomes(db, "whale_rock", wins=0, losses=10)  # all wrong → weight down
    changes = {c["source_key"]: c for c in rw.recalibrate(db)}
    # 0.5 * (1 + 0.3*(1.4-1)) = 0.56 ; 0.5 * (1 + 0.3*(0.6-1)) = 0.44
    assert get_source("appaloosa", db_path=db).base_weight == pytest.approx(0.56)  # type: ignore[union-attr]
    assert get_source("whale_rock", db_path=db).base_weight == pytest.approx(0.44)  # type: ignore[union-attr]
    assert changes["appaloosa"]["hit_rate"] == 1.0
    assert get_source("appaloosa", db_path=db).last_calibrated_at is not None  # type: ignore[union-attr]


def test_recalibrate_skips_thin_history(db: Path) -> None:
    _seed_outcomes(db, "appaloosa", wins=3, losses=1)  # < MIN_OUTCOMES
    assert rw.recalibrate(db) == []
    unchanged = get_source("appaloosa", db_path=db)
    assert unchanged is not None and unchanged.base_weight == 0.5


# ----------------------------------------------------------------------------
# end-to-end + migration
# ----------------------------------------------------------------------------


def test_run_is_noop_without_history(db: Path, tmp_path: Path) -> None:
    _seed_signal(db, "ACME", "appaloosa", "2026-06-10")  # too recent to measure
    summary = rw.run(
        tmp_path, loader=lambda t: _series(("2026-06-10", 100.0)), as_of=date(2026, 6, 13)
    )
    assert summary["snapshotted"] == 1
    assert summary["measured"] == 0
    assert summary["recalibrated"] == []


def test_run_missing_db_degrades() -> None:
    summary = rw.run(Path("/nonexistent/repo"))
    assert summary == {"snapshotted": 0, "measured": 0, "recalibrated": []}


def test_migration_0100_ledger(tmp_path: Path) -> None:
    p = tmp_path / "mig.db"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{p}")
    command.stamp(cfg, "0099_discovery_sources_ciks")
    command.upgrade(cfg, "0100_investor_calibration")
    conn = sqlite3.connect(p)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(investor_calibration)")}
        assert {"source_key", "ticker", "observed_at", "entry_price", "outcome"} <= cols
        conn.execute(
            "INSERT INTO investor_calibration (source_key, ticker, observed_at, entry_recorded_at)"
            " VALUES ('f', 'X', '2026-01-01', 's')"
        )
        with pytest.raises(sqlite3.IntegrityError):  # outcome CHECK
            conn.execute(
                "INSERT INTO investor_calibration (source_key, ticker, observed_at,"
                " entry_recorded_at, outcome) VALUES ('f', 'Y', '2026-01-01', 's', 'bogus')"
            )
        with pytest.raises(sqlite3.IntegrityError):  # UNIQUE identity
            conn.execute(
                "INSERT INTO investor_calibration (source_key, ticker, observed_at, entry_recorded_at)"
                " VALUES ('f', 'X', '2026-01-01', 's')"
            )
    finally:
        conn.close()
    command.downgrade(cfg, "0099_discovery_sources_ciks")
