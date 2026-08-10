"""Read-only Overview fragment retained for Work OS drill-throughs.

The Portfolio Cockpit home is hydrated by ``/api/work-os/portfolio``.  This
fragment preserves the richer governed overview endpoint without the retired
command-center shell, Ask dock, eager prefetch, or periodic browser polling.
"""

from __future__ import annotations

from pipeline.research_cockpit import CockpitRow


def render_overview_panel(
    rows_by_list: dict[str, list[CockpitRow]],
    coverage: dict[str, dict[str, int]] | None,
    inbox_html: str | None = None,
    upcoming_html: str | None = None,
    open_loops_html: str | None = None,
) -> str:
    """Compose existing deterministic overview renderers once per request."""

    from pipeline.analytical_dashboard_html import render_tier_coverage_strip
    from pipeline.research_cockpit import render_research_cockpit

    main = (
        '<div class="cc-today-band">'
        + (open_loops_html or "")
        + (upcoming_html or "")
        + "</div>"
        + '<div id="cc-cockpit-live">'
        + render_research_cockpit(rows_by_list)
        + "</div>"
        + render_tier_coverage_strip(coverage or {})
    )
    if not inbox_html:
        return main
    rail = (
        '<aside class="cc-home-rail"><div class="cc-home-rail-head">'
        '<h2>Inbox</h2><span class="cc-home-rail-links"><a href="/feed">full feed</a></span>'
        f"</div>{inbox_html}</aside>"
    )
    return f'<div class="cc-home-grid"><div class="cc-home-main">{main}</div>{rail}</div>'


__all__ = ["render_overview_panel"]
