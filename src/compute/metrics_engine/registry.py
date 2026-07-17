"""FormulaDef (typed spec) + the versioned formula registry.

`REGISTRY` is append-only in the literal sense: once a `(formula_key,
version)` pair is committed, its `display_formula`/`method_notes`/inputs
never change — a formula change is always a new `version` entry. A DB
mirror (`formula_definitions`, alembic 0153) is upserted from this module at
engine-startup via `io.upsert_formula_definitions`, mirroring
`pipeline.kpi_persistence.find_or_create_kpi_definition`'s idempotency
pattern.

Phase 1 scope (docs/design/bottoms_up_metrics_engine.md §6): the ~15
unambiguous metrics with no method-variant ambiguity. `net_debt_*`,
`roic_*`, `roce`, and every other `alt_of`-grouped pair are explicitly out
of scope until Phase 2 settles the variant-surfacing convention.
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
    (f.formula_key, f.version): f for f in _PHASE_1_FORMULAS
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
