"""Route-level tests for the consequence-receipts PR: the ratify route's
receipt (armed vs queued-for-arming), and the Ledger panel's armed-falsifiers
table. ``decisions`` predates the 0059 stamp (``db.init_db()`` territory) and
the 0059-fixture upgrade never creates it (the 0086/0130 migrations only
ALTER it when present) — hand-built here post-upgrade, per the
tests/test_open_loops.py ``_DECISIONS_DDL`` pattern, extended with the
0086 falsifiable-conditions columns + 0130's owner-decision columns this PR's
routes actually read.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

PRIOR_HEAD = "0059_kpi_facts_restatement"

_DECISIONS_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    recommendation_value FLOAT,
    conviction VARCHAR(16),
    source_lens VARCHAR(64),
    outcome_label VARCHAR(16) NOT NULL DEFAULT 'pending',
    outcome_at DATETIME,
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    scope VARCHAR(16) NOT NULL DEFAULT 'ticker',
    falsifier TEXT,
    size_usd FLOAT,
    user_notes TEXT,
    decision_conditions TEXT,
    conditions_extracted_at DATETIME,
    made_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE TABLE tracked_companies (
    ticker TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    list_type TEXT NOT NULL,
    archived_at TEXT
);
"""


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_DECISIONS_DDL)
        conn.commit()
    finally:
        conn.close()
    return db


@pytest.fixture
def client(db_path: Path, tmp_path: Path) -> FlaskClient:
    assert db_path.exists()
    return comments_server.create_app(tmp_path).test_client()


def _seed_owner_decision(
    db_path: Path,
    *,
    ticker: str = "NU",
    falsifier: str = "15-90d NPL >5% for 2Q (inferred)",
    decision_conditions: str | None = None,
    conditions_extracted_at: str | None = None,
) -> int:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, falsifier, "
            "decision_conditions, conditions_extracted_at, made_at, created_at) "
            "VALUES (?, 'add', 'owner', ?, ?, ?, '2026-06-01T00:00:00', '2026-06-01T00:00:00')",
            (ticker, falsifier, decision_conditions, conditions_extracted_at),
        )
        conn.execute(
            "INSERT OR IGNORE INTO tracked_companies (ticker, list_type) VALUES (?, 'portfolio')",
            (ticker,),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# POST /api/reconcile/falsifier/<id> — ratify receipt (0142)
# ----------------------------------------------------------------------------


def test_ratify_receipt_queued_when_never_extracted(client: FlaskClient, db_path: Path) -> None:
    decision_id = _seed_owner_decision(db_path)
    resp = client.post(
        f"/api/reconcile/falsifier/{decision_id}",
        json={"action": "ratify"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["receipt"] == "ratified — queued for arming (next extraction pass)"


def test_ratify_receipt_armed_when_already_extracted(client: FlaskClient, db_path: Path) -> None:
    import json

    conditions = json.dumps(
        [
            {
                "metric": "NPL 90d+",
                "metric_source": "kpi",
                "op": "gt",
                "threshold": 5.0,
                "unit": "percent",
                "for_periods": 2,
                "note": "15-90d NPL >5% for 2Q",
            }
        ]
    )
    decision_id = _seed_owner_decision(
        db_path,
        decision_conditions=conditions,
        conditions_extracted_at="2026-06-15T00:00:00",
    )
    resp = client.post(
        f"/api/reconcile/falsifier/{decision_id}",
        json={"action": "ratify"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["receipt"] == "armed — now watched by the tripwire engine"


def test_edit_and_drop_actions_carry_their_own_receipt(client: FlaskClient, db_path: Path) -> None:
    """Ledger UX overhaul (requirement B — registered feedback): every
    falsifier action now carries SOME receipt so the UI always has something
    to paint, not just 'ratify'. edit/drop get a plain confirmation distinct
    from ratify's arming-status receipt."""
    decision_id = _seed_owner_decision(db_path)
    resp = client.post(
        f"/api/reconcile/falsifier/{decision_id}",
        json={"action": "edit", "text": "My own words"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["receipt"] == "Saved — falsifier rewritten in your words"

    decision_id2 = _seed_owner_decision(db_path)
    resp2 = client.post(f"/api/reconcile/falsifier/{decision_id2}", json={"action": "drop"})
    assert resp2.status_code == 200
    assert resp2.get_json()["receipt"] == "Dropped — no tripwire will watch this decision"


def test_ratify_unknown_decision_404s(client: FlaskClient) -> None:
    resp = client.post("/api/reconcile/falsifier/424242", json={"action": "ratify"})
    assert resp.status_code == 404


# ----------------------------------------------------------------------------
# Armed-falsifiers table (Ledger > Reconcile section)
# ----------------------------------------------------------------------------


def test_armed_table_renders_row_for_ratified_armed_falsifier(db_path: Path) -> None:
    import json

    from pipeline.ledger_panel import render_armed_falsifiers_table

    conditions = json.dumps(
        [
            {
                "metric": "NPL 90d+",
                "metric_source": "kpi",
                "op": "gt",
                "threshold": 5.0,
                "unit": "percent",
                "for_periods": 2,
                "note": "15-90d NPL >5% for 2Q",
            }
        ]
    )
    _seed_owner_decision(
        db_path,
        ticker="NU",
        decision_conditions=conditions,
        conditions_extracted_at="2026-06-15T00:00:00",
    )
    html = render_armed_falsifiers_table(db_path)
    assert "Armed falsifiers (1)" in html
    assert "NU" in html
    assert "15-90d NPL &gt;5% for 2Q" in html or "15-90d NPL >5% for 2Q" in html
    assert 'href="/#decisions_record"' in html


def test_armed_table_absent_when_no_conditions(db_path: Path) -> None:
    from pipeline.ledger_panel import render_armed_falsifiers_table

    _seed_owner_decision(db_path)  # falsifier present but no conditions extracted yet
    assert render_armed_falsifiers_table(db_path) == ""


def test_armed_table_absent_on_empty_db(db_path: Path) -> None:
    from pipeline.ledger_panel import render_armed_falsifiers_table

    assert render_armed_falsifiers_table(db_path) == ""
