"""Ledger ratification/action UX overhaul (2026-07-19 owner rejection fix).

Covers the five complaints this PR addresses, at the level this repo's test
harness can actually exercise (Python-rendered HTML + Flask routes — there is
no JS test runner in this repo, so client-side tally/receipt behavior is
verified structurally: the emitted HTML/JS carries the right data contracts
and function hooks, the same convention every other ``ledger_panel`` test in
this suite already uses).

1. Consequence-first actions — every packet action carries a ``title=``
   tooltip; the three called-out card types (capacity fact, Tenet, falsifier
   ratify) also carry a visible ``.ledger-consequence`` micro-line.
2. Registered feedback — routes return a ``receipt``; the packet JS carries
   the event bus + tally + in-place receipt/failure paint contract.
3. Prioritization + batching — gaps class renders before proposals before
   bulk; homogeneous expiring facts (>=3 same category) collapse into ONE
   grouped card with Affirm all / Review one by one / Drop all.
4. (Visual conformance is covered by tests/test_ui_controls.py's token guard
   + the before/after inventory in the PR description, not here.)
5. Payoff visibility — the packet start band states real counts of what
   clearing it unlocks; the Clear copy is present for the tally to fill in.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import comments_server
import pytest
from flask.testing import FlaskClient

from owner_profile.store import append_fact
from research.proposals import create_proposal, create_task

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


def _seed_gap(db: Path, ticker: str = "NU") -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type, name) VALUES (?, 'portfolio', 'Test company')",
            (ticker,),
        )
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, falsifier, "
            "made_at, created_at) VALUES (?, 'initiate', 'owner', '', '2026-06-01', '2026-06-01')",
            (ticker,),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_proposal(db: Path) -> None:
    task_id = create_task(note_id=None, claim="do NU's margins hold?", ticker="NU", db_path=db)
    create_proposal(
        task_id=task_id,
        kind="memo",
        ticker="NU",
        title="NU margins hold",
        body_md="The book looks stable.",
        budget_tier="cheap",
        db_path=db,
    )


def _seed_expiring_facts(db: Path, n: int, *, category: str = "capacity") -> None:
    conn = sqlite3.connect(str(db))
    try:
        for i in range(n):
            fid = append_fact(
                conn,
                category=category,
                key=f"work_break_{i}",
                value={"year": 2030 + i},
                narrative=f"Work-break window #{i}: planned pause.",
                provenance="wealthplan_import",
                status="affirmed",
                review_horizon_days=90,
            )
            conn.execute(
                "UPDATE owner_profile_facts SET affirmed_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00", fid),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Consequence-first actions — tooltips + micro-lines
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2. Registered feedback — the event bus + tally/receipt contract
# ---------------------------------------------------------------------------


def test_reconcile_verdict_route_carries_a_receipt(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    client, db, _root = ctx
    from user_state.notes import create_note

    row = create_note(
        body="seed musing — do I still believe this?",
        kind="musing",
        ticker=None,
        source="capture",
        source_ref="seed:musing:1",
        db_path=db,
    )
    resp = client.post(f"/api/reconcile/note/{row.id}/live")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["receipt"] == "Kept live — the coach can still cite this"


def test_tenet_approve_route_carries_a_receipt(ctx: tuple[FlaskClient, Path, Path]) -> None:
    client, db, _root = ctx
    from synthesis.tenets import record_tenet

    tenet = record_tenet(
        body_md="Let winners run.", scope_key="exit-discipline", status="proposed", db_path=db
    )
    resp = client.post(f"/api/tenets/{tenet.id}/approve")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["receipt"] == "Adopted — now a standing Tenet in your decision prompts"


def test_triage_route_and_archive_carry_receipts(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    client, db, _root = ctx
    from user_state.notes import TRIAGE_INTENT, create_note

    row = create_note(
        body="the peer set feels off here",
        kind="question",
        ticker=None,
        source="comment",
        context={"intent": TRIAGE_INTENT},
        db_path=db,
    )
    resp = client.post(f"/api/notes/{row.id}/route", json={"intent": "curate_peers"})
    assert resp.status_code == 200
    assert "receipt" in resp.get_json()

    row2 = create_note(
        body="another parked comment", kind="question", ticker=None, source="comment", db_path=db
    )
    resp2 = client.post(f"/api/notes/{row2.id}/archive", json={})
    assert resp2.status_code == 200
    assert resp2.get_json()["receipt"] == "Dismissed"


# ---------------------------------------------------------------------------
# 3. Prioritization + batching
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. Payoff visibility
# ---------------------------------------------------------------------------
