"""P4.3 cross-links between workspace tabs: earnings themes ↔ bear case,
valuation ↔ thesis KPI drivers, signals → news, concentration → bear. The
links are ``data-xtab`` anchors the workspace JS turns into tab switches;
these tests pin the markup contract on both ends of each pair."""

from __future__ import annotations

from datetime import date
from io import StringIO

from report.models import (
    BearCaseSection,
    EarningsSection,
    FailureMode,
    SectionStatus,
    SignalRow,
    SignalsSection,
    ThesisSection,
    ValuationBasisSection,
)
from report.renderers.workspace_html import (
    _earnings_themes_panel,  # pyright: ignore[reportPrivateUsage]
    _failure_modes_panel,  # pyright: ignore[reportPrivateUsage]
    _signals_panel,  # pyright: ignore[reportPrivateUsage]
    _thesis_hygiene_panels,  # pyright: ignore[reportPrivateUsage]
    _valuation_tab,  # pyright: ignore[reportPrivateUsage]
    _xlink_html,  # pyright: ignore[reportPrivateUsage]
)
from report.renderers.workspace_script import JS


def test_xlink_html_markup() -> None:
    out = _xlink_html("bear", "bear case ↔", "panel-failure-modes")
    assert 'data-xtab="bear"' in out
    assert 'data-anchor="panel-failure-modes"' in out
    assert 'class="panel-xlink"' in out
    assert _xlink_html("news", "related news →") == (
        '<a class="panel-xlink" href="#" data-xtab="news">related news →</a>'
    )


def test_themes_panel_links_to_bear() -> None:
    section = EarningsSection(status=SectionStatus.OK, themes_note="only QA side had material")
    out = StringIO()
    _earnings_themes_panel(out, section)
    html = out.getvalue()
    assert 'id="panel-themes"' in html
    assert 'data-xtab="bear"' in html
    assert 'data-anchor="panel-failure-modes"' in html


def test_failure_modes_panel_links_back_to_themes() -> None:
    bear = BearCaseSection(
        status=SectionStatus.OK,
        failure_modes=[
            FailureMode(
                hypothesis="NIM compresses faster than priced",
                evidence_in_data="NIM -100bps QoQ",
                leading_indicator="cost of risk",
                quantitative_impact="-20% EPS",
                refutation_criteria="NIM stabilizes 2 quarters",
            )
        ],
    )
    out = StringIO()
    _failure_modes_panel(out, bear)
    html = out.getvalue()
    assert 'id="panel-failure-modes"' in html
    assert 'data-xtab="earnings"' in html
    assert 'data-anchor="panel-themes"' in html


def test_valuation_rationale_links_to_kpi_ledger() -> None:
    vb = ValuationBasisSection(
        status=SectionStatus.OK,
        multiple_name="P/E (NTM)",
        rationale="Earnings-based multiple fits.",
    )
    out = StringIO()
    _valuation_tab(out, vb)
    html = out.getvalue()
    assert 'data-xtab="thesis"' in html
    assert 'data-anchor="panel-kpi-ledger"' in html


def test_kpi_ledger_anchors_and_links_to_valuation() -> None:
    thesis = ThesisSection(
        status=SectionStatus.OK,
        thesis_full="t",
        overall_breach_status="ok",
        kpi_ledger=[],
    )
    # Ledger renders only when kpi_ledger is non-empty — give it one row.
    from report.models import KpiLedgerRow

    thesis.kpi_ledger.append(
        KpiLedgerRow(
            name="ROE",
            tier="tier_1",
            unit="%",
            current_status="green",
            history=[("2026-03-31", 25.0)],
        )
    )
    out = StringIO()
    _thesis_hygiene_panels(out, thesis, date(2026, 6, 10))
    html = out.getvalue()
    assert 'id="panel-kpi-ledger"' in html
    assert 'data-xtab="valuation"' in html


def test_signals_panel_links_to_news() -> None:
    signals = SignalsSection(
        status=SectionStatus.OK,
        red_signals=[
            SignalRow(
                metric_name="Revenue",
                metric_kind="financial",
                signal_type="inflection",
                severity="red",
                narrative="growth rolling over",
                value_summary="z=-2.1",
            )
        ],
    )
    out = StringIO()
    _signals_panel(out, signals)
    html = out.getvalue()
    assert 'id="panel-signals"' in html
    assert 'data-xtab="news"' in html


def test_workspace_js_carries_xtab_handler() -> None:
    assert "data-xtab" in JS
    assert "scrollIntoView" in JS
