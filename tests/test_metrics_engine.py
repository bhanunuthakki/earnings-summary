"""Pure-function unit tests for src/compute/metrics_engine/ -- registry,
inputs, applicability, engine. No DB, no prod data; synthetic values only.
"""

from __future__ import annotations

from decimal import Decimal

from compute.metrics_engine.applicability import applicable_formulas
from compute.metrics_engine.engine import (
    STATUTORY_TAX_RATE_FALLBACK_FLAG,
    ComputedValue,
    NotComputable,
    ResolvedInputs,
    compute,
    compute_ebitda,
    compute_invested_capital_strict,
    compute_net_debt_strict,
    compute_nopat,
)
from compute.metrics_engine.inputs import CanonicalConcept, resolve_concept
from compute.metrics_engine.registry import REGISTRY, ReasonCode, all_latest, latest
from models.companies import AccountingStandard, BusinessModelClass

# ---------------------------------------------------------------------------
# registry.py
# ---------------------------------------------------------------------------


_PHASE_1_KEYS = {
    "gross_margin",
    "operating_margin",
    "net_margin",
    "ebitda_margin",
    "fcf_margin",
    "revenue_yoy",
    "revenue_qoq",
    "eps_diluted_yoy",
    "current_ratio",
    "quick_ratio",
    "cash_ratio",
    "debt_to_equity",
    "roe",
    "roa",
    "sbc_pct_revenue",
}

_PHASE_2_KEYS = {
    "roic_strict",
    "roic_lease_adjusted",
    "roce",
    "net_debt_strict",
    "net_debt_incl_lt_securities",
    "net_debt_to_ebitda",
    "interest_coverage",
    "asset_turnover",
    "receivables_turnover",
    "inventory_turnover",
    "cash_conversion_cycle",
    "eps_adjusted_ex_sbc",
    "bvps",
    "fcf_per_share",
    "revenue_cagr_3y",
    "ebitda_cagr_3y",
}


def test_registry_has_exactly_the_15_phase1_formulas() -> None:
    actual = {key for key, _version in REGISTRY}
    assert actual >= _PHASE_1_KEYS
    assert len({k for k in actual if k in _PHASE_1_KEYS}) == 15


def test_registry_has_exactly_the_16_phase2_formulas() -> None:
    """Phase 2 (docs/design/bottoms_up_metrics_engine.md section 6): the
    method-variant pairs, roce, net_debt_to_ebitda, interest_coverage, the
    turnover/CCC efficiency metrics, the remaining per-share metrics, and
    the two FY-cadence CAGR metrics."""
    actual = {key for key, _version in REGISTRY}
    assert actual >= _PHASE_2_KEYS
    assert len(_PHASE_2_KEYS) == 16


def test_registry_has_no_other_formulas() -> None:
    actual = {key for key, _version in REGISTRY}
    assert actual == _PHASE_1_KEYS | _PHASE_2_KEYS
    assert len(REGISTRY) == 31


def test_all_latest_returns_one_entry_per_formula_key() -> None:
    formulas = all_latest()
    keys = [f.formula_key for f in formulas]
    assert len(keys) == len(set(keys)) == 31


def test_latest_returns_none_for_unknown_key() -> None:
    assert latest("not_a_real_formula") is None


def test_formula_applies_to_respects_excluded_business_models() -> None:
    gross_margin = latest("gross_margin")
    assert gross_margin is not None
    assert gross_margin.applies_to(BusinessModelClass.OPERATING_COMPANY) is True
    assert gross_margin.applies_to(BusinessModelClass.BANK) is False
    assert gross_margin.applies_to(BusinessModelClass.INSURANCE) is False


def test_roe_has_no_business_model_exclusion() -> None:
    """Doc: ROE is computed for a bank too, just annotated -- no exclusion."""
    roe = latest("roe")
    assert roe is not None
    assert roe.applies_to(BusinessModelClass.BANK) is True


