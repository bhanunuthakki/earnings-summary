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
from alerts import apply_action, dismiss_alert, fire_alert, get_action, queue_action
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
# Memo action chips (S3 PR2): .k-chip dismiss + open-memo, by identity
# ----------------------------------------------------------------------------


def test_portfolio_memo_note_card_carries_dismiss_and_open_memo_chips(db_path: Path) -> None:
    """The portfolio next-dollar memo (note-only) gets the shared-kit
    affordances: an open-memo link to the Memos surface and a dismiss chip on
    the /api/notes archive endpoint — keyed by its own note id. Plain notes do
    NOT get them."""
    from advisor.memos import persist_memo

    persist_memo(
        db_path=db_path,
        user_id=DEFAULT_USER_ID,
        kind="next_dollar",
        ticker=None,
        counter_ticker=None,
        title="Next-dollar memo · 2026-06-12",
        body_md="Deepen the highest-conviction names where the DCF gap is widest.",
        context={},
        write_ledger=False,
    )
    create_note(
        user_id=DEFAULT_USER_ID, ticker="NU", kind="watch",
        body="An ordinary watch item, no chips.", db_path=db_path,
    )  # fmt: skip

    items = collect_inbox(db_path)
    memo = next(it for it in items if it.semantic_kind == "advisor_memo")
    assert memo.note_id is not None
    for compact in (True, False):
        html = render_inbox_stream(items, db_path=db_path, compact=compact)
        assert f'class="k-chip k-chip-btn ix-note-dismiss" data-note-id="{memo.note_id}"' in html
        assert '<a class="k-chip k-chip-btn ix-memo-open" href="/#advisor_memos">' in html
        # Exactly one card carries the affordances — the memo, not the watch.
        assert html.count("ix-memo-acts") == 1


def test_ledger_advisor_memo_gets_open_memo_without_dismiss(db_path: Path) -> None:
    """A ticker-scoped swap memo collapses to its ledger echo (no note_id on
    the survivor): it still opens the memo record, but has no dismiss chip —
    archiving targets a note, and this survivor isn't one."""
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
    survivor = next(it for it in items if it.semantic_kind == "advisor_memo")
    assert survivor.kind == "ledger"
    assert survivor.note_id is None

    html = render_inbox_stream(items, db_path=db_path)
    assert 'class="k-chip k-chip-btn ix-memo-open"' in html
    assert "ix-note-dismiss" not in html


