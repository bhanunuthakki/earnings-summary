"""Unit tests for the reusable model-version provenance + freshness helpers.

Kept schema-free where possible: the versioning helpers run against a tiny
stand-in versioned table, and the freshness read layer runs against a stand-in
``v_decision_freshness`` shaped like the real view (its SQL is exercised by
test_alembic_provenance_freshness). This isolates the Python contract from the
migration.
"""

from __future__ import annotations

import sqlite3

from model_provenance import (
    decision_freshness,
    mark_superseded_by,
    stale_material_decisions,
    supersede_current,
)

_VERSIONED_DDL = """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    segment_name TEXT,
    value REAL,
    is_latest INTEGER NOT NULL DEFAULT 1,
    superseded_at TEXT,
    superseded_by_id INTEGER
);
CREATE UNIQUE INDEX uq_runs_latest ON runs (ticker, COALESCE(segment_name,''))
    WHERE is_latest = 1;
"""

_FRESHNESS_VIEW_DDL = """
CREATE TABLE v_decision_freshness (
    decision_id INTEGER,
    ticker TEXT,
    recommendation_kind TEXT,
    decided_by TEXT,
    outcome_label TEXT,
    basis_kind TEXT,
    basis_ref_id INTEGER,
    basis_value REAL,
    basis_as_of TEXT,
    current_ref_id INTEGER,
    current_value REAL,
    current_as_of TEXT,
    valuation_superseded INTEGER,
    basis_drift_pct REAL,
    basis_status TEXT
);
"""


def _insert_run(conn: sqlite3.Connection, ticker: str, value: float) -> int:
    """The append-and-supersede dance a real writer performs."""
    superseded = supersede_current(
        conn,
        table="runs",
        entity_where="ticker = :ticker AND COALESCE(segment_name,'') = ''",
        entity_params={"ticker": ticker},
    )
    cur = conn.execute(
        "INSERT INTO runs (ticker, value, is_latest) VALUES (?, ?, 1)", (ticker, value)
    )
    new_id = int(cur.lastrowid or 0)
    mark_superseded_by(conn, table="runs", superseded_ids=superseded, new_id=new_id)
    conn.commit()
    return new_id


def test_supersede_keeps_one_current_and_preserves_history() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_VERSIONED_DDL)
    v1 = _insert_run(conn, "RBRK", 91.0)
    v2 = _insert_run(conn, "RBRK", 66.45)
    # exactly one current row, and it's the newest
    current = conn.execute("SELECT id, value FROM runs WHERE is_latest=1").fetchall()
    assert current == [(v2, 66.45)]
    # the old version survives, stamped + back-linked to its successor
    old = conn.execute(
        "SELECT is_latest, superseded_at, superseded_by_id FROM runs WHERE id=?", (v1,)
    ).fetchone()
    assert old[0] == 0 and old[1] is not None and old[2] == v2
    # total history retained (nothing destroyed)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2


def test_first_version_supersedes_nothing() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_VERSIONED_DDL)
    superseded = supersede_current(
        conn,
        table="runs",
        entity_where="ticker = :ticker AND COALESCE(segment_name,'') = ''",
        entity_params={"ticker": "NEW"},
    )
    assert superseded == []
    mark_superseded_by(conn, table="runs", superseded_ids=[], new_id=999)  # no-op, no raise


def _seed_freshness(conn: sqlite3.Connection) -> None:
    conn.executescript(_FRESHNESS_VIEW_DDL)
    conn.executemany(
        "INSERT INTO v_decision_freshness (decision_id, ticker, decided_by, outcome_label, "
        "basis_kind, basis_value, current_value, valuation_superseded, basis_drift_pct, "
        "basis_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "RBRK", "advisor", "pending", "dcf", 91.0, 66.45, 1, -0.27, "superseded_material"),
            (2, "NU", "advisor", "pending", "dcf", 12.0, 12.4, 1, 0.033, "superseded_minor"),
            (
                3,
                "MELI",
                "advisor",
                "correct",
                "dcf",
                2000.0,
                1000.0,
                1,
                -0.5,
                "superseded_material",
            ),
            (4, "WIX", "advisor", "pending", "dcf", 90.0, 90.0, 0, 0.0, "fresh"),
            (5, "FLKR", "owner", "pending", None, None, None, 0, None, "unknown"),
        ],
    )
    conn.commit()


def test_stale_material_decisions_filters_and_defaults_pending() -> None:
    conn = sqlite3.connect(":memory:")
    _seed_freshness(conn)
    stale = stale_material_decisions(conn)  # pending_only=True by default
    ids = [d.decision_id for d in stale]
    assert ids == [1]  # only the pending, materially-superseded one (MELI is graded 'correct')
    # including graded rows surfaces MELI too, sorted by |drift| desc (0.5 before 0.27)
    all_material = stale_material_decisions(conn, pending_only=False)
    assert [d.decision_id for d in all_material] == [3, 1]


def test_decision_freshness_decodes_types_and_sorts_by_drift() -> None:
    conn = sqlite3.connect(":memory:")
    _seed_freshness(conn)
    rows = decision_freshness(conn)
    # sorted by absolute drift desc: MELI(.5), RBRK(.27), NU(.033), then null-drift rows
    assert [r.decision_id for r in rows[:3]] == [3, 1, 2]
    rbrk = next(r for r in rows if r.decision_id == 1)
    assert rbrk.basis_value == 91.0 and rbrk.current_value == 66.45
    assert rbrk.valuation_superseded is True
    assert rbrk.basis_drift_pct is not None and rbrk.basis_drift_pct < 0
    flkr = next(r for r in rows if r.decision_id == 5)
    assert flkr.basis_status == "unknown" and flkr.basis_value is None


def test_freshness_degrades_when_view_absent() -> None:
    conn = sqlite3.connect(":memory:")  # no view created
    assert decision_freshness(conn) == []
    assert stale_material_decisions(conn) == []
