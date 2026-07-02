"""The stale_valuation_basis standup watcher (PR2): a still-open recommendation
resting on a materially-superseded model-version auto-queues a re-review signal.

Runs against a stand-in ``v_decision_freshness`` table (the view's SQL is exercised
in test_alembic_provenance_freshness); this isolates the signal-building contract.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from standup.config import StandupConfig
from standup.signals import _stale_basis_signals, collect_signals

_VIEW_STANDIN = """
CREATE TABLE v_decision_freshness (
    decision_id INTEGER, ticker TEXT, recommendation_kind TEXT, decided_by TEXT,
    outcome_label TEXT, basis_kind TEXT, basis_ref_id INTEGER, basis_value REAL,
    basis_as_of TEXT, current_ref_id INTEGER, current_value REAL, current_as_of TEXT,
    valuation_superseded INTEGER, basis_drift_pct REAL, basis_status TEXT
);
"""

_NOW = datetime(2026, 7, 2, 12, 0, 0)


def _seed(conn: sqlite3.Connection) -> None:
    conn.executescript(_VIEW_STANDIN)
    conn.executemany(
        "INSERT INTO v_decision_freshness (decision_id, ticker, recommendation_kind, decided_by, "
        "outcome_label, basis_kind, basis_value, current_value, basis_as_of, current_as_of, "
        "valuation_superseded, basis_drift_pct, basis_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            # pending + materially superseded → a signal
            (
                76,
                "RBRK",
                "hold",
                "advisor",
                "pending",
                "dcf",
                91.0,
                66.45,
                "2026-07-01",
                "2026-07-02",
                1,
                -0.27,
                "superseded_material",
            ),
            # fresh → no signal
            (
                80,
                "WIX",
                "add",
                "advisor",
                "pending",
                "dcf",
                90.0,
                90.0,
                "2026-07-02",
                "2026-07-02",
                0,
                0.0,
                "fresh",
            ),
            # material but already graded → not pending → no signal
            (
                12,
                "MELI",
                "buy",
                "advisor",
                "correct",
                "dcf",
                2000.0,
                1000.0,
                "2026-01-01",
                "2026-07-02",
                1,
                -0.5,
                "superseded_material",
            ),
        ],
    )
    conn.commit()


def test_stale_basis_signal_fires_for_pending_material_only() -> None:
    conn = sqlite3.connect(":memory:")
    _seed(conn)
    sigs = _stale_basis_signals(conn, user_id="owner", now=_NOW, config=StandupConfig())
    assert len(sigs) == 1
    s = sigs[0]
    assert s.kind == "stale_valuation_basis"
    assert s.ticker == "RBRK"
    assert s.evidence["decision_id"] == 76
    assert s.evidence["basis_value"] == 91.0 and s.evidence["current_value"] == 66.45
    assert "re-review" in s.headline.lower()
    assert s.materiality > 0.4  # material drift lifts it above the floor


def test_stale_basis_signature_moves_with_new_model() -> None:
    """When a newer model supersedes again, current_as_of changes and the signal
    re-surfaces (a distinct dedup signature)."""
    conn = sqlite3.connect(":memory:")
    _seed(conn)
    s1 = _stale_basis_signals(conn, user_id="owner", now=_NOW, config=StandupConfig())[0]
    conn.execute("UPDATE v_decision_freshness SET current_as_of='2026-08-01' WHERE decision_id=76")
    s2 = _stale_basis_signals(conn, user_id="owner", now=_NOW, config=StandupConfig())[0]
    assert s1.signature != s2.signature


def test_stale_basis_degrades_without_view() -> None:
    conn = sqlite3.connect(":memory:")  # no v_decision_freshness
    assert _stale_basis_signals(conn, user_id="owner", now=_NOW, config=StandupConfig()) == []


def test_collect_signals_includes_stale_basis_and_never_raises() -> None:
    conn = sqlite3.connect(":memory:")
    _seed(conn)
    # collect_signals runs every watcher; the others degrade to [] on missing tables
    sigs = collect_signals(conn, user_id="owner", now=_NOW)
    kinds = {s.kind for s in sigs}
    assert "stale_valuation_basis" in kinds
