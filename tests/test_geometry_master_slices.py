"""Focused ownership checks for the final local CSS geometry tails."""

from __future__ import annotations

from pathlib import Path

from pipeline.research_panel_styles import DIET_PANEL_STYLE
from report.renderers.workspace_reader_assets import READER_CSS
from report.renderers.workspace_styles import READER_OVERRIDE_CSS
from ui.conformance_scan import geometry_debt_fingerprints

ROOT = Path(__file__).resolve().parents[1]


def test_geometry_tail_consumers_are_css_free() -> None:
    for rel in (
        "src/pipeline/diet_panel.py",
        "src/report/renderers/workspace_reader_assets.py",
    ):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert geometry_debt_fingerprints(rel, source) == ()


def test_geometry_tail_slices_are_composed_without_output_reordering() -> None:
    diet_source = (ROOT / "src/pipeline/diet_panel.py").read_text(encoding="utf-8")
    reader_source = (ROOT / "src/report/renderers/workspace_reader_assets.py").read_text(
        encoding="utf-8"
    )
    assert "DIET_PANEL_STYLE" in diet_source
    assert "READER_OVERRIDE_CSS" in reader_source
    assert ".diet-sec" in DIET_PANEL_STYLE
    assert ".reader-group-title" in READER_OVERRIDE_CSS
    assert READER_CSS.endswith(READER_OVERRIDE_CSS)
