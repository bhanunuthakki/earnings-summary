"""Phase C — per-action instrumentation.

* every action route bumps a durable ``act:*`` row in
  ``panel_activation_counts`` so the next redesign argues from usage data.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import comments_server
import pytest
from flask.testing import FlaskClient

from user_state.notes import create_note

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ctx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> tuple[FlaskClient, Path, Path]:
    monkeypatch.setenv("LEDGER_ONMYMIND", "1")
    monkeypatch.setenv("LEDGER_WORLDVIEW", "1")
    db = tmp_path / "data" / "portfolio.db"
    migrated_db(db)
    client = comments_server.create_app(tmp_path).test_client()
    return client, db, tmp_path


# ---------------------------------------------------------------------------
# The packet
# ---------------------------------------------------------------------------


def _seed_unreconciled(db: Path, n: int) -> None:
    """N seed notes awaiting a verdict — list_unreconciled picks up any
    ``source_ref LIKE 'seed:%'`` note with no reconcile stamp yet.

    The ctx fixture stamps at 0059 then upgrades, so tables created in
    migrations <=0059 (``decisions``) never exist — ``list_unreconciled`` also
    queries ``decisions`` and would raise (masking the note rows), so a minimal
    stand-in (zero inferred falsifiers) is created first."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS decisions "
            "(id INTEGER PRIMARY KEY, ticker TEXT, falsifier TEXT, decided_by TEXT)"
        )
        conn.commit()
    finally:
        conn.close()
    for i in range(1, n + 1):
        create_note(
            body=f"seed musing {i} — do I still believe this?",
            kind="musing",
            ticker=None,
            source="capture",
            source_ref=f"seed:musing:{i}",
            db_path=db,
        )


# ---------------------------------------------------------------------------
# Instrumentation
# ---------------------------------------------------------------------------


def _act_count(db: Path, panel_id: str) -> int:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT SUM(count) FROM panel_activation_counts WHERE panel_id = ?", (panel_id,)
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def test_action_routes_bump_act_counters(
    ctx: tuple[FlaskClient, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db, _root = ctx
    row = create_note(
        body="a musing",
        kind="musing",
        ticker=None,
        source="capture",
        context={"channel": "tray"},
        db_path=db,
    )
    assert client.post(f"/api/onmymind/{row.id}/save", json={}).status_code == 404
    assert _act_count(db, "act:om:save") == 0
    # This test owns activation counters, not the async answer tap. Keep the
    # capture deterministic so no daemon LLM worker outlives its monkeypatches
    # and races a later test's transport stub.
    monkeypatch.setenv("LEDGER_ANSWER", "0")
    resp = client.post("/api/capture/text", json={"text": "plain thought, no question"})
    assert resp.status_code == 200
    assert "answering" not in resp.get_json()
    assert _act_count(db, "act:capture") == 1