# ---------------------------------------------------------------------------
# inputs.py
# ---------------------------------------------------------------------------


def test_resolve_concept_us_gaap_identity_map() -> None:
    assert resolve_concept(AccountingStandard.US_GAAP, CanonicalConcept.REVENUE) == "revenue"
    assert resolve_concept(AccountingStandard.US_GAAP, CanonicalConcept.TOTAL_DEBT) == "total_debt"


def test_resolve_concept_handles_naming_mismatches() -> None:
    """PRETAX_INCOME and EPS_BASIC map to FMP's own field spelling, not the
    concept's own name."""
    assert (
        resolve_concept(AccountingStandard.US_GAAP, CanonicalConcept.PRETAX_INCOME)
        == "income_before_tax"
    )
    assert resolve_concept(AccountingStandard.US_GAAP, CanonicalConcept.EPS_BASIC) == "eps"


def test_resolve_concept_ifrs_populated_in_phase2() -> None:
    """Phase 2 verified (against real data/portfolio.db rows for NU/BN/ASML/
    NVO) that FMP's normalization collapses IFRS filers onto the identical
    US-GAAP vocabulary -- every concept below resolves, not None."""
    assert resolve_concept(AccountingStandard.IFRS, CanonicalConcept.REVENUE) == "revenue"
    assert (
        resolve_concept(AccountingStandard.IFRS, CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY)
        == "total_stockholders_equity"
    )
    assert (
        resolve_concept(AccountingStandard.IFRS, CanonicalConcept.OPERATING_LEASE_LIABILITY)
        == "operating_lease_liability"
    )


def test_resolve_concept_operating_lease_liability_mapped_for_both_standards() -> None:
    """Phase 2 addition: needed by roic_lease_adjusted, previously
    unmapped (docs/design/bottoms_up_metrics_engine.md section 2 noted the
    gap explicitly)."""
    assert (
        resolve_concept(AccountingStandard.US_GAAP, CanonicalConcept.OPERATING_LEASE_LIABILITY)
        == "operating_lease_liability"
    )


# ---------------------------------------------------------------------------
# applicability.py
# ---------------------------------------------------------------------------


def test_applicable_formulas_operating_company_gets_all_31() -> None:
    formulas = applicable_formulas(BusinessModelClass.OPERATING_COMPANY)
    assert len(formulas) == 31


def test_applicable_formulas_bank_excludes_roic_and_roce() -> None:
    formulas = applicable_formulas(BusinessModelClass.BANK)
    keys = {f.formula_key for f in formulas}
    assert "roic_strict" not in keys
    assert "roic_lease_adjusted" not in keys
    assert "roce" not in keys
    assert "net_debt_to_ebitda" not in keys
    assert "interest_coverage" not in keys
    assert "asset_turnover" not in keys
    assert "inventory_turnover" not in keys
    # net_debt_strict/incl_lt_securities and the per-share metrics apply to ALL.
    assert "net_debt_strict" in keys
    assert "bvps" in keys


def test_applicable_formulas_ticker_override_excludes_zero_inventory_names() -> None:
    formulas = applicable_formulas(BusinessModelClass.OPERATING_COMPANY, ticker="NOW")
    keys = {f.formula_key for f in formulas}
    assert "inventory_turnover" not in keys
    assert "cash_conversion_cycle" not in keys
    # Unaffected formulas still apply.
    assert "receivables_turnover" in keys
    assert "asset_turnover" in keys


def test_applicable_formulas_no_override_for_unlisted_ticker() -> None:
    formulas = applicable_formulas(BusinessModelClass.OPERATING_COMPANY, ticker="MELI")
    keys = {f.formula_key for f in formulas}
    assert "inventory_turnover" in keys
    assert "cash_conversion_cycle" in keys


