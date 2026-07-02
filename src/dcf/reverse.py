"""Reverse-DCF — solve for the market-implied assumption set ("what's priced in").

The forward DCF (``dcf.redesign.value``) maps an assumption set to a fair value
per share. This module inverts it: given today's price and the analyst's own
model structure, it solves for the single assumption the market would need for
the DCF to equal the price — holding everything else at the analyst's base case.
That turns a bare over/under number into a decision-relevant question: *is the
growth (or exit multiple) the market is pricing one I'd actually underwrite?*

Two inversions per FCFF name — the two levers the market moves most:

* **Implied 5-year revenue CAGR** — the uniform near-term growth shift (applied to
  every segment, the same mechanic as a scenario growth delta) that reprices the
  model to market, *at base margins and the base terminal*. Reported as the
  resulting 5y revenue CAGR next to the analyst's base-case CAGR.
* **Implied terminal lever** — the exit multiple (exit-multiple method) or the
  terminal growth ``g`` (perpetuity method) that reprices to market *at base
  growth and margins*.

Both are one-dimensional root finds over the pure ``value()`` oracle, which is
monotone in each lever (more growth / higher multiple / higher terminal g → higher
value), so a bracketed bisection converges. When the price is unreachable inside
the model's sane bounds — the market implies growth below the model floor, or a
multiple beyond the ceiling — the inversion honestly returns ``None`` with a
directional note rather than a clamped, misleading number (the same honest-``None``
contract as ``dcf.scenario_reward``).

This module is the single producer of the ``priced_in`` block that
``execution/refresh_dcf`` persists into ``dcf_runs.assumption_snapshot_json``; the
valuation card reads it back (never recomputing on render). It is pure (stdlib +
the pure redesign engine) and touches only the *public* redesign API
(``apply_scenario`` / ``value`` / ``RedesignValuation.forecast_revenue_m``), so a
change to the private projection can never silently desync it.

Bespoke archetypes (bank / holdco / fintech / platform) do not route through
``redesign.value`` and have no honest single-lever inversion here — the refresher
simply persists no ``priced_in`` block for them and the card shows an explicit
n/a. Only the redesigned FCFF workbook is invertible by this module.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from dcf.redesign import (
    PERPETUITY,
    RedesignError,
    RedesignInputs,
    ScenarioDeltas,
    apply_scenario,
    value,
)

# Bisection bounds for each lever — deliberately wide (the point is to reveal an
# implied assumption even when it is aggressive), but finite so an unreachable
# price returns an honest n/a instead of running away. Growth is a DELTA on every
# segment's near-term rate (matching the scenario mechanic); exit multiple and
# terminal g are absolute driver values (mirroring dcf.fact_drivers bounds).
_GROWTH_DELTA_LO = -0.60  # shift every segment's near-term growth down 60 pts
_GROWTH_DELTA_HI = 1.00  # ...or up 100 pts
_EXIT_MULT_LO = 0.5
_EXIT_MULT_HI = 75.0
_TERM_G_LO = -0.05
_TERM_G_HI = 0.10  # further clamped below WACC per name (perpetuity requires g < WACC)

_CAGR_YEARS = 5  # "5-year revenue CAGR" — the decision-relevant horizon
_MAX_ITER = 80  # bisection depth; 2**-80 is far past float precision
_REL_TOL = 1e-6  # |value - price| <= _REL_TOL * price ends the search early


@dataclass(frozen=True, slots=True)
class Inversion:
    """One reverse-DCF lever: the value the market price implies vs the analyst's
    base case for the same lever.

    ``implied_value`` is ``None`` when the price is unreachable within the lever's
    sane bounds; ``note`` then carries the direction ("market implies growth below
    the model floor"). ``unit`` is ``"pct"`` (a rate, e.g. a CAGR or terminal g)
    or ``"turns"`` (an EV multiple), so the renderer formats without re-deciding.
    """

    lever: str  # "revenue_cagr_5y" | "exit_multiple" | "terminal_growth_g"
    label: str
    unit: str  # "pct" | "turns"
    base_value: float
    implied_value: float | None
    note: str = ""

    @property
    def solved(self) -> bool:
        return self.implied_value is not None

    @property
    def gap(self) -> float | None:
        """implied − base (same unit). Positive = the market prices a higher
        value of this lever than the analyst's base case. ``None`` when unsolved."""
        return None if self.implied_value is None else self.implied_value - self.base_value


@dataclass(frozen=True, slots=True)
class PricedIn:
    """What today's price implies about the analyst's own FCFF model, per lever.

    ``base_value_per_share`` / ``price`` anchor the gap the inversions explain;
    ``growth`` and ``terminal`` each answer "what single assumption would the
    market need for the DCF to equal the price". ``terminal.lever`` distinguishes
    the exit-multiple vs perpetuity terminal so a consumer never mis-labels it.
    """

    price: float
    base_value_per_share: float
    growth: Inversion
    terminal: Inversion

    @property
    def any_solved(self) -> bool:
        return self.growth.solved or self.terminal.solved


def _bisect(
    f: Callable[[float], float | None], target: float, lo: float, hi: float
) -> float | None:
    """Root of ``f(x) == target`` on ``[lo, hi]`` for a monotone ``f``.

    Returns ``None`` when the target is not bracketed (``f`` never reaches it
    inside the bounds — the honest "unreachable" signal) or when ``f`` cannot be
    evaluated at a bound. Handles both increasing and decreasing ``f`` by reading
    the sign at the endpoints; ``value()`` is increasing in every lever here, but
    the bracket check is what actually gates correctness, not the assumed slope.
    """
    flo = f(lo)
    fhi = f(hi)
    if flo is None or fhi is None:
        return None
    # Target must sit between the endpoint values (inclusive), else it is
    # unreachable inside the bounds — return None rather than a clamped bound.
    if not (min(flo, fhi) <= target <= max(flo, fhi)):
        return None
    increasing = fhi >= flo
    a, b = lo, hi
    for _ in range(_MAX_ITER):
        mid = 0.5 * (a + b)
        fmid = f(mid)
        if fmid is None:
            return None
        if abs(fmid - target) <= _REL_TOL * max(abs(target), 1.0):
            return mid
        below = fmid < target
        # Move the bound that keeps the target bracketed.
        if below == increasing:
            a = mid
        else:
            b = mid
    return 0.5 * (a + b)


def _repriced_by_growth(inp: RedesignInputs, delta: float) -> float | None:
    """Value per share (USD) with every segment's near-term growth shifted by
    ``delta`` — the scenario growth mechanic, reused so the inversion moves the
    model exactly the way a Bull/Bear growth delta does. ``None`` if the shifted
    inputs are un-valuable (never under an exit-multiple terminal, but guarded)."""
    try:
        return value(apply_scenario(inp, ScenarioDeltas(growth_near=delta))).value_per_share_usd
    except RedesignError:
        return None


def _cagr_5y(inp: RedesignInputs, delta: float) -> float | None:
    """The projected 5-year revenue CAGR with near-term growth shifted by ``delta``.

    Reads the revenue path off the public ``RedesignValuation.forecast_revenue_m``
    (year-1..N), so it never touches the private projection. ``None`` if the base
    revenue or the 5-year point is non-positive (a shrinking-to-zero path has no
    real CAGR)."""
    base_total = sum(inp.base_revenue_by_segment.values())
    if base_total <= 0:
        return None
    try:
        rev = value(apply_scenario(inp, ScenarioDeltas(growth_near=delta))).forecast_revenue_m
    except RedesignError:
        return None
    if len(rev) < _CAGR_YEARS:
        return None
    rev_5 = rev[_CAGR_YEARS - 1]  # year-5 revenue (forecast_revenue_m[0] is year 1)
    if rev_5 <= 0:
        return None
    return (rev_5 / base_total) ** (1.0 / _CAGR_YEARS) - 1.0


def _solve_growth(inp: RedesignInputs, price: float) -> Inversion:
    """Implied 5y revenue CAGR: the near-term growth shift that reprices to
    market, at base margins and terminal, expressed as the resulting CAGR."""
    base_cagr = _cagr_5y(inp, 0.0)
    base = base_cagr if base_cagr is not None else 0.0
    delta = _bisect(
        lambda d: _repriced_by_growth(inp, d), price, _GROWTH_DELTA_LO, _GROWTH_DELTA_HI
    )
    if delta is None:
        # Direction: at the low bound the model is already worth >= price ⇒ the
        # market prices even less growth than the floor; else more than the ceiling.
        floor_val = _repriced_by_growth(inp, _GROWTH_DELTA_LO)
        note = (
            "market implies growth below the model floor"
            if floor_val is not None and floor_val >= price
            else "market implies growth beyond the model ceiling"
        )
        return Inversion("revenue_cagr_5y", "Implied 5y revenue CAGR", "pct", base, None, note)
    implied = _cagr_5y(inp, delta)
    if implied is None:
        return Inversion(
            "revenue_cagr_5y",
            "Implied 5y revenue CAGR",
            "pct",
            base,
            None,
            "implied growth path has no real CAGR",
        )
    return Inversion("revenue_cagr_5y", "Implied 5y revenue CAGR", "pct", base, implied)


def _solve_terminal(inp: RedesignInputs, price: float) -> Inversion:
    """Implied terminal lever: the exit multiple (exit-multiple method) or the
    terminal growth g (perpetuity method) that reprices to market, at base growth
    and margins."""
    if inp.terminal_method == PERPETUITY:
        hi = min(_TERM_G_HI, inp.wacc - 0.0025)
        if hi <= _TERM_G_LO:  # degenerate WACC — no room to solve g
            return Inversion(
                "terminal_growth_g",
                "Implied terminal growth (g)",
                "pct",
                inp.terminal_growth_g,
                None,
                "WACC too low to invert terminal g",
            )

        def price_at_g(g: float) -> float | None:
            try:
                return value(replace(inp, terminal_growth_g=g)).value_per_share_usd
            except RedesignError:
                return None

        implied = _bisect(price_at_g, price, _TERM_G_LO, hi)
        note = "" if implied is not None else "market implies terminal g outside model bounds"
        return Inversion(
            "terminal_growth_g",
            "Implied terminal growth (g)",
            "pct",
            inp.terminal_growth_g,
            implied,
            note,
        )

    # Exit-multiple method (the default): invert the multiple directly.
    def price_at_mult(m: float) -> float | None:
        try:
            return value(replace(inp, exit_multiple=m)).value_per_share_usd
        except RedesignError:
            return None

    implied = _bisect(price_at_mult, price, _EXIT_MULT_LO, _EXIT_MULT_HI)
    note = "" if implied is not None else "market implies an exit multiple outside model bounds"
    label = f"Implied exit multiple ({inp.terminal_basis})"
    return Inversion("exit_multiple", label, "turns", inp.exit_multiple, implied, note)


def solve_priced_in(inp: RedesignInputs, *, price: float | None = None) -> PricedIn | None:
    """Solve the market-implied assumption set for a redesigned FCFF name.

    ``price`` defaults to the workbook's current price (``inp.current_price``).
    Returns ``None`` when there is no usable price or the base model is
    un-valuable — the same honest-``None`` fallback the reward path uses, so a
    caller can persist "no priced-in block" without special-casing. Otherwise it
    returns both inversions; either may individually be unsolved (``None`` implied
    value) when the price is unreachable for that lever.
    """
    px = price if price is not None else inp.current_price
    if px <= 0:
        return None
    try:
        base_val = value(inp).value_per_share_usd
    except RedesignError:
        return None
    if base_val <= 0:
        return None
    return PricedIn(
        price=px,
        base_value_per_share=base_val,
        growth=_solve_growth(inp, px),
        terminal=_solve_terminal(inp, px),
    )
