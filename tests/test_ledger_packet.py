"""Phase C — the bounded packet walk, the research→Telegram push-back, and
the per-action instrumentation.

* ``_packet_section``: a finite "N need you" walk over research proposals,
  proposed Tenets, triage suggestions, and the reconcile queue — reusing each
  section's own card builders; absent entirely when nothing needs the owner.
* a WEB-initiated research run pushes its drafted proposal card to the
  owner's Telegram thread (best-effort; missing token/chat skips quietly).
* every action route bumps a durable ``act:*`` row in
  ``panel_activation_counts`` so the next redesign argues from usage data.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from pipeline.ledger_panel import render_ledger_panel  # noqa: E402
from research.proposals import create_proposal, create_task  # noqa: E402
from user_state.notes import TRIAGE_INTENT, create_note, patch_note_context  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_db(db_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FlaskClient, Path, Path]:
    monkeypatch.setenv("LEDGER_RESEARCH_TAP", "0")
    monkeypatch.setenv("LEDGER_ONMYMIND", "1")
    monkeypatch.setenv("LEDGER_WORLDVIEW", "1")
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)
    client = comments_server.create_app(tmp_path).test_client()
    return client, db, tmp_path


def _seed_proposal(db: Path) -> int:
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
    return task_id


def _seed_triage_suggestion(db: Path) -> int:
    row = create_note(
        body="the peer set feels off here",
        kind="question",
        ticker=None,
        source="comment",
        source_ref="T/x1",
        context={"intent": TRIAGE_INTENT},
        db_path=db,
    )
    patch_note_context(
        row.id, {"route_suggestion": {"intent": "curate_peers", "reason": "peers"}}, db_path=db
    )
    return row.id


# ---------------------------------------------------------------------------
# The packet
# ---------------------------------------------------------------------------


def test_packet_absent_when_nothing_needs_you(ctx: tuple[FlaskClient, Path, Path]) -> None:
    _client, db, _root = ctx
    html = render_ledger_panel(db)
    assert 'id="ledger-packet"' not in html


def test_packet_walks_everything_awaiting_a_verdict(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    _client, db, _root = ctx
    _seed_proposal(db)
    _seed_triage_suggestion(db)
    html = render_ledger_panel(db)
    assert 'id="ledger-packet"' in html
    assert "2 need you" in html
    assert html.count('class="pk-item"') == 2
    assert "data-pk-start" in html and "data-pk-skip" in html
    assert "Clear" in html
    # The proposal item reuses the research inbox card (its verbs included);
    # the triage item carries packet-scoped hooks.
    assert 'data-verb="approve"' in html
    assert "data-pk-route" in html and 'data-intent="curate_peers"' in html
    assert "data-pk-dismiss" in html


def test_packet_singular_copy(ctx: tuple[FlaskClient, Path, Path]) -> None:
    _client, db, _root = ctx
    _seed_proposal(db)
    html = render_ledger_panel(db)
    assert "1 needs you" in html


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


def test_packet_reconcile_copy_drops_duplicate_id(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    """Each reconcile row is its OWN pk-item — the packet must not embed the
    #ledger-reconcile container (the real one lives in the Queues block on the
    same page), and N unreconciled rows must count as N items, not 1 (the old
    single-blob append made the first row-action's settle-detector skip the
    rest of the batch)."""
    from pipeline.ledger_panel import (
        _packet_section,  # pyright: ignore[reportPrivateUsage]
        _reconcile_packet_items,  # pyright: ignore[reportPrivateUsage]
        render_reconcile_list,
    )
    from synthesis.reconcile import list_unreconciled

    _client, db, _root = ctx
    _seed_unreconciled(db, 2)
    assert len(list_unreconciled(db)) == 2

    # The reconcile queue becomes two SEPARATE packet fragments, each carrying
    # its own action hooks and NONE the #ledger-reconcile container id.
    frags = _reconcile_packet_items(db)
    assert len(frags) == 2
    assert all('id="ledger-reconcile"' not in f for f in frags)

    packet = _packet_section(db)
    # Two reconcile rows → two pk-items (was one blob before the fix).
    assert packet.count('class="pk-item"') == 2
    assert "2 need you" in packet
    # The container id lives ONLY in the Queues block, never in the packet copy.
    assert 'id="ledger-reconcile"' not in packet

    # Whole-panel invariant: at most one #ledger-reconcile across the page.
    html = render_ledger_panel(db)
    assert html.count('id="ledger-reconcile"') == 1

    # Regression: render_reconcile_list's own output is UNCHANGED — one
    # #ledger-reconcile div carrying both rows' verdict buttons (the Queues-block
    # / ?fragment=reconcile contract _RECONCILE_JS reload() swaps).
    standalone = render_reconcile_list(db)
    assert standalone.count('id="ledger-reconcile"') == 1
    assert standalone.startswith('<div id="ledger-reconcile">')
    assert standalone.endswith("</div>")
    # 2 seed notes x the 4 reconcile verdicts = 8 verdict buttons, all inside
    # the single container.
    assert standalone.count("data-rec-verdict=") == 8


# ---------------------------------------------------------------------------
# Research run → Telegram push-back
# ---------------------------------------------------------------------------


def test_web_research_run_pushes_drafted_proposal_to_telegram(
    ctx: tuple[FlaskClient, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db, _root = ctx
    monkeypatch.setenv("LEDGER_RESEARCH_RUN", "1")
    task_id = _seed_proposal(db)  # its pending proposal doubles as the "draft"
    import research.run as run_mod
    from capture import research_notify, token_store
    from research.proposals import list_proposals

    pid = list_proposals(status="pending", db_path=db)[0].id
    sent = threading.Event()
    calls: list[tuple[str, int, int]] = []

    monkeypatch.setattr(run_mod, "run_research_task", lambda tid, **kw: pid)
    monkeypatch.setattr(token_store, "load_token", lambda p=None: "tok")
    monkeypatch.setattr(token_store, "load_chat_id", lambda p=None: 777)

    def _fake_send(token: str, chat_id: int, proposal: object, **kw: object) -> None:
        calls.append((token, chat_id, getattr(proposal, "id", -1)))
        sent.set()

    monkeypatch.setattr(research_notify, "send_proposal_card", _fake_send)
    resp = client.post(f"/api/research/task/{task_id}/run")
    assert resp.get_json() == {"started": True}
    assert sent.wait(timeout=5), "telegram push never fired"
    assert calls == [("tok", 777, pid)]


def test_web_research_run_survives_missing_telegram_setup(
    ctx: tuple[FlaskClient, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db, _root = ctx
    monkeypatch.setenv("LEDGER_RESEARCH_RUN", "1")
    task_id = _seed_proposal(db)
    import research.run as run_mod

    ran = threading.Event()

    def _fake_run(tid: int, **kw: object) -> int:
        ran.set()
        return 4242

    monkeypatch.setattr(run_mod, "run_research_task", _fake_run)
    # No token file exists under the tmp repo_root — the push path must skip
    # quietly (load_token raises CaptureSetupError inside the thread).
    resp = client.post(f"/api/research/task/{task_id}/run")
    assert resp.status_code == 200
    assert ran.wait(timeout=5)


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
    assert client.post(f"/api/onmymind/{row.id}/save", json={}).status_code == 200
    assert _act_count(db, "act:om:save") == 1
    # This test owns activation counters, not the async answer tap. Keep the
    # capture deterministic so no daemon LLM worker outlives its monkeypatches
    # and races a later test's transport stub.
    monkeypatch.setenv("LEDGER_ANSWER", "0")
    resp = client.post("/api/capture/text", json={"text": "plain thought, no question"})
    assert resp.status_code == 200
    assert resp.get_json()["answering"] is False
    assert _act_count(db, "act:capture") == 1
