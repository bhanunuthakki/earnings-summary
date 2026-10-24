"""Regression contract for the dashboard layout master.

These checks are intentionally source-level: the CSS is inlined into several
independently served fragments, so a rendered-page assertion alone would miss
one surface quietly growing its own visual vocabulary again.
"""

from __future__ import annotations

from pathlib import Path

from dashboard._styles import ACTIONS_CSS, COCKPIT_CSS, CSS, INBOX_CSS, UPCOMING_CSS
from pipeline.dashboard_html import render_actions_panel
from pipeline.research_cockpit import render_research_cockpit

ROOT = Path(__file__).resolve().parents[1]


def test_all_owned_surface_css_is_composed_by_dashboard_master() -> None:
    """The four fragment vocabularies are exported from one source string."""
    for fragment in (INBOX_CSS, UPCOMING_CSS, ACTIONS_CSS, COCKPIT_CSS):
        assert fragment in CSS


def test_owned_renderers_do_not_reintroduce_local_visual_blocks() -> None:
    """Local modules keep markup/behavior; layout rules stay in _styles.py."""
    sources = {
        "dashboard_html.py": (ROOT / "src/pipeline/dashboard_html.py").read_text(),
        "analytical_dashboard_html.py": (
            ROOT / "src/pipeline/analytical_dashboard_html.py"
        ).read_text(),
        "research_cockpit.py": (ROOT / "src/pipeline/research_cockpit.py").read_text(),
        "inbox.py": (ROOT / "src/dashboard/inbox.py").read_text(),
        "upcoming.py": (ROOT / "src/dashboard/upcoming.py").read_text(),
    }
    for source in sources.values():
        assert ".actions-section {" not in source
        assert ".calib-bar {" not in source
        assert ".cockpit-section h2 {" not in source
        assert ".ix-stream {" not in source
        assert ".up-strip {" not in source


def test_action_and_cockpit_fragments_use_master_style_aliases() -> None:
    actions = render_actions_panel()
    cockpit = render_research_cockpit({})
    assert "{ACTIONS_CSS}" not in actions
    assert ".actions-section" in actions
    assert ".cockpit-section" in cockpit or ".cockpit-table" in cockpit
