"""Dimensionally-valid conversion between :class:`~models.facts.Unit` values.

A KPI's value carries a unit, but the unit a value is *stored* in often differs
from the unit a downstream consumer expects: the LLM extractor picks a per-call
unit (raw dollars ``actual``) while a holdings break-rule declares its threshold
in ``millions``. Comparing or persisting the bare numbers then silently misfires
(``115_000_000 < 80`` is always False). Both the persist path
(``pipeline.kpi_persistence``) and the compare path (the break-rule evaluator)
reconcile through this single helper so the two can never drift apart — the whole
class of unit bugs collapses to "is the stored unit the canonical one?".

Two convertible families. WITHIN a family a value rescales by the ratio of
magnitudes; ACROSS families (a money magnitude vs a proportion vs a raw count)
there is no dimensionally-valid conversion, so the caller is told (``None``) to
treat the value as unconvertible rather than compare/store on mismatched
dimensions.
"""

from __future__ import annotations

from decimal import Decimal

from models.facts import Unit

# Money / count magnitudes — rescale by the ratio of their powers of ten.
_MAGNITUDE_SCALE: dict[Unit, Decimal] = {
    Unit.ACTUAL: Decimal(1),
    Unit.THOUSANDS: Decimal(1_000),
    Unit.MILLIONS: Decimal(1_000_000),
    Unit.BILLIONS: Decimal(1_000_000_000),
}
# Proportions — scale to a common "ratio" base: 1 ratio = 100 percent = 10_000 bps.
_PROPORTION_SCALE: dict[Unit, Decimal] = {
    Unit.RATIO: Decimal(1),
    Unit.PERCENT: Decimal("0.01"),
    Unit.BPS: Decimal("0.0001"),
}


def convert_unit(value: Decimal, from_unit: Unit, to_unit: Unit) -> Decimal | None:
    """Express ``value`` (currently in ``from_unit``) in ``to_unit``.

    Same unit is the identity (exact — Decimal, no float drift). A money/count
    magnitude (actual/thousands/millions/billions) rescales by the ratio of its
    powers of ten; a proportion (ratio/percent/bps) rescales through a common
    fraction base. Returns ``None`` when the two units belong to different
    families (e.g. a money magnitude vs a percentage, or anything vs a raw
    ``count``) — no dimensionally-valid conversion exists, so the caller must
    treat the value as unconvertible rather than rescale blindly.
    """
    if from_unit is to_unit:
        return value
    for scale in (_MAGNITUDE_SCALE, _PROPORTION_SCALE):
        if from_unit in scale and to_unit in scale:
            return value * scale[from_unit] / scale[to_unit]
    return None


def same_family(a: Unit, b: Unit) -> bool:
    """True when ``a`` and ``b`` admit a dimensionally-valid conversion.

    Equivalent to ``convert_unit(x, a, b) is not None`` for any ``x``: two units
    share a family when a value can be rescaled between them (money/count
    magnitudes; or proportions). Crossing a family means they measure
    dimensionally different things — a dollar LEVEL vs a percentage RATE, or
    anything vs a raw ``count`` — which is the signal a break-rule's threshold
    unit is a *comparison* unit, not the metric's own reported-value unit.
    """
    return convert_unit(Decimal(1), a, b) is not None
