"""SVG presentation ownership for the portfolio panel family."""

from __future__ import annotations

from pathlib import Path

from ui.conformance_scan import css_text, scan_surface_evidence

ROOT = Path(__file__).parents[1]
OWNED = (
    "pipeline/portfolio_panel.py",
    "pipeline/allocation_decisions_panel.py",
)


def test_portfolio_svg_emitters_have_no_presentation_debt() -> None:
    for relative in OWNED:
        evidence = scan_surface_evidence(relative, css_text(ROOT / "src" / relative))
        assert evidence.violations().get("svg-presentation", []) == []
        assert evidence.unverifiable_markup == ()
