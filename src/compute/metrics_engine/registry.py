"""FormulaDef (typed spec) + the versioned formula registry.

`REGISTRY` is append-only in the literal sense: once a `(formula_key,
version)` pair is committed, its `display_formula`/`method_notes`/inputs
never change — a formula change is always a new `version` entry. A DB
mirror (`formula_definitions`, alembic 0160) is upserted from this module at
engine-startup via `io.upsert_formula_definitions`, mirroring
`pipeline.kpi_persistence.find_or_create_kpi_definition`'s idempotency
pattern.

Phase 1 scope (docs/design/bottoms_up_metrics_engine.md §6): the ~15
unambiguous metrics with no method-variant ambiguity.

Phase 2 scope: the remaining §1 catalog — the two `alt_of`-grouped pairs
(`roic_strict`/`roic_lease_adjusted`, `net_debt_strict`/
`net_debt_incl_lt_securities`), `roce`, `net_debt_to_ebitda`,
`interest_coverage`, the three turnover metrics + `cash_conversion_cycle`,
the remaining per-share metrics, and the two 3-year CAGR metrics (the only
`period_grid="fy"` formulas in the registry so far).

Phase 3 scope: the §1 Valuation table — `pe_ttm`, `ps_ttm`, `pb`,
`ev_ebitda`, `ev_sales`, `fcf_yield`, `earnings_yield`, plus
`enterprise_value_strict` (the shared EV intermediate, also stored/queryable
in its own right, mirroring `net_debt_strict`'s precedent). All 8 are
`period_grid="ttm"` but computed ONLY at the latest calendar quarter — see
`compute.metrics_engine.io._VALUATION_FORMULA_KEYS` for why a spot
price/market-cap leg cannot be backfilled historically.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from models.companies import BusinessModelClass
from models.facts import Unit

from .inputs import CanonicalConcept

# Bumped whenever engine.py's compute semantics change in a way that could
# alter a stored value without a formula version bump (e.g. a rounding-mode
# fix) — persisted onto metric_computation_attempts.engine_version so a
# stale computation is identifiable. Short git-describe-style string, per
# docs/design/bottoms_up_metrics_engine.md §3.
ENGINE_VERSION = "metrics_engine==2026.07.1"


class MetricCategory(StrEnum):
    MARGIN = "margin"
    GROWTH = "growth"
    RETURNS = "returns"
    LIQUIDITY = "liquidity"
    LEVERAGE = "leverage"
    EFFICIENCY = "efficiency"
    PER_SHARE = "per_share"
    VALUATION = "valuation"


class ReasonCode(StrEnum):
    """Why a (ticker, period, formula) cell has no kpi_facts row.

    Persisted onto metric_computation_attempts.reason_code — queryable
    directly, never inferred from a row's mere absence.
    """

    MISSING_INPUT = "missing_input"
    MISSING_INPUT_MAPPING = "missing_input_mapping"  # IFRS/standard has no mapped field
    NOT_APPLICABLE_BUSINESS_MODEL = "not_applicable_business_model"
    DENOMINATOR_LE_ZERO = "denominator_le_zero"
    UNSEALED_LINEAGE = "unsealed_lineage"


PeriodGrid = Literal["quarterly", "ttm", "fy"]


class FormulaDef(BaseModel):
    """One versioned formula spec. Immutable once committed — see module
    docstring on the append-only contract."""

    formula_key: str
    version: int
    category: MetricCategory
    display_formula: str
    method_notes: str
    required_inputs: tuple[CanonicalConcept, ...]
    optional_inputs: tuple[CanonicalConcept, ...] = ()
    alt_of: str | None = None
    period_grid: PeriodGrid
    unit: Unit
    excluded_business_models: frozenset[BusinessModelClass] = frozenset()

    def applies_to(self, business_model: BusinessModelClass) -> bool:
        return business_model not in self.excluded_business_models


# ---------------------------------------------------------------------------
# Phase 1 catalog — the 15 unambiguous metrics named in docs/design/
# bottoms_up_metrics_engine.md §6 "Phase 1". Every entry here is version 1.
# ---------------------------------------------------------------------------

GROSS_MARGIN = FormulaDef(
    formula_key="gross_margin",
    version=1,
    category=MetricCategory.MARGIN,
    display_formula="gross_profit / revenue (%)",
    method_notes="Both are as-filed lines; no method ambiguity.",
    required_inputs=(CanonicalConcept.GROSS_PROFIT, CanonicalConcept.REVENUE),
    period_grid="quarterly",
    unit=Unit.PERCENT,
    excluded_business_models=frozenset({BusinessModelClass.BANK, BusinessModelClass.INSURANCE}),
)

OPERATING_MARGIN = FormulaDef(
    formula_key="operating_margin",
    version=1,
    category=MetricCategory.MARGIN,
    display_formula="operating_income / revenue (%)",
    method_notes="No ambiguity for an operating company.",
    required_inputs=(CanonicalConcept.OPERATING_INCOME, CanonicalConcept.REVENUE),
    period_grid="quarterly",
    unit=Unit.PERCENT,
    excluded_business_models=frozenset({BusinessModelClass.BANK}),
)

NET_MARGIN = FormulaDef(
    formula_key="net_margin",
    version=1,
    category=MetricCategory.MARGIN,
    display_formula="net_income / revenue (%)",
    method_notes=(
        "net_income is as-filed (attributable to parent when minority interest exists); "
        "a net_margin_incl_minority variant is NOT created in v1."
    ),
    required_inputs=(CanonicalConcept.NET_INCOME, CanonicalConcept.REVENUE),
    period_grid="quarterly",
    unit=Unit.PERCENT,
)

EBITDA_MARGIN = FormulaDef(
    formula_key="ebitda_margin",
    version=1,
    category=MetricCategory.MARGIN,
    display_formula="ebitda / revenue (%), ebitda = operating_income + D&A",
    method_notes=(
        "EBITDA is computed as operating_income + depreciation_and_amortization, NOT "
        "net_income + interest + tax + D&A — a documented, disclosed method choice "
        "(the two can differ when there is non-operating income/expense above "
        "operating_income)."
    ),
    required_inputs=(
        CanonicalConcept.OPERATING_INCOME,
        CanonicalConcept.DEPRECIATION_AND_AMORTIZATION,
        CanonicalConcept.REVENUE,
    ),
    period_grid="quarterly",
    unit=Unit.PERCENT,
    excluded_business_models=frozenset({BusinessModelClass.BANK, BusinessModelClass.INSURANCE}),
)

FCF_MARGIN = FormulaDef(
    formula_key="fcf_margin",
    version=1,
    category=MetricCategory.MARGIN,
    display_formula="free_cash_flow / revenue (%)",
    method_notes=(
        "free_cash_flow = operating_cash_flow - capex as filed/derived; matches the "
        "existing fmp_derived_kpis.KPI_FCF_MARGIN_GAAP definition."
    ),
    required_inputs=(CanonicalConcept.FREE_CASH_FLOW, CanonicalConcept.REVENUE),
    period_grid="quarterly",
    unit=Unit.PERCENT,
)

REVENUE_YOY = FormulaDef(
    formula_key="revenue_yoy",
    version=1,
    category=MetricCategory.GROWTH,
    display_formula="(rev_t - rev_t-4q) / rev_t-4q (%)",
    method_notes=(
        "Same-calendar-quarter comparison (via report.sections.financials' "
        "calendar_quarter_key), never fiscal-label comparison."
    ),
    required_inputs=(CanonicalConcept.REVENUE,),
    period_grid="quarterly",
    unit=Unit.PERCENT,
)

REVENUE_QOQ = FormulaDef(
    formula_key="revenue_qoq",
    version=1,
    category=MetricCategory.GROWTH,
    display_formula="(rev_t - rev_t-1q) / rev_t-1q (%)",
    method_notes="Sequential — noisy for seasonal businesses; documented, not hidden.",
    required_inputs=(CanonicalConcept.REVENUE,),
    period_grid="quarterly",
    unit=Unit.PERCENT,
)

EPS_DILUTED_YOY = FormulaDef(
    formula_key="eps_diluted_yoy",
    version=1,
    category=MetricCategory.GROWTH,
    display_formula="(eps_t - eps_t-4q) / eps_t-4q (%)",
    method_notes="Skipped when prior EPS <= 0 (sign flip makes % meaningless).",
    required_inputs=(CanonicalConcept.EPS_DILUTED,),
    period_grid="quarterly",
    unit=Unit.PERCENT,
)

CURRENT_RATIO = FormulaDef(
    formula_key="current_ratio",
    version=1,
    category=MetricCategory.LIQUIDITY,
    display_formula="total_current_assets / total_current_liabilities",
    method_notes="No ambiguity.",
    required_inputs=(
        CanonicalConcept.TOTAL_CURRENT_ASSETS,
        CanonicalConcept.TOTAL_CURRENT_LIABILITIES,
    ),
    period_grid="quarterly",
    unit=Unit.RATIO,
    excluded_business_models=frozenset({BusinessModelClass.BANK, BusinessModelClass.INSURANCE}),
)

QUICK_RATIO = FormulaDef(
    formula_key="quick_ratio",
    version=1,
    category=MetricCategory.LIQUIDITY,
    display_formula="(total_current_assets - inventory) / total_current_liabilities",
    method_notes=(
        "inventory defaults to 0 when the concept doesn't exist for the filer "
        "(e.g. pure-services) rather than not_computable — a services business "
        "legitimately has zero inventory, this is NOT a missing input."
    ),
    required_inputs=(
        CanonicalConcept.TOTAL_CURRENT_ASSETS,
        CanonicalConcept.TOTAL_CURRENT_LIABILITIES,
    ),
    optional_inputs=(CanonicalConcept.INVENTORY,),
    period_grid="quarterly",
    unit=Unit.RATIO,
    excluded_business_models=frozenset({BusinessModelClass.BANK, BusinessModelClass.INSURANCE}),
)

CASH_RATIO = FormulaDef(
    formula_key="cash_ratio",
    version=1,
    category=MetricCategory.LIQUIDITY,
    display_formula="cash_and_equivalents / total_current_liabilities",
    method_notes="No ambiguity.",
    required_inputs=(
        CanonicalConcept.CASH_AND_EQUIVALENTS,
        CanonicalConcept.TOTAL_CURRENT_LIABILITIES,
    ),
    period_grid="quarterly",
    unit=Unit.RATIO,
    excluded_business_models=frozenset({BusinessModelClass.BANK, BusinessModelClass.INSURANCE}),
)

DEBT_TO_EQUITY = FormulaDef(
    formula_key="debt_to_equity",
    version=1,
    category=MetricCategory.LEVERAGE,
    display_formula="total_debt / total_stockholders_equity",
    method_notes=(
        "not_computable: denominator_le_zero when equity <= 0 (common for "
        "buyback-heavy names) rather than a nonsensical negative ratio."
    ),
    required_inputs=(CanonicalConcept.TOTAL_DEBT, CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY),
    period_grid="quarterly",
    unit=Unit.RATIO,
    excluded_business_models=frozenset({BusinessModelClass.BANK}),
)

ROE = FormulaDef(
    formula_key="roe",
    version=1,
    category=MetricCategory.RETURNS,
    display_formula="TTM net_income / total_stockholders_equity (%)",
    method_notes=(
        "Matches the existing fmp_derived_kpis.KPI_ROE definition; equity is "
        "period-END, not average — a documented choice (avg-of-2-quarters is the "
        "alternate, not built in v1). A bank's ROE has a different capital-adequacy "
        "context but is still computed here, just annotated — no exclusion."
    ),
    required_inputs=(CanonicalConcept.NET_INCOME, CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY),
    period_grid="ttm",
    unit=Unit.PERCENT,
)

ROA = FormulaDef(
    formula_key="roa",
    version=1,
    category=MetricCategory.RETURNS,
    display_formula="TTM net_income / total_assets (%)",
    method_notes="period-end assets, not average — same choice as ROE.",
    required_inputs=(CanonicalConcept.NET_INCOME, CanonicalConcept.TOTAL_ASSETS),
    period_grid="ttm",
    unit=Unit.PERCENT,
)

SBC_PCT_REVENUE = FormulaDef(
    formula_key="sbc_pct_revenue",
    version=1,
    category=MetricCategory.EFFICIENCY,
    display_formula="stock_based_compensation / revenue (%)",
    method_notes=(
        "Always computable when SBC is disclosed (0 for banks/older-economy names, "
        "that's a real number, not a gap)."
    ),
    required_inputs=(CanonicalConcept.STOCK_BASED_COMPENSATION, CanonicalConcept.REVENUE),
    period_grid="quarterly",
    unit=Unit.PERCENT,
)

# ---------------------------------------------------------------------------
# Phase 2 catalog — docs/design/bottoms_up_metrics_engine.md §1's remaining
# rows: the method-variant pairs, roce, net_debt_to_ebitda,
# interest_coverage, the turnover/CCC efficiency metrics, the remaining
# per-share metrics, and the two FY-cadence CAGR metrics. Every entry here
# is version 1.
# ---------------------------------------------------------------------------

ROIC_STRICT = FormulaDef(
    formula_key="roic_strict",
    version=1,
    category=MetricCategory.RETURNS,
    display_formula="nopat / (total_debt + total_stockholders_equity - cash_and_equivalents) (%)",
    method_notes=(
        "NOPAT = operating_income * (1 - effective_tax_rate); "
        "effective_tax_rate = clip(income_tax_expense / pretax_income, 0.0, 1.0), falling "
        "back to a flat 21% US-federal statutory proxy (method_flag: "
        "'statutory_tax_rate_fallback') when pretax_income <= 0 -- still a real value, not "
        "not_computable. Invested capital (strict) excludes operating leases -- this is the "
        "'strict' variant of the roic alt_of pair; both are stored, neither is preferred. "
        "TTM grid: operating_income/tax legs are TTM-summed, balance-sheet legs are "
        "period-end (matches the roe/roa convention)."
    ),
    required_inputs=(
        CanonicalConcept.OPERATING_INCOME,
        CanonicalConcept.INCOME_TAX_EXPENSE,
        CanonicalConcept.PRETAX_INCOME,
        CanonicalConcept.TOTAL_DEBT,
        CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY,
        CanonicalConcept.CASH_AND_EQUIVALENTS,
    ),
    alt_of="roic",
    period_grid="ttm",
    unit=Unit.PERCENT,
    excluded_business_models=frozenset({BusinessModelClass.BANK, BusinessModelClass.INSURANCE}),
)

ROIC_LEASE_ADJUSTED = FormulaDef(
    formula_key="roic_lease_adjusted",
    version=1,
    category=MetricCategory.RETURNS,
    display_formula=(
        "nopat / (total_debt + total_stockholders_equity - cash_and_equivalents + "
        "operating_lease_liability) (%)"
    ),
    method_notes=(
        "Alt of roic_strict: invested capital additionally adds back "
        "operating_lease_liability. Same NOPAT/tax-fallback method note as roic_strict."
    ),
    required_inputs=(
        CanonicalConcept.OPERATING_INCOME,
        CanonicalConcept.INCOME_TAX_EXPENSE,
        CanonicalConcept.PRETAX_INCOME,
        CanonicalConcept.TOTAL_DEBT,
        CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY,
        CanonicalConcept.CASH_AND_EQUIVALENTS,
        CanonicalConcept.OPERATING_LEASE_LIABILITY,
    ),
    alt_of="roic",
    period_grid="ttm",
    unit=Unit.PERCENT,
    excluded_business_models=frozenset({BusinessModelClass.BANK, BusinessModelClass.INSURANCE}),
)

ROCE = FormulaDef(
    formula_key="roce",
    version=1,
    category=MetricCategory.RETURNS,
    display_formula="ebit / (total_assets - total_current_liabilities) (%), ebit = operating_income",
    method_notes=(
        "'Capital employed' = total assets minus current liabilities (the common textbook "
        "definition); the equity + total_debt alternate definition is NOT built in v1. EBIT "
        "is treated as equivalent to operating_income -- no separate EBIT line exists in "
        "financial_facts, and this matches FMP's own cached ratios (ebitMargin == "
        "operatingProfitMargin, verified directly against a real MELI cache row)."
    ),
    required_inputs=(
        CanonicalConcept.OPERATING_INCOME,
        CanonicalConcept.TOTAL_ASSETS,
        CanonicalConcept.TOTAL_CURRENT_LIABILITIES,
    ),
    period_grid="ttm",
    unit=Unit.PERCENT,
    excluded_business_models=frozenset({BusinessModelClass.BANK, BusinessModelClass.INSURANCE}),
)

NET_DEBT_STRICT = FormulaDef(
    formula_key="net_debt_strict",
    version=1,
    category=MetricCategory.LEVERAGE,
    display_formula="total_debt - cash_and_equivalents - short_term_investments",
    method_notes=(
        "The AAPL-scale ambiguity the owner flagged: nets only cash + short-term "
        "(near-cash) investments. Can be negative (a net-cash position) -- that is a valid "
        "value, not an error."
    ),
    required_inputs=(
        CanonicalConcept.TOTAL_DEBT,
        CanonicalConcept.CASH_AND_EQUIVALENTS,
        CanonicalConcept.SHORT_TERM_INVESTMENTS,
    ),
    alt_of="net_debt",
    period_grid="quarterly",
    unit=Unit.ACTUAL,
)

NET_DEBT_INCL_LT_SECURITIES = FormulaDef(
    formula_key="net_debt_incl_lt_securities",
    version=1,
    category=MetricCategory.LEVERAGE,
    display_formula="net_debt_strict - long_term_investments",
    method_notes=(
        "Alt of net_debt_strict: whether long-term marketable securities count as "
        "cash-like is a real analyst judgment call (the ~$78B AAPL swing case), so both are "
        "stored, neither is 'the' net debt."
    ),
    required_inputs=(
        CanonicalConcept.TOTAL_DEBT,
        CanonicalConcept.CASH_AND_EQUIVALENTS,
        CanonicalConcept.SHORT_TERM_INVESTMENTS,
        CanonicalConcept.LONG_TERM_INVESTMENTS,
    ),
    alt_of="net_debt",
    period_grid="quarterly",
    unit=Unit.ACTUAL,
)

NET_DEBT_TO_EBITDA = FormulaDef(
    formula_key="net_debt_to_ebitda",
    version=1,
    category=MetricCategory.LEVERAGE,
    display_formula="net_debt_strict / ebitda_ttm",
    method_notes=(
        "Uses the strict net-debt variant by convention (documented); a sibling using "
        "net_debt_incl_lt_securities is not built as its own formula_id -- compute on "
        "demand from the two already-stored components if needed. ebitda_ttm uses the same "
        "operating_income + D&A definition as ebitda_margin."
    ),
    required_inputs=(
        CanonicalConcept.TOTAL_DEBT,
        CanonicalConcept.CASH_AND_EQUIVALENTS,
        CanonicalConcept.SHORT_TERM_INVESTMENTS,
        CanonicalConcept.OPERATING_INCOME,
        CanonicalConcept.DEPRECIATION_AND_AMORTIZATION,
    ),
    period_grid="ttm",
    unit=Unit.RATIO,
    excluded_business_models=frozenset({BusinessModelClass.BANK, BusinessModelClass.INSURANCE}),
)

INTEREST_COVERAGE = FormulaDef(
    formula_key="interest_coverage",
    version=1,
    category=MetricCategory.LEVERAGE,
    display_formula="ebit / interest_expense, ebit = operating_income",
    method_notes=(
        "not_computable: missing_input when interest_expense is 0/absent (debt-free names) "
        "-- never a divide-by-zero crash, never an infinite ratio silently shown."
    ),
    required_inputs=(CanonicalConcept.OPERATING_INCOME, CanonicalConcept.INTEREST_EXPENSE),
    period_grid="quarterly",
    unit=Unit.RATIO,
    excluded_business_models=frozenset({BusinessModelClass.BANK, BusinessModelClass.INSURANCE}),
)

ASSET_TURNOVER = FormulaDef(
    formula_key="asset_turnover",
    version=1,
    category=MetricCategory.EFFICIENCY,
    display_formula="revenue_ttm / total_assets_avg",
    method_notes=(
        "Uses AVERAGE assets (2-point avg of the TTM window's first and last quarter-end "
        "balances), unlike roe/roa above which use period-end -- documented per-metric, not "
        "a blanket convention."
    ),
    required_inputs=(CanonicalConcept.REVENUE, CanonicalConcept.TOTAL_ASSETS),
    period_grid="ttm",
    unit=Unit.RATIO,
    excluded_business_models=frozenset({BusinessModelClass.BANK}),
)

RECEIVABLES_TURNOVER = FormulaDef(
    formula_key="receivables_turnover",
    version=1,
    category=MetricCategory.EFFICIENCY,
    display_formula="revenue_ttm / accounts_receivable_avg",
    method_notes=(
        "not_computable: not_applicable_business_model for subscription/consumer "
        "businesses with no material AR -- documented per-ticker via "
        "applicability._TICKER_EFFICIENCY_OVERRIDES, not inferred. No portfolio/evaluation "
        "roster name currently qualifies (every checked ticker carries AR that is a "
        "material fraction of revenue) -- the override table stays empty for this formula "
        "until a real case is verified, per the same no-guessing discipline as "
        "IFRS_FIELD_MAP."
    ),
    required_inputs=(CanonicalConcept.REVENUE, CanonicalConcept.ACCOUNTS_RECEIVABLE),
    period_grid="ttm",
    unit=Unit.RATIO,
    excluded_business_models=frozenset({BusinessModelClass.BANK}),
)

INVENTORY_TURNOVER = FormulaDef(
    formula_key="inventory_turnover",
    version=1,
    category=MetricCategory.EFFICIENCY,
    display_formula="cost_of_revenue_ttm / inventory_avg",
    method_notes=(
        "not_computable: not_applicable_business_model for services/software names -- "
        "inventory=0 companies get this metric suppressed entirely (unlike quick_ratio, "
        "where inventory=0 still produces a valid ratio). Per-ticker overrides in "
        "applicability._TICKER_EFFICIENCY_OVERRIDES, verified against real "
        "financial_facts.inventory values (a consistent $0 balance across quarters, not an "
        "absent field)."
    ),
    required_inputs=(CanonicalConcept.COST_OF_REVENUE, CanonicalConcept.INVENTORY),
    period_grid="ttm",
    unit=Unit.RATIO,
    excluded_business_models=frozenset({BusinessModelClass.BANK, BusinessModelClass.INSURANCE}),
)

CASH_CONVERSION_CYCLE = FormulaDef(
    formula_key="cash_conversion_cycle",
    version=1,
    category=MetricCategory.EFFICIENCY,
    display_formula="DIO + DSO - DPO (days)",
    method_notes=(
        "DIO = 365 * inventory_avg / cost_of_revenue_ttm; DSO = 365 * "
        "accounts_receivable_avg / revenue_ttm; DPO = 365 * accounts_payable_avg / "
        "cost_of_revenue_ttm. A composite of 3 already-ambiguous inputs -- not_computable "
        "propagates if any leg is not_computable. Same business-model/ticker exclusions as "
        "its inventory_turnover leg (the binding constraint of the three). Expressed in "
        "days; there is no dedicated models.facts.Unit member for a day-count, so this uses "
        "Unit.RATIO with the day-count documented here rather than in the unit itself."
    ),
    required_inputs=(
        CanonicalConcept.REVENUE,
        CanonicalConcept.COST_OF_REVENUE,
        CanonicalConcept.INVENTORY,
        CanonicalConcept.ACCOUNTS_RECEIVABLE,
        CanonicalConcept.ACCOUNTS_PAYABLE,
    ),
    period_grid="ttm",
    unit=Unit.RATIO,
    excluded_business_models=frozenset({BusinessModelClass.BANK, BusinessModelClass.INSURANCE}),
)

EPS_ADJUSTED_EX_SBC = FormulaDef(
    formula_key="eps_adjusted_ex_sbc",
    version=1,
    category=MetricCategory.PER_SHARE,
    display_formula="(net_income + stock_based_compensation) / weighted_avg_shares_diluted",
    method_notes=(
        "method_flag: 'sbc_addback_pretax' is ALWAYS present on a successful compute -- "
        "adding back SBC pre-tax overstates adjusted EPS versus a proper tax-effected "
        "add-back; v1 documents this as the known simplification rather than computing a "
        "marginal tax adjustment (Phase 3+ candidate)."
    ),
    required_inputs=(
        CanonicalConcept.NET_INCOME,
        CanonicalConcept.STOCK_BASED_COMPENSATION,
        CanonicalConcept.WEIGHTED_AVG_SHARES_DILUTED,
    ),
    period_grid="quarterly",
    unit=Unit.ACTUAL,
)

BVPS = FormulaDef(
    formula_key="bvps",
    version=1,
    category=MetricCategory.PER_SHARE,
    display_formula="total_stockholders_equity / weighted_avg_shares_diluted",
    method_notes=(
        "Uses period-end diluted share count, not spot shares outstanding -- documented."
    ),
    required_inputs=(
        CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY,
        CanonicalConcept.WEIGHTED_AVG_SHARES_DILUTED,
    ),
    period_grid="quarterly",
    unit=Unit.ACTUAL,
)

FCF_PER_SHARE = FormulaDef(
    formula_key="fcf_per_share",
    version=1,
    category=MetricCategory.PER_SHARE,
    display_formula="free_cash_flow / weighted_avg_shares_diluted",
    method_notes="No ambiguity.",
    required_inputs=(
        CanonicalConcept.FREE_CASH_FLOW,
        CanonicalConcept.WEIGHTED_AVG_SHARES_DILUTED,
    ),
    period_grid="quarterly",
    unit=Unit.ACTUAL,
)

REVENUE_CAGR_3Y = FormulaDef(
    formula_key="revenue_cagr_3y",
    version=1,
    category=MetricCategory.GROWTH,
    display_formula="(rev_FY / rev_FY-3)^(1/3) - 1 (%)",
    method_notes=(
        "Annual-cadence only (period_grid='fy'); requires 3 full FY gaps -- io.py guards "
        "the actual calendar-day span between the two FY rows so a data gap can never be "
        "silently read as 'exactly 3 years apart' (no interpolation across a stub year). "
        "not_computable: denominator_le_zero when either endpoint is <= 0 (a sign flip "
        "makes a CAGR percentage meaningless, same reasoning as eps_diluted_yoy)."
    ),
    required_inputs=(CanonicalConcept.REVENUE,),
    period_grid="fy",
    unit=Unit.PERCENT,
)

EBITDA_CAGR_3Y = FormulaDef(
    formula_key="ebitda_cagr_3y",
    version=1,
    category=MetricCategory.GROWTH,
    display_formula="(ebitda_FY / ebitda_FY-3)^(1/3) - 1 (%)",
    method_notes=(
        "Same shape as revenue_cagr_3y over EBITDA (FY); inherits the ebitda_margin "
        "EBITDA method note (operating_income + D&A, not net_income + interest + tax + D&A)."
    ),
    required_inputs=(
        CanonicalConcept.OPERATING_INCOME,
        CanonicalConcept.DEPRECIATION_AND_AMORTIZATION,
    ),
    period_grid="fy",
    unit=Unit.PERCENT,
    excluded_business_models=frozenset({BusinessModelClass.BANK, BusinessModelClass.INSURANCE}),
)

_PHASE_2_FORMULAS: tuple[FormulaDef, ...] = (
    ROIC_STRICT,
    ROIC_LEASE_ADJUSTED,
    ROCE,
    NET_DEBT_STRICT,
    NET_DEBT_INCL_LT_SECURITIES,
    NET_DEBT_TO_EBITDA,
    INTEREST_COVERAGE,
    ASSET_TURNOVER,
    RECEIVABLES_TURNOVER,
    INVENTORY_TURNOVER,
    CASH_CONVERSION_CYCLE,
    EPS_ADJUSTED_EX_SBC,
    BVPS,
    FCF_PER_SHARE,
    REVENUE_CAGR_3Y,
    EBITDA_CAGR_3Y,
)

# ---------------------------------------------------------------------------
# Phase 3 catalog -- docs/design/bottoms_up_metrics_engine.md section 1's
# Valuation table: the 7 valuation formulas wired to live price
# (pe_ttm/ps_ttm/pb/ev_ebitda/ev_sales/fcf_yield/earnings_yield) plus
# enterprise_value_strict, the shared intermediate they build on. Every
# entry here is version 1, period_grid="ttm" (TTM fundamentals combined with
# a SPOT price/market-cap leg -- see compute.metrics_engine.io's
# _VALUATION_FORMULA_KEYS docstring for why these are computed ONLY at the
# latest calendar quarter, never backfilled historically).
# ---------------------------------------------------------------------------

_MARKET_CAP_METHOD_NOTE = (
    "market_cap is computed bottoms-up as a live spot price (sources.price."
    "read_live_price -- the SAME multi-source stack src/dcf/reprice.py and "
    "research_cockpit.profile_quote already use, per the mandate to reuse the "
    "existing live-price path rather than add a new fetcher) times the latest "
    "quarter's weighted_avg_shares_diluted, NOT read from the cached "
    "historical_market_cap.json row docs/design/bottoms_up_metrics_engine.md "
    "section 1 names. Deviation, justified: the cached file can be up to a day "
    "stale relative to a live quote, which would otherwise silently misalign a "
    "P/E computed from a fresh price against a P/S computed from yesterday's "
    "cap -- computing both legs from the SAME price keeps every valuation "
    "multiple internally consistent. Spot-only: computed ONLY for the latest "
    "period (never backfilled to a historical calendar quarter), since a live "
    "price has no historical equivalent without a separate price-series fetch. "
    "Staleness is answerable without re-deriving anything: the kpi_facts row's "
    "own period_end is the FUNDAMENTALS date (the TTM window's end); the "
    "PRICE/MARKET_CAP inputs' own period_end inside computed_from.inputs[] is "
    "the live quote's fetched_at date -- comparing the two tells you exactly "
    "how stale the price leg is relative to the fundamentals leg."
)

ENTERPRISE_VALUE_STRICT_OMITTED_FLAG = "minority_interest_preferred_omitted"

ENTERPRISE_VALUE_STRICT = FormulaDef(
    formula_key="enterprise_value_strict",
    version=1,
    category=MetricCategory.VALUATION,
    display_formula="market_cap + net_debt_strict",
    method_notes=(
        f"{_MARKET_CAP_METHOD_NOTE} Deviation from docs/design/"
        "bottoms_up_metrics_engine.md section 1: the doc's formula additionally "
        "adds minority_interest + preferred_equity, but neither concept has a "
        "verified financial_facts.line_item mapping (no _LINE_ITEM_SPEC/XBRL-tag "
        "entry exists for either, unlike net_debt_strict's constituent concepts, "
        "which reuse Phase 1/2's already-verified fields) -- inventing an "
        "unverified field name would break the engine's no-guessing discipline "
        "(the same reasoning IFRS_FIELD_MAP and applicability's empty override "
        "tables already apply). v1 ships the two-term sum and tags every "
        f"successful compute with method_flag '{ENTERPRISE_VALUE_STRICT_OMITTED_FLAG}' "
        "rather than silently guessing a field or blocking the whole metric; "
        "adding the two remaining legs is a tracked follow-up once verified "
        "against a roster filer that actually carries them."
    ),
    required_inputs=(
        CanonicalConcept.MARKET_CAP,
        CanonicalConcept.TOTAL_DEBT,
        CanonicalConcept.CASH_AND_EQUIVALENTS,
        CanonicalConcept.SHORT_TERM_INVESTMENTS,
    ),
    period_grid="ttm",
    unit=Unit.ACTUAL,
)

PE_TTM = FormulaDef(
    formula_key="pe_ttm",
    version=1,
    category=MetricCategory.VALUATION,
    display_formula="price / eps_diluted_ttm",
    method_notes=(
        "price is a live spot quote (sources.price.read_live_price). "
        "not_computable: denominator_le_zero when TTM diluted EPS <= 0 -- never "
        "a negative or inverted P/E. Spot-only (see enterprise_value_strict's "
        "method_notes for the shared spot-price/staleness contract)."
    ),
    required_inputs=(CanonicalConcept.PRICE, CanonicalConcept.EPS_DILUTED),
    period_grid="ttm",
    unit=Unit.RATIO,
)

PS_TTM = FormulaDef(
    formula_key="ps_ttm",
    version=1,
    category=MetricCategory.VALUATION,
    display_formula="market_cap / revenue_ttm",
    method_notes=_MARKET_CAP_METHOD_NOTE,
    required_inputs=(CanonicalConcept.MARKET_CAP, CanonicalConcept.REVENUE),
    period_grid="ttm",
    unit=Unit.RATIO,
)

PB = FormulaDef(
    formula_key="pb",
    version=1,
    category=MetricCategory.VALUATION,
    display_formula="market_cap / total_stockholders_equity",
    method_notes=(
        f"{_MARKET_CAP_METHOD_NOTE} not_computable: denominator_le_zero when "
        "equity <= 0 (same reasoning as debt_to_equity)."
    ),
    required_inputs=(CanonicalConcept.MARKET_CAP, CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY),
    period_grid="ttm",
    unit=Unit.RATIO,
)

EV_EBITDA = FormulaDef(
    formula_key="ev_ebitda",
    version=1,
    category=MetricCategory.VALUATION,
    display_formula="enterprise_value_strict / ebitda_ttm",
    method_notes=(
        f"{_MARKET_CAP_METHOD_NOTE} Inherits enterprise_value_strict's "
        "minority-interest/preferred-equity omission (method_flag "
        f"'{ENTERPRISE_VALUE_STRICT_OMITTED_FLAG}' carried through) and the "
        "same ebitda definition as ebitda_margin (operating_income + D&A)."
    ),
    required_inputs=(
        CanonicalConcept.MARKET_CAP,
        CanonicalConcept.TOTAL_DEBT,
        CanonicalConcept.CASH_AND_EQUIVALENTS,
        CanonicalConcept.SHORT_TERM_INVESTMENTS,
        CanonicalConcept.OPERATING_INCOME,
        CanonicalConcept.DEPRECIATION_AND_AMORTIZATION,
    ),
    period_grid="ttm",
    unit=Unit.RATIO,
    excluded_business_models=frozenset({BusinessModelClass.BANK, BusinessModelClass.INSURANCE}),
)

EV_SALES = FormulaDef(
    formula_key="ev_sales",
    version=1,
    category=MetricCategory.VALUATION,
    display_formula="enterprise_value_strict / revenue_ttm",
    method_notes=(
        f"{_MARKET_CAP_METHOD_NOTE} Inherits enterprise_value_strict's "
        "minority-interest/preferred-equity omission (method_flag "
        f"'{ENTERPRISE_VALUE_STRICT_OMITTED_FLAG}' carried through)."
    ),
    required_inputs=(
        CanonicalConcept.MARKET_CAP,
        CanonicalConcept.TOTAL_DEBT,
        CanonicalConcept.CASH_AND_EQUIVALENTS,
        CanonicalConcept.SHORT_TERM_INVESTMENTS,
        CanonicalConcept.REVENUE,
    ),
    period_grid="ttm",
    unit=Unit.RATIO,
)

FCF_YIELD = FormulaDef(
    formula_key="fcf_yield",
    version=1,
    category=MetricCategory.VALUATION,
    display_formula="free_cash_flow_ttm / market_cap (%)",
    method_notes=_MARKET_CAP_METHOD_NOTE,
    required_inputs=(CanonicalConcept.FREE_CASH_FLOW, CanonicalConcept.MARKET_CAP),
    period_grid="ttm",
    unit=Unit.PERCENT,
)

EARNINGS_YIELD = FormulaDef(
    formula_key="earnings_yield",
    version=1,
    category=MetricCategory.VALUATION,
    display_formula="net_income_ttm / market_cap (%)",
    method_notes=(
        f"{_MARKET_CAP_METHOD_NOTE} Inverse of trailing P/E, kept as its own "
        "formula for direct comparability with fcf_yield (doc's own reasoning)."
    ),
    required_inputs=(CanonicalConcept.NET_INCOME, CanonicalConcept.MARKET_CAP),
    period_grid="ttm",
    unit=Unit.PERCENT,
)

_PHASE_3_FORMULAS: tuple[FormulaDef, ...] = (
    ENTERPRISE_VALUE_STRICT,
    PE_TTM,
    PS_TTM,
    PB,
    EV_EBITDA,
    EV_SALES,
    FCF_YIELD,
    EARNINGS_YIELD,
)

_PHASE_1_FORMULAS: tuple[FormulaDef, ...] = (
    GROSS_MARGIN,
    OPERATING_MARGIN,
    NET_MARGIN,
    EBITDA_MARGIN,
    FCF_MARGIN,
    REVENUE_YOY,
    REVENUE_QOQ,
    EPS_DILUTED_YOY,
    CURRENT_RATIO,
    QUICK_RATIO,
    CASH_RATIO,
    DEBT_TO_EQUITY,
    ROE,
    ROA,
    SBC_PCT_REVENUE,
)

# dict[(formula_key, version), FormulaDef] per docs/design/
# bottoms_up_metrics_engine.md §2's registry.py sketch.
REGISTRY: dict[tuple[str, int], FormulaDef] = {
    (f.formula_key, f.version): f
    for f in (*_PHASE_1_FORMULAS, *_PHASE_2_FORMULAS, *_PHASE_3_FORMULAS)
}


def latest(formula_key: str) -> FormulaDef | None:
    """Return the highest-version FormulaDef for `formula_key`, or None."""
    candidates = [f for (key, _v), f in REGISTRY.items() if key == formula_key]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.version)


def all_latest() -> tuple[FormulaDef, ...]:
    """One FormulaDef per formula_key — the highest version of each."""
    keys = {key for key, _v in REGISTRY}
    formulas = [latest(key) for key in keys]
    return tuple(sorted((f for f in formulas if f is not None), key=lambda f: f.formula_key))
