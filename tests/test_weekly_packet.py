"""The Sunday packet (PR2, Deliverable 1) — assembly, idempotent resumption,
delivery + predraft degrade, verdict dispatch, and the completion receipt."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from capture import pending_replies, research_notify, telegram
from pipeline import weekly_packet as wp

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# A self-contained schema (mirrors tests/test_standup.py's own pattern) rather
# than an alembic replay: `decisions` predates this repo's alembic history
# (created pre-0001, outside migration tracking — see 0046_decisions.py, which
# only EXTENDS it), so a stamp-then-upgrade harness starting mid-chain can
# never produce it. analyst_notes/insight_notes/research_proposals columns
# mirror the real production schema (verified via PRAGMA table_info against
# data/portfolio.db); weekly_packet_*/decision_nudges/pending_telegram_replies
# mirror alembic/versions/0149_weekly_packet_and_nudges.py exactly.
_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    recommendation_kind TEXT,
    conviction TEXT,
    falsifier TEXT,
    decided_by TEXT,
    outcome_label TEXT,
    made_at TEXT,
    user_notes TEXT DEFAULT '',
    created_at TEXT
);
CREATE TABLE analyst_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    body TEXT NOT NULL,
    anchor_type TEXT,
    anchor_key TEXT,
    source TEXT,
    source_ref TEXT,
    supersedes_id INTEGER,
    resolution_note TEXT,
    context_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    decision_id INTEGER,
    position_entry_id INTEGER,
    link_auto_resolve INTEGER,
    fact_ref TEXT
);
CREATE TABLE insight_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    body_md TEXT NOT NULL,
    source_note_ids TEXT,
    meta_json TEXT,
    as_of TEXT,
    window_start TEXT,
    window_end TEXT,
    watermark_id INTEGER,
    supersedes_id INTEGER,
    status TEXT NOT NULL DEFAULT 'current',
    esi REAL,
    provenance TEXT NOT NULL DEFAULT 'derived',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE research_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    kind TEXT NOT NULL,
    ticker TEXT,
    title TEXT NOT NULL,
    body_md TEXT,
    evidence_json TEXT,
    source_note_ids TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    adversarial_verdict TEXT,
    budget_tier TEXT,
    provenance TEXT NOT NULL DEFAULT 'derived',
    tainted_by_proposal_id INTEGER,
    artifact_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE research_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER,
    claim TEXT NOT NULL,
    ticker TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',
    budget_tier TEXT,
    budget_usd REAL,
    adversarial_verdict TEXT,
    estimated_cost_usd REAL,
    task_metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE weekly_packet_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iso_year INTEGER NOT NULL,
    iso_week INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    total_items INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (iso_year, iso_week)
);
CREATE TABLE weekly_packet_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES weekly_packet_runs(id),
    item_kind TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    ticker TEXT,
    title TEXT NOT NULL,
    predraft TEXT,
    message_id INTEGER,
    sent_at TEXT,
    verdict TEXT,
    acted_at TEXT,
    order_index INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, item_kind, ref_id)
);
CREATE TABLE decision_nudges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL UNIQUE,
    chat_id INTEGER,
    status TEXT NOT NULL DEFAULT 'sent',
    sent_at TEXT NOT NULL,
    awaiting_until TEXT,
    filled_at TEXT
);
CREATE TABLE pending_telegram_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);
"""


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return db


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _seed_reconcile_note(db: Path) -> int:
    conn = _conn(db)
    try:
        cur = conn.execute(
            "INSERT INTO analyst_notes (user_id, ticker, kind, status, body, source, "
            "source_ref, created_at, updated_at) VALUES "
            "('bhanu', 'NU', 'intent', 'open', 'NVDA-LEAP thesis note', 'manual', "
            "'seed:1', '2026-06-01', '2026-06-01')"
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _seed_theme(db: Path) -> int:
    conn = _conn(db)
    try:
        cur = conn.execute(
            "INSERT INTO insight_notes (scope_key, kind, body_md, source_note_ids, status, "
            "provenance, created_at, updated_at) VALUES "
            "('theme:margins', 'theme', 'Margins are the standing worry.', '[]', 'current', "
            "'derived', '2026-06-01', '2026-06-01')"
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _seed_tenet(db: Path) -> int:
    conn = _conn(db)
    try:
        cur = conn.execute(
            "INSERT INTO insight_notes (scope_key, kind, body_md, source_note_ids, status, "
            "provenance, created_at, updated_at) VALUES "
            "('tenet:size-into-drawdowns', 'tenet', 'I size into drawdowns, not rallies.', "
            "'[]', 'proposed', 'derived', '2026-06-01', '2026-06-01')"
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _seed_falsifier(db: Path) -> int:
    conn = _conn(db)
    try:
        cur = conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, made_at, "
            "falsifier, outcome_label, created_at) VALUES "
            "('NU', 'add', 'owner', '2026-06-01', 'NPL rises above 7% (inferred)', "
            "'pending', '2026-06-01')"
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _seed_proposal(db: Path) -> int:
    from research.proposals import create_proposal

    return create_proposal(
        task_id=None,
        kind="memo",
        ticker="MELI",
        title="MELI margin memo",
        body_md="body",
        db_path=db,
    )


def _seed_decision_stub(db: Path) -> int:
    conn = _conn(db)
    try:
        cur = conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, made_at, "
            "outcome_label, created_at) VALUES "
            "('BKNG', 'add', 'owner', '2026-07-06', 'pending', '2026-07-06')"
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


class _Spy:
    def __init__(self, message_id_start: int = 100) -> None:
        self.sends: list[tuple[int, str, object]] = []
        self._next_mid = message_id_start

    def send(self, token: str, chat_id: int, text: str, *, reply_markup: object = None) -> object:
        self.sends.append((chat_id, text, reply_markup))
        mid = self._next_mid
        self._next_mid += 1
        return {"message_id": mid}


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def test_assemble_packet_collects_every_kind(db_path: Path) -> None:
    _seed_reconcile_note(db_path)
    _seed_theme(db_path)
    _seed_falsifier(db_path)
    _seed_tenet(db_path)
    _seed_proposal(db_path)
    _seed_decision_stub(db_path)

    plans = wp.assemble_packet(db_path=db_path)
    kinds = {p.item_kind for p in plans}
    assert kinds == {
        "reconcile_note",
        "reconcile_theme",
        "reconcile_falsifier",
        "tenet",
        "proposal",
        "decision_stub",
    }


def test_assemble_packet_empty_on_a_clean_book(db_path: Path) -> None:
    assert wp.assemble_packet(db_path=db_path) == []


# --------------------------------------------------------------------------
# Delivery + idempotent resumption
# --------------------------------------------------------------------------


def test_send_packet_sends_ritual_clear_when_empty(db_path: Path) -> None:
    spy = _Spy()
    report = wp.send_packet("tok", 5, db_path=db_path, send=spy.send)
    assert report.cleared is True
    assert len(spy.sends) == 1
    assert "Ritual clear" in spy.sends[0][1]

    run = wp.get_run(report.run_id, db_path=db_path)
    assert run is not None and run.status == "clear"


def test_send_packet_header_then_one_card_per_item(db_path: Path) -> None:
    _seed_reconcile_note(db_path)
    _seed_decision_stub(db_path)
    spy = _Spy()
    report = wp.send_packet("tok", 5, db_path=db_path, send=spy.send)

    assert report.total_items == 2
    assert report.items_sent == 2
    # header + 2 item cards
    assert len(spy.sends) == 3
    assert "Sunday packet: 2 items" in spy.sends[0][1]
    items = wp.list_items(report.run_id, db_path=db_path)
    assert all(it.sent_at is not None and it.message_id is not None for it in items)
    # decision_stub gets [Fill in now / Defer] only
    stub = next(it for it in items if it.item_kind == "decision_stub")
    stub_kb = str(spy.sends[[i.id for i in items].index(stub.id) + 1][2])
    assert f"wk:fillin:{stub.id}" in stub_kb and f"wk:accept:{stub.id}" not in stub_kb
    # the reconcile item gets the full 4-button set
    note_item = next(it for it in items if it.item_kind == "reconcile_note")
    note_kb = str(spy.sends[[i.id for i in items].index(note_item.id) + 1][2])
    assert f"wk:accept:{note_item.id}" in note_kb and f"wk:rewrite:{note_item.id}" in note_kb


def test_send_packet_same_week_is_idempotent_and_additive(db_path: Path) -> None:
    _seed_reconcile_note(db_path)
    spy1 = _Spy()
    report1 = wp.send_packet("tok", 5, db_path=db_path, send=spy1.send)
    assert report1.items_sent == 1

    # A second run finds nothing NEW — no re-send of the already-delivered card.
    spy2 = _Spy()
    report2 = wp.send_packet("tok", 5, db_path=db_path, send=spy2.send)
    assert report2.run_id == report1.run_id
    assert report2.items_sent == 0
    assert spy2.sends == []

    # A newly-surfaced item is ADDED to the same run, not duplicated.
    _seed_decision_stub(db_path)
    spy3 = _Spy()
    report3 = wp.send_packet("tok", 5, db_path=db_path, send=spy3.send)
    assert report3.run_id == report1.run_id
    assert report3.total_items == 2
    assert report3.items_sent == 1  # only the new item is sent
    items = wp.list_items(report1.run_id, db_path=db_path)
    assert len(items) == 2


def test_send_packet_predraft_degrades_per_item(db_path: Path) -> None:
    _seed_reconcile_note(db_path)
    _seed_decision_stub(db_path)

    def flaky_call(prompt: str, **kw: object) -> dict[str, object]:
        if "missing conviction" in prompt:
            raise RuntimeError("claude cli exit 1")
        return {"draft": "ratify - the falsifier still matches the position"}

    spy = _Spy()
    report = wp.send_packet("tok", 5, db_path=db_path, send=spy.send, predraft_call=flaky_call)
    assert report.predrafted == 1
    assert report.degraded == 1
    items = wp.list_items(report.run_id, db_path=db_path)
    with_draft = next(it for it in items if it.item_kind == "reconcile_note")
    without_draft = next(it for it in items if it.item_kind == "decision_stub")
    assert with_draft.predraft and "ratify" in with_draft.predraft
    assert without_draft.predraft is None
    # both items still shipped with buttons regardless of the predraft outcome
    assert len(spy.sends) == 3  # header + 2 cards


# --------------------------------------------------------------------------
# B7 — expiring research (kill silent expiry)
# --------------------------------------------------------------------------


def _seed_research_task(
    db: Path,
    *,
    claim: str = "do NU margins hold?",
    ticker: str | None = "NU",
    status: str = "proposed",
    created_at: str = "2020-01-01T00:00:00",
    estimated_cost_usd: float | None = None,
    metadata_json: str | None = None,
) -> int:
    """A task old enough (2020) to be past ANY warn_days threshold — avoids
    the fragility of computing a relative cutoff against "now" in the test."""
    conn = _conn(db)
    try:
        cur = conn.execute(
            "INSERT INTO research_tasks (claim, ticker, status, estimated_cost_usd, "
            "task_metadata_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (claim, ticker, status, estimated_cost_usd, metadata_json, created_at, created_at),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_expiring_research_tasks_picks_up_old_proposed_only(db_path: Path) -> None:
    from research.proposals import create_task

    old_id = _seed_research_task(db_path)
    fresh_id = create_task(note_id=None, claim="fresh", ticker="MELI", db_path=db_path)
    dropped_id = _seed_research_task(db_path, claim="dropped", status="expired")

    tasks = wp._expiring_research_tasks(  # pyright: ignore[reportPrivateUsage]
        warn_days=7, db_path=db_path
    )
    ids = {t.id for t in tasks}
    assert old_id in ids
    assert fresh_id not in ids
    assert dropped_id not in ids


def test_send_packet_delivers_expiring_research_card_and_stamps_task(db_path: Path) -> None:
    tid = _seed_research_task(db_path, claim="do NU margins hold?", ticker="NU")
    spy = _Spy()
    report = wp.send_packet("tok", 5, db_path=db_path, send=spy.send)

    assert report.expiring_research_sent == 1
    assert report.cleared is False  # a card WAS sent; must not read as "Ritual clear"
    assert not any("Ritual clear" in s[1] for s in spy.sends)
    card = next(s for s in spy.sends if "Expiring research" in s[1])
    assert "NU - do NU margins hold?" in card[1]
    assert "unanswered" not in card[1]  # first appearance -> no age note
    assert f"rx:run:{tid}" in str(card[2])
    assert f"rx:session:{tid}" in str(card[2])
    assert f"rx:drop:{tid}" in str(card[2])

    from research.proposals import get_task

    task = get_task(tid, db_path=db_path)
    assert task is not None
    assert task.metadata.get("unanswered_weeks") == 1
    assert task.metadata.get("packeted_at")
    assert task.estimated_cost_usd is not None and task.estimated_cost_usd > 0


def test_send_packet_expiring_research_not_resent_same_week(db_path: Path) -> None:
    _seed_research_task(db_path)
    spy1 = _Spy()
    report1 = wp.send_packet("tok", 5, db_path=db_path, send=spy1.send)
    assert report1.expiring_research_sent == 1

    spy2 = _Spy()
    report2 = wp.send_packet("tok", 5, db_path=db_path, send=spy2.send)
    assert report2.expiring_research_sent == 0
    assert not any("Expiring research" in s[1] for s in spy2.sends)


def test_send_packet_expiring_research_resurfaces_next_week_with_incremented_count(
    db_path: Path,
) -> None:
    tid = _seed_research_task(db_path)
    spy1 = _Spy()
    wp.send_packet("tok", 5, db_path=db_path, send=spy1.send)

    # Simulate the task having been packeted in a PRIOR ISO week (not this
    # one) — the per-week idempotency guard must not suppress a resend.
    from research.proposals import get_task, set_task_extras

    set_task_extras(tid, packeted_at="2020-01-08T00:00:00", db_path=db_path)

    spy2 = _Spy()
    report2 = wp.send_packet("tok", 5, db_path=db_path, send=spy2.send)
    assert report2.expiring_research_sent == 1
    card = next(s for s in spy2.sends if "Expiring research" in s[1])
    assert "unanswered 2 weeks" in card[1]

    task = get_task(tid, db_path=db_path)
    assert task is not None and task.metadata.get("unanswered_weeks") == 2


def test_send_packet_with_only_expiring_research_and_no_other_items(db_path: Path) -> None:
    """Every OTHER substrate is empty, but a research task IS expiring — the
    'Ritual clear' message (meant for a genuinely empty packet) must be
    suppressed, since it would directly contradict the card just sent."""
    _seed_research_task(db_path)
    spy = _Spy()
    report = wp.send_packet("tok", 5, db_path=db_path, send=spy.send)
    assert report.expiring_research_sent == 1
    assert report.cleared is False
    assert not any("Ritual clear" in s[1] for s in spy.sends)
    run = wp.get_run(report.run_id, db_path=db_path)
    assert run is not None and run.status != "clear"


# --------------------------------------------------------------------------
# Verdict dispatch — the packet's action core
# --------------------------------------------------------------------------


def test_apply_verdict_accept_ratifies_reconcile_note(db_path: Path) -> None:
    note_id = _seed_reconcile_note(db_path)
    run = wp.ensure_run(db_path=db_path)
    items = wp.sync_items(
        run.id, [wp.PacketItemPlan("reconcile_note", note_id, "NU", "x")], db_path=db_path
    )
    result = wp.apply_verdict(items[0].id, "accept", chat_id=5, db_path=db_path)
    assert result == "accept"

    from synthesis.reconcile import list_unreconciled

    assert list_unreconciled(db_path) == []  # ratified -> no longer unreconciled


def test_apply_verdict_drop_rejects_tenet(db_path: Path) -> None:
    tenet_id = _seed_tenet(db_path)
    run = wp.ensure_run(db_path=db_path)
    items = wp.sync_items(
        run.id, [wp.PacketItemPlan("tenet", tenet_id, None, "x")], db_path=db_path
    )
    result = wp.apply_verdict(items[0].id, "drop", chat_id=5, db_path=db_path)
    assert result == "drop"

    from synthesis.tenets import list_tenets

    assert list_tenets(status="proposed", db_path=db_path) == []
    rejected = list_tenets(status="rejected", db_path=db_path)
    assert len(rejected) == 1 and rejected[0].id == tenet_id


def test_apply_verdict_defer_writes_nothing_to_substrate(db_path: Path) -> None:
    prop_id = _seed_proposal(db_path)
    run = wp.ensure_run(db_path=db_path)
    items = wp.sync_items(
        run.id, [wp.PacketItemPlan("proposal", prop_id, "MELI", "x")], db_path=db_path
    )
    result = wp.apply_verdict(items[0].id, "defer", chat_id=5, db_path=db_path)
    assert result == "defer"

    from research.proposals import get_proposal

    assert get_proposal(prop_id, db_path=db_path).status == "pending"  # untouched
    item = wp.get_item(items[0].id, db_path=db_path)
    assert item is not None and item.verdict == "defer"


def test_apply_verdict_rewrite_stashes_await_and_reply_edits_falsifier(db_path: Path) -> None:
    decision_id = _seed_falsifier(db_path)
    run = wp.ensure_run(db_path=db_path)
    items = wp.sync_items(
        run.id, [wp.PacketItemPlan("reconcile_falsifier", decision_id, "NU", "x")], db_path=db_path
    )
    item_id = items[0].id
    result = wp.apply_verdict(item_id, "rewrite", chat_id=5, db_path=db_path)
    assert result == "awaiting"
    assert wp.get_item(item_id, db_path=db_path).verdict is None  # not final yet

    pending = pending_replies.peek(5, db_path=db_path)
    assert pending is not None and pending.kind == wp.REWRITE_KIND and pending.ref_id == item_id

    handled_item, wrote = wp.handle_awaited_reply(
        pending, "NPL sustained above 9% for two quarters", db_path=db_path
    )
    assert wrote is True
    assert handled_item is not None and handled_item.verdict == "rewrite"

    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT falsifier FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "NPL sustained above 9% for two quarters"
    # the await is one-shot
    assert pending_replies.peek(5, db_path=db_path) is None


def test_apply_verdict_stale_repress_returns_none(db_path: Path) -> None:
    prop_id = _seed_proposal(db_path)
    run = wp.ensure_run(db_path=db_path)
    items = wp.sync_items(
        run.id, [wp.PacketItemPlan("proposal", prop_id, "MELI", "x")], db_path=db_path
    )
    wp.apply_verdict(items[0].id, "accept", chat_id=5, db_path=db_path)
    # a second press on an already-handled item is a stale no-op
    assert wp.apply_verdict(items[0].id, "drop", chat_id=5, db_path=db_path) is None


# --------------------------------------------------------------------------
# The completion receipt
# --------------------------------------------------------------------------


def test_receipt_fires_only_after_the_last_item_and_tallies_correctly(db_path: Path) -> None:
    n1 = _seed_reconcile_note(db_path)
    prop_id = _seed_proposal(db_path)
    run = wp.ensure_run(db_path=db_path)
    items = wp.sync_items(
        run.id,
        [
            wp.PacketItemPlan("reconcile_note", n1, "NU", "x"),
            wp.PacketItemPlan("proposal", prop_id, "MELI", "y"),
        ],
        db_path=db_path,
    )
    wp.apply_verdict(items[0].id, "accept", chat_id=5, db_path=db_path)
    spy = _Spy()
    fired = wp.maybe_send_receipt("tok", 5, run.id, db_path=db_path, send=spy.send)
    assert fired is False  # one item still pending
    assert spy.sends == []

    wp.apply_verdict(items[1].id, "drop", chat_id=5, db_path=db_path)
    spy2 = _Spy()
    fired2 = wp.maybe_send_receipt("tok", 5, run.id, db_path=db_path, send=spy2.send)
    assert fired2 is True
    assert "Packet clear" in spy2.sends[0][1]
    assert "1 ratified" in spy2.sends[0][1] and "1 dropped" in spy2.sends[0][1]

    # idempotent — a second check never re-sends the receipt
    spy3 = _Spy()
    assert wp.maybe_send_receipt("tok", 5, run.id, db_path=db_path, send=spy3.send) is False
    assert spy3.sends == []
    assert wp.get_run(run.id, db_path=db_path).status == "complete"


# --------------------------------------------------------------------------
# Callback dispatch (research_notify.dispatch_callback -> the wk: branch)
# --------------------------------------------------------------------------


def _cb(data: str, *, chat_id: int = 5) -> telegram.Update:
    return telegram.Update(
        update_id=1, kind="callback", chat_id=chat_id, callback_data=data, callback_query_id="cq"
    )


def test_dispatch_wk_accept_and_sends_receipt_on_last_item(db_path: Path) -> None:
    n1 = _seed_reconcile_note(db_path)
    run = wp.ensure_run(db_path=db_path)
    items = wp.sync_items(
        run.id, [wp.PacketItemPlan("reconcile_note", n1, "NU", "x")], db_path=db_path
    )
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb(f"wk:accept:{items[0].id}"), db_path=db_path, send=spy.send, answer=spy.send
    )
    assert status == "wk_accept"
    assert any("Packet clear" in s[1] for s in spy.sends)


def test_dispatch_wk_unknown_item_is_acknowledged(db_path: Path) -> None:
    status = research_notify.dispatch_callback(
        "tok",
        _cb("wk:accept:999999"),
        db_path=db_path,
        send=lambda *a, **k: {},
        answer=lambda *a, **k: {},
    )
    assert status == "wk_stale"
