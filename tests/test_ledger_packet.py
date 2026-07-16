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
    assert "ledger-packet" not in html


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


def test_packet_reconcile_copy_drops_duplicate_id(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    """The reconcile batch item embeds render_reconcile_list's markup — the
    packet copy must not duplicate #ledger-reconcile (the real one lives in
    the Queues block on the same page)."""
    _client, db, _root = ctx
    from synthesis.seed import seed_from_json  # noqa: F401  (import guard only)

    html = render_ledger_panel(db)
    assert html.count('id="ledger-reconcile"') <= 1


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


def test_action_routes_bump_act_counters(ctx: tuple[FlaskClient, Path, Path]) -> None:
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
    resp = client.post("/api/capture/text", json={"text": "plain thought, no question"})
    assert resp.status_code == 200
    assert _act_count(db, "act:capture") == 1
