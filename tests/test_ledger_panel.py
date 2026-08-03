"""The Ledger flat musings panel — rendering over the analyst_notes spine."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from capture import ingest
from capture.matcher import build_roster_index
from pipeline.ledger_panel import render_ledger_list, render_ledger_panel

PRIOR_HEAD = "0059_kpi_facts_restatement"


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "ledger.db", stamp=PRIOR_HEAD)


def test_empty_panel_shows_capture_box(db_path: Path) -> None:
    # LEDGER_ONMYMIND defaults ON (2026-07-14): an empty panel shows the On-My-Mind
    # feed's empty state, not the legacy "No musings yet" list. The capture box is
    # present in either mode — that's what this test guards.
    html = render_ledger_panel(db_path)
    assert "Nothing on your mind yet" in html
    assert "ledger-cap" in html
    assert ">Capture<" in html
    assert "/api/capture/text" in html  # the capture box POST target


def test_panel_lists_musings_newest_first(db_path: Path) -> None:
    roster = build_roster_index(symbols=["NU"], phrases={"nubank": "NU"})
    ingest.ingest_capture(
        channel="tray", text="Nubank NPL formation worries me", roster=roster, db_path=db_path
    )
    ingest.ingest_capture(
        channel="telegram", text="the macro tape feels off today", roster=roster, db_path=db_path
    )
    html = render_ledger_list(db_path)
    assert "NPL formation worries me" in html
    assert "macro tape feels off" in html
    assert "ledger-musing" in html
    assert "unattributed" in html  # the macro musing has no roster name


def test_needs_ticker_chip_renders(db_path: Path) -> None:
    roster = build_roster_index(symbols=["NU", "MELI"], phrases={})
    ingest.ingest_capture(
        channel="tray", text="NU and MELI both look compelling", roster=roster, db_path=db_path
    )
    html = render_ledger_list(db_path)
    assert "needs ticker" in html


def test_panel_shows_synthesized_stances(db_path: Path) -> None:
    from synthesis.insights import record_insight

    roster = build_roster_index(symbols=["NU"], phrases={"nubank": "NU"})
    ingest.ingest_capture(
        channel="tray", text="Nubank credit cycle looks early", roster=roster, db_path=db_path
    )
    record_insight(
        scope_key="NU",
        kind="stance",
        body_md="Constructive on NU; the credit cycle still looks early.",
        source_note_ids=[1],
        watermark_id=1,
        db_path=db_path,
    )
    html = render_ledger_panel(db_path)
    assert "What you think now" in html
    assert "Constructive on NU" in html


def test_reject_drops_wondering_from_list(db_path: Path) -> None:
    from pipeline.ledger_panel import render_ledger_research_list
    from research.proposals import create_task, set_task_status

    task_id = create_task(
        note_id=None, claim="do NU's margins still hold?", ticker="NU", db_path=db_path
    )
    before = render_ledger_research_list(db_path)
    assert "Open wonderings" in before
    assert f'data-reject-task="{task_id}"' in before

    set_task_status(task_id, "rejected", db_path=db_path)
    after = render_ledger_research_list(db_path)
    assert "Open wonderings" not in after
    assert f'data-reject-task="{task_id}"' not in after


def test_proposal_card_renders_backlink_from_source_note(db_path: Path) -> None:
    """Red-team wave A: prod proposal rows carry source_note_ids='[54]' but the
    dataclass never mapped the column, so the "from your note" doorway NEVER
    rendered. A seeded id must now produce the data-goto-note backlink."""
    from pipeline.ledger_panel import render_ledger_research_list
    from research.proposals import create_proposal

    create_proposal(
        task_id=None,
        kind="memo",
        ticker="NU",
        title="NU margin durability",
        body_md="The margin question, researched.",
        source_note_ids="[54]",
        db_path=db_path,
    )
    html = render_ledger_research_list(db_path)
    assert 'data-goto-note="54"' in html
    assert "from your note" in html


def test_proposal_card_garbage_source_note_ids_renders_without_backlink(db_path: Path) -> None:
    from pipeline.ledger_panel import render_ledger_research_list
    from research.proposals import create_proposal

    create_proposal(
        task_id=None,
        kind="memo",
        ticker="NU",
        title="NU margin durability",
        body_md="The margin question, researched.",
        source_note_ids="not json at all",
        db_path=db_path,
    )
    html = render_ledger_research_list(db_path)  # must not crash
    assert "NU margin durability" in html
    assert "ledger-backlink" not in html


# ---------------------------------------------------------------------------
# W2 — desk-capture entry coaching: the capture box renders the
# pledge_challenge card and the annotated_decision_id receipt the server
# already computes (execution/comments_server.py capture_text), instead of
# throwing them away. See src/pipeline/command_center_shell.py trayRenderCoach
# for the tray's duplicated counterpart.
# ---------------------------------------------------------------------------


def test_capture_box_has_coach_mount_between_cap_row_and_list(db_path: Path) -> None:
    html = render_ledger_panel(db_path)
    cap_idx = html.index('id="ledger-cap-btn"')
    coach_idx = html.index('id="ledger-cap-coach"')
    list_idx = html.index('id="ledger-list"') if 'id="ledger-list"' in html else len(html)
    assert cap_idx < coach_idx
    # Coach mount renders before the (possibly-empty-state) list content.
    assert coach_idx < list_idx


def test_capture_js_renders_pledge_challenge_and_receipt(db_path: Path) -> None:
    from pipeline.ledger_panel import (
        _CAPTURE_JS,  # pyright: ignore[reportPrivateUsage]  # coaching-render contract under test
    )

    # The renderer branches on both fields the server returns and never on
    # ticker/needs_ticker alone (that stays the plain 4s status fallback).
    assert "res.pledge_challenge" in _CAPTURE_JS
    assert "res.annotated_decision_id" in _CAPTURE_JS
    assert "res.wondering_task_id" not in _CAPTURE_JS or "ledger-list" in _CAPTURE_JS
    # Escaped before injection (challenge text carries ** and tickers).
    assert "function esc(" in _CAPTURE_JS
    assert "replace(/&/g" in _CAPTURE_JS
    # Newlines become <br>, not raw markdown bold rendering.
    assert "<br>" in _CAPTURE_JS
    # The annotation tap re-POSTs the SAME endpoint (fills the newest pending
    # stub) rather than a separate annotate route.
    assert _CAPTURE_JS.count("/api/capture/text") == 2
    # The receipt doorway is a real decisions_record panel hash, built by
    # interpolation (not a literal '#dec...' — see the guard note at the top
    # of the module).
    assert "/#decisions_record" in _CAPTURE_JS
    assert "__DECISIONS_HASH__" not in _CAPTURE_JS
    # Dismiss is plain element removal, no CCOverlay (inline content).
    assert "data-coach-dismiss" in _CAPTURE_JS
    assert "CCOverlay" not in _CAPTURE_JS


def test_owner_utterances_never_use_window_prompt() -> None:
    """PR9: the two highest-stakes owner utterances (falsifier Rewrite, research
    Steer) are in-card textarea swaps — a native prompt is a single-line,
    unstyled OS modal that hides the card being edited and escapes the overlay
    stack entirely. Comments may mention the old idiom; live calls may not."""
    import inspect

    import pipeline.ledger_panel as lp

    src = inspect.getsource(lp)
    assert "window.prompt(" not in src
    # Both in-card editors exist, with kit buttons and a pre-fill/focus path.
    assert "data-rewrite-save" in src and "data-rewrite-cancel" in src
    assert "data-steer-save" in src and "data-steer-cancel" in src
    assert "beginRewrite" in src and "beginSteer" in src


def test_jump_toolbar_has_no_dead_onmymind_chip_when_flag_off() -> None:
    """PR9 regression: the jump-chip toolbar must not offer an 'On My Mind'
    chip when that section is suppressed (flag off) — a chip to a section that
    doesn't render is the broken doorway the audit fought. Off, the front feed
    is the plain Musings list, so the chip reads 'Musings' instead."""
    from pipeline.ledger_panel import (
        _jump_chip_toolbar,  # pyright: ignore[reportPrivateUsage]  # toolbar contract under test
    )

    off = _jump_chip_toolbar({}, onmymind_on=False)
    assert "On My Mind" not in off
    assert "ledger-jump-onmymind" not in off
    assert "ledger-jump-musings" in off

    on = _jump_chip_toolbar({}, onmymind_on=True)
    assert "ledger-jump-onmymind" in on
    assert "ledger-jump-musings" not in on


def test_ledger_console_renders_one_merged_nav_band(db_path: Path) -> None:
    """Phase-5 verifier fix 3: the Ledger console rendered TWO stacked chrome
    bands — the console's jump band over the feed's own chip toolbar, with a
    'Ledger' chip directly above an identical <h2>Ledger</h2>. Now ONE merged
    band: the console band carries the feed's chips (extra_nav), the feed
    renders embedded (internal toolbar suppressed), and the redundant Ledger
    chip is dropped while Triage + Journal chips stay."""
    from pipeline.ledger_console_panel import render_ledger_console

    html = render_ledger_console(db_path)
    assert "This section failed to render" not in html
    # The feed's chips appear exactly once — in the console band, not a second
    # internal toolbar.
    assert html.count('data-ledger-jump="ledger-jump-capture"') == 1
    assert 'class="ledger-jump-toolbar"' not in html
    # The redundant 'Ledger' console chip is gone; Triage + Journal console
    # chips stay.
    assert 'data-console-jump="csec-feed"' not in html
    assert 'data-console-jump="csec-triage"' in html
    assert 'data-console-jump="csec-journal"' in html
    # Band order follows page order: feed chips lead, console chips follow.
    assert html.index('data-ledger-jump="ledger-jump-capture"') < html.index(
        'data-console-jump="csec-triage"'
    )
    # The feed's jump targets still exist and the data-ledger-jump listener
    # (which opens the collapsed Queues block before scrolling) still ships.
    assert 'id="ledger-jump-capture"' in html
    assert 'id="ledger-jump-research"' in html
    assert "__ledgerJumpNav" in html
    # No 'Ledger' <h2> on the page at all: the merged band is already titled
    # 'Ledger' and its Capture chip names/jumps the leading feed section, so the
    # feed sheds its own <h2>Ledger</h2> echo in embedded mode (the vertical
    # space the owner flagged 2026-07-17). Triage/Journal keep their section h3s.
    assert ">Ledger</h2>" not in html


def test_ledger_console_nav_band_is_sticky_with_underline_chips(db_path: Path) -> None:
    """Owner directive 2026-08-02: the merged Ledger band pins below the shell
    topbar (``.k-toolbar-sticky``) and every chip — both the feed's own
    ``extra_nav`` chips and the console's section chips — shares the
    underline-active ``.k-chip-tab`` modifier, one primitive for the whole
    band."""
    from pipeline.ledger_console_panel import render_ledger_console

    html = render_ledger_console(db_path)
    assert 'class="k-toolbar k-toolbar-sticky"' in html
    assert 'class="k-chip k-chip-btn k-chip-tab" data-ledger-jump="ledger-jump-capture"' in html
    assert 'class="k-chip k-chip-btn k-chip-tab" data-console-jump="csec-triage"' in html
    assert 'class="k-chip k-chip-btn k-chip-tab" data-console-jump="csec-journal"' in html


def test_standalone_ledger_panel_keeps_its_internal_toolbar(db_path: Path) -> None:
    """The legacy non-console rendering is unchanged by the embedded seam:
    the default (embedded=False) still carries the internal chip toolbar."""
    html = render_ledger_panel(db_path)
    assert 'class="ledger-jump-toolbar"' in html
    assert 'data-ledger-jump="ledger-jump-capture"' in html
    # Standalone keeps its page title (no console band names it here).
    assert ">Ledger</h2>" in html
    # Embedded mode suppresses the toolbar AND the redundant <h2> (the console
    # band + its Capture chip name the section); the sections themselves stay.
    embedded = render_ledger_panel(db_path, embedded=True)
    assert 'class="ledger-jump-toolbar"' not in embedded
    assert ">Ledger</h2>" not in embedded
    assert 'id="ledger-jump-capture"' in embedded


def test_proposal_group_card_is_div_balanced() -> None:
    """A research/proposal card must close its own <div>s. It was missing the
    outer ledger-stance close — tolerated in the research list (browsers just
    cascade-nest the siblings) but catastrophic in the bounded "N need you"
    packet: an unclosed <div> keeps pk-stage[hidden] open and swallows the whole
    On My Mind feed into a display:none stage (owner 2026-07-18: "an entire
    screen of wasted space"). A body with a markdown table exercises the inner
    table-scroll <div> that made the miscount easy to miss."""
    from pipeline.ledger_panel import (
        _proposal_group_card,  # pyright: ignore[reportPrivateUsage]
    )
    from research.proposals import ResearchProposal

    prop = ResearchProposal(
        id=1,
        task_id=7,
        kind="memo",
        ticker="NU",
        title="Do NU's margins still hold?",
        body_md="A finding.\n\n| Metric | Q1 | Q2 |\n| --- | --- | --- |\n| NIM | 1 | 2 |\n",
        evidence_json="{}",
        status="pending",
        adversarial_verdict=None,
        budget_tier="standard",
        provenance="engine",
        tainted_by_proposal_id=None,
    )
    html = _proposal_group_card([prop])
    assert html.count("<div") == html.count("</div>"), (
        f"unbalanced <div>s: {html.count('<div')} open vs {html.count('</div>')} close"
    )


def test_onmymind_js_has_no_dead_discuss_popup_branch() -> None:
    """Phase-5 verifier fix 6: no web button emits data-om-verb="discuss"
    anymore (Discuss is the inline-chat data-om-ask path), so the old
    window.open(thread_url) branch was unreachable from the web. The
    server-side discuss verb stays — Telegram uses it."""
    from pipeline.ledger_panel import (
        _ONMYMIND_JS,  # pyright: ignore[reportPrivateUsage]  # dead-branch regression guard
    )

    assert "thread_url" not in _ONMYMIND_JS
    assert "window.open" not in _ONMYMIND_JS


def test_incorporated_ladder_badge_is_a_research_doorway(db_path: Path) -> None:
    """Wave B (B7): the "in research" badge is a doorway — a k-chip button
    carrying data-ledger-jump into the Research queue (the existing jump
    listener opens Queues + scrolls). Other ladder values stay inert spans."""
    from onmymind.feed import FeedItem
    from pipeline.ledger_panel import render_feed_card
    from user_state.notes import create_note

    note = create_note(
        ticker="NU",
        kind="musing",
        body="unit economics wondering, now in the research queue",
        source="capture",
        context={"ledger": "musing", "ladder": "incorporated"},
        db_path=db_path,
    )
    html = render_feed_card(
        FeedItem(note=note, item_type="musing", ladder="incorporated", wondering=None)
    )
    assert 'data-ledger-jump="ledger-jump-research"' in html
    assert 'class="k-chip k-chip-btn om-ladder"' in html
    assert ">in research</button>" in html
    # A merely-saved musing keeps the inert badge — no doorway.
    saved = render_feed_card(
        FeedItem(note=note, item_type="musing", ladder="saved", wondering=None)
    )
    assert "data-ledger-jump" not in saved
    assert '<span class="om-ladder">saved</span>' in saved


def test_queues_summary_reflects_armed_falsifiers(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wave B (B11): the Queues summary said "N pending" (research+reconcile+
    worldview) while the block ALSO held a large armed-falsifiers table — the
    badge must reflect what's inside. The count is parsed off the rendered
    table header, so it can never drift from what actually rendered."""
    import pipeline.ledger_panel as lp

    monkeypatch.setattr(
        lp,
        "render_armed_falsifiers_table",
        lambda _db: (
            '<h4 class="ledger-armed-h">Armed falsifiers (184)</h4>'
            '<table class="ledger-armed-table"></table>'
        ),
    )
    html = lp.render_ledger_panel(db_path)
    assert "184 armed falsifiers" in html
    # The badge sits inside the <summary> — visible while the block is closed.
    summ_start = html.index('<summary class="ledger-queues-sum">')
    summ_end = html.index("</summary>", summ_start)
    assert summ_start < html.index("184 armed falsifiers") < summ_end


def test_queues_summary_stays_quiet_without_armed_falsifiers(db_path: Path) -> None:
    """No armed table → no phantom badge (hide-don't-stub)."""
    html = render_ledger_panel(db_path)
    assert "armed falsifier" not in html
