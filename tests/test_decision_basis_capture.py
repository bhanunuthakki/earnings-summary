"""Decision basis capture (PR2): record_decision snapshots the DCF model-version a
recommendation rests on, the dcf_basis resolver picks the current run, and the
backfill lands a basis on pre-capture rows from their rationale prose.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from decision_extractor import record_decision
from execution.backfill_decision_basis import backfill, parse_fair_value
from model_provenance.basis import dcf_basis

_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    recommendation_value FLOAT,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
    source_lens VARCHAR(32),
    rationale_excerpt TEXT,
    source_prose TEXT,
    made_at DATETIME NOT NULL,
    outcome_label VARCHAR(16),
    created_at DATETIME NOT NULL,
    basis_kind VARCHAR(16),
    basis_ref_id INTEGER,
    basis_value FLOAT,
    basis_as_of VARCHAR(32),
    basis_meta_json TEXT
);
CREATE UNIQUE INDEX uq_decisions_artifact ON decisions (source_artifact_id)
    WHERE source_artifact_id IS NOT NULL;
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR NOT NULL,
    valuation_date VARCHAR(10),
    npv_per_share NUMERIC,
    over_under_pct FLOAT,
    segment_name VARCHAR,
    is_latest INTEGER NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT (CURRENT_TIMESTAMP)
);
"""


def _db(tmp_path: Path, *, with_basis_cols: bool = True) -> Path:
    db = tmp_path / "d.db"
    conn = sqlite3.connect(str(db))
    schema = _SCHEMA
    if not with_basis_cols:
        # strip the five basis_* column lines to simulate a pre-0137 schema
        schema = "\n".join(ln for ln in _SCHEMA.splitlines() if "basis_" not in ln)
        schema = schema.replace("basis_meta_json TEXT\n", "").replace(",\n);", "\n);")
    conn.executescript(schema)
    conn.commit()
    conn.close()
    return db


def _add_dcf(db: Path, ticker: str, npv: float, as_of: str, *, is_latest: int = 1) -> int:
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, over_under_pct, is_latest) "
            "VALUES (?,?,?,?,?)",
            (ticker, as_of, npv, 0.27, is_latest),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def test_record_decision_captures_current_dcf_basis(tmp_path: Path) -> None:
    db = _db(tmp_path)
    run_id = _add_dcf(db, "RBRK", 66.45, "2026-07-02")
    pid = record_decision(
        ticker="RBRK",
        recommendation_kind="trim",
        recommendation_value=None,
        conviction="high",
        source_artifact_id=1,
        source_lens="five_min_reread",
        rationale_excerpt="over-valued vs fair",
        made_at=datetime(2026, 7, 2, tzinfo=UTC),
        db_path=db,
    )
    assert pid is not None
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT basis_kind, basis_ref_id, basis_value, basis_as_of, basis_meta_json "
        "FROM decisions WHERE id=?",
        (pid,),
    ).fetchone()
    conn.close()
    assert row["basis_kind"] == "dcf"
    assert row["basis_ref_id"] == run_id
    assert row["basis_value"] == 66.45
    assert row["basis_as_of"] == "2026-07-02"
    assert "over_under_pct" in (row["basis_meta_json"] or "")


def test_record_decision_no_dcf_leaves_basis_null(tmp_path: Path) -> None:
    db = _db(tmp_path)  # no dcf_runs row for this ticker
    pid = record_decision(
        ticker="ZZZ",
        recommendation_kind="hold",
        recommendation_value=None,
        conviction=None,
        source_artifact_id=7,
        source_lens="five_min_reread",
        rationale_excerpt="nothing moved",
        made_at=datetime(2026, 7, 2, tzinfo=UTC),
        db_path=db,
    )
    assert pid is not None
    conn = sqlite3.connect(str(db))
    val = conn.execute("SELECT basis_kind FROM decisions WHERE id=?", (pid,)).fetchone()[0]
    conn.close()
    assert val is None


def test_record_decision_idempotent(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_dcf(db, "NU", 12.0, "2026-06-01")
    kw = dict(
        ticker="NU",
        recommendation_kind="add",
        recommendation_value=None,
        conviction=None,
        source_artifact_id=42,
        source_lens="five_min_reread",
        rationale_excerpt="x",
        made_at=datetime(2026, 6, 1, tzinfo=UTC),
        db_path=db,
    )
    first = record_decision(**kw)  # type: ignore[arg-type]
    second = record_decision(**kw)  # type: ignore[arg-type]
    assert first == second
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM decisions WHERE source_artifact_id=42").fetchone()[0]
    conn.close()
    assert n == 1


def test_record_decision_pre0137_writes_without_basis(tmp_path: Path) -> None:
    db = _db(tmp_path, with_basis_cols=False)
    _add_dcf(db, "RBRK", 66.45, "2026-07-02")
    pid = record_decision(
        ticker="RBRK",
        recommendation_kind="trim",
        recommendation_value=None,
        conviction=None,
        source_artifact_id=1,
        source_lens="five_min_reread",
        rationale_excerpt="x",
        made_at=datetime(2026, 7, 2, tzinfo=UTC),
        db_path=db,
    )
    assert pid is not None  # no crash on the pre-0137 schema


def test_dcf_basis_resolver_picks_current_not_superseded(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_dcf(db, "RBRK", 91.0, "2026-06-01", is_latest=0)  # superseded
    cur_id = _add_dcf(db, "RBRK", 66.45, "2026-07-02", is_latest=1)  # current
    conn = sqlite3.connect(str(db))
    try:
        b = dcf_basis(conn, "rbrk")
    finally:
        conn.close()
    assert b is not None
    assert b.ref_id == cur_id and b.value == 66.45


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Stock at $78 vs. NPV $91 is a discount", 91.0),
        ("NPV/share: $66.45 · Live: $84 · Over/Under: +27%", 66.45),
        ("trades above fair value $80 today", 80.0),
        ("no valuation mentioned here at all", None),
        ("", None),
    ],
)
def test_parse_fair_value(text: str, expected: float | None) -> None:
    assert parse_fair_value(text) == expected


def test_backfill_sets_basis_from_rationale_and_is_idempotent(tmp_path: Path) -> None:
    db = _db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.executemany(
        "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, "
        "rationale_excerpt, made_at, created_at) VALUES (?,?,?,?,?,?)",
        [
            ("RBRK", "hold", 1, "Stock at $78 vs. NPV $91 — half the MoS bar.", "2026-07-01", "x"),
            ("NU", "add", 2, "no fair value stated", "2026-06-15", "x"),
        ],
    )
    conn.commit()
    conn.close()
    tally = backfill(db, apply=True)
    assert tally["updated"] == 1 and tally["no_value"] == 1
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rbrk = conn.execute("SELECT * FROM decisions WHERE ticker='RBRK'").fetchone()
    assert rbrk["basis_kind"] == "dcf" and rbrk["basis_value"] == 91.0
    assert rbrk["basis_as_of"] == "2026-07-01" and rbrk["basis_ref_id"] is None
    conn.close()
    # second run touches nothing (idempotent — basis_kind now set)
    again = backfill(db, apply=True)
    assert again["updated"] == 0


def test_backfill_requires_0137_schema(tmp_path: Path) -> None:
    db = _db(tmp_path, with_basis_cols=False)
    with pytest.raises(SystemExit):
        backfill(db, apply=True)