def test_applicable_formulas_bank_excludes_margin_and_liquidity_metrics() -> None:
    formulas = applicable_formulas(BusinessModelClass.BANK)
    keys = {f.formula_key for f in formulas}
    assert "gross_margin" not in keys
    assert "current_ratio" not in keys
    assert "debt_to_equity" not in keys
    # ROE/ROA/growth/per-share-adjacent still apply.
    assert "roe" in keys
    assert "roa" in keys
    assert "revenue_yoy" in keys


# ---------------------------------------------------------------------------
# engine.py
# ---------------------------------------------------------------------------


def test_compute_ebitda_sums_operating_income_and_da() -> None:
    resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("100"),
        CanonicalConcept.DEPRECIATION_AND_AMORTIZATION: Decimal("20"),
    }
    result = compute_ebitda(resolved)
    assert result == Decimal("120")


def test_compute_ebitda_missing_da_is_not_computable() -> None:
    resolved: ResolvedInputs = {CanonicalConcept.OPERATING_INCOME: Decimal("100")}
    result = compute_ebitda(resolved)
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.MISSING_INPUT


def test_gross_margin_happy_path() -> None:
    formula = latest("gross_margin")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.GROSS_PROFIT: Decimal("400"),
        CanonicalConcept.REVENUE: Decimal("1000"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, ComputedValue)
    assert result.value == Decimal("40")


def test_gross_margin_missing_input() -> None:
    formula = latest("gross_margin")
    assert formula is not None
    result = compute(formula, {CanonicalConcept.GROSS_PROFIT: Decimal("400")})
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.MISSING_INPUT


def test_gross_margin_zero_revenue_is_denominator_le_zero() -> None:
    formula = latest("gross_margin")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.GROSS_PROFIT: Decimal("400"),
        CanonicalConcept.REVENUE: Decimal("0"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.DENOMINATOR_LE_ZERO


def test_ebitda_margin_composes_compute_ebitda() -> None:
    formula = latest("ebitda_margin")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("150"),
        CanonicalConcept.DEPRECIATION_AND_AMORTIZATION: Decimal("50"),
        CanonicalConcept.REVENUE: Decimal("1000"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, ComputedValue)
    assert result.value == Decimal("20")


def test_quick_ratio_defaults_missing_inventory_to_zero() -> None:
    formula = latest("quick_ratio")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.TOTAL_CURRENT_ASSETS: Decimal("500"),
        CanonicalConcept.TOTAL_CURRENT_LIABILITIES: Decimal("250"),
        # INVENTORY intentionally absent -- a services business.
    }
    result = compute(formula, resolved)
    assert isinstance(result, ComputedValue)
    assert result.value == Decimal("2")


