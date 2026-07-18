"""Pure compute(formula, inputs) -> ComputedValue | NotComputable.

No DB I/O, no logging side effects -- io.py is the only module in this
package touching sqlite3.Connection. It resolves each formula's inputs
from financial_facts (including any TTM summing for a period_grid="ttm"
formula) and calls compute() here with plain, already-resolved values.

Deviation from docs/design/bottoms_up_metrics_engine.md section 2's
engine.py sketch: the doc's compute() signature takes a single
dict[CanonicalConcept, Decimal | None], which cannot hold two values for
the same concept -- but a growth formula (revenue_yoy/revenue_qoq/
eps_diluted_yoy) needs BOTH the current and the same-concept prior-period
value. Added a keyword-only prior_inputs parameter (default None,
ignored by every non-growth formula) rather than overloading
CanonicalConcept with synthetic _PRIOR members.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from pydantic import BaseModel

from .inputs import CanonicalConcept
from .registry import FormulaDef, ReasonCode

ResolvedInputs = dict[CanonicalConcept, Decimal | None]


class ComputedValue(BaseModel):
    """A successfully computed metric value."""

    value: Decimal
    method_flags: tuple[str, ...] = ()


class NotComputable(BaseModel):
    """Why a (ticker, period, formula) cell has no value this run."""

    reason_code: ReasonCode
    reason_detail: str


ComputeResult = ComputedValue | NotComputable


def _missing(concept: CanonicalConcept) -> NotComputable:
    return NotComputable(
        reason_code=ReasonCode.MISSING_INPUT,
        reason_detail=f"{concept.value} not resolved for this period",
    )


def _denominator_le_zero(concept: CanonicalConcept, value: Decimal) -> NotComputable:
    return NotComputable(
        reason_code=ReasonCode.DENOMINATOR_LE_ZERO,
        reason_detail=f"{concept.value}={value} <= 0",
    )


def compute_ebitda(resolved: ResolvedInputs) -> Decimal | NotComputable:
    """EBITDA = operating_income + depreciation_and_amortization.

    A first-class intermediate (docs/design/bottoms_up_metrics_engine.md
    section 2): any formula consuming EBITDA inherits this ONE documented
    definition rather than re-deriving it. NOT net_income + interest + tax
    + D&A -- a documented, disclosed method choice (see
    registry.EBITDA_MARGIN's method_notes).
    """
    operating_income = resolved.get(CanonicalConcept.OPERATING_INCOME)
    d_and_a = resolved.get(CanonicalConcept.DEPRECIATION_AND_AMORTIZATION)
    if operating_income is None:
        return _missing(CanonicalConcept.OPERATING_INCOME)
    if d_and_a is None:
        return _missing(CanonicalConcept.DEPRECIATION_AND_AMORTIZATION)
    return operating_income + d_and_a


def _pct_ratio(
    numerator: CanonicalConcept, denominator: CanonicalConcept
) -> Callable[[ResolvedInputs, ResolvedInputs | None], ComputeResult]:
    """numerator / denominator * 100, guarding a non-positive denominator."""

    def _fn(resolved: ResolvedInputs, _prior: ResolvedInputs | None) -> ComputeResult:
        num = resolved.get(numerator)
        den = resolved.get(denominator)
        if num is None:
            return _missing(numerator)
        if den is None:
            return _missing(denominator)
        if den <= 0:
            return _denominator_le_zero(denominator, den)
        return ComputedValue(value=(num / den) * Decimal(100))

    return _fn


def _ratio(
    numerator: CanonicalConcept, denominator: CanonicalConcept
) -> Callable[[ResolvedInputs, ResolvedInputs | None], ComputeResult]:
    """numerator / denominator (no percent scaling), guarding the denominator."""

    def _fn(resolved: ResolvedInputs, _prior: ResolvedInputs | None) -> ComputeResult:
        num = resolved.get(numerator)
        den = resolved.get(denominator)
        if num is None:
            return _missing(numerator)
        if den is None:
            return _missing(denominator)
        if den <= 0:
            return _denominator_le_zero(denominator, den)
        return ComputedValue(value=num / den)

    return _fn


def _ebitda_margin(resolved: ResolvedInputs, _prior: ResolvedInputs | None) -> ComputeResult:
    ebitda = compute_ebitda(resolved)
    if isinstance(ebitda, NotComputable):
        return ebitda
    revenue = resolved.get(CanonicalConcept.REVENUE)
    if revenue is None:
        return _missing(CanonicalConcept.REVENUE)
    if revenue <= 0:
        return _denominator_le_zero(CanonicalConcept.REVENUE, revenue)
    return ComputedValue(value=(ebitda / revenue) * Decimal(100))


def _quick_ratio(resolved: ResolvedInputs, _prior: ResolvedInputs | None) -> ComputeResult:
    tca = resolved.get(CanonicalConcept.TOTAL_CURRENT_ASSETS)
    tcl = resolved.get(CanonicalConcept.TOTAL_CURRENT_LIABILITIES)
    if tca is None:
        return _missing(CanonicalConcept.TOTAL_CURRENT_ASSETS)
    if tcl is None:
        return _missing(CanonicalConcept.TOTAL_CURRENT_LIABILITIES)
    if tcl <= 0:
        return _denominator_le_zero(CanonicalConcept.TOTAL_CURRENT_LIABILITIES, tcl)
    # inventory is optional and zero-defaults (registry.QUICK_RATIO's
    # method_notes) -- a services business legitimately has none; that is
    # NOT a missing input.
    inventory = resolved.get(CanonicalConcept.INVENTORY)
    if inventory is None:
        inventory = Decimal(0)
    return ComputedValue(value=(tca - inventory) / tcl)


def _ttm_return(
    denominator: CanonicalConcept,
) -> Callable[[ResolvedInputs, ResolvedInputs | None], ComputeResult]:
    """TTM net_income / a point-in-time balance-sheet stock concept, as a percent.

    Shared shape for roe (denominator=TOTAL_STOCKHOLDERS_EQUITY) and roa
    (denominator=TOTAL_ASSETS). io.py is responsible for having already
    summed NET_INCOME over the trailing 4 calendar quarters before calling
    compute() -- this function only guards and divides.
    """

    def _fn(resolved: ResolvedInputs, _prior: ResolvedInputs | None) -> ComputeResult:
        ttm_net_income = resolved.get(CanonicalConcept.NET_INCOME)
        denom_value = resolved.get(denominator)
        if ttm_net_income is None:
            return _missing(CanonicalConcept.NET_INCOME)
        if denom_value is None:
            return _missing(denominator)
        if denom_value <= 0:
            return _denominator_le_zero(denominator, denom_value)
        return ComputedValue(value=(ttm_net_income / denom_value) * Decimal(100))

    return _fn


def _period_over_period_pct(
    concept: CanonicalConcept,
) -> Callable[[ResolvedInputs, ResolvedInputs | None], ComputeResult]:
    """(cur - prior) / prior * 100 for the same concept across two periods.

    Shared shape for revenue_yoy, revenue_qoq, eps_diluted_yoy. A
    non-positive prior value is skipped (DENOMINATOR_LE_ZERO) rather than
    producing a sign-flipped or divide-by-zero result -- the doc calls this
    out explicitly for eps_diluted_yoy ("skipped when prior EPS <= 0") and
    it applies uniformly here since a prior value <= 0 makes any of these
    percent-change formulas equally meaningless.
    """

    def _fn(resolved: ResolvedInputs, prior: ResolvedInputs | None) -> ComputeResult:
        if prior is None:
            return NotComputable(
                reason_code=ReasonCode.MISSING_INPUT,
                reason_detail=f"no prior-period {concept.value} resolved",
            )
        current = resolved.get(concept)
        prior_value = prior.get(concept)
        if current is None:
            return _missing(concept)
        if prior_value is None:
            return NotComputable(
                reason_code=ReasonCode.MISSING_INPUT,
                reason_detail=f"no prior-period {concept.value} resolved",
            )
        if prior_value <= 0:
            return _denominator_le_zero(concept, prior_value)
        return ComputedValue(value=(current - prior_value) / prior_value * Decimal(100))

    return _fn


_DISPATCH: dict[str, Callable[[ResolvedInputs, ResolvedInputs | None], ComputeResult]] = {
    "gross_margin": _pct_ratio(CanonicalConcept.GROSS_PROFIT, CanonicalConcept.REVENUE),
    "operating_margin": _pct_ratio(CanonicalConcept.OPERATING_INCOME, CanonicalConcept.REVENUE),
    "net_margin": _pct_ratio(CanonicalConcept.NET_INCOME, CanonicalConcept.REVENUE),
    "ebitda_margin": _ebitda_margin,
    "fcf_margin": _pct_ratio(CanonicalConcept.FREE_CASH_FLOW, CanonicalConcept.REVENUE),
    "revenue_yoy": _period_over_period_pct(CanonicalConcept.REVENUE),
    "revenue_qoq": _period_over_period_pct(CanonicalConcept.REVENUE),
    "eps_diluted_yoy": _period_over_period_pct(CanonicalConcept.EPS_DILUTED),
    "current_ratio": _ratio(
        CanonicalConcept.TOTAL_CURRENT_ASSETS, CanonicalConcept.TOTAL_CURRENT_LIABILITIES
    ),
    "quick_ratio": _quick_ratio,
    "cash_ratio": _ratio(
        CanonicalConcept.CASH_AND_EQUIVALENTS, CanonicalConcept.TOTAL_CURRENT_LIABILITIES
    ),
    "debt_to_equity": _ratio(
        CanonicalConcept.TOTAL_DEBT, CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY
    ),
    "roe": _ttm_return(CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY),
    "roa": _ttm_return(CanonicalConcept.TOTAL_ASSETS),
    "sbc_pct_revenue": _pct_ratio(
        CanonicalConcept.STOCK_BASED_COMPENSATION, CanonicalConcept.REVENUE
    ),
}


def compute(
    formula: FormulaDef,
    resolved_inputs: ResolvedInputs,
    *,
    prior_inputs: ResolvedInputs | None = None,
) -> ComputeResult:
    """Compute one formula's value from already-resolved inputs.

    resolved_inputs carries the CURRENT period's values (TTM-summed already
    for a period_grid="ttm" formula -- io.py's job, not this function's).
    prior_inputs is required only by the growth formulas (see module
    docstring); every other dispatch ignores it.
    """
    fn = _DISPATCH.get(formula.formula_key)
    if fn is None:
        raise ValueError(
            f"metrics_engine.engine: no compute dispatch registered for "
            f"formula_key={formula.formula_key!r} (v{formula.version})"
        )
    return fn(resolved_inputs, prior_inputs)
