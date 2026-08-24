"""Structural contract for the isolated Performance & Risk page mockup."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOCKUP_PATH = ROOT / "mockups" / "performance_risk_mockup.html"


def _mockup() -> str:
    return MOCKUP_PATH.read_text(encoding="utf-8")


def _section(html: str, testid: str) -> str:
    start = html.index(f'data-testid="{testid}"')
    end = html.index("</section>", start)
    return html[start:end]


def test_mockup_is_isolated_from_production_renderers() -> None:
    shell = (ROOT / "src" / "pipeline" / "work_os_shell.py").read_text(encoding="utf-8")

    assert MOCKUP_PATH.exists()
    assert "performance_risk_mockup.html" not in shell
    assert '"mockups" / "harvey_sidebar_flow.html"' in shell


def test_label_contract_and_combined_page_title() -> None:
    html = _mockup()

    assert html.count('data-label-surface="sidebar">Performance<') == 1
    assert html.count('data-label-surface="fixed-header">Performance<') == 1
    assert 'data-label-surface="top-panel"' not in html
    assert '<div class="top-panel">' not in html
    assert "<h1>Performance &amp; Risk</h1>" in html
    assert "Performance vs Index" not in html
    assert "Risk &amp; Allocations" not in html


def test_removed_cards_and_tables_are_absent() -> None:
    html = _mockup()

    for removed in (
        ">Read<",
        ">The read<",
        "Position drivers",
        "Position Drivers",
        "Next dollar",
        "Next Dollar",
        "Incremental Dollar Recommendation",
    ):
        assert removed not in html


def test_performance_card_has_one_title_and_one_horizontal_pnl_row() -> None:
    html = _mockup()
    section = _section(html, "performance-card")
    pnl_row = section.split('data-testid="pnl-row"', 1)[1].split(
        'data-testid="benchmark-chart"', 1
    )[0]

    assert section.count("<h2") == 1
    assert '<h2 class="k-card-title">Index Benchmarking</h2>' in section
    assert "Performance vs benchmarks" not in section
    assert section.count('data-testid="pnl-metric"') == 4
    for label in ("Actual P&amp;L", "Matched SPY P&amp;L", "Alpha vs SPY", "Modified Dietz"):
        assert label in section
    assert ".pnl-row { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));" in html
    assert pnl_row.index("Actual P&amp;L") < pnl_row.index("Matched SPY P&amp;L")


def test_every_content_card_has_one_visible_title() -> None:
    html = _mockup()

    expected = {
        "performance-card": "Index Benchmarking",
        "posture-card": "Portfolio Posture",
        "allocation-card": "Portfolio Allocation",
        "risk-explorer-card": "Risk Explorer",
    }
    for testid, title in expected.items():
        section = _section(html, testid)
        assert section.count("<h2") == 1
        assert f">{title}</h2>" in section

    assert "Risk Budget" not in html
    assert 'data-testid="risk-budget-card"' not in html


def test_policy_mix_is_editable_where_it_is_cited() -> None:
    html = _mockup()
    section = _section(html, "performance-card")
    editor = section.split('data-testid="policy-mix-editor"', 1)[1].split("</form>", 1)[0]

    assert "Policy mix" in editor
    assert 'class="policy-mix-editor"' in section
    for benchmark in ("QQQ", "SGOV", "VTI", "VWO"):
        assert f'for="policy{benchmark}"' in editor
        assert f'id="policy{benchmark}"' in editor
        assert 'type="number"' in editor
    assert ">Apply mix</button>" in editor
    assert "applyPolicyMix" in html
    assert 'id="policyMixError"' in editor
    assert 'aria-live="polite"' in editor
    assert '<div class="k-well">Policy mix' not in section


def test_allocation_card_reuses_the_home_cockpit_four_bucket_contract() -> None:
    html = _mockup()
    section = _section(html, "allocation-card")
    rows = re.findall(r'data-allocation-pct="([^\"]+)"[^>]*>\s*<span>([^<]+)</span>', section)

    assert rows == [
        ("18", "US ETF"),
        ("7", "Intl ETF"),
        ("41", "US Equity"),
        ("20", "Intl Equity"),
    ]
    assert sum(int(value) for value, _ in rows) + 14 == 100
    assert "14% cash reserve" in section
    assert section.count('class="allocation-track"') == 4


def test_risk_is_consolidated_with_correlation_grid_and_truth_labels() -> None:
    html = _mockup()
    explorer = _section(html, "risk-explorer-card")

    assert "Correlation" in explorer
    assert 'data-risk-panel="correlation"' in explorer
    assert 'data-testid="correlation-grid"' in explorer
    assert "16 names" in explorer
    assert "252 common trading days" in explorer
    assert "prices through 2026-05-18" in explorer
    assert "Not modeled" in explorer
    assert 'data-truth="captured-live-snapshot"' in html
    assert 'data-truth="derived-local-cache"' in explorer
    assert "Illustrative snapshot" in html


def test_prototype_interactions_and_responsive_contract_are_present() -> None:
    html = _mockup()

    assert "setPerformanceWindow" in html
    assert "setRiskPanel" in html
    assert "showPrototypeToast" in html
    assert "togglePostureEditor" in html
    assert 'aria-live="polite"' in html
    assert 'aria-selected="true"' in html
    assert 'role="tablist"' in html
    assert "@media (max-width: 70rem)" in html
    assert "@media (max-width: 48rem)" in html
    assert ".pnl-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }" in html
    assert ".pnl-row { grid-template-columns: minmax(0, 1fr); }" in html
    assert ".correlation-scroll { overflow: auto;" in html
    assert "prefers-reduced-motion: reduce" in html


def test_mockup_uses_kit_classes_and_has_no_inline_styles() -> None:
    html = _mockup()

    for primitive in ("k-card", "k-btn", "k-chip", "k-pill", "k-well"):
        assert primitive in html
    assert not re.search(r'\sstyle="', html)
    assert "Visual-only kit simulation" in html
