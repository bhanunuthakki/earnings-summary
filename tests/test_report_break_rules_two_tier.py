"""Tests for the §2 thesis break-rule two-tier renderer.

Covers the split into "Catastrophic tripwires" (universal) and "Thesis breakers"
(business_model) introduced when the universal GAAP-margin rules were retired in
favor of per-ticker rules. Pins:
  - Both tiers render under the same column contract.
  - Empty tiers are fully suppressed (no heading, no table).
  - Universal tier renders before business_model tier in both formats.
  - Pre-tier persisted rows default to business_model (back-compat).
"""

from __future__ import annotations

from datetime import datetime
from io import StringIO

from report.models import (
    BreakRuleEvaluation,
    BreakRuleObservation,
    SoftRuleEvaluation,
    ThesisSection,
)
from report.renderers.html import _break_rules_block as _html_break_rules
from report.renderers.markdown import _break_rules_block as _md_break_rules


def _ev(
    rule_id: str,
    kpi: str,
    tier: str,
    status: str = "ok",
    threshold: float = 0,
    comparator: str = "lt",
) -> BreakRuleEvaluation:
    return BreakRuleEvaluation(
        rule_id=rule_id,
        kpi_name=kpi,
        comparator=comparator,
        threshold=threshold,
        consecutive_periods=2,
        tier=tier,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        detail=f"{rule_id} detail",
        narrative=f"{kpi} narrative",
        observations=[
            BreakRuleObservation(period_end="2026-01-31", value=42.0, unit="percent"),
            BreakRuleObservation(period_end="2025-10-31", value=44.0, unit="percent"),
        ],
    )


def _section(evals: list[BreakRuleEvaluation]) -> ThesisSection:
    return ThesisSection(
        status="ok",  # type: ignore[arg-type]
        thesis_full="t",
        overall_breach_status="ok",
        break_rule_evaluations=evals,
        last_evaluated_at=datetime(2026, 5, 15, 12, 0, 0),
    )


def test_html_renders_universal_table_when_only_universal_present() -> None:
    """Only universal rules -> one 'Catastrophic tripwires' table, no 'Thesis
    breakers' heading."""
    s = _section([_ev("rev_decline", "Revenue YoY", "universal")])
    out = StringIO()
    _html_break_rules(out, s, "TEST")
    html = out.getvalue()
    assert "Catastrophic tripwires" in html
    assert "Thesis breakers" not in html
    assert "Revenue YoY" in html


def test_html_renders_business_table_when_only_business_present() -> None:
    """Only business_model rules -> 'Thesis breakers' table only.

    Important for RBRK-style names whose universal tripwires don't fire on the
    pre-existing data (e.g. revenue growth is +46%) but per-ticker rules do.
    """
    s = _section([_ev("nrr_below_115", "NRR", "business_model")])
    out = StringIO()
    _html_break_rules(out, s, "RBRK")
    html = out.getvalue()
    assert "Thesis breakers" in html
    assert "Catastrophic tripwires" not in html
    assert "NRR" in html


def test_html_renders_both_tables_in_universal_first_order() -> None:
    """Universal table appears BEFORE business_model — catastrophic first."""
    s = _section(
        [
            _ev("rev_decline", "Revenue YoY", "universal"),
            _ev("nrr_below_115", "NRR", "business_model"),
        ]
    )
    out = StringIO()
    _html_break_rules(out, s, "RBRK")
    html = out.getvalue()
    universal_idx = html.index("Catastrophic tripwires")
    business_idx = html.index("Thesis breakers")
    assert universal_idx < business_idx


def test_html_back_compat_pre_tier_evaluations_render_as_business_model() -> None:
    """A persisted thesis_evaluation written before the tier field existed has
    no tier on the wire — the parser defaults it to business_model, and that
    rule renders under 'Thesis breakers', not 'Catastrophic tripwires'.

    This is the safety net for tickers whose evaluator runs are stale until the
    next cron tick re-persists with explicit tiers.
    """
    # Build evaluation without specifying tier (model default kicks in).
    legacy = BreakRuleEvaluation(
        rule_id="legacy_rule",
        kpi_name="Legacy KPI",
        comparator="lt",
        threshold=0,
        consecutive_periods=1,
        status="ok",
        detail="d",
        narrative="n",
    )
    assert legacy.tier == "business_model"
    s = _section([legacy])
    out = StringIO()
    _html_break_rules(out, s, "TEST")
    html = out.getvalue()
    assert "Thesis breakers" in html
    assert "Catastrophic tripwires" not in html


