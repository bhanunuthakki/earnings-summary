"""Tests for src/dashboard/inbox.py — the unified inbox model + stream renderer.

Inbox v2 coverage: cross-kind fuzzy dedupe (an advisor memo's ledger entry vs
its journal-observation echo), the Home rail's hover ✓/✕ quick actions, and
the unread-tracking markup (``data-when`` / ``data-ix-surface`` / INBOX_JS).
JS behavior is locked via rendered-markup assertions, per repo style. The
substrate is built via alembic exactly like tests/test_dashboard_feed.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from alerts import apply_action, fire_alert, queue_action
from dashboard.inbox import INBOX_JS, InboxItem, collect_inbox, render_inbox_stream
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
# Semantic identity (S3): rank/label/body by stored identity, not source table
# ----------------------------------------------------------------------------


def test_swap_memo_note_and_ledger_collapse_with_clean_body(db_path: Path) -> None:
    """The write site stopped emitting the "[advisor memo #N · kind]" tag and
    pins the note body to "{title} — {summary}". The ticker-scoped swap memo
    still writes BOTH a note and a ledger echo ("{title} (memo #N) — {summary}")
    — they must still _fuzzy_norm-collapse to ONE card (the richer ledger), now
    carrying advisor identity so it labels "Advisor memo" and demotes to
    synthesis."""
    from advisor.memos import persist_memo

    persist_memo(
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
    items = collect_inbox(db_path)
    echoes = [it for it in items if "margin survives the tax drag" in it.body]
    assert len(echoes) == 1
    survivor = echoes[0]
    assert survivor.kind == "ledger"
    assert survivor.semantic_kind == "advisor_memo"
    assert survivor.category == "synthesis"

    html = render_inbox_stream(items, db_path=db_path)
    assert html.count("margin survives the tax drag") == 1
    assert ">Advisor memo<" in html
    assert "[advisor memo" not in html


def test_portfolio_memo_note_ranks_at_synthesis_not_top(db_path: Path) -> None:
    """The owner's #1 complaint: a portfolio next-dollar memo reaches the inbox
    ONLY as an advisor note (ticker=None, no ledger echo). By identity it must
    demote to synthesis weight and sit BELOW a real, fresher event — never on
    top of the stream."""
    from advisor.memos import persist_memo

    persist_memo(
        db_path=db_path,
        user_id=DEFAULT_USER_ID,
        kind="next_dollar",
        ticker=None,
        counter_ticker=None,
        title="Next-dollar memo · 2026-06-12",
        body_md="Where the next dollar works hardest: deepen the highest-conviction names.",
        context={},
        write_ledger=False,  # portfolio-level: note only, no ledger sibling
    )
    append_entry(
        ticker="NU", entry_kind="thesis_update", body="Q2 take-rate inflected up.", db_path=db_path
    )

    items = collect_inbox(db_path, position_weights={})
    memo = next(it for it in items if it.semantic_kind == "advisor_memo")
    assert memo.kind == "note"
    assert memo.category == "synthesis"
    # The fresh thesis change (severity 2.8) outranks the memo (synthesis 1.0).
    assert items[0].semantic_kind != "advisor_memo"
    assert items.index(memo) > 0


def test_no_raw_enum_or_internal_tag_reaches_a_label_or_body(db_path: Path) -> None:
    """Guard (Law 1): no source-table string or raw enum — ``observation``,
    ``[advisor memo #N · kind]`` — ever surfaces as a card label OR body, for
    any kind. Covers a LEGACY advisor note still carrying the retired tag
    (prod rows persist it; the inbox strips it at render)."""
    create_note(
        user_id=DEFAULT_USER_ID,
        ticker=None,
        kind="observation",
        body="[advisor memo #5 · next_dollar] Next-dollar memo · 2026-06-12 — Deepen NU.",
        source="advisor",
        source_ref="advisor_memo:5",
        context={"memo_id": 5, "kind": "next_dollar"},
        db_path=db_path,
    )
    # Plain notes whose kind is the raw enum 'observation' / 'watch' — their
    # bodies deliberately avoid the word so the assertion catches a LEAKED enum
    # (the kind chip), not legitimate user prose.
    create_note(
        user_id=DEFAULT_USER_ID, ticker="NU", kind="observation",
        body="A plain analyst note about the print.", db_path=db_path,
    )  # fmt: skip
    create_note(
        user_id=DEFAULT_USER_ID, ticker="NU", kind="watch",
        body="Watch the funding mix next quarter.", db_path=db_path,
    )  # fmt: skip

    items = collect_inbox(db_path)
    for compact in (True, False):
        html = render_inbox_stream(items, db_path=db_path, compact=compact)
        assert "observation" not in html  # raw note-kind enum never reaches a chip
        assert "[advisor memo" not in html  # retired internal tag never reaches a body
        # The retired tag's body survived as clean prose, identity as a label,
        # and the plain observation note humanized to "Note".
        assert ">Advisor memo<" in html
        assert "Deepen NU." in html
        assert ">Note<" in html


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
    # Full (feed) cards keep the existing <a href="/approve"> links.
    full = render_inbox_stream(items, db_path=db_path)
    assert 'class="ix-act' not in full
    assert f'href="/approve?action_id={action_id}"' in full
    # Once the action settles, the rail card has nothing approvable.
    apply_action(action_id, db_path=db_path)
    compact = render_inbox_stream(collect_inbox(db_path), db_path=db_path, compact=True)
    assert 'class="ix-act' not in compact


# ----------------------------------------------------------------------------
# Flat stream (2026-06-11): no recency-bucket headers
# ----------------------------------------------------------------------------


def test_stream_renders_flat_without_bucket_headers() -> None:
    """Owner feedback 2026-06-11: Today/Yesterday/This week headers wasted
    screen — the per-card relative stamp (top right) is enough. The stream is
    one flat list."""
    now = datetime.now(UTC).replace(tzinfo=None)
    items = [
        InboxItem(kind="ledger", ticker="NU", when=now, title="Thesis update", body="today"),
        InboxItem(
            kind="ledger",
            ticker="NU",
            when=now - timedelta(days=3),
            title="Thesis update",
            body="three days ago",
        ),
        InboxItem(
            kind="ledger",
            ticker="NU",
            when=now - timedelta(days=20),
            title="Thesis update",
            body="long ago",
        ),
    ]
    html = render_inbox_stream(items, db_path=None)
    assert "ix-bucket" not in html
    for label in ("Today", "Yesterday", "This week", "Earlier"):
        assert f">{label}<" not in html
    # All three cards still render, with their stamps.
    assert html.count('class="ix-card"') == 3
    assert html.count('data-when="') == 3


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
