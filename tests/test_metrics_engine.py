"""Pure-function unit tests for src/compute/metrics_engine/ -- registry,
inputs, applicability, engine. No DB, no prod data; synthetic values only.
"""

from __future__ import annotations

from decimal import Decimal

from compute.metrics_engine.applicability import applicable_formulas
from compute.metrics_engine.engine import (
    ComputedValue,
    NotComputable,
    ResolvedInputs,
    compute,
    compute_ebitda,
)
from compute.metrics_engine.inputs import CanonicalConcept, resolve_concept
from compute.metrics_engine.registry import REGISTRY, ReasonCode, all_latest, latest
from models.companies import AccountingStandard, BusinessModelClass


# ---------------------------------------------------------------------------
# registry.py
# ---------------------------------------------------------------------------


def test_registry_has_exactly_the_15_phase1_formulas() -> None:
    expected = {
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
    actual = {key for key, _version in REGISTRY}
    assert actual == expected
    assert len(REGISTRY) == 15


def test_registry_excludes_phase2_alt_of_metrics() -> None:
    """net_debt_*, roic_*, roce and other alt_of-grouped pairs are Phase 2 --
    explicitly out of scope per docs/design/bottoms_up_metrics_engine.md section 6."""
    phase2_only = {"net_debt_strict", "net_debt_incl_lt_securities", "roic_strict", "roic_lease_adjusted", "roce"}
    actual = {key for key, _version in REGISTRY}
    assert actual.isdisjoint(phase2_only)


def test_all_latest_returns_one_entry_per_formula_key() -> None:
    formulas = all_latest()
    keys = [f.formula_key for f in formulas]
    assert len(keys) == len(set(keys)) == 15


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
    assert (
        resolve_concept(AccountingStandard.US_GAAP, CanonicalConcept.TOTAL_DEBT) == "total_debt"
    )


def test_resolve_concept_handles_naming_mismatches() -> None:
    """PRETAX_INCOME and EPS_BASIC map to FMP's own field spelling, not the
    concept's own name."""
    assert (
        resolve_concept(AccountingStandard.US_GAAP, CanonicalConcept.PRETAX_INCOME)
        == "income_before_tax"
    )
    assert resolve_concept(AccountingStandard.US_GAAP, CanonicalConcept.EPS_BASIC) == "eps"


def test_resolve_concept_ifrs_empty_in_phase1() -> None:
    """Phase 1 has no IFRS mapping -- every concept resolves to None."""
    assert resolve_concept(AccountingStandard.IFRS, CanonicalConcept.REVENUE) is None


# ---------------------------------------------------------------------------
# applicability.py
# ---------------------------------------------------------------------------


def test_applicable_formulas_operating_company_gets_all_15() -> None:
    formulas = applicable_formulas(BusinessModelClass.OPERATING_COMPANY)
    assert len(formulas) == 15


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
