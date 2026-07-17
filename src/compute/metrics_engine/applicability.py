"""business_model_class -> which formula_ids are in-scope for a ticker.

`excluded_business_models` on a `FormulaDef` (registry.py) is checked
directly via `FormulaDef.applies_to`. `_TICKER_EFFICIENCY_OVERRIDES` is the
finer-grained per-ticker exception list (e.g. `receivables_turnover` for a
subscription-consumer business) — same "owner-maintained exception list"
pattern the repo already uses for the AMZN segment-KPI defaults in
`compute.fmp_derived_kpis._derive_segment_kpis`. Empty in Phase 1: none of
the ~15 Phase-1 formulas need a per-ticker override (the turnover metrics
this table exists for are Phase 2 scope).
"""

from __future__ import annotations

from models.companies import BusinessModelClass

from .registry import FormulaDef, all_latest

# Per-ticker formula_key exclusions beyond the blanket
# excluded_business_models check — populated in Phase 2 (turnovers,
# inventory-less business models). Kept here now so Phase 2 is a data-only
# change, not a structural one.
_TICKER_EFFICIENCY_OVERRIDES: dict[str, frozenset[str]] = {}


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
        _TICKER_EFFICIENCY_OVERRIDES.get(ticker.upper(), frozenset())
        if ticker
        else frozenset()
    )
    return tuple(
        f
        for f in all_latest()
        if f.applies_to(business_model) and f.formula_key not in ticker_excludes
    )
