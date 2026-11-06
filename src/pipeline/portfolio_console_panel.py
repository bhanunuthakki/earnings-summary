"""The Decision Audit Log: owner decisions first, supporting records on demand."""

from __future__ import annotations

from pathlib import Path

from dcf.input_evidence import SourceReadContext
from identity import DEFAULT_USER_ID
from pipeline.console_scaffold import ConsoleSection, render_console
from pipeline.portfolio_styles import console_css
from ui.panel import panel_empty

_CONSOLE_CSS = console_css()
_RECORD_REFERENCE = ("process", "research-items", "triggers", "activity")


def _record_sections(
    db_path: Path, user_id: str, source_context: SourceReadContext | None = None
) -> list[ConsoleSection]:
    """Lead with the decision log. Reference sections load when opened."""
    from pipeline.allocation_decisions_panel import (
        render_allocation_decisions_panel,
        render_decisions_timeline,
    )
    from pipeline.decision_log import render_decision_log_panel
    from pipeline.journal_panel import render_journal_panel

    return [
        ("decisions", "Decisions", lambda: render_decision_log_panel(db_path)),
        (
            "process",
            "Sizing and calibration",
            lambda: render_allocation_decisions_panel(db_path, user_id=user_id),
        ),
        (
            "research-items",
            "Research notes",
            lambda: render_journal_panel(
                db_path,
                user_id=user_id,
                embedded=True,
                research_items_only=False,
            ),
        ),
        ("triggers", "Price triggers", lambda: _render_triggers(db_path)),
        (
            "activity",
            "All record activity",
            lambda: render_decisions_timeline(db_path, user_id=user_id),
        ),
    ]


def render_portfolio_record_fragment(
    db_path: Path,
    fragment: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    source_context: SourceReadContext | None = None,
) -> str:
    """Build one audit section without waiting for unrelated dependencies."""
    sections = (
        _record_sections(db_path, user_id)
        if source_context is None
        else _record_sections(db_path, user_id, source_context)
    )
    for anchor, _label, builder in sections:
        if anchor == fragment:
            return builder()
    raise ValueError("Unknown record fragment")


def render_portfolio_record_panel(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    lazy: bool = False,
    source_context: SourceReadContext | None = None,
) -> str:
    """Render the audit console, deferring independent sections on live routes."""
    sections = (
        _record_sections(db_path, user_id)
        if source_context is None
        else _record_sections(db_path, user_id, source_context)
    )
    return _CONSOLE_CSS + render_console(
        "Record",
        sections,
        wrap_class="portfolio-record-console",
        nav_exclude=tuple(anchor for anchor, _label, _builder in sections),
        collapsed=_RECORD_REFERENCE,
        deferred={
            anchor: f"/api/panel/portfolio_record?fragment={anchor}"
            for anchor, _label, _builder in sections
        }
        if lazy
        else None,
    )


def _render_triggers(db_path: Path) -> str:
    """The Triggers ladder (old ``holdings`` panel id) — the trigger-ladder
    section of the analytical dashboard, rendered through the same seam the
    ``/api/panel/holdings`` route uses so there is no second code path."""
    from pipeline.analytical_dashboard import build_analytical_dashboard
    from pipeline.analytical_dashboard_html import render_panel_fragment

    # The owner's thesis'd names live on the portfolio + evaluation lists;
    # watchlist rows are bulk-onboarded stubs — exactly the irrelevant data
    # he flagged (2026-07-14) — so the Record console scopes them out. The
    # standalone /api/panel/holdings route keeps the builder's default scope.
    dash = build_analytical_dashboard(
        db_path,
        sections={"trigger_ladder"},
        ticker=None,
        list_types=("portfolio", "evaluation"),
    )
    fragment = render_panel_fragment(dash, "holdings")
    return fragment or panel_empty("No triggers.")
