"""The retained portfolio Record console and its dated decision/research log."""

from __future__ import annotations

from html import escape
from pathlib import Path

from dcf.input_evidence import SourceReadContext
from identity import DEFAULT_USER_ID
from pipeline.console_scaffold import ConsoleSection, render_console
from pipeline.portfolio_styles import console_css
from ui.panel import panel_empty

# The D1 tile grid + Band-1 brief. Tokens only (design_language §2): auto-fit
# tiles ≥460px so a wide desktop viewport gets 2-3 columns and a narrow one
# degrades to a single column without a media query; `csec-wide` spans the
# brief and the landing section across every column.
_CONSOLE_CSS = console_css()


# composition over already-governed caches; every fact is individually
# best-effort so a missing table drops its line, never the brief.
# --------------------------------------------------------------------------- #


def _brief_shell(title: str, sub: str, lines: list[str], links: list[str]) -> str:
    body = "".join(f'<p class="cb-line">{ln}</p>' for ln in lines) or (
        '<p class="cb-line muted">Not enough live data for a read yet — the tiles '
        "below carry the detail.</p>"
    )
    links_html = f'<div class="cb-links">{"".join(links)}</div>' if links else ""
    return (
        '<div class="console-brief" data-brief-title="'
        f'{escape(title)}"><p class="cb-sub">{escape(sub)}</p>'
        f"{body}{links_html}</div>"
    )


def _jump_chip(anchor: str, label: str) -> str:
    return (
        f'<button type="button" class="k-chip k-chip-btn" data-console-jump="csec-{anchor}">'
        f"{escape(label)}</button>"
    )


def _record_brief(db_path: Path) -> str:
    """The Record read: the last thing that entered the record and the name
    farthest from fair value, each a doorway into its tile."""
    lines: list[str] = []
    links: list[str] = []

    try:
        from user_state.ledger import list_recent_entries

        entries = list_recent_entries(user_id=DEFAULT_USER_ID, limit=1, db_path=db_path)
        if entries:
            e = entries[0]
            when = str(e.created_at)[:10]
            lines.append(
                f"Last on the record: <strong>{escape(e.entry_kind.replace('_', ' '))}</strong> "
                f"on {escape(e.ticker or 'the book')} ({escape(when)})."
            )
            links.append(_jump_chip("decisions", "Decisions"))
    except Exception:
        pass

    try:
        from pipeline.analytical_dashboard import build_analytical_dashboard

        dash = build_analytical_dashboard(
            db_path, sections={"trigger_ladder"}, list_types=("portfolio", "evaluation")
        )
        rows = [r for r in dash.trigger_ladder if r.over_under_pct is not None]
        if rows:
            top = max(rows, key=lambda r: abs(r.over_under_pct or 0.0))
            lines.append(
                f"Farthest from fair: <strong>{escape(top.ticker)}</strong> at "
                f"{(top.over_under_pct or 0.0) * 100.0:+.0f}% vs its DCF."
            )
            links.append(_jump_chip("triggers", "Valuation triggers"))
    except Exception:
        pass

    return _brief_shell(
        "The read",
        "What last entered the audit trail and where valuation is most stretched.",
        lines,
        links,
    )


def _record_sections(
    db_path: Path, user_id: str, source_context: SourceReadContext | None = None
) -> list[ConsoleSection]:
    """Portfolio → Record: the audit trail, on the D1 page model — the Band-1
    read leads, Decisions spans wide. Notes and valuation triggers use their existing owners."""
    from pipeline.allocation_decisions_panel import render_allocation_decisions_panel
    from pipeline.journal_panel import render_journal_panel

    return [
        ("brief", "Read", lambda: _record_brief(db_path)),
        (
            "decisions",
            "Decisions",
            lambda: render_allocation_decisions_panel(db_path, user_id=user_id),
        ),
        (
            "research-items",
            "Research Items",
            lambda: render_journal_panel(
                db_path,
                user_id=user_id,
                embedded=True,
                research_items_only=False,
            ),
        ),
        ("triggers", "Triggers", lambda: _render_triggers(db_path)),
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
        nav_exclude=("brief",),
        grid=True,
        wide=("brief", "decisions"),
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
