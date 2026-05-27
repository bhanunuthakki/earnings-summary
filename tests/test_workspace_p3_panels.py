"""Tests for the P4-A1 workspace HTML panels that surface P3 data.

Covers panel rendering + empty-state contracts for:
  - macro sensitivities (Thesis tab)
  - strategic targets / customer concentrations / lease ladder (Company tab)
  - decision history full ledger + conviction × outcome (Decisions tab)
  - say·do verdict overlay (Say·Do tab)
  - peer comparison (Eval Screen, evaluation flavor)

Plus a fixture-DB end-to-end test for the bundle loader (
``load_workspace_p3_panels``) — mirrors the test_p3_data_accessors.py
fixture pattern so the renderer wiring exercises the same code path the
real pipeline does.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime
from io import StringIO
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.models import (  # noqa: E402
    BearCaseSection,
    CompanyDescriptionSection,
    EvaluationSnapshotSection,
    IrDocsSection,
    QuickCategorizationRow,
    SayDoCard,
    SayDoSection,
    SectionStatus,
    SnapshotSection,
    ThesisSection,
    ValuationSnapshot,
)
from report.renderers.workspace_data import (  # noqa: E402
    WorkspaceP3Panels,
    load_workspace_p3_panels,
)
from report.renderers.workspace_html import (  # noqa: E402
    _company_tab,
    _customer_concentration_panel,
    _decisions_tab,
    _eval_tab,
    _lease_ladder_panel,
    _macro_sensitivity_panel,
    _peer_comp_panel,
    _saydo_tab,
    _saydo_verdicts_panel,
    _strategic_targets_panel,
    _thesis_tab,
)
from report.sections.p3_data import (  # noqa: E402
    CustomerConcentrationRow,
    DecisionHistorySummary,
    DecisionRow,
    LeaseLadderRow,
    MacroSensitivityRow,
    PeerCompRow,
    SayDoVerdictRow,
    StrategicTargetRow,
)

# ---------------------------------------------------------------------------
# Macro sensitivity panel
# ---------------------------------------------------------------------------


def test_macro_sensitivity_panel_renders_rows() -> None:
    rows = [
        MacroSensitivityRow(
            series_id="10y_treasury",
            beta=-0.65,
            r_squared=0.31,
            lookback_window_days=90,
            computed_at=datetime(2026, 5, 1),
        ),
        MacroSensitivityRow(
            series_id="fed_funds_rate",
            beta=0.30,
            r_squared=0.40,
            lookback_window_days=90,
            computed_at=datetime(2026, 5, 1),
        ),
    ]
    out = StringIO()
    _macro_sensitivity_panel(out, rows)
    html = out.getvalue()
    # Header bits
    assert "Macro factor sensitivity" in html
    assert "2 factors" in html
    assert "lookback 90d" in html
    # Friendly label mapping
    assert "10Y Treasury" in html
    assert "Fed funds rate" in html
    # Signed β + R² values
    assert "-0.65" in html
    assert "+0.30" in html
    # Strong beta (|β|>=0.5) gets a tone class
    assert ' class="num neg"' in html  # -0.65
    # Weak fed funds (|β|=0.3) is neither strong nor below 0.2 -> no tone
    # (just confirm it has `num` with no extra tone)
    assert '<td class="num">+0.30</td>' in html or '<td class="num ">+0.30</td>' in html


def test_macro_sensitivity_panel_empty_state() -> None:
    out = StringIO()
    _macro_sensitivity_panel(out, [])
    html = out.getvalue()
    assert "Macro factor sensitivity" in html
    assert "no factors tracked" in html
    assert "stub-label" in html
    assert "cold ticker" in html
    # No table rows
    assert "<tbody>" not in html


# ---------------------------------------------------------------------------
# Strategic targets panel (Company tab)
# ---------------------------------------------------------------------------


def test_strategic_targets_panel_renders_rows() -> None:
    rows = [
        StrategicTargetRow(
            target_kind="revenue",
            target_value=500.0,
            target_unit="USD_B",
            target_period="FY2027",
            target_currency="USD",
            narrative_excerpt="targeting $500B revenue by FY27",
            confidence=0.9,
        ),
    ]
    out = StringIO()
    _strategic_targets_panel(out, rows)
    html = out.getvalue()
    assert "Strategic targets" in html
    assert "1 long-term commitment" in html
    assert "revenue" in html
    assert "FY2027" in html
    assert "90%" in html
    assert "targeting $500B revenue by FY27" in html
    # currency prefix on the value
    assert "USD" in html
    assert "500.0" in html


def test_strategic_targets_panel_empty_state() -> None:
    out = StringIO()
    _strategic_targets_panel(out, [])
    html = out.getvalue()
    assert "Strategic targets" in html
    assert "no decks extracted" in html
    assert "stub-label" in html
    assert "<tbody>" not in html


def test_strategic_targets_panel_handles_null_value() -> None:
    """A target with no quantitative value (just a unit + narrative)."""
    rows = [
        StrategicTargetRow(
            target_kind="ai_leadership",
            target_value=None,
            target_unit="qualitative",
            target_period="medium-term",
            target_currency=None,
            narrative_excerpt="position as the AI infrastructure leader",
            confidence=0.7,
        ),
    ]
    out = StringIO()
    _strategic_targets_panel(out, rows)
    html = out.getvalue()
    assert "ai_leadership" in html
    assert "qualitative" in html


# ---------------------------------------------------------------------------
# Customer concentration panel (Company tab)
# ---------------------------------------------------------------------------


def test_customer_concentration_panel_renders_rows() -> None:
    rows = [
        CustomerConcentrationRow(
            fiscal_period="2025",
            fiscal_period_type="FY",
            customer_label="Customer A",
            pct_of_revenue=0.22,
            revenue_amount=1_100_000_000.0,
            revenue_currency="USD",
        ),
        CustomerConcentrationRow(
            fiscal_period="2025",
            fiscal_period_type="FY",
            customer_label="Customer B",
            pct_of_revenue=0.15,
            revenue_amount=None,
            revenue_currency=None,
        ),
    ]
    out = StringIO()
    _customer_concentration_panel(out, rows)
    html = out.getvalue()
    assert "Customer concentration" in html
    assert "2 customers" in html
    assert "Customer A" in html
    assert "22.0%" in html
    assert "Customer B" in html
    assert "15.0%" in html
    assert "USD 1,100,000,000" in html
    # Missing revenue → muted dash
    assert "muted" in html


def test_customer_concentration_panel_empty_state() -> None:
    out = StringIO()
    _customer_concentration_panel(out, [])
    html = out.getvalue()
    assert "Customer concentration" in html
    assert "none" in html
    assert "no material concentration" in html
    assert "<tbody>" not in html


# ---------------------------------------------------------------------------
# Lease ladder panel (Company tab)
# ---------------------------------------------------------------------------


def test_lease_ladder_panel_renders_y1_through_thereafter() -> None:
    rows = [
        LeaseLadderRow(
            fiscal_year=2025,
            as_of_date=date(2025, 12, 31),
            lease_type="operating",
            ladder_year=y,
            ladder_calendar_year=2026 + i if i < 5 else None,
            amount=10_000 - i * 500,
            currency="USD",
            unit="millions",
        )
        for i, y in enumerate(["Y1", "Y2", "Y3", "Y4", "Y5", "Thereafter"])
    ]
    out = StringIO()
    _lease_ladder_panel(out, rows)
    html = out.getvalue()
    assert "Operating lease maturity ladder" in html
    assert "FY2025" in html
    assert "Year 1" in html
    assert "Year 5" in html
    assert "Thereafter" in html
    # Numbers should render with thousands separators
    assert "10,000" in html


def test_lease_ladder_panel_emphasizes_summary_rows() -> None:
    """TotalPayments / ImputedInterest / LeaseLiability rows get .emph class."""
    rows = [
        LeaseLadderRow(
            fiscal_year=2025,
            as_of_date=date(2025, 12, 31),
            lease_type="operating",
            ladder_year="Y1",
            ladder_calendar_year=2026,
            amount=10_000,
            currency="USD",
            unit="millions",
        ),
        LeaseLadderRow(
            fiscal_year=2025,
            as_of_date=date(2025, 12, 31),
            lease_type="operating",
            ladder_year="TotalPayments",
            ladder_calendar_year=None,
            amount=50_000,
            currency="USD",
            unit="millions",
        ),
        LeaseLadderRow(
            fiscal_year=2025,
            as_of_date=date(2025, 12, 31),
            lease_type="operating",
            ladder_year="LeaseLiability",
            ladder_calendar_year=None,
            amount=45_000,
            currency="USD",
            unit="millions",
        ),
    ]
    out = StringIO()
    _lease_ladder_panel(out, rows)
    html = out.getvalue()
    assert 'class="emph"' in html
    assert "Total payments" in html
    assert "Lease liability" in html


def test_lease_ladder_panel_empty_state() -> None:
    out = StringIO()
    _lease_ladder_panel(out, [])
    html = out.getvalue()
    assert "Operating lease maturity ladder" in html
    assert "no ladder on file" in html
    assert "stub-label" in html


# ---------------------------------------------------------------------------
# Decisions tab (full audit ledger)
# ---------------------------------------------------------------------------


def _decision_row(
    kind: str,
    conviction: str | None,
    days_ago: int,
    outcome_pct: float | None,
    rationale: str = "",
) -> DecisionRow:
    return DecisionRow(
        recommendation_kind=kind,
        recommendation_value=None,
        conviction=conviction,
        made_at=datetime(2026, 5, 1).replace(day=max(1, 30 - days_ago)),
        outcome_pct=outcome_pct,
        outcome_at=datetime(2026, 5, 15) if outcome_pct is not None else None,
        rationale_excerpt=rationale or None,
    )


def test_decisions_tab_empty_state() -> None:
    history = DecisionHistorySummary(
        total=0,
        by_kind={},
        by_conviction={},
        win_rate_overall=None,
        rows=[],
    )
    out = StringIO()
    _decisions_tab(out, history)
    html = out.getvalue()
    assert "No decisions recorded" in html
    assert "stub-label" in html
    assert "cold ticker" in html
    # Tab body wraps both the missing callout and the eyebrow
    assert 'class="tab-body"' in html


def test_decisions_tab_renders_summary_chips_and_ledger() -> None:
    rows = [
        _decision_row("ADD", "high", 10, 0.08, "Q4 cleared every KPI."),
        _decision_row("ADD", "medium", 30, -0.02, "Within MoS band."),
        _decision_row("TRIM", "low", 5, -0.06, "Premium too wide."),
        _decision_row("HOLD", None, 50, None, "No action."),
    ]
    history = DecisionHistorySummary(
        total=4,
        by_kind={"ADD": 2, "TRIM": 1, "HOLD": 1},
        by_conviction={"high": 1, "medium": 1, "low": 1},
        win_rate_overall=2 / 3,  # 2 of 3 graded
        rows=rows,
    )
    out = StringIO()
    _decisions_tab(out, history)
    html = out.getvalue()
    assert "4 decisions tracked" in html
    assert "67% win rate" in html
    # Chip ribbon shows each kind (uppercased)
    assert ">ADD<" in html
    assert ">TRIM<" in html
    assert ">HOLD<" in html
    # Conviction chips
    assert ">high<" in html
    assert ">medium<" in html
    # Ledger rows
    assert "Q4 cleared every KPI." in html
    assert "Premium too wide." in html
    # Outcome cells: 8% ADD is positive return = correct → 'pos'
    assert '+8.0%' in html
    # TRIM -6% return is correct (trim then drop) → 'pos'
    assert '-6.0%' in html


def test_decisions_tab_conviction_outcome_breakdown() -> None:
    """High conviction → 2/2 correct, low conviction → 0/1 correct."""
    rows = [
        _decision_row("ADD", "high", 30, 0.08),
        _decision_row("ADD", "high", 20, 0.12),
        _decision_row("ADD", "low", 10, -0.05),
    ]
    history = DecisionHistorySummary(
        total=3,
        by_kind={"ADD": 3},
        by_conviction={"high": 2, "low": 1},
        win_rate_overall=2 / 3,
        rows=rows,
    )
    out = StringIO()
    _decisions_tab(out, history)
    html = out.getvalue()
    # Conviction x outcome cross-tab
    cross_label = f"Conviction {chr(0x00D7)} outcome"
    assert cross_label in html
    # High row: 2 correct, 0 wrong, 100% hit rate
    assert ">high<" in html
    assert "100%" in html
    # Low row: 0 correct, 1 wrong, 0% hit rate
    assert ">low<" in html
    assert "0%" in html


# ---------------------------------------------------------------------------
# Say·Do verdict panel
# ---------------------------------------------------------------------------


def test_saydo_verdicts_panel_renders_rows() -> None:
    rows = [
        SayDoVerdictRow(
            period_made=datetime(2025, 10, 30),
            period_target=datetime(2025, 12, 31),
            kpi_name="Cloud revenue growth",
            comparator=">=",
            target_value=30.0,
            unit="percent",
            narrative="Cloud growth at the prior quarter pace through year-end",
            realized_value=32.0,
            outcome="met",
            evaluated_at=datetime(2026, 1, 30),
        ),
        SayDoVerdictRow(
            period_made=datetime(2025, 7, 30),
            period_target=datetime(2025, 9, 30),
            kpi_name="Operating margin",
            comparator=">=",
            target_value=28.0,
            unit="percent",
            narrative="Operating margin expansion sustained",
            realized_value=27.5,
            outcome="miss",
            evaluated_at=datetime(2025, 10, 30),
        ),
    ]
    out = StringIO()
    _saydo_verdicts_panel(out, rows)
    html = out.getvalue()
    assert "Say·Do verdict ledger" in html
    assert "2 commitments" in html
    assert "2 graded" in html
    assert "Cloud revenue growth" in html
    assert "Operating margin" in html
    # comparator symbol mapping (≥) — the literal '>=' falls through
    assert ">= 30 percent" in html or "&gt;= 30 percent" in html
    # Outcome pills
    assert "HIT" in html or "MET" in html  # _outcome_pill maps both
    assert "MISS" in html


def test_saydo_verdicts_panel_empty_state() -> None:
    out = StringIO()
    _saydo_verdicts_panel(out, [])
    html = out.getvalue()
    assert "Say·Do verdict ledger" in html
    assert "no commitments extracted" in html
    assert "stub-label" in html


# ---------------------------------------------------------------------------
# Peer comp panel (Eval Screen)
# ---------------------------------------------------------------------------


def test_peer_comp_panel_renders_rows() -> None:
    rows = [
        PeerCompRow(
            peer_ticker="META",
            peer_name="Meta Platforms Inc.",
            market_cap_usd=1_200_000_000_000,
            revenue_ttm_usd=165_000_000_000,
            net_margin_ttm=0.30,
            roic_ttm=0.22,
        ),
        PeerCompRow(
            peer_ticker="MSFT",
            peer_name=None,
            market_cap_usd=None,
            revenue_ttm_usd=None,
            net_margin_ttm=None,
            roic_ttm=None,
        ),
    ]
    out = StringIO()
    _peer_comp_panel(out, rows)
    html = out.getvalue()
    assert "Peer comparison" in html
    assert "2 peers" in html
    assert "META" in html
    assert "Meta Platforms Inc." in html
    # Market cap compact format ($1.2T)
    assert "$1.2T" in html
    # Net margin percentage
    assert "30.0%" in html
    # MSFT has all-None fields → muted dashes
    assert "MSFT" in html
    assert "muted" in html


def test_peer_comp_panel_empty_state() -> None:
    out = StringIO()
    _peer_comp_panel(out, [])
    html = out.getvalue()
    assert "Peer comparison" in html
    assert "peers cache cold" in html
    assert "stub-label" in html
    assert "_peers.json" in html


# ---------------------------------------------------------------------------
# Tab-level integration: panels reach the right tab
# ---------------------------------------------------------------------------


def _empty_snapshot() -> SnapshotSection:
    return SnapshotSection(
        status=SectionStatus.OK,
        ticker="TEST",
        company_name="Test Co",
        thesis_one_liner="Test thesis.",
        verdict="intact",
        valuation=ValuationSnapshot(
            consolidated_npv_per_share=100.0,
            current_price=90.0,
        ),
    )


def _empty_thesis() -> ThesisSection:
    return ThesisSection(
        status=SectionStatus.OK,
        thesis_full="t",
        overall_breach_status="ok",
    )


def _empty_bear() -> BearCaseSection:
    return BearCaseSection(status=SectionStatus.OK)


def _empty_company_description() -> CompanyDescriptionSection:
    return CompanyDescriptionSection(
        status=SectionStatus.OK,
        elevator_pitch="A test company.",
        sector="Tech",
        industry="Software",
    )


def _empty_ir_docs() -> IrDocsSection:
    return IrDocsSection(status=SectionStatus.OK)


def test_thesis_tab_emits_macro_sensitivity_section() -> None:
    """The Thesis tab must always emit the macro sensitivity panel, even with
    no macro rows (empty-state per the audit-pass contract)."""
    out = StringIO()
    _thesis_tab(out, _empty_snapshot(), _empty_thesis(), _empty_bear(), [])
    html = out.getvalue()
    assert "Macro factor sensitivity" in html
    assert "no factors tracked" in html


def test_thesis_tab_emits_macro_panel_with_rows() -> None:
    macro = [
        MacroSensitivityRow(
            series_id="vix",
            beta=-0.42,
            r_squared=0.18,
            lookback_window_days=180,
            computed_at=datetime(2026, 5, 1),
        ),
    ]
    out = StringIO()
    _thesis_tab(out, _empty_snapshot(), _empty_thesis(), _empty_bear(), macro)
    html = out.getvalue()
    assert "Macro factor sensitivity" in html
    assert "VIX" in html
    assert "-0.42" in html
    assert "lookback 180d" in html


def test_company_tab_emits_all_three_p3_panels_empty() -> None:
    """Company tab always emits strategic / concentration / lease panels —
    each with empty-state when given empty lists."""
    out = StringIO()
    _company_tab(
        out,
        _empty_company_description(),
        _empty_ir_docs(),
        None,
        [],
        [],
        [],
    )
    html = out.getvalue()
    assert "Strategic targets" in html
    assert "Customer concentration" in html
    assert "Operating lease maturity ladder" in html
    # All three should show stub labels (empty-state)
    assert html.count("stub-label") >= 3


def test_company_tab_emits_panels_with_data() -> None:
    out = StringIO()
    _company_tab(
        out,
        _empty_company_description(),
        _empty_ir_docs(),
        None,
        [
            StrategicTargetRow(
                target_kind="revenue",
                target_value=500.0,
                target_unit="USD_B",
                target_period="FY2027",
                target_currency="USD",
                narrative_excerpt="targeting $500B",
                confidence=0.9,
            ),
        ],
        [
            CustomerConcentrationRow(
                fiscal_period="2025",
                fiscal_period_type="FY",
                customer_label="Big Customer",
                pct_of_revenue=0.30,
                revenue_amount=None,
                revenue_currency=None,
            ),
        ],
        [
            LeaseLadderRow(
                fiscal_year=2025,
                as_of_date=date(2025, 12, 31),
                lease_type="operating",
                ladder_year="Y1",
                ladder_calendar_year=2026,
                amount=10_000,
                currency="USD",
                unit="millions",
            ),
        ],
    )
    html = out.getvalue()
    assert "targeting $500B" in html
    assert "Big Customer" in html
    assert "Year 1" in html
    # No stub-label for the data-populated panels
    assert "1 long-term commitment" in html
    assert "1 customer" in html


def test_saydo_tab_includes_verdicts_panel() -> None:
    section = SayDoSection(
        status=SectionStatus.OK,
        cards=[
            SayDoCard(
                current_quarter="Q1",
                current_year=2026,
                prior_quarter="Q4",
                prior_year=2025,
                saydo_md="# SayDo\n\nSome narrative.",
                rating="MET",
                thesis_view="Bullish",
                attribution="Execution.",
            )
        ],
    )
    # Tab needs a spec for .repo_root/.llm_enabled — minimal stub.
    spec = _spec_for_saydo()
    verdicts = [
        SayDoVerdictRow(
            period_made=datetime(2025, 10, 30),
            period_target=datetime(2025, 12, 31),
            kpi_name="Cloud growth",
            comparator=">=",
            target_value=30.0,
            unit="percent",
            narrative="Cloud growth",
            realized_value=32.0,
            outcome="met",
            evaluated_at=datetime(2026, 1, 30),
        ),
    ]
    out = StringIO()
    _saydo_tab(out, section, spec, verdicts)
    html = out.getvalue()
    assert "Say·Do verdict ledger" in html
    assert "Cloud growth" in html


def test_eval_tab_includes_peer_comp_panel() -> None:
    eval_snap = EvaluationSnapshotSection(
        status=SectionStatus.OK,
        ticker="TEST",
        company_name="Test Co",
        rows=[
            QuickCategorizationRow(
                metric="Revenue",
                unit="USD M",
                digits=0,
                lfy_minus_2=1000.0,
                lfy_minus_1=1200.0,
                lfy=1500.0,
                ttm=1600.0,
                cagr_3y=0.22,
            )
        ],
        fiscal_years=[2023, 2024, 2025],
    )
    peers = [
        PeerCompRow(
            peer_ticker="ALT",
            peer_name="Alt Co",
            market_cap_usd=50_000_000_000,
            revenue_ttm_usd=10_000_000_000,
            net_margin_ttm=0.20,
            roic_ttm=0.18,
        ),
    ]
    out = StringIO()
    _eval_tab(out, eval_snap, peers)
    html = out.getvalue()
    assert "Quick categorization" in html
    assert "Peer comparison" in html
    assert "ALT" in html
    assert "Alt Co" in html


def test_eval_tab_renders_empty_peer_panel_when_no_peers() -> None:
    eval_snap = EvaluationSnapshotSection(
        status=SectionStatus.OK,
        ticker="TEST",
        rows=[],
        fiscal_years=[],
    )
    out = StringIO()
    _eval_tab(out, eval_snap, [])
    html = out.getvalue()
    assert "Peer comparison" in html
    assert "peers cache cold" in html


# ---------------------------------------------------------------------------
# Bundle loader — fixture DB integration
# ---------------------------------------------------------------------------


def test_load_workspace_p3_panels_cold_repo(tmp_path: Path) -> None:
    """A repo with no portfolio.db and no FMP cache returns an all-empty bundle."""
    p3 = load_workspace_p3_panels("TEST", tmp_path)
    assert p3.macro_sensitivities == []
    assert p3.strategic_targets == []
    assert p3.customer_concentrations == []
    assert p3.lease_ladder == []
    assert p3.decision_history.total == 0
    assert p3.saydo_verdicts == []
    assert p3.peer_comp == []


def test_load_workspace_p3_panels_with_seeded_db(tmp_path: Path) -> None:
    """Seed the macro table only — bundle reflects it; other accessors empty."""
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE macro_sensitivities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            series_id TEXT NOT NULL,
            beta REAL NOT NULL,
            r_squared REAL,
            lookback_window_days INTEGER NOT NULL,
            computed_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO macro_sensitivities (ticker, series_id, beta, r_squared, "
        "lookback_window_days, computed_at) VALUES "
        "('TEST', 'vix', -0.4, 0.15, 90, '2026-05-01 00:00:00')"
    )
    conn.commit()
    conn.close()
    p3 = load_workspace_p3_panels("TEST", tmp_path)
    assert len(p3.macro_sensitivities) == 1
    assert p3.macro_sensitivities[0].series_id == "vix"
    # Other accessors return empty because their tables are absent.
    assert p3.strategic_targets == []
    assert p3.decision_history.total == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec_for_saydo():
    """Minimal stand-in for ``ReportSpec`` carrying just the attrs ``_saydo_tab``
    needs (``repo_root``, ``ticker``, ``llm_enabled``).

    Using a plain object rather than a full ReportSpec to keep the test
    setup local to this file.
    """

    class _Spec:
        repo_root = "."
        ticker = "TEST"
        llm_enabled = False

    return _Spec()


def test_workspace_p3_panels_empty_factory() -> None:
    """`.empty()` classmethod provides a safe default instance."""
    empty = WorkspaceP3Panels.empty()
    assert empty.macro_sensitivities == []
    assert empty.strategic_targets == []
    assert empty.customer_concentrations == []
    assert empty.lease_ladder == []
    assert empty.decision_history.total == 0
    assert empty.decision_history.rows == []
    assert empty.saydo_verdicts == []
    assert empty.peer_comp == []
