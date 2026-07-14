"""The Ledger flat musings panel — rendering over the analyst_notes spine."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from capture import ingest
from capture.matcher import build_roster_index
from pipeline.ledger_panel import render_ledger_list, render_ledger_panel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


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
