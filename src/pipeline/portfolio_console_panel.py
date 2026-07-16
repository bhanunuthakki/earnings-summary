"""Portfolio composite consoles (Phase-5 IA: 8 sub-tabs → 3 destinations).

The owner's aggressive-redesign verdict: "too many surfaces with duplicative
functions." The eight Portfolio sub-tabs (Synthesis / Performance / Risk / Red
Team / Positioning / Decisions / Memos / Triggers) collapse into three composite
pages, each COMPOSING the existing builders behind an anchor-nav band — the S10
Provenance-console pattern (``pipeline/console_scaffold.py``):

* **Health** — thesis health & what could break it: Synthesis + Risk + Red Team.
* **Allocation** — where capital goes & how it's doing: Positioning + Performance.
* **Record** — the audit trail: Decisions + Memos + Triggers.

No builder logic is duplicated — every section is one of the existing
``render_*`` panel builders, each of which already degrades to a quiet stub on
missing data, so the consoles are robust by construction. The per-builder
``/api/panel/<id>`` fetch routes stay live (the old ids alias to these composites
via ``command_center_shell._LEGACY_PANEL_REDIRECTS``, and any direct fetch / peek
still hits the builder route). No new CSS — the kit + each builder's own styling
carry the page.
"""

from __future__ import annotations

from pathlib import Path

from identity import DEFAULT_USER_ID
from pipeline.console_scaffold import ConsoleSection, render_console


def render_portfolio_health_panel(db_path: Path, *, user_id: str = DEFAULT_USER_ID) -> str:
    """Portfolio → Health: thesis health and what could break it. Composes the
    Synthesis (thesis rollup + allocation) landing, the whole-book Risk cockpit,
    and the monthly adversarial Red Team brief."""
    from pipeline.portfolio_panel import (
        render_portfolio_risk_panel,
        render_portfolio_synthesis_panel,
    )
    from pipeline.red_team_panel import render_red_team_panel

    sections: list[ConsoleSection] = [
        ("synthesis", "Synthesis", lambda: render_portfolio_synthesis_panel(db_path)),
        ("risk", "Risk", lambda: render_portfolio_risk_panel(db_path=db_path)),
        ("red_team", "Red Team", lambda: render_red_team_panel(db_path=db_path)),
    ]
    return render_console("Health", sections, wrap_class="portfolio-health-console")


def render_portfolio_allocation_panel(
    db_path: Path, repo_root: Path | None = None, *, user_id: str = DEFAULT_USER_ID
) -> str:
    """Portfolio → Allocation: where capital goes and how it's doing. Composes
    the durable target book (Positioning) and the tracker-fed Performance page."""
    from pipeline.portfolio_panel import render_portfolio_panel
    from pipeline.positioning_panel import render_positioning_panel

    sections: list[ConsoleSection] = [
        ("positioning", "Positioning", lambda: render_positioning_panel(db_path, repo_root)),
        ("performance", "Performance", lambda: render_portfolio_panel(db_path=db_path)),
    ]
    return render_console("Allocation", sections, wrap_class="portfolio-allocation-console")


def render_portfolio_record_panel(db_path: Path, *, user_id: str = DEFAULT_USER_ID) -> str:
    """Portfolio → Record: the audit trail. Composes the allocation-decisions
    record (sizing audit + merged decisions timeline), the advisor memos, and
    the Triggers ladder (the tier-1 break watch)."""
    from pipeline.advisor_memos_panel import render_advisor_memos_panel
    from pipeline.allocation_decisions_panel import render_allocation_decisions_panel

    sections: list[ConsoleSection] = [
        (
            "decisions",
            "Decisions",
            lambda: render_allocation_decisions_panel(db_path, user_id=user_id),
        ),
        ("memos", "Memos", lambda: render_advisor_memos_panel(db_path, user_id=user_id)),
        ("triggers", "Triggers", lambda: _render_triggers(db_path)),
    ]
    return render_console("Record", sections, wrap_class="portfolio-record-console")


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
    return fragment or '<section class="panel"><p class="muted">No triggers.</p></section>'