def test_debt_to_equity_negative_equity_is_denominator_le_zero() -> None:
    formula = latest("debt_to_equity")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.TOTAL_DEBT: Decimal("500"),
        CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY: Decimal("-100"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.DENOMINATOR_LE_ZERO


def test_roe_happy_path_uses_ttm_summed_net_income() -> None:
    """roe's `resolved` NET_INCOME is expected pre-summed by io.py -- the
    engine itself just divides."""
    formula = latest("roe")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.NET_INCOME: Decimal("400"),  # already TTM-summed
        CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY: Decimal("2000"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, ComputedValue)
    assert result.value == Decimal("20")


def test_revenue_yoy_happy_path() -> None:
    formula = latest("revenue_yoy")
    assert formula is not None
    resolved: ResolvedInputs = {CanonicalConcept.REVENUE: Decimal("1100")}
    prior: ResolvedInputs = {CanonicalConcept.REVENUE: Decimal("1000")}
    result = compute(formula, resolved, prior_inputs=prior)
    assert isinstance(result, ComputedValue)
    assert result.value == Decimal("10")


def test_revenue_yoy_no_prior_is_missing_input() -> None:
    formula = latest("revenue_yoy")
    assert formula is not None
    result = compute(formula, {CanonicalConcept.REVENUE: Decimal("1100")})
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.MISSING_INPUT


def test_eps_diluted_yoy_skips_when_prior_le_zero() -> None:
    """Doc: 'skipped when prior EPS <= 0 (sign flip makes % meaningless)'."""
    formula = latest("eps_diluted_yoy")
    assert formula is not None
    resolved: ResolvedInputs = {CanonicalConcept.EPS_DILUTED: Decimal("0.50")}
    prior: ResolvedInputs = {CanonicalConcept.EPS_DILUTED: Decimal("-0.10")}
    result = compute(formula, resolved, prior_inputs=prior)
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.DENOMINATOR_LE_ZERO


def test_compute_raises_for_unregistered_formula_key() -> None:
    """A FormulaDef with a formula_key the dispatch table doesn't know
    about is a programming error, not a data problem -- raise loudly."""
    import pytest

    from compute.metrics_engine.registry import FormulaDef, MetricCategory
    from models.facts import Unit

    bogus = FormulaDef(
        formula_key="not_a_real_formula",
        version=1,
        category=MetricCategory.MARGIN,
        display_formula="x",
        method_notes="x",
        required_inputs=(CanonicalConcept.REVENUE,),
        period_grid="quarterly",
        unit=Unit.PERCENT,
    )
    with pytest.raises(ValueError, match="no compute dispatch"):
        compute(bogus, {CanonicalConcept.REVENUE: Decimal("1")})


# ---------------------------------------------------------------------------
# Phase 2 -- NOPAT / invested capital / net debt intermediates
# ---------------------------------------------------------------------------


def test_compute_nopat_no_tax_fallback() -> None:
    resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("1000"),
        CanonicalConcept.INCOME_TAX_EXPENSE: Decimal("210"),
        CanonicalConcept.PRETAX_INCOME: Decimal("1000"),
    }
    result = compute_nopat(resolved)
    assert not isinstance(result, NotComputable)
    nopat, used_fallback = result
    assert nopat == Decimal("790")
    assert used_fallback is False


def test_compute_nopat_uses_statutory_fallback_when_pretax_le_zero() -> None:
    resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("1000"),
        CanonicalConcept.INCOME_TAX_EXPENSE: Decimal("50"),
        CanonicalConcept.PRETAX_INCOME: Decimal("-10"),
    }
    result = compute_nopat(resolved)
    assert not isinstance(result, NotComputable)
    nopat, used_fallback = result
    assert used_fallback is True
    assert nopat == Decimal("1000") * (Decimal(1) - Decimal("0.21"))


def test_compute_nopat_clips_tax_rate_above_one() -> None:
    """A tax expense larger than pretax income (a one-off charge) must not
    flip NOPAT negative via a >100% effective rate -- clipped to 1.0."""
    resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("1000"),
        CanonicalConcept.INCOME_TAX_EXPENSE: Decimal("500"),
        CanonicalConcept.PRETAX_INCOME: Decimal("100"),
    }
    result = compute_nopat(resolved)
    assert not isinstance(result, NotComputable)
    nopat, used_fallback = result
    assert used_fallback is False
    assert nopat == Decimal("0")


def test_compute_invested_capital_strict() -> None:
    resolved: ResolvedInputs = {
        CanonicalConcept.TOTAL_DEBT: Decimal("500"),
        CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY: Decimal("2000"),
        CanonicalConcept.CASH_AND_EQUIVALENTS: Decimal("300"),
    }
    result = compute_invested_capital_strict(resolved)
    assert result == Decimal("2200")


def test_compute_net_debt_strict_can_be_negative() -> None:
    """A net-cash position is a valid value, never guarded against."""
    resolved: ResolvedInputs = {
        CanonicalConcept.TOTAL_DEBT: Decimal("100"),
        CanonicalConcept.CASH_AND_EQUIVALENTS: Decimal("500"),
        CanonicalConcept.SHORT_TERM_INVESTMENTS: Decimal("200"),
    }
    result = compute_net_debt_strict(resolved)
    assert result == Decimal("-600")


