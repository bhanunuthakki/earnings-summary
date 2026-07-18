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


def _denominator_le_zero_label(label: str, value: Decimal) -> NotComputable:
    """Same as _denominator_le_zero but for a composite denominator with no
    single matching CanonicalConcept (e.g. invested_capital, capital_employed
    -- each a combination of several concepts, not one raw input)."""
    return NotComputable(
        reason_code=ReasonCode.DENOMINATOR_LE_ZERO,
        reason_detail=f"{label}={value} <= 0",
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


# ---------------------------------------------------------------------------
# Phase 2 additions -- roic_*/roce (NOPAT + invested capital), net_debt_*,
# net_debt_to_ebitda, interest_coverage, the turnover/CCC efficiency metrics,
# the remaining per-share metrics, and the two FY-cadence CAGR metrics.
# ---------------------------------------------------------------------------

_STATUTORY_TAX_RATE_FALLBACK = Decimal("0.21")
STATUTORY_TAX_RATE_FALLBACK_FLAG = "statutory_tax_rate_fallback"
SBC_ADDBACK_PRETAX_FLAG = "sbc_addback_pretax"


def _effective_tax_rate(resolved: ResolvedInputs) -> tuple[Decimal, bool]:
    """clip(income_tax_expense / pretax_income, 0, 1); falls back to the flat
    21% US-federal statutory proxy when pretax_income <= 0 (registry.py's
    NOPAT method note). Returns (rate, used_fallback)."""
    tax = resolved.get(CanonicalConcept.INCOME_TAX_EXPENSE)
    pretax = resolved.get(CanonicalConcept.PRETAX_INCOME)
    if tax is None or pretax is None or pretax <= 0:
        return _STATUTORY_TAX_RATE_FALLBACK, True
    rate = tax / pretax
    clipped = min(max(rate, Decimal(0)), Decimal(1))
    return clipped, False


def compute_nopat(resolved: ResolvedInputs) -> tuple[Decimal, bool] | NotComputable:
    """NOPAT = operating_income * (1 - effective_tax_rate).

    Returns (nopat, used_statutory_fallback) so callers can attach
    method_flags.STATUTORY_TAX_RATE_FALLBACK_FLAG on success.
    """
    operating_income = resolved.get(CanonicalConcept.OPERATING_INCOME)
    if operating_income is None:
        return _missing(CanonicalConcept.OPERATING_INCOME)
    rate, used_fallback = _effective_tax_rate(resolved)
    return operating_income * (Decimal(1) - rate), used_fallback


def compute_invested_capital_strict(resolved: ResolvedInputs) -> Decimal | NotComputable:
    """total_debt + total_stockholders_equity - cash_and_equivalents."""
    total_debt = resolved.get(CanonicalConcept.TOTAL_DEBT)
    equity = resolved.get(CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY)
    cash = resolved.get(CanonicalConcept.CASH_AND_EQUIVALENTS)
    if total_debt is None:
        return _missing(CanonicalConcept.TOTAL_DEBT)
    if equity is None:
        return _missing(CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY)
    if cash is None:
        return _missing(CanonicalConcept.CASH_AND_EQUIVALENTS)
    return total_debt + equity - cash


def _roic(
    *, lease_adjusted: bool
) -> Callable[[ResolvedInputs, ResolvedInputs | None], ComputeResult]:
    def _fn(resolved: ResolvedInputs, _prior: ResolvedInputs | None) -> ComputeResult:
        nopat_result = compute_nopat(resolved)
        if isinstance(nopat_result, NotComputable):
            return nopat_result
        nopat, used_fallback = nopat_result

        invested_capital = compute_invested_capital_strict(resolved)
        if isinstance(invested_capital, NotComputable):
            return invested_capital
        if lease_adjusted:
            lease_liability = resolved.get(CanonicalConcept.OPERATING_LEASE_LIABILITY)
            if lease_liability is None:
                return _missing(CanonicalConcept.OPERATING_LEASE_LIABILITY)
            invested_capital = invested_capital + lease_liability

        if invested_capital <= 0:
            label = (
                "invested_capital_lease_adjusted" if lease_adjusted else "invested_capital_strict"
            )
            return _denominator_le_zero_label(label, invested_capital)
        flags = (STATUTORY_TAX_RATE_FALLBACK_FLAG,) if used_fallback else ()
        return ComputedValue(value=(nopat / invested_capital) * Decimal(100), method_flags=flags)

    return _fn


def _roce(resolved: ResolvedInputs, _prior: ResolvedInputs | None) -> ComputeResult:
    ebit = resolved.get(CanonicalConcept.OPERATING_INCOME)
    total_assets = resolved.get(CanonicalConcept.TOTAL_ASSETS)
    total_current_liabilities = resolved.get(CanonicalConcept.TOTAL_CURRENT_LIABILITIES)
    if ebit is None:
        return _missing(CanonicalConcept.OPERATING_INCOME)
    if total_assets is None:
        return _missing(CanonicalConcept.TOTAL_ASSETS)
    if total_current_liabilities is None:
        return _missing(CanonicalConcept.TOTAL_CURRENT_LIABILITIES)
    capital_employed = total_assets - total_current_liabilities
    if capital_employed <= 0:
        return _denominator_le_zero_label("capital_employed", capital_employed)
    return ComputedValue(value=(ebit / capital_employed) * Decimal(100))


def compute_net_debt_strict(resolved: ResolvedInputs) -> Decimal | NotComputable:
    """total_debt - cash_and_equivalents - short_term_investments.

    May legitimately be negative (a net-cash position) -- that is a valid
    value, never guarded against.
    """
    total_debt = resolved.get(CanonicalConcept.TOTAL_DEBT)
    cash = resolved.get(CanonicalConcept.CASH_AND_EQUIVALENTS)
    short_term_investments = resolved.get(CanonicalConcept.SHORT_TERM_INVESTMENTS)
    if total_debt is None:
        return _missing(CanonicalConcept.TOTAL_DEBT)
    if cash is None:
        return _missing(CanonicalConcept.CASH_AND_EQUIVALENTS)
    if short_term_investments is None:
        return _missing(CanonicalConcept.SHORT_TERM_INVESTMENTS)
    return total_debt - cash - short_term_investments


def _net_debt_strict(resolved: ResolvedInputs, _prior: ResolvedInputs | None) -> ComputeResult:
    net_debt = compute_net_debt_strict(resolved)
    if isinstance(net_debt, NotComputable):
        return net_debt
    return ComputedValue(value=net_debt)


def _net_debt_incl_lt_securities(
    resolved: ResolvedInputs, _prior: ResolvedInputs | None
) -> ComputeResult:
    net_debt = compute_net_debt_strict(resolved)
    if isinstance(net_debt, NotComputable):
        return net_debt
    long_term_investments = resolved.get(CanonicalConcept.LONG_TERM_INVESTMENTS)
    if long_term_investments is None:
        return _missing(CanonicalConcept.LONG_TERM_INVESTMENTS)
    return ComputedValue(value=net_debt - long_term_investments)


def _net_debt_to_ebitda(resolved: ResolvedInputs, _prior: ResolvedInputs | None) -> ComputeResult:
    net_debt = compute_net_debt_strict(resolved)
    if isinstance(net_debt, NotComputable):
        return net_debt
    ebitda = compute_ebitda(resolved)
    if isinstance(ebitda, NotComputable):
        return ebitda
    if ebitda <= 0:
        return _denominator_le_zero(CanonicalConcept.EBITDA, ebitda)
    return ComputedValue(value=net_debt / ebitda)


def _interest_coverage(resolved: ResolvedInputs, _prior: ResolvedInputs | None) -> ComputeResult:
    ebit = resolved.get(CanonicalConcept.OPERATING_INCOME)
    interest_expense = resolved.get(CanonicalConcept.INTEREST_EXPENSE)
    if ebit is None:
        return _missing(CanonicalConcept.OPERATING_INCOME)
    # <= 0, not just == 0: a real-sweep check against data/portfolio.db
    # (Phase 2 validation) found financial_facts rows where
    # interest_expense is stored as a NEGATIVE value (net interest income,
    # or a sign-convention disagreement between ingestion sources for a
    # given ticker/period) -- dividing EBIT by a negative "expense" produces
    # a nonsensical negative coverage ratio that reads as "can't cover
    # interest" when the true story is "no interest burden at all", the
    # same debt-free case the positive-zero guard already existed for.
    if interest_expense is None or interest_expense <= 0:
        return NotComputable(
            reason_code=ReasonCode.MISSING_INPUT,
            reason_detail="interest_expense is 0/absent/negative (debt-free name or a sign-convention data issue)",
        )
    return ComputedValue(value=ebit / interest_expense)


def _turnover_ratio(
    numerator: CanonicalConcept, denominator_avg: CanonicalConcept
) -> Callable[[ResolvedInputs, ResolvedInputs | None], ComputeResult]:
    """numerator_ttm / denominator_avg -- io.py has already TTM-summed the
    numerator and 2-point-averaged the denominator before compute() is
    called (see io._AVERAGE_STOCK_FORMULAS)."""

    def _fn(resolved: ResolvedInputs, _prior: ResolvedInputs | None) -> ComputeResult:
        num = resolved.get(numerator)
        den = resolved.get(denominator_avg)
        if num is None:
            return _missing(numerator)
        if den is None:
            return _missing(denominator_avg)
        if den <= 0:
            return _denominator_le_zero(denominator_avg, den)
        return ComputedValue(value=num / den)

    return _fn


_CCC_DAYS_PER_YEAR = Decimal(365)


def _cash_conversion_cycle(
    resolved: ResolvedInputs, _prior: ResolvedInputs | None
) -> ComputeResult:
    revenue = resolved.get(CanonicalConcept.REVENUE)
    cost_of_revenue = resolved.get(CanonicalConcept.COST_OF_REVENUE)
    inventory_avg = resolved.get(CanonicalConcept.INVENTORY)
    receivables_avg = resolved.get(CanonicalConcept.ACCOUNTS_RECEIVABLE)
    payables_avg = resolved.get(CanonicalConcept.ACCOUNTS_PAYABLE)
    if revenue is None:
        return _missing(CanonicalConcept.REVENUE)
    if cost_of_revenue is None:
        return _missing(CanonicalConcept.COST_OF_REVENUE)
    if inventory_avg is None:
        return _missing(CanonicalConcept.INVENTORY)
    if receivables_avg is None:
        return _missing(CanonicalConcept.ACCOUNTS_RECEIVABLE)
    if payables_avg is None:
        return _missing(CanonicalConcept.ACCOUNTS_PAYABLE)
    if revenue <= 0:
        return _denominator_le_zero(CanonicalConcept.REVENUE, revenue)
    if cost_of_revenue <= 0:
        return _denominator_le_zero(CanonicalConcept.COST_OF_REVENUE, cost_of_revenue)
    dio = _CCC_DAYS_PER_YEAR * inventory_avg / cost_of_revenue
    dso = _CCC_DAYS_PER_YEAR * receivables_avg / revenue
    dpo = _CCC_DAYS_PER_YEAR * payables_avg / cost_of_revenue
    return ComputedValue(value=dio + dso - dpo)


def _eps_adjusted_ex_sbc(resolved: ResolvedInputs, _prior: ResolvedInputs | None) -> ComputeResult:
    net_income = resolved.get(CanonicalConcept.NET_INCOME)
    sbc = resolved.get(CanonicalConcept.STOCK_BASED_COMPENSATION)
    diluted_shares = resolved.get(CanonicalConcept.WEIGHTED_AVG_SHARES_DILUTED)
    if net_income is None:
        return _missing(CanonicalConcept.NET_INCOME)
    if sbc is None:
        return _missing(CanonicalConcept.STOCK_BASED_COMPENSATION)
    if diluted_shares is None:
        return _missing(CanonicalConcept.WEIGHTED_AVG_SHARES_DILUTED)
    if diluted_shares <= 0:
        return _denominator_le_zero(CanonicalConcept.WEIGHTED_AVG_SHARES_DILUTED, diluted_shares)
    return ComputedValue(
        value=(net_income + sbc) / diluted_shares, method_flags=(SBC_ADDBACK_PRETAX_FLAG,)
    )


def _per_share(
    numerator: CanonicalConcept,
) -> Callable[[ResolvedInputs, ResolvedInputs | None], ComputeResult]:
    def _fn(resolved: ResolvedInputs, _prior: ResolvedInputs | None) -> ComputeResult:
        num = resolved.get(numerator)
        diluted_shares = resolved.get(CanonicalConcept.WEIGHTED_AVG_SHARES_DILUTED)
        if num is None:
            return _missing(numerator)
        if diluted_shares is None:
            return _missing(CanonicalConcept.WEIGHTED_AVG_SHARES_DILUTED)
        if diluted_shares <= 0:
            return _denominator_le_zero(
                CanonicalConcept.WEIGHTED_AVG_SHARES_DILUTED, diluted_shares
            )
        return ComputedValue(value=num / diluted_shares)

    return _fn


def _cagr_result(label: str, current: Decimal | None, base: Decimal | None) -> ComputeResult:
    """(current / base)^(1/3) - 1, as a percent. A sign flip on either
    endpoint (<= 0) makes the percentage meaningless -- same reasoning as
    eps_diluted_yoy's prior<=0 guard -- so both endpoints must be strictly
    positive."""
    if current is None:
        return NotComputable(
            reason_code=ReasonCode.MISSING_INPUT,
            reason_detail=f"no current-period {label} resolved",
        )
    if base is None:
        return NotComputable(
            reason_code=ReasonCode.MISSING_INPUT,
            reason_detail=f"no prior-period (3 FY back) {label} resolved",
        )
    if base <= 0 or current <= 0:
        return NotComputable(
            reason_code=ReasonCode.DENOMINATOR_LE_ZERO,
            reason_detail=(
                f"{label} sign flip across the 3-year window makes CAGR meaningless "
                f"(current={current}, base={base})"
            ),
        )
    factor = (float(current) / float(base)) ** (1.0 / 3.0)
    return ComputedValue(value=(Decimal(str(factor)) - Decimal(1)) * Decimal(100))


def _revenue_cagr_3y(resolved: ResolvedInputs, prior: ResolvedInputs | None) -> ComputeResult:
    if prior is None:
        return NotComputable(
            reason_code=ReasonCode.MISSING_INPUT,
            reason_detail="no prior-period (3 FY back) revenue resolved",
        )
    return _cagr_result(
        "revenue",
        resolved.get(CanonicalConcept.REVENUE),
        prior.get(CanonicalConcept.REVENUE),
    )


def _ebitda_cagr_3y(resolved: ResolvedInputs, prior: ResolvedInputs | None) -> ComputeResult:
    if prior is None:
        return NotComputable(
            reason_code=ReasonCode.MISSING_INPUT,
            reason_detail="no prior-period (3 FY back) ebitda resolved",
        )
    current_ebitda = compute_ebitda(resolved)
    if isinstance(current_ebitda, NotComputable):
        return current_ebitda
    prior_ebitda = compute_ebitda(prior)
    if isinstance(prior_ebitda, NotComputable):
        return prior_ebitda
    return _cagr_result("ebitda", current_ebitda, prior_ebitda)


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
    # Phase 2
    "roic_strict": _roic(lease_adjusted=False),
    "roic_lease_adjusted": _roic(lease_adjusted=True),
    "roce": _roce,
    "net_debt_strict": _net_debt_strict,
    "net_debt_incl_lt_securities": _net_debt_incl_lt_securities,
    "net_debt_to_ebitda": _net_debt_to_ebitda,
    "interest_coverage": _interest_coverage,
    "asset_turnover": _turnover_ratio(CanonicalConcept.REVENUE, CanonicalConcept.TOTAL_ASSETS),
    "receivables_turnover": _turnover_ratio(
        CanonicalConcept.REVENUE, CanonicalConcept.ACCOUNTS_RECEIVABLE
    ),
    "inventory_turnover": _turnover_ratio(
        CanonicalConcept.COST_OF_REVENUE, CanonicalConcept.INVENTORY
    ),
    "cash_conversion_cycle": _cash_conversion_cycle,
    "eps_adjusted_ex_sbc": _eps_adjusted_ex_sbc,
    "bvps": _per_share(CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY),
    "fcf_per_share": _per_share(CanonicalConcept.FREE_CASH_FLOW),
    "revenue_cagr_3y": _revenue_cagr_3y,
    "ebitda_cagr_3y": _ebitda_cagr_3y,
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
