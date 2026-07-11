"""``execution/log_scored_miss.py`` — the CLI that unblocks the re-underwrite
gate by recording a scored miss into the EXISTING ``decisions`` calibration
ledger (see tests/test_thesis_reunderwrite_gate.py for the gate side)."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import log_scored_miss as lsm  # noqa: E402

_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
    source_memo_id INTEGER,
    rationale_excerpt TEXT,
    made_at DATETIME NOT NULL,
    outcome_at DATETIME,
    outcome_label VARCHAR(16),
    outcome_pct FLOAT,
    outcome_notes TEXT,
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    scope VARCHAR(16) NOT NULL DEFAULT 'ticker',
    created_at DATETIME NOT NULL,
    CONSTRAINT ck_decisions_source_present CHECK (
        source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL
        OR recommendation_kind = 'avoid' OR decided_by = 'owner'
    ),
    CONSTRAINT ck_decisions_ticker_scope CHECK (scope = 'portfolio' OR ticker IS NOT NULL)
);
CREATE TABLE thesis_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, evaluated_at TEXT,
    overall_status TEXT, rule_evaluations_json TEXT, run_id TEXT
);
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _rows(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM decisions WHERE recommendation_kind = 'scored_miss' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_log_scored_miss_inserts_owner_row(db: Path) -> None:
    decision_id, created = lsm.log_scored_miss(
        db_path=db,
        ticker="nvo",
        conviction="high",
        belief="GLP-1 volume growth offsets US price erosion",
        outcome="US pricing reform cut realized price faster than volume grew",
        outcome_pct=-0.35,
    )
    assert created is True
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "NVO"
    assert row["decided_by"] == "owner"
    assert row["scope"] == "ticker"
    assert row["conviction"] == "high"
    assert row["outcome_label"] == "wrong"  # default
    assert row["outcome_pct"] == -0.35
    assert "GLP-1 volume growth" in row["rationale_excerpt"]
    assert "pricing reform" in row["outcome_notes"]
    assert row["id"] == decision_id


def test_log_scored_miss_folds_prior_probability_into_rationale(db: Path) -> None:
    lsm.log_scored_miss(
        db_path=db,
        ticker="NVO",
        conviction="high",
        belief="belief text",
        outcome="outcome text",
        prior_probability=0.72,
    )
    row = _rows(db)[0]
    assert "0.72" in row["rationale_excerpt"]


def test_log_scored_miss_is_idempotent_without_force(db: Path) -> None:
    id1, created1 = lsm.log_scored_miss(
        db_path=db, ticker="NVO", conviction="high", belief="b1", outcome="o1"
    )
    id2, created2 = lsm.log_scored_miss(
        db_path=db, ticker="NVO", conviction="medium", belief="b2", outcome="o2"
    )
    assert created1 is True
    assert created2 is False
    assert id2 == id1
    assert len(_rows(db)) == 1


def test_log_scored_miss_force_inserts_a_second_row(db: Path) -> None:
    lsm.log_scored_miss(db_path=db, ticker="NVO", conviction="high", belief="b1", outcome="o1")
    _, created2 = lsm.log_scored_miss(
        db_path=db, ticker="NVO", conviction="high", belief="b2", outcome="o2", force=True
    )
    assert created2 is True
    assert len(_rows(db)) == 2


def test_log_scored_miss_uses_breach_onset_as_made_at_default(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO thesis_evaluations (ticker, evaluated_at, overall_status, rule_evaluations_json) "
        "VALUES ('NVO', '2026-02-15T00:00:00', 'breach', '[]')"
    )
    conn.commit()
    conn.close()
    lsm.log_scored_miss(db_path=db, ticker="NVO", conviction="high", belief="b", outcome="o")
    row = _rows(db)[0]
    assert row["made_at"] == "2026-02-15T00:00:00"


def test_cli_main_writes_json_and_exits_zero(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = lsm.main(
        [
            "--ticker",
            "NVO",
            "--conviction",
            "high",
            "--belief",
            "b",
            "--outcome",
            "o",
            "--db",
            str(db),
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ticker"] == "NVO"
    assert out["created"] is True


def test_cli_main_rejects_out_of_range_prior_probability(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = lsm.main(
        [
            "--ticker",
            "NVO",
            "--conviction",
            "high",
            "--belief",
            "b",
            "--outcome",
            "o",
            "--prior-probability",
            "1.5",
            "--db",
            str(db),
        ]
    )
    assert rc == 1
    assert _rows(db) == []