# ---------------------------------------------------------------------------
# Phase 2 -- roic_strict / roic_lease_adjusted (method-variant disagreement)
# ---------------------------------------------------------------------------


def test_roic_strict_happy_path() -> None:
    formula = latest("roic_strict")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("1000"),
        CanonicalConcept.INCOME_TAX_EXPENSE: Decimal("210"),
        CanonicalConcept.PRETAX_INCOME: Decimal("1000"),
        CanonicalConcept.TOTAL_DEBT: Decimal("500"),
        CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY: Decimal("2000"),
        CanonicalConcept.CASH_AND_EQUIVALENTS: Decimal("500"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, ComputedValue)
    # NOPAT = 1000 * 0.79 = 790; invested_capital = 500+2000-500 = 2000.
    assert result.value == Decimal("790") / Decimal("2000") * Decimal(100)
    assert result.method_flags == ()


def test_roic_strict_tax_fallback_sets_method_flag() -> None:
    formula = latest("roic_strict")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("1000"),
        CanonicalConcept.INCOME_TAX_EXPENSE: Decimal("50"),
        CanonicalConcept.PRETAX_INCOME: Decimal("-10"),
        CanonicalConcept.TOTAL_DEBT: Decimal("500"),
        CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY: Decimal("2000"),
        CanonicalConcept.CASH_AND_EQUIVALENTS: Decimal("500"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, ComputedValue)
    assert result.method_flags == (STATUTORY_TAX_RATE_FALLBACK_FLAG,)


def test_roic_lease_adjusted_disagrees_with_roic_strict_when_leases_present() -> None:
    """Method-variant disagreement case: adding operating_lease_liability to
    invested capital must produce a DIFFERENT (lower) value than
    roic_strict for the same underlying company -- both stored, neither
    silently preferred, per registry.py's alt_of contract."""
    strict_formula = latest("roic_strict")
    lease_formula = latest("roic_lease_adjusted")
    assert strict_formula is not None
    assert lease_formula is not None

    base_resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("1000"),
        CanonicalConcept.INCOME_TAX_EXPENSE: Decimal("210"),
        CanonicalConcept.PRETAX_INCOME: Decimal("1000"),
        CanonicalConcept.TOTAL_DEBT: Decimal("500"),
        CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY: Decimal("2000"),
        CanonicalConcept.CASH_AND_EQUIVALENTS: Decimal("500"),
    }
    lease_resolved = {**base_resolved, CanonicalConcept.OPERATING_LEASE_LIABILITY: Decimal("1000")}

    strict_result = compute(strict_formula, base_resolved)
    lease_result = compute(lease_formula, lease_resolved)
    assert isinstance(strict_result, ComputedValue)
    assert isinstance(lease_result, ComputedValue)
    # Same NOPAT (790), larger invested capital (2000 -> 3000) -> a strictly
    # LOWER lease-adjusted ROIC. This is the documented, expected
    # disagreement between the two variants, not a bug.
    assert lease_result.value < strict_result.value
    assert strict_result.value == Decimal("790") / Decimal("2000") * Decimal(100)
    assert lease_result.value == Decimal("790") / Decimal("3000") * Decimal(100)


def test_roic_lease_adjusted_missing_lease_liability_is_missing_input() -> None:
    formula = latest("roic_lease_adjusted")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("1000"),
        CanonicalConcept.INCOME_TAX_EXPENSE: Decimal("210"),
        CanonicalConcept.PRETAX_INCOME: Decimal("1000"),
        CanonicalConcept.TOTAL_DEBT: Decimal("500"),
        CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY: Decimal("2000"),
        CanonicalConcept.CASH_AND_EQUIVALENTS: Decimal("500"),
        # OPERATING_LEASE_LIABILITY intentionally absent.
    }
    result = compute(formula, resolved)
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.MISSING_INPUT