def test_html_unknown_status_with_no_evals_renders_callout() -> None:
    """No evaluation row in DB + unknown status -> 'Not yet evaluated' callout."""
    s = ThesisSection(
        status="missing_data",  # type: ignore[arg-type]
        overall_breach_status="unknown",
        break_rule_evaluations=[],
    )
    out = StringIO()
    _html_break_rules(out, s, "TEST")
    html = out.getvalue()
    assert "Not yet evaluated" in html
    assert "Catastrophic" not in html
    assert "Thesis breakers" not in html


def test_markdown_mirrors_html_two_tier_split() -> None:
    """Markdown renderer produces the same two-section layout with #### headings."""
    s = _section(
        [
            _ev("rev_decline", "Revenue YoY", "universal"),
            _ev("nrr_below_115", "NRR", "business_model"),
        ]
    )
    out = StringIO()
    _md_break_rules(out, s)
    md = out.getvalue()
    assert "#### Catastrophic tripwires" in md
    assert "#### Thesis breakers" in md
    assert md.index("Catastrophic tripwires") < md.index("Thesis breakers")


def test_markdown_suppresses_empty_business_model_section() -> None:
    """No business_model rules -> no 'Thesis breakers' heading."""
    s = _section([_ev("rev_decline", "Revenue YoY", "universal")])
    out = StringIO()
    _md_break_rules(out, s)
    md = out.getvalue()
    assert "#### Catastrophic tripwires" in md
    assert "Thesis breakers" not in md


# ---------------------------------------------------------------------------
# Soft signals — predicate-style YELLOW signals rendered inline with hard rules.
# ---------------------------------------------------------------------------


def _section_with_soft(soft: list[SoftRuleEvaluation]) -> ThesisSection:
    return ThesisSection(
        status="ok",  # type: ignore[arg-type]
        thesis_full="t",
        overall_breach_status="warn",
        break_rule_evaluations=[],
        soft_rule_evaluations=soft,
        last_evaluated_at=datetime(2026, 5, 25, 12, 0, 0),
    )


def test_html_renders_soft_signals_block_when_rules_present() -> None:
    """Soft rule fires -> 'Soft signals' heading + the evidence string."""
    soft = [
        SoftRuleEvaluation(
            rule_name="growth_decel_2q",
            status="yellow",
            evidence="Revenue YoY decel 1000→460 bps",
            details={},
        )
    ]
    out = StringIO()
    _html_break_rules(out, _section_with_soft(soft), "VEEV")
    html_out = out.getvalue()
    assert "Soft signals" in html_out
    assert "growth_decel_2q" in html_out
    assert "Revenue YoY decel" in html_out
    # Yellow signal gets the 'partial' badge class (the warn palette).
    assert "status-partial" in html_out


def test_html_suppresses_soft_signals_block_when_empty() -> None:
    """No soft rules -> no 'Soft signals' heading. Backwards-compatible."""
    out = StringIO()
    _html_break_rules(out, _section_with_soft([]), "VEEV")
    assert "Soft signals" not in out.getvalue()


def test_markdown_renders_soft_signals_table() -> None:
    """Markdown mirrors the HTML soft-signals section as a #### block + table."""
    soft = [
        SoftRuleEvaluation(
            rule_name="fcf_margin_below_15",
            status="yellow",
            evidence="FCF margin 10.0% (threshold 15.0%)",
            details={},
        )
    ]
    out = StringIO()
    _md_break_rules(out, _section_with_soft(soft))
    md = out.getvalue()
    assert "#### Soft signals" in md
    assert "fcf_margin_below_15" in md
    assert "FCF margin 10.0%" in md
