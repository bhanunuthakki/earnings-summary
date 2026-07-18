"""business_model_class -> which formula_ids are in-scope for a ticker.

`excluded_business_models` on a `FormulaDef` (registry.py) is checked
directly via `FormulaDef.applies_to`. `_TICKER_EFFICIENCY_OVERRIDES` is the
finer-grained per-ticker exception list (e.g. `inventory_turnover` for a
zero-inventory business) — same "owner-maintained exception list" pattern
the repo already uses for the AMZN segment-KPI defaults in
`compute.fmp_derived_kpis._derive_segment_kpis`.

Phase 2: populated from a real, read-only query against `data/portfolio.db`
(never guessed) — every portfolio/evaluation-roster ticker's most recent
`financial_facts.inventory` value was checked. NOW, VEEV, WIX, META, GOOG,
UBER, BKNG, and BN report a consistent $0 inventory balance across quarters
(a genuine zero-inventory business, not an absent field) -- `inventory_turnover`
and its `cash_conversion_cycle` composite (which shares the same binding
leg) are excluded for exactly these tickers. RBRK and MELI carry real,
material inventory balances (RBRK: hardware appliances; MELI: marketplace
logistics) and are NOT overridden. `receivables_turnover` has NO ticker
override: every roster name checked carries accounts_receivable that is a
material fraction of revenue (no "subscription-consumer, no material AR"
case was found) — the table stays empty for that formula_key until a real
case is verified, per the same no-guessing discipline as `inputs.IFRS_FIELD_MAP`.
"""

from __future__ import annotations

from models.companies import BusinessModelClass

from .registry import FormulaDef, all_latest

_ZERO_INVENTORY_OVERRIDE: frozenset[str] = frozenset(
    {"inventory_turnover", "cash_conversion_cycle"}
)

# Per-ticker formula_key exclusions beyond the blanket
# excluded_business_models check.
_TICKER_EFFICIENCY_OVERRIDES: dict[str, frozenset[str]] = {
    "NOW": _ZERO_INVENTORY_OVERRIDE,
    "VEEV": _ZERO_INVENTORY_OVERRIDE,
    "WIX": _ZERO_INVENTORY_OVERRIDE,
    "META": _ZERO_INVENTORY_OVERRIDE,
    "GOOG": _ZERO_INVENTORY_OVERRIDE,
    "UBER": _ZERO_INVENTORY_OVERRIDE,
    "BKNG": _ZERO_INVENTORY_OVERRIDE,
    "BN": _ZERO_INVENTORY_OVERRIDE,
}


def applicable_formulas(
    business_model: BusinessModelClass,
    *,
    ticker: str | None = None,
) -> tuple[FormulaDef, ...]:
    """Return every latest-version FormulaDef in scope for `business_model`.

    `ticker` (optional) additionally applies `_TICKER_EFFICIENCY_OVERRIDES` —
    a formula_key listed for that ticker is excluded even when the blanket
    business-model check would otherwise include it.
    """
    ticker_excludes: frozenset[str] = (
        _TICKER_EFFICIENCY_OVERRIDES.get(ticker.upper(), frozenset()) if ticker else frozenset()
    )
    return tuple(
        f
        for f in all_latest()
        if f.applies_to(business_model) and f.formula_key not in ticker_excludes
    )
