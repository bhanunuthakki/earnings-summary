"""UX9 grouped tabs: the 12-14 per-section tabs fold into ~6 grouped
top-level tabs; a slim sub-tab pill row switches the sections inside a
group. These tests pin:

  - the group map (ids, labels, section order) for both flavors
  - the Position-group visibility rules (held / exited-with-decisions / cold)
  - tab badges aggregating counts across the group
  - the markup contract that keeps legacy per-section ``data-tab`` ids on
    ``.tab-pane`` sub-panes (saved comment anchors + data-xtab links)
  - the JS contract (group switching, pill switching, ``#tab=`` deep links)
"""

from __future__ import annotations

import dataclasses
import sys
from datetime import date
from io import StringIO
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.models import (  # noqa: E402
    AppendixSection,
    BearCaseSection,
    CompanyDescriptionSection,
    EarningsSection,
    EvaluationSnapshotSection,
    FinancialsSection,
    IrDocsSection,
    KpiLedgerRow,
    PortfolioPositionSection,
    ProvenanceSection,
    RecentDevelopmentsSection,
    ReportFlavor,
    ReportSpec,
    SayDoSection,
    SectionStatus,
    SegmentsSection,
    SnapshotSection,
    ThesisSection,
    ValuationSnapshot,
)
from report.renderers.workspace_data import WorkspaceP3Panels  # noqa: E402
from report.renderers.workspace_html import (  # noqa: E402
    _tab_groups,  # pyright: ignore[reportPrivateUsage]  # testing an internal seam
    _tabs,  # pyright: ignore[reportPrivateUsage]  # testing an internal seam
    render,
)
from report.renderers.workspace_script import JS  # noqa: E402
from report.renderers.workspace_styles import CSS  # noqa: E402
from report.sections.p3_data import DecisionHistorySummary  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_spec(
    *,
    flavor: ReportFlavor = ReportFlavor.PORTFOLIO,
    held: bool = False,
    repo_root: str = ".",
    kpi_rows: int = 0,
) -> ReportSpec:
    """Minimal real ReportSpec — every section in its empty/OK state."""
    ledger = [
        KpiLedgerRow(
            name=f"KPI {i}",
            tier="tier_1",
            unit="%",
            current_status="green",
            history=[("2026-03-31", 10.0 + i)],
        )
        for i in range(kpi_rows)
    ]
    return ReportSpec(
        ticker="TEST",
        generation_date=date(2026, 6, 11),
        repo_root=repo_root,
        flavor=flavor,
        snapshot=SnapshotSection(
            status=SectionStatus.OK, ticker="TEST", valuation=ValuationSnapshot()
        ),
        evaluation_snapshot=(
            EvaluationSnapshotSection(status=SectionStatus.OK, ticker="TEST")
            if flavor == ReportFlavor.EVALUATION
            else None
        ),
        company_description=CompanyDescriptionSection(status=SectionStatus.OK),
        thesis=ThesisSection(status=SectionStatus.OK, kpi_ledger=ledger),
        financials=FinancialsSection(status=SectionStatus.OK),
        segments=SegmentsSection(status=SectionStatus.OK),
        earnings=EarningsSection(status=SectionStatus.OK),
        saydo=SayDoSection(status=SectionStatus.OK),
        ir_docs=IrDocsSection(status=SectionStatus.OK),
        recent_developments=RecentDevelopmentsSection(status=SectionStatus.OK),
        bear_case=BearCaseSection(status=SectionStatus.OK),
        provenance=ProvenanceSection(status=SectionStatus.OK),
        appendix=AppendixSection(status=SectionStatus.OK),
        portfolio_position=(
            PortfolioPositionSection(status=SectionStatus.OK, held=True) if held else None
        ),
    )


def _p3_with_decisions(total: int) -> WorkspaceP3Panels:
    return dataclasses.replace(
        WorkspaceP3Panels.empty(),
        decision_history=DecisionHistorySummary(
            total=total, by_kind={}, by_conviction={}, win_rate_overall=None, rows=[]
        ),
    )


def _group_map(spec: ReportSpec, p3: WorkspaceP3Panels) -> list[tuple[str, str, list[str]]]:
    return [
        (gid, label, [sid for sid, _lbl, _count, _fn in sections])
        for gid, label, sections in _tab_groups(spec, p3)
    ]


# ---------------------------------------------------------------------------
# Group map — both flavors
# ---------------------------------------------------------------------------


def test_portfolio_group_map_held() -> None:
    """Portfolio flavor: Overview leads; Position group (with Decisions)
    appears when held; every legacy section has a home."""
    got = _group_map(_make_spec(held=True), WorkspaceP3Panels.empty())
    assert got == [
        ("overview", "Overview", ["thesis", "valuation"]),
        ("quarter", "Quarter", ["earnings", "saydo", "news"]),
        ("financials", "Financials", ["financials"]),
        ("research", "Research", ["bear", "company", "exec_comp", "synthesis"]),
        ("position", "Position", ["position", "decisions"]),
        ("sources", "Sources", ["sources"]),
    ]


