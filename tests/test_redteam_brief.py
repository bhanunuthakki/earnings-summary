"""Render tests for the Red Team Brief (src/redteam/brief.py). PR5 shipped it
read-only (status chips, no response actions); PR6 adds REFUTE/ACCEPT/DEFER
buttons on answerable items and the month-status header pill."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from redteam.brief import render_red_team_brief  # noqa: E402
from redteam.gate import MonthStatus  # noqa: E402
from redteam.models import Kind, RedTeamItemRow, Severity, Status  # noqa: E402


def _row(
    *,
    id: int = 1,  # noqa: A002 - shadows builtin only within this test helper's scope
    ticker: str | None = "NU",
    lens: str = "fx_translation",
    kind: Kind = "per_name",
    severity: Severity = "high",
    status: Status = "open",
) -> RedTeamItemRow:
    return RedTeamItemRow(
        id=id,
        run_key="red_team_2026_08",
        ticker=ticker,
        lens=lens,
        kind=kind,
        attack_md="Attack text.",
        question_md="Question text?",
        proposed_change_md="Proposed change text.",
        severity=severity,
        status=status,
        defer_count=0,
        response_md=None,
        responded_at=None,
        created_at=datetime(2026, 8, 1, 10, 0, 0),
    )


def test_empty_state_renders_no_items_message() -> None:
    html = render_red_team_brief([], run_key=None)
    assert "No red-team items yet" in html
    assert "<style>" in html  # self-registered CSS present even in the empty state


def test_per_name_item_renders_ticker_severity_and_lens() -> None:
    html = render_red_team_brief([_row()], run_key="red_team_2026_08")
    assert "NU" in html
    assert "HIGH" in html
    assert "k-pill" in html
    assert "k-well" in html
    assert "Attack text." in html
    assert "Question text?" in html
    assert "Proposed change text." in html
    assert "OPEN" in html


def test_cross_book_item_renders_without_a_ticker() -> None:
    row = _row(id=2, ticker=None, lens="factor_block", kind="cross_book", severity="med")
    html = render_red_team_brief([row], run_key="red_team_2026_08")
    assert "Cross-book" in html
    assert "Factor Block" in html


def test_all_severity_and_status_tones_render_without_error() -> None:
    for severity in ("high", "med", "low"):
        for status in ("open", "refuted", "accepted", "deferred", "closed"):
            html = render_red_team_brief(
                [_row(severity=severity, status=status)], run_key="red_team_2026_08"
            )
            assert "k-well" in html


def test_items_grouped_per_name_then_cross_book() -> None:
    per_name = _row(id=1)
    cross = _row(id=2, ticker=None, lens="style_drift", kind="cross_book")
    html = render_red_team_brief([per_name, cross], run_key="red_team_2026_08")
    assert html.index("Per-name") < html.index("Cross-book")


def test_open_item_renders_response_action_buttons() -> None:
    html = render_red_team_brief([_row(status="open")], run_key="red_team_2026_08")
    assert 'data-rt-act="refute"' in html
    assert 'data-rt-act="accept"' in html
    assert 'data-rt-act="defer"' in html
    assert "k-btn" in html
    assert "ix-act" in html


def test_deferred_item_still_renders_action_buttons() -> None:
    # Deferred is not terminal — refute/accept remain answerable.
    html = render_red_team_brief([_row(status="deferred")], run_key="red_team_2026_08")
    assert 'data-rt-act="refute"' in html


def test_terminal_status_items_render_no_action_buttons() -> None:
    # The panel-wide wiring <script> (a static selector referencing
    # '[data-rt-act]') AND the CSS rule '.rt-actions { ... }' are still
    # present once there's at least one item, so assert on the rendered
    # element markup, not the bare class-name substring.
    for status in ("refuted", "accepted", "closed"):
        html = render_red_team_brief([_row(status=status)], run_key="red_team_2026_08")
        assert '<div class="rt-actions"' not in html
        assert 'data-rt-act="refute"' not in html


def test_response_script_only_present_when_there_are_items() -> None:
    html = render_red_team_brief([_row()], run_key="red_team_2026_08")
    assert "<script>" in html
    assert 'data-panel="red_team"' in html
    # No script tag / actions wiring in the empty state — nothing to click.
    empty_html = render_red_team_brief([], run_key=None)
    assert "<script>" not in empty_html
    assert "data-rt-act" not in empty_html


def test_month_status_pill_absent_without_month_status() -> None:
    html = render_red_team_brief([_row()], run_key="red_team_2026_08")
    assert "k-pill-warn" not in html
    assert "CLOSED" not in html


def test_month_status_pill_open_count() -> None:
    ms = MonthStatus(run_key="red_team_2026_08", unresolved_count=3, is_closed=False)
    html = render_red_team_brief([_row()], run_key="red_team_2026_08", month_status=ms)
    assert "OPEN 3" in html
    assert "k-pill-warn" in html


def test_month_status_pill_closed() -> None:
    ms = MonthStatus(run_key="red_team_2026_08", unresolved_count=0, is_closed=True)
    html = render_red_team_brief(
        [_row(status="accepted")], run_key="red_team_2026_08", month_status=ms
    )
    assert "CLOSED" in html
    assert "k-pill-ok" in html