def test_inbox_js_wires_memo_dismiss() -> None:
    """Rendered-markup contract: the dismiss chip POSTs the note-archive
    endpoint and fades the card — distinct from the .ix-act /approve path."""
    assert "ix-note-dismiss" in INBOX_JS
    assert "/api/notes/" in INBOX_JS
    assert "/archive" in INBOX_JS
    assert "ix-dismissed" in INBOX_JS


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

    # The ✓ approves the drafted action; the ✕ dismisses the ALERT itself
    # (the card-level dismiss, distinct from the action's own lifecycle) so it
    # leaves the inbox in one click.
    assert (
        f'class="ix-act ix-act-approve k-btn k-btn-quiet k-btn-sm" type="button" '
        f'data-action-id="{action_id}"' in html
    )
    alert_id = items[0].alert.id  # type: ignore[union-attr]
    assert (
        f'class="ix-act ix-act-dismiss k-btn k-btn-quiet k-btn-sm" type="button" data-alert-id="{alert_id}"'
        in html
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
    # Once the action settles there is nothing to APPROVE, but the parent alert
    # is still pending — so the card keeps its dismiss-alert ✕ (you can still
    # clear the alert from the rail).
    apply_action(action_id, db_path=db_path)
    compact = render_inbox_stream(collect_inbox(db_path), db_path=db_path, compact=True)
    assert "ix-act-approve" not in compact
    assert "ix-act-dismiss" in compact
    assert "data-alert-id=" in compact


def test_alert_without_a_drafted_action_still_gets_dismiss_alert(db_path: Path) -> None:
    """The gap the owner flagged: an alert that never drafted a queued action
    (e.g. earnings_tone) had no rail affordance at all. It now carries a
    dismiss-alert ✕ — no approve (nothing to approve), just the card-level
    clear."""
    alert = fire_alert(
        ticker="NU",
        trigger_kind="earnings_tone",
        fired_at=datetime.now(UTC),
        evidence_json=json.dumps({"summary": "NIM contracted 100bps QoQ."}),
        signature_sha="sig-no-action",
        db_path=db_path,
    )
    items = collect_inbox(db_path)
    compact = render_inbox_stream(items, db_path=db_path, compact=True)
    assert (
        f'class="ix-act ix-act-dismiss k-btn k-btn-quiet k-btn-sm" type="button" data-alert-id="{alert.id}"'
        in compact
    )
    assert "ix-act-approve" not in compact
    # Off the rail (feed), the compact quick buttons never render.
    assert 'class="ix-act' not in render_inbox_stream(items, db_path=db_path)


def test_tier1_decisive_alert_carries_severity_rail_and_chip(db_path: Path) -> None:
    """A decisive alert — an OWNER falsifier (decision_condition) breach or a
    registered threshold crossing — reads apart from routine cards at a
    glance: a bad-toned left rail on the card + a `tier 1` kit chip with the
    reason in the hover. A routine alert carries neither, and an ADVISOR
    condition breach is NOT tier-1 (the #875 relevance gate: informative,
    never screaming)."""
    fire_alert(
        ticker="NU",
        trigger_kind="decision_condition",
        fired_at=datetime.now(UTC),
        evidence_json=json.dumps(
            {"summary": "NPL 15-90d crossed the owner bar.", "decided_by": "owner"}
        ),
        signature_sha="sig-tier1",
        db_path=db_path,
    )
    fire_alert(
        ticker="NU",
        trigger_kind="decision_condition",
        fired_at=datetime.now(UTC) - timedelta(minutes=30),
        evidence_json=json.dumps({"summary": "Advisor sweep condition.", "decided_by": "advisor"}),
        signature_sha="sig-advisor",
        db_path=db_path,
    )
    fire_alert(
        ticker="NU",
        trigger_kind="kpi_inflection",
        fired_at=datetime.now(UTC) - timedelta(hours=1),
        evidence_json=json.dumps({"zscore": 1.2}),
        signature_sha="sig-routine",
        db_path=db_path,
    )
    html = render_inbox_stream(collect_inbox(db_path), db_path=db_path, compact=True)
    assert html.count('class="ix-card ix-sev-bad"') == 1
    assert html.count(">tier 1</span>") == 1
    assert 'title="owner falsifier breach"' in html
    # The advisor + routine cards stay plain ix-cards.
    assert html.count('class="ix-card"') == 2


def test_tier1_threshold_crossed_evidence_marks_the_card(db_path: Path) -> None:
    """Evidence-driven decisiveness (threshold_crossed / is_thesis_breaker)
    marks the card the same way the falsifier trigger does — ONE definition
    shared with the ranking layer's strength factor."""
    fire_alert(
        ticker="NU",
        trigger_kind="kpi_inflection",
        fired_at=datetime.now(UTC),
        evidence_json=json.dumps({"threshold_crossed": True, "zscore": 4.0}),
        signature_sha="sig-threshold",
        db_path=db_path,
    )
    html = render_inbox_stream(collect_inbox(db_path), db_path=db_path)
    assert 'class="ix-card ix-sev-bad"' in html
    assert 'title="threshold crossed"' in html
    # The severity rail CSS exists and is declared AFTER the unread accent
    # rail so a new + decisive card shows the status color.
    from dashboard.inbox import INBOX_CSS

    assert ".ix-sev-bad { box-shadow: inset 2px 0 0 var(--bad); }" in INBOX_CSS
    assert INBOX_CSS.index(".ix-new {") < INBOX_CSS.index(".ix-sev-bad {")


def test_plain_note_gets_a_dismiss_chip_but_ledger_does_not(db_path: Path) -> None:
    """Parity for actionable cards only (owner choice): a plain analyst note is
    dismissable (archive) from the rail, but an informational thesis-ledger
    entry is not — it ages out on recency decay."""
    note = create_note(
        user_id=DEFAULT_USER_ID, ticker="NU", kind="watch",
        body="Watch the funding mix next quarter.", db_path=db_path,
    )  # fmt: skip
    append_entry(
        ticker="META", entry_kind="thesis_update", body="Equity raise news.", db_path=db_path
    )
    items = collect_inbox(db_path)
    compact = render_inbox_stream(items, db_path=db_path, compact=True)
    # The note carries a dismiss ✕ keyed to the notes-archive endpoint.
    assert (
        f'class="ix-act ix-act-dismiss k-btn k-btn-quiet k-btn-sm" type="button" data-note-id="{note.id}"'
        in compact
    )
    # The ledger card stays read-only: no quick-action block on it.
    ledger_card = compact[compact.index('data-kind="ledger"') :]
    head_end = ledger_card.index("</div>", ledger_card.index('class="ix-head"'))
    assert "ix-quick" not in ledger_card[:head_end]


def test_quick_actions_post_by_id_via_htmx() -> None:
    """Wave 3b: each quick action routes by id via HTMX hx-post (not the old JS
    fetch dispatcher) — alert-id → alert-dismiss, note-id → note-archive,
    action-id → /approve — and swaps its .ix-quick span in place."""
    from dashboard.inbox import _btn_approve, _btn_dismiss_alert, _btn_dismiss_note

    dis_alert = _btn_dismiss_alert(7)
    assert 'hx-post="/api/alerts/7/dismiss"' in dis_alert
    assert "data-alert-id" in dis_alert
    assert 'hx-target="closest .ix-quick"' in dis_alert and 'hx-swap="outerHTML"' in dis_alert
    dis_note = _btn_dismiss_note(9)
    assert 'hx-post="/api/notes/9/archive"' in dis_note and "data-note-id" in dis_note
    approve = _btn_approve(5)
    assert 'hx-post="/approve"' in approve and '"action_id": "5"' in approve
    # The old JS fetch dispatcher is gone — HTMX drives the actions now.
    assert "/api/alerts/" not in INBOX_JS


# ----------------------------------------------------------------------------
# Pending-alert visibility + dismissed-card safety (red-team wave A)
# ----------------------------------------------------------------------------


def _fire(db_path: Path, *, sig: str, when: datetime, kind: str = "kpi_inflection"):
    return fire_alert(
        ticker="NU",
        trigger_kind=kind,
        fired_at=when,
        evidence_json=json.dumps({"summary": f"probe {sig}"}),
        signature_sha=sig,
        db_path=db_path,
    )


def test_pending_alerts_surface_past_a_flood_of_newer_dismissed_rows(db_path: Path) -> None:
    """The prod failure: collect_inbox(limit=14) truncated on recency BEFORE
    ranking, so 14 newer dismissed rows pushed the owner's old pending alerts
    out of the stream entirely. Pending is the QUEUE — it must always surface,
    regardless of age or dismissed volume."""
    now = datetime.now(UTC)
    old_pending = [
        _fire(db_path, sig=f"sig-pend-{i}", when=now - timedelta(days=30 + i)).id for i in range(4)
    ]
    for i in range(20):  # newer, already-settled noise
        a = _fire(db_path, sig=f"sig-dis-{i}", when=now - timedelta(hours=i))
        dismiss_alert(a.id, db_path=db_path)

    items = collect_inbox(db_path, limit=14)
    alert_ids = {it.alert.id for it in items if it.kind == "alert" and it.alert is not None}
    assert set(old_pending) <= alert_ids
    # ...and not one dismissed card in the default stream.
    assert all(it.status != "dismissed" for it in items)


def test_dismissed_alerts_absent_by_default_but_reachable_via_status_filter(
    db_path: Path,
) -> None:
    a = _fire(db_path, sig="sig-dismissed-one", when=datetime.now(UTC))
    dismiss_alert(a.id, db_path=db_path)

    default_ids = {
        it.alert.id for it in collect_inbox(db_path) if it.kind == "alert" and it.alert is not None
    }
    assert a.id not in default_ids
    # The /feed ?status= filter still reaches the history.
    filtered = collect_inbox(db_path, status="dismissed")
    assert [it.alert.id for it in filtered if it.alert is not None] == [a.id]
    assert filtered[0].status == "dismissed"


def test_dismissed_alert_card_never_renders_a_live_approve(db_path: Path) -> None:
    """Prod carried 12 pending queued_actions dangling under dismissed alerts —
    the card rendered a working ✓ on a decision the owner already closed. A
    non-pending parent must yield NO approvable action, on the alert card and
    via the standalone-draft lane both."""
    action_id = _seed_alert_with_pending_action(db_path, ticker="NU")
    alert_id = get_action(action_id, db_path=db_path).alert_id
    dismiss_alert(alert_id, db_path=db_path)
    # The queued action is still pending (the dangling-orphan prod state).
    assert get_action(action_id, db_path=db_path).status == "pending"

    # The dismissed alert card (reached via the explicit status filter).
    items = collect_inbox(db_path, status="dismissed")
    html = render_inbox_stream(items, db_path=db_path, compact=True)
    assert "ix-act-approve" not in html
    assert f'href="/approve?action_id={action_id}"' not in render_inbox_stream(
        items, db_path=db_path
    )

    # The standalone-draft lane skips orphans too.
    drafts = collect_inbox(db_path, kinds=("draft",))
    assert drafts == []


def test_pending_action_helper_requires_pending_parent(db_path: Path) -> None:
    from dashboard.inbox import _pending_action  # pyright: ignore[reportPrivateUsage]

    action_id = _seed_alert_with_pending_action(db_path, ticker="NU")
    (card,) = collect_inbox(db_path, kinds=("alert",))
    assert _pending_action(card) is not None  # pending parent → approvable

    dismiss_alert(get_action(action_id, db_path=db_path).alert_id, db_path=db_path)
    (dismissed_card,) = collect_inbox(db_path, kinds=("alert",), status="dismissed")
    assert _pending_action(dismissed_card) is None  # settled parent → never


# ----------------------------------------------------------------------------
# Consequence receipts (REQ-11): acted_span's optional detail / detail_href /
# dismiss_why_id renderings, and byte-compat when none are passed.
# ----------------------------------------------------------------------------


def test_acted_span_byte_compatible_when_detail_absent() -> None:
    """Every pre-receipts caller (approve/dismiss/archive chips elsewhere in
    this module) keeps rendering identically — detail is purely additive."""
    from dashboard.inbox import acted_span

    assert acted_span("✓ applied", "applied") == (
        '<span class="ix-quick ix-acted ix-acted-applied">✓ applied</span>'
    )
    assert acted_span("✕ dismissed", "cancelled", undo_url="/api/actions/5/uncancel") == (
        '<span class="ix-quick ix-acted ix-acted-cancelled">✕ dismissed'
        ' <button type="button" class="ix-undo k-btn k-btn-quiet k-btn-sm" '
        'hx-post="/api/actions/5/uncancel" hx-target="closest .ix-quick" '
        'hx-swap="outerHTML">undo</button></span>'
    )


def test_acted_span_detail_renders_muted_truncated_suffix() -> None:
    from dashboard.inbox import acted_span

    long_consequence = (
        "Approved queued_action id=9 (action_kind=thesis_update). "
        "Ledger entry id=42 written: NU thesis_update source_alert_id=7."
    )
    html = acted_span("✓ applied", "applied", detail=long_consequence)
    assert "ix-acted-detail" in html
    assert f'title="{long_consequence}"' in html  # full text always on title=
    # Truncated to 60 visible chars + ellipsis in the VISIBLE tag content
    # (after the title= attribute closes) — the body itself is truncated.
    visible_body = html.split(f'title="{long_consequence}">', 1)[1]
    assert "…</span>" in visible_body
    assert long_consequence not in visible_body


def test_acted_span_short_detail_renders_untruncated() -> None:
    from dashboard.inbox import acted_span

    html = acted_span("✓ applied", "applied", detail="Cancelled queued_action id=3.")
    assert ">Cancelled queued_action id=3.<" in html
    assert "…" not in html


def test_acted_span_detail_href_becomes_a_doorway() -> None:
    from dashboard.inbox import acted_span

    html = acted_span(
        "✓ applied",
        "applied",
        detail="Ledger entry id=42 written.",
        detail_href="/#decisions_record",
    )
    assert '<a class="ix-acted-detail" href="/#decisions_record"' in html


def test_acted_span_dismiss_why_renders_toggle_and_hidden_input() -> None:
    from dashboard.inbox import acted_span

    html = acted_span("✕ dismissed", "cancelled", dismiss_why_id=17)
    assert "ix-why-toggle" in html
    assert 'hx-post="/api/alerts/17/dismiss"' in html
    assert 'name="reason"' in html
    assert "hidden" in html  # the input starts hidden until "why?" is clicked


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
    keys, the rail badge hook, and see-it-to-clear-it (IntersectionObserver).
    The quick actions themselves are HTMX now (Wave 3b) — the old POST /approve
    fetch dispatcher is gone (see test_quick_actions_post_by_id_via_htmx)."""
    assert "ix-last-seen:" in INBOX_JS
    assert "data-ix-badge" in INBOX_JS
    assert "IntersectionObserver" in INBOX_JS
    assert "ix-new" in INBOX_JS
    # The quick-action fetch dispatcher moved to HTMX hx-post on the buttons
    # (the advisor-memo dismiss below still POSTs via fetch — a separate path).
    assert "'/approve'" not in INBOX_JS