def test_evaluation_group_map_leads_with_research_company_first() -> None:
    """Evaluation flavor: Research is the default group and Company its
    default section — the reader hasn't internalized the business yet."""
    got = _group_map(_make_spec(flavor=ReportFlavor.EVALUATION), WorkspaceP3Panels.empty())
    assert got == [
        ("research", "Research", ["company", "bear", "exec_comp", "synthesis"]),
        ("overview", "Overview", ["thesis", "valuation"]),
        ("quarter", "Quarter", ["earnings", "saydo", "news"]),
        ("financials", "Financials", ["financials"]),
        ("sources", "Sources", ["sources"]),
    ]


def test_not_held_no_decisions_hides_position_group() -> None:
    got = _group_map(_make_spec(held=False), WorkspaceP3Panels.empty())
    assert [gid for gid, _lbl, _sids in got] == [
        "overview",
        "quarter",
        "financials",
        "research",
        "sources",
    ]


def test_exited_name_keeps_decisions_as_standalone_tab() -> None:
    """Not held but with decision history (an exited name): the audit trail
    stays reachable as a single-section Decisions group."""
    got = _group_map(_make_spec(held=False), _p3_with_decisions(3))
    assert ("decisions", "Decisions", ["decisions"]) in got
    assert [gid for gid, _lbl, _sids in got] == [
        "overview",
        "quarter",
        "financials",
        "research",
        "decisions",
        "sources",
    ]


def test_held_decisions_fold_into_position_group() -> None:
    got = _group_map(_make_spec(held=True), _p3_with_decisions(3))
    assert ("position", "Position", ["position", "decisions"]) in got
    assert all(gid != "decisions" for gid, _lbl, _sids in got)


# ---------------------------------------------------------------------------
# Badge aggregation
# ---------------------------------------------------------------------------


def test_tab_badge_aggregates_counts_across_group() -> None:
    """Overview badge = thesis KPI rows (valuation carries no count);
    Position badge = accounts + decisions."""
    spec = _make_spec(held=True, kpi_rows=2)
    out = StringIO()
    _tabs(out, _tab_groups(spec, _p3_with_decisions(3)))
    html = out.getvalue()
    assert '<span class="tab-label">Overview</span><span class="tab-count">2</span>' in html
    # Position group: 0 accounts + 3 decisions.
    assert '<span class="tab-label">Position</span><span class="tab-count">3</span>' in html


def test_tab_badge_omitted_when_no_section_counts() -> None:
    """A group whose sections all carry None counts renders no badge."""

    def _noop(_b: StringIO) -> None:
        return None

    out = StringIO()
    _tabs(out, [("g", "Group", [("a", "A", None, _noop), ("b", "B", None, _noop)])])
    assert "tab-count" not in out.getvalue()


# ---------------------------------------------------------------------------
# Rendered markup — grouped panes keep the legacy section contract
# ---------------------------------------------------------------------------


def test_render_grouped_markup_portfolio(tmp_path: Path) -> None:
    html = render(_make_spec(held=True, repo_root=str(tmp_path)))
    # Top bar: one button per GROUP, none for the old section tabs.
    assert 'data-tab="overview"' in html
    assert 'data-tab="quarter"' in html
    assert '<span class="tab-label">Say · Do</span>' not in html
    # Default group pane active, its default sub-pane active too.
    assert (
        '<div class="tab-group-pane active" data-tab-group="overview">'
        '<div class="subtabs">'
        '<button class="subtab active" data-subtab="thesis">' in html
    )
    # Section panes keep the legacy per-section data-tab ids on .tab-pane —
    # the contract saved comment anchors and data-xtab links resolve against.
    for sid in (
        "thesis",
        "valuation",
        "earnings",
        "saydo",
        "news",
        "financials",
        "bear",
        "company",
        "exec_comp",
        "synthesis",
        "position",
        "decisions",
        "sources",
    ):
        assert f'<div class="tab-pane subtab-pane active" data-tab="{sid}">' in html or (
            f'<div class="tab-pane subtab-pane" data-tab="{sid}">' in html
        ), sid
    # Single-section groups render the pane directly — no pill row.
    assert (
        'data-tab-group="financials">'
        '<div class="tab-pane subtab-pane active" data-tab="financials">' in html
    )
    assert 'data-subtab="financials"' not in html
    assert 'data-subtab="sources"' not in html


def test_render_evaluation_defaults_to_company(tmp_path: Path) -> None:
    html = render(_make_spec(flavor=ReportFlavor.EVALUATION, repo_root=str(tmp_path)))
    assert (
        '<div class="tab-group-pane active" data-tab-group="research">'
        '<div class="subtabs">'
        '<button class="subtab active" data-subtab="company">' in html
    )


# ---------------------------------------------------------------------------
# JS + CSS contract
# ---------------------------------------------------------------------------


def test_workspace_js_carries_grouped_tab_contract() -> None:
    assert "data-tab-group" in JS
    assert "data-subtab" in JS
    assert ".subtab-pane[data-tab]" in JS
    assert "activateSection" in JS
    # Deep links: #tab=<section-or-group id> + live hash changes.
    assert "#tab=" in JS
    assert "hashchange" in JS


def test_workspace_css_styles_grouped_tabs() -> None:
    assert ".tab-group-pane.active { display: block; }" in CSS
    assert ".subtab.active" in CSS
    # Print: group panes flatten open; both tab strips hide.
    assert ".tab-group-pane { display: block !important; }" in CSS
    assert ".tabs, .subtabs { display: none; }" in CSS
