"""Matrix renderer: ratio/percentage rows show ABSOLUTE levels, dollar/flow rows
keep YoY% + CAGR. Plus the financials currency label is data-driven, not a
hardcoded "USD millions".

Regression for NU report comments: ROE/NIM/CET1 rendered as YoY% growth of a
ratio (e.g. "+64%") — meaningless; the user wants the absolute level (e.g.
"29%"). And the "USD millions" label was hardcoded, wrong for ~10 non-USD names.
"""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.models import (  # noqa: E402
    FinancialsSection,
    QuarterlyLineItem,
    SectionStatus,
    SegmentsSection,
)
from report.renderers.charts_v2 import (  # noqa: E402
    CSS,
    BarSpec,
    LineSeries,
    MatrixRow,
    _is_level_unit,  # pyright: ignore[reportPrivateUsage]  # testing an internal seam
    bar_chart,
    multi_line_chart,
    stacked_area,
    yoy_heatmap_table,
)
from report.renderers.workspace_html import (  # noqa: E402
    _line_items_levels_panel,  # pyright: ignore[reportPrivateUsage]
)
from ui.conformance_scan import css_text as _css_text  # noqa: E402
from ui.conformance_scan import geometry_debt_fingerprints, scan_surface  # noqa: E402

_PERIODS = [f"2024 Q{i}" for i in range(1, 5)] + [f"2025 Q{i}" for i in range(1, 5)]


def test_ratio_row_renders_absolute_level_not_yoy() -> None:
    # ROE rising 20% -> 29% over 8 quarters; unit "%" => level mode.
    levels: list[float | None] = [20.0, 21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 29.0]
    out = yoy_heatmap_table([MatrixRow(name="ROE", levels=levels, unit="%")], _PERIODS, title="")
    # Absolute levels are shown.
    assert "29.0%" in out
    assert "24.0%" in out
    # The YoY% of the ratio (29 vs 23 a year ago = +26.1%) is NOT rendered —
    # that meaningless growth-of-a-ratio was the bug.
    assert "+26.1%" not in out
    # Trailing column shows the 1y absolute change in pp (29 - 23 = +6.0pp).
    assert "+6.0pp" in out


def test_flow_row_keeps_yoy_pct() -> None:
    # A dollar flow keeps the YoY% treatment.
    levels: list[float | None] = [100.0, 100.0, 100.0, 100.0, 150.0, 160.0, 170.0, 200.0]
    out = yoy_heatmap_table(
        [MatrixRow(name="Revenue", levels=levels, unit="USD millions")], _PERIODS, title=""
    )
    # Latest YoY = 200 / 100 - 1 = +100.0%.
    assert "+100.0%" in out


def test_is_level_unit_classification() -> None:
    for u in ("%", "percent", "bps", "ratio", "PP"):
        assert _is_level_unit(u), u
    for u in ("USD millions", "USD", "", "bn"):
        assert not _is_level_unit(u), u


def test_line_items_panel_uses_section_currency() -> None:
    fin = FinancialsSection(
        status=SectionStatus.OK,
        currency="EUR",
        quarter_labels=["2025 Q1"],
        line_items=[
            QuarterlyLineItem(
                line_item="Revenue",
                unit="USD millions",
                digits=0,
                quarters=["2025 Q1"],
                values=[1_000.0],
            )
        ],
    )
    seg = SegmentsSection(status=SectionStatus.MISSING_DATA)
    out = StringIO()
    _line_items_levels_panel(out, fin, seg)
    html_out = out.getvalue()
    assert "EUR millions" in html_out
    assert "USD millions" not in html_out


def test_chart_css_uses_the_canonical_visual_tokens() -> None:
    """charts_v2 is governed CSS; visual choices cannot hide behind its old exemption."""
    violations = scan_surface(
        "report/renderers/charts_v2.py",
        _css_text(PROJECT_ROOT / "src" / "report" / "renderers" / "charts_v2.py"),
    )
    assert violations == {}, violations
    assert "var(--fs-" in CSS
    assert "var(--mono)" in CSS
    assert "var(--radius)" in CSS


def test_chart_css_has_no_raw_geometry_debt() -> None:
    source = PROJECT_ROOT / "src" / "report" / "renderers" / "charts_v2.py"
    assert geometry_debt_fingerprints("report/renderers/charts_v2.py", _css_text(source)) == ()


def test_chart_svg_uses_tokenized_current_color_presentation() -> None:
    """Dynamic series colors use currentColor instead of raw SVG presentation values."""
    outputs = (
        bar_chart(BarSpec(values=[1.0, -1.0], labels=["A", "B"], title="Bars")),
        multi_line_chart([LineSeries("Revenue", [1.0, 2.0])], ["A", "B"], "Lines"),
        multi_line_chart([LineSeries("Point", [1.0])], ["A"], "Point"),
        stacked_area([LineSeries("Revenue", [1.0, 2.0])], ["A", "B"], "Area"),
    )
    for output in outputs:
        assert 'fill="currentColor"' in output
        assert 'fill="{' not in output
        assert 'stroke="{' not in output
    assert 'stroke="currentColor"' in outputs[1]
    assert 'stroke="var(--surface)"' in outputs[3]
