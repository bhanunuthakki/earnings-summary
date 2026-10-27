"""Consequence receipts and falsifier routes against the current database schema."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import comments_server
import pytest
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    migrated_db(db)
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
            "INSERT OR IGNORE INTO tracked_companies (ticker, name, list_type) VALUES (?, 'Test company', 'portfolio')",
            (ticker,),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid
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