def test_roic_strict_negative_invested_capital_is_denominator_le_zero() -> None:
    formula = latest("roic_strict")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("1000"),
        CanonicalConcept.INCOME_TAX_EXPENSE: Decimal("210"),
        CanonicalConcept.PRETAX_INCOME: Decimal("1000"),
        CanonicalConcept.TOTAL_DEBT: Decimal("100"),
        CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY: Decimal("-500"),
        CanonicalConcept.CASH_AND_EQUIVALENTS: Decimal("50"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.DENOMINATOR_LE_ZERO


# ---------------------------------------------------------------------------
# Phase 2 -- roce
# ---------------------------------------------------------------------------


def test_roce_happy_path() -> None:
    formula = latest("roce")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("400"),
        CanonicalConcept.TOTAL_ASSETS: Decimal("3000"),
        CanonicalConcept.TOTAL_CURRENT_LIABILITIES: Decimal("1000"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, ComputedValue)
    assert result.value == Decimal("400") / Decimal("2000") * Decimal(100)


def test_roce_zero_capital_employed_is_denominator_le_zero() -> None:
    formula = latest("roce")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("400"),
        CanonicalConcept.TOTAL_ASSETS: Decimal("1000"),
        CanonicalConcept.TOTAL_CURRENT_LIABILITIES: Decimal("1000"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.DENOMINATOR_LE_ZERO


# ---------------------------------------------------------------------------
# Phase 2 -- net_debt_strict / net_debt_incl_lt_securities (method-variant
# disagreement, the AAPL-scale case named in the design doc)
# ---------------------------------------------------------------------------


def test_net_debt_incl_lt_securities_disagrees_with_strict_when_lt_investments_present() -> None:
    strict_formula = latest("net_debt_strict")
    incl_formula = latest("net_debt_incl_lt_securities")
    assert strict_formula is not None
    assert incl_formula is not None

    base_resolved: ResolvedInputs = {
        CanonicalConcept.TOTAL_DEBT: Decimal("1000"),
        CanonicalConcept.CASH_AND_EQUIVALENTS: Decimal("300"),
        CanonicalConcept.SHORT_TERM_INVESTMENTS: Decimal("200"),
    }
    incl_resolved = {**base_resolved, CanonicalConcept.LONG_TERM_INVESTMENTS: Decimal("400")}

    strict_result = compute(strict_formula, base_resolved)
    incl_result = compute(incl_formula, incl_resolved)
    assert isinstance(strict_result, ComputedValue)
    assert isinstance(incl_result, ComputedValue)
    assert strict_result.value == Decimal("500")  # 1000 - 300 - 200
    assert incl_result.value == Decimal("100")  # 500 - 400
    # This IS the documented AAPL-scale disagreement -- both stored, neither preferred.
    assert incl_result.value != strict_result.value


def test_net_debt_to_ebitda_happy_path() -> None:
    formula = latest("net_debt_to_ebitda")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.TOTAL_DEBT: Decimal("1000"),
        CanonicalConcept.CASH_AND_EQUIVALENTS: Decimal("200"),
        CanonicalConcept.SHORT_TERM_INVESTMENTS: Decimal("100"),
        CanonicalConcept.OPERATING_INCOME: Decimal("300"),
        CanonicalConcept.DEPRECIATION_AND_AMORTIZATION: Decimal("100"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, ComputedValue)
    # net_debt = 700; ebitda = 400 -> 1.75x.
    assert result.value == Decimal("700") / Decimal("400")


def test_net_debt_to_ebitda_negative_ebitda_is_denominator_le_zero() -> None:
    formula = latest("net_debt_to_ebitda")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.TOTAL_DEBT: Decimal("1000"),
        CanonicalConcept.CASH_AND_EQUIVALENTS: Decimal("200"),
        CanonicalConcept.SHORT_TERM_INVESTMENTS: Decimal("100"),
        CanonicalConcept.OPERATING_INCOME: Decimal("-300"),
        CanonicalConcept.DEPRECIATION_AND_AMORTIZATION: Decimal("100"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.DENOMINATOR_LE_ZERO


# ---------------------------------------------------------------------------
# Phase 2 -- interest_coverage
# ---------------------------------------------------------------------------


def test_interest_coverage_happy_path() -> None:
    formula = latest("interest_coverage")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("470"),
        CanonicalConcept.INTEREST_EXPENSE: Decimal("50"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, ComputedValue)
    assert result.value == Decimal("9.4")


def test_interest_coverage_negative_interest_expense_is_missing_input() -> None:
    """Real-sweep finding (Phase 2 validation against data/portfolio.db):
    at least one ticker/period has interest_expense stored as a negative
    value (net interest income, or a sign-convention disagreement between
    ingestion sources) -- must not produce a nonsensical negative "coverage"
    ratio, same treatment as the 0/absent debt-free case."""
    formula = latest("interest_coverage")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("470"),
        CanonicalConcept.INTEREST_EXPENSE: Decimal("-50"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.MISSING_INPUT


def test_interest_coverage_zero_interest_expense_is_missing_input() -> None:
    """Doc: 'not_computable: missing_input when interest_expense is 0/absent
    (debt-free names)' -- never a divide-by-zero, never an infinite ratio."""
    formula = latest("interest_coverage")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("470"),
        CanonicalConcept.INTEREST_EXPENSE: Decimal("0"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.MISSING_INPUT


# ---------------------------------------------------------------------------
# Phase 2 -- turnovers + cash_conversion_cycle
# ---------------------------------------------------------------------------


def test_asset_turnover_uses_pre_averaged_denominator() -> None:
    """io.py has already averaged TOTAL_ASSETS before compute() is called --
    the engine itself just divides."""
    formula = latest("asset_turnover")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.REVENUE: Decimal("4000"),  # already TTM-summed
        CanonicalConcept.TOTAL_ASSETS: Decimal("2000"),  # already averaged
    }
    result = compute(formula, resolved)
    assert isinstance(result, ComputedValue)
    assert result.value == Decimal("2")


def test_inventory_turnover_zero_average_is_denominator_le_zero() -> None:
    formula = latest("inventory_turnover")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.COST_OF_REVENUE: Decimal("1000"),
        CanonicalConcept.INVENTORY: Decimal("0"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.DENOMINATOR_LE_ZERO


def test_cash_conversion_cycle_happy_path() -> None:
    formula = latest("cash_conversion_cycle")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.REVENUE: Decimal("3650"),
        CanonicalConcept.COST_OF_REVENUE: Decimal("3650"),
        CanonicalConcept.INVENTORY: Decimal("100"),
        CanonicalConcept.ACCOUNTS_RECEIVABLE: Decimal("200"),
        CanonicalConcept.ACCOUNTS_PAYABLE: Decimal("50"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, ComputedValue)
    # DIO = 365*100/3650 = 10; DSO = 365*200/3650 = 20; DPO = 365*50/3650 = 5.
    # CCC = 10 + 20 - 5 = 25.
    assert result.value == Decimal("25")


def test_cash_conversion_cycle_missing_leg_propagates_not_computable() -> None:
    formula = latest("cash_conversion_cycle")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.REVENUE: Decimal("3650"),
        CanonicalConcept.COST_OF_REVENUE: Decimal("3650"),
        CanonicalConcept.INVENTORY: Decimal("100"),
        CanonicalConcept.ACCOUNTS_RECEIVABLE: Decimal("200"),
        # ACCOUNTS_PAYABLE intentionally absent -- one leg missing propagates.
    }
    result = compute(formula, resolved)
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.MISSING_INPUT


# ---------------------------------------------------------------------------
# Phase 2 -- per-share metrics
# ---------------------------------------------------------------------------


def test_eps_adjusted_ex_sbc_always_sets_method_flag() -> None:
    formula = latest("eps_adjusted_ex_sbc")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.NET_INCOME: Decimal("900"),
        CanonicalConcept.STOCK_BASED_COMPENSATION: Decimal("100"),
        CanonicalConcept.WEIGHTED_AVG_SHARES_DILUTED: Decimal("100"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, ComputedValue)
    assert result.value == Decimal("10")
    from compute.metrics_engine.engine import SBC_ADDBACK_PRETAX_FLAG

    assert result.method_flags == (SBC_ADDBACK_PRETAX_FLAG,)


def test_bvps_happy_path() -> None:
    formula = latest("bvps")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY: Decimal("2000"),
        CanonicalConcept.WEIGHTED_AVG_SHARES_DILUTED: Decimal("100"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, ComputedValue)
    assert result.value == Decimal("20")


def test_fcf_per_share_zero_shares_is_denominator_le_zero() -> None:
    formula = latest("fcf_per_share")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.FREE_CASH_FLOW: Decimal("500"),
        CanonicalConcept.WEIGHTED_AVG_SHARES_DILUTED: Decimal("0"),
    }
    result = compute(formula, resolved)
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.DENOMINATOR_LE_ZERO


# ---------------------------------------------------------------------------
# Phase 2 -- FY-cadence CAGR metrics
# ---------------------------------------------------------------------------


def test_revenue_cagr_3y_happy_path() -> None:
    formula = latest("revenue_cagr_3y")
    assert formula is not None
    resolved: ResolvedInputs = {CanonicalConcept.REVENUE: Decimal("1331")}
    prior: ResolvedInputs = {CanonicalConcept.REVENUE: Decimal("1000")}
    result = compute(formula, resolved, prior_inputs=prior)
    assert isinstance(result, ComputedValue)
    # (1331/1000)^(1/3) - 1 = 1.1 - 1 = 0.10 -> 10%. Float roundtrip tolerance.
    assert abs(result.value - Decimal("10")) < Decimal("0.001")


def test_revenue_cagr_3y_no_prior_is_missing_input() -> None:
    formula = latest("revenue_cagr_3y")
    assert formula is not None
    result = compute(formula, {CanonicalConcept.REVENUE: Decimal("1331")})
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.MISSING_INPUT


def test_revenue_cagr_3y_negative_base_is_denominator_le_zero() -> None:
    formula = latest("revenue_cagr_3y")
    assert formula is not None
    resolved: ResolvedInputs = {CanonicalConcept.REVENUE: Decimal("1000")}
    prior: ResolvedInputs = {CanonicalConcept.REVENUE: Decimal("-500")}
    result = compute(formula, resolved, prior_inputs=prior)
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.DENOMINATOR_LE_ZERO


def test_ebitda_cagr_3y_composes_compute_ebitda() -> None:
    formula = latest("ebitda_cagr_3y")
    assert formula is not None
    resolved: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("1000"),
        CanonicalConcept.DEPRECIATION_AND_AMORTIZATION: Decimal("331"),
    }
    prior: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("800"),
        CanonicalConcept.DEPRECIATION_AND_AMORTIZATION: Decimal("200"),
    }
    result = compute(formula, resolved, prior_inputs=prior)
    assert isinstance(result, ComputedValue)
    # current ebitda = 1331, base ebitda = 1000 -> same 10% CAGR as above.
    assert abs(result.value - Decimal("10")) < Decimal("0.001")


def test_ebitda_cagr_3y_missing_da_propagates_not_computable() -> None:
    formula = latest("ebitda_cagr_3y")
    assert formula is not None
    resolved: ResolvedInputs = {CanonicalConcept.OPERATING_INCOME: Decimal("1000")}
    prior: ResolvedInputs = {
        CanonicalConcept.OPERATING_INCOME: Decimal("800"),
        CanonicalConcept.DEPRECIATION_AND_AMORTIZATION: Decimal("200"),
    }
    result = compute(formula, resolved, prior_inputs=prior)
    assert isinstance(result, NotComputable)
    assert result.reason_code == ReasonCode.MISSING_INPUT
