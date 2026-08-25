"""Archetype-neutral reverse valuation persistence primitives.

The FCFF ``dcf.reverse`` module remains intentionally tied to
``RedesignInputs`` and its two conventional levers.  Bespoke models need the
same honest market-implied analysis without pretending that a holdco NAV or a
platform FCFE model has FCFF revenue/terminal semantics.  This module owns the
small, JSON-safe common shape and a monotonic one-dimensional solver.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import isfinite

_MAX_ITERATIONS = 80
_RELATIVE_TOLERANCE = 1e-6
SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SolveResult:
    """Result of an attempted monotonic market-price inversion."""

    implied_value: float | None
    status: str  # solved | unreachable | unavailable
    note: str = ""


@dataclass(frozen=True, slots=True)
class ReverseLever:
    """One archetype-specific market-implied valuation lever."""

    lever_id: str
    label: str
    unit: str
    base_value: float
    implied_value: float | None
    method: str  # monotonic_bisection | bridge_residual
    status: str  # solved | unreachable | unavailable
    note: str = ""

    def to_snapshot_dict(self) -> dict[str, object]:
        return {
            "id": self.lever_id,
            "label": self.label,
            "unit": self.unit,
            "base_value": self.base_value,
            "implied_value": self.implied_value,
            "method": self.method,
            "status": self.status,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ReverseValuation:
    """Persistable reverse valuation for an explicitly named model archetype."""

    archetype: str
    price: float
    base_value_per_share_usd: float
    valuation_scope: str
    levers: Sequence[ReverseLever]

    def to_snapshot_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "archetype": self.archetype,
            "price": self.price,
            "base_value_per_share_usd": self.base_value_per_share_usd,
            "valuation_scope": self.valuation_scope,
            "levers": [lever.to_snapshot_dict() for lever in self.levers],
        }


def _evaluate(value_at: Callable[[float], float | None], argument: float) -> float | None:
    try:
        value = value_at(argument)
    except (ArithmeticError, ValueError):
        return None
    if value is None or not isfinite(value):
        return None
    return value


def solve_monotonic(
    value_at: Callable[[float], float | None],
    target_value: float,
    lower_bound: float,
    upper_bound: float,
) -> SolveResult:
    """Solve ``value_at(x) == target_value`` only when the target is bracketed.

    The function may be increasing or decreasing.  A missing/invalid endpoint
    is an unavailable calculation, while a valid but unbracketed target is an
    honest unreachable result—not a clamped valuation assumption.
    """
    if not all(isfinite(value) for value in (target_value, lower_bound, upper_bound)):
        return SolveResult(None, "unavailable", "non-finite target or solver bound")
    if lower_bound >= upper_bound:
        return SolveResult(None, "unavailable", "invalid solver bounds")
    lower_value = _evaluate(value_at, lower_bound)
    upper_value = _evaluate(value_at, upper_bound)
    if lower_value is None or upper_value is None:
        return SolveResult(None, "unavailable", "model could not value a solver bound")
    if not min(lower_value, upper_value) <= target_value <= max(lower_value, upper_value):
        return SolveResult(
            None, "unreachable", "market price is outside the configured solver bounds"
        )

    increasing = upper_value >= lower_value
    lower, upper = lower_bound, upper_bound
    for _ in range(_MAX_ITERATIONS):
        midpoint = (lower + upper) / 2.0
        midpoint_value = _evaluate(value_at, midpoint)
        if midpoint_value is None:
            return SolveResult(
                None, "unavailable", "model could not value an interior solver point"
            )
        if abs(midpoint_value - target_value) <= _RELATIVE_TOLERANCE * max(abs(target_value), 1.0):
            return SolveResult(midpoint, "solved")
        is_below_target = midpoint_value < target_value
        if is_below_target == increasing:
            lower = midpoint
        else:
            upper = midpoint
    return SolveResult((lower + upper) / 2.0, "solved", "maximum solver iterations reached")


def solve_lever(
    *,
    lever_id: str,
    label: str,
    unit: str,
    base_value: float,
    method: str,
    price_at: Callable[[float], float | None],
    target_price: float,
    lower_bound: float,
    upper_bound: float,
) -> ReverseLever:
    """Return one persisted lever from an honest monotonic inversion."""
    result = solve_monotonic(price_at, target_price, lower_bound, upper_bound)
    return ReverseLever(
        lever_id=lever_id,
        label=label,
        unit=unit,
        base_value=base_value,
        implied_value=result.implied_value,
        method=method,
        status=result.status,
        note=result.note,
    )


def residual_lever(
    *,
    lever_id: str,
    label: str,
    unit: str,
    base_value: float,
    implied_value: float | None,
    note: str = "",
) -> ReverseLever:
    """Persist an exact equity-bridge residual without inventing a DCF root."""
    status = "solved" if implied_value is not None and isfinite(implied_value) else "unavailable"
    return ReverseLever(
        lever_id=lever_id,
        label=label,
        unit=unit,
        base_value=base_value,
        implied_value=implied_value if status == "solved" else None,
        method="bridge_residual",
        status=status,
        note=note if status == "solved" else (note or "market price is unavailable"),
    )
