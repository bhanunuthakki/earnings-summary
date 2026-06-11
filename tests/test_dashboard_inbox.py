"""Tests for src/dashboard/inbox.py — the unified inbox model + stream renderer.

Inbox v2 coverage: cross-kind fuzzy dedupe (an advisor memo's ledger entry vs
its journal-observation echo), the Home rail's hover ✓/✕ quick actions, and
the unread-tracking markup (``data-when`` / ``data-ix-surface`` / INBOX_JS).
JS behavior is locked via rendered-markup assertions, per repo style. The
substrate is built via alembic exactly like tests/test_dashboard_digest.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from alerts import apply_action, fire_alert, queue_action
from dashboard.inbox import INBOX_JS, collect_inbox, render_inbox_stream
from identity import DEFAULT_USER_ID
from user_state.ledger import append_entry
from user_state.notes import create_note

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "dashboard_inbox.db"
    cfg = _build_config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


# ----------------------------------------------------------------------------
# Cross-kind dedupe (Inbox v2): the memory-everywhere echo renders ONCE
# ----------------------------------------------------------------------------


def test_advisor_memo_and_journal_echo_collapse_to_the_ledger_entry(db_path: Path) -> None:
    """The advisor's persist_memo writes one memo line to the thesis ledger
    AND to the journal under different decorations ("Title (memo #N) — line"
    vs "[advisor memo #N · kind] Title — line"). The old per-kind dedupe key
    rendered both; they must collapse to the richer kind (ledger)."""
    from advisor.memos import persist_memo

    result = persist_memo(
        db_path=db_path,
        user_id=DEFAULT_USER_ID,
        kind="swap_check",
        ticker="RBRK",
        counter_ticker="S",
        title="RBRK vs S swap margin",
        body_md="The screened margin survives the tax drag by ~9 points.",
        context={},
        write_ledger=True,
    )
    assert result.ok

    items = collect_inbox(db_path)
    echoes = [it for it in items if "swap margin" in it.body or "margin survives" in it.body]
    assert len(echoes) == 1
    assert echoes[0].kind == "ledger"

    html = render_inbox_stream(items, db_path=db_path)
    assert html.count("margin survives the tax drag") == 1


def test_cross_kind_dedupe_keeps_the_richer_kind_regardless_of_age(db_path: Path) -> None:
    """A plain note + ledger entry carrying the same narrative collapse to
    the ledger entry even though the note is newer (richness beats recency
    across kinds; recency still breaks ties within one)."""
    append_entry(
        ticker="GOOG",
        entry_kind="thesis_update",
        body="Cloud margin softening confirmed by Q2 print.",
        db_path=db_path,
    )
    create_note(
        ticker="GOOG",
        kind="observation",
        body="Cloud margin softening confirmed by Q2 print.",
        db_path=db_path,
    )

    items = collect_inbox(db_path)
    matches = [it for it in items if "Cloud margin softening" in it.body]
    assert [it.kind for it in matches] == ["ledger"]


def test_same_body_different_tickers_do_not_collapse(db_path: Path) -> None:
    body = "Margin inflection flagged by the quarterly screen."
    append_entry(ticker="NU", entry_kind="thesis_update", body=body, db_path=db_path)
    append_entry(ticker="MELI", entry_kind="thesis_update", body=body, db_path=db_path)

    items = collect_inbox(db_path)
    assert sorted(it.ticker or "" for it in items if it.body == body) == ["MELI", "NU"]


# ----------------------------------------------------------------------------
# Quick approve/dismiss (Inbox v2): hover ✓/✕ on the compact rail only
# ----------------------------------------------------------------------------


def _seed_alert_with_pending_action(db_path: Path, *, ticker: str = "NU") -> int:
    alert = fire_alert(
        ticker=ticker,
        trigger_kind="kpi_inflection",
        fired_at=datetime.now(UTC),
        evidence_json=json.dumps({"summary": "rail quick-action test"}),
        signature_sha=f"sig-quick-{ticker}",
        db_path=db_path,
    )
    qa = queue_action(
        alert_id=alert.id,
        action_kind="thesis_update",
        payload={"body": "Quick-action draft body"},
        db_path=db_path,
    )
    return qa.id


def test_compact_rail_renders_quick_buttons_in_the_header_row(db_path: Path) -> None:
    action_id = _seed_alert_with_pending_action(db_path)
    items = collect_inbox(db_path)
    html = render_inbox_stream(items, db_path=db_path, compact=True)

    assert f'class="ix-act ix-act-approve" type="button" data-action-id="{action_id}"' in html
    assert (
        f'class="ix-act ix-act-dismiss" type="button" data-action-id="{action_id}" '
        f'data-dismiss="1"' in html
    )
    # Zero extra rows: the buttons sit inside the existing header, BEFORE the
    # relative timestamp (right side), never as a row of their own.
    card = html[html.index('data-kind="alert"') :]
    assert card.index('class="ix-quick"') < card.index('class="ix-when"')
    assert card.index("</div>", card.index('class="ix-head"')) > card.index('class="ix-quick"')


def test_quick_buttons_render_on_standalone_draft_cards(db_path: Path) -> None:
    action_id = _seed_alert_with_pending_action(db_path)
    items = collect_inbox(db_path, kinds=("draft",))
    assert [it.kind for it in items] == ["draft"]
    html = render_inbox_stream(items, db_path=db_path, compact=True)
    assert 'data-kind="draft"' in html
    assert f'data-action-id="{action_id}"' in html


def test_quick_buttons_absent_off_the_rail_and_for_settled_actions(db_path: Path) -> None:
    action_id = _seed_alert_with_pending_action(db_path)
    items = collect_inbox(db_path)
    # Full (digest/feed) cards keep the existing <a href="/approve"> links.
    full = render_inbox_stream(items, db_path=db_path)
    assert 'class="ix-act' not in full
    assert f'href="/approve?action_id={action_id}"' in full
    # Once the action settles, the rail card has nothing approvable.
    apply_action(action_id, db_path=db_path)
    compact = render_inbox_stream(collect_inbox(db_path), db_path=db_path, compact=True)
    assert 'class="ix-act' not in compact


# ----------------------------------------------------------------------------
# Unread tracking markup (Inbox v2): data-when / data-ix-surface / INBOX_JS
# ----------------------------------------------------------------------------


def test_stream_carries_surface_and_per_card_timestamps(db_path: Path) -> None:
    append_entry(ticker="NU", entry_kind="thesis_update", body="unread probe", db_path=db_path)
    html = render_inbox_stream(
        collect_inbox(db_path), db_path=db_path, compact=True, surface="home"
    )
    assert 'data-ix-surface="home"' in html
    assert 'data-when="' in html
    # The stamp is the lexicographically comparable naive-UTC shape the JS
    # compares against localStorage ("YYYY-MM-DDTHH:MM:SS").
    stamp = html.split('data-when="', 1)[1].split('"', 1)[0]
    assert len(stamp) == 19
    datetime.fromisoformat(stamp)


def test_inbox_js_wires_unread_and_quick_actions() -> None:
    """Rendered-markup contract for the page script: per-surface localStorage
    keys, the rail badge hook, see-it-to-clear-it (IntersectionObserver), and
    the POST /approve fetch that updates cards in place."""
    assert "ix-last-seen:" in INBOX_JS
    assert "data-ix-badge" in INBOX_JS
    assert "IntersectionObserver" in INBOX_JS
    assert "ix-new" in INBOX_JS
    assert "'/approve'" in INBOX_JS
    assert "method: 'POST'" in INBOX_JS
    # Draft cards swap their status chip; alert cards must NOT have theirs
    # swapped (the chip shows the ALERT's status, which approving a queued
    # action does not change) — the kind gate is load-bearing.
    assert "data-kind') === 'draft'" in INBOX_JS
