"""Pure DCF field bounds and unit-conversion safeguards.

Metric resolution and application to DCF inputs are retired. Field safety
metadata and conversion refusals remain available. WACC stays derived from
its capital-cost inputs and is absent from this field registry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from models.facts import Unit
from models.unit_convert import convert_unit

PROPORTION = "proportion"  # Decimal ratio: target Unit.RATIO.
MONEY = "money"  # Amount in millions: target Unit.MILLIONS.
RAW = "raw"  # Plain number: beta or exit multiple.


class FactDriverError(Exception):
    """A value has incompatible units or is outside the field's safety bounds."""


@dataclass(frozen=True, slots=True)
class DriverField:
    """Target unit family and inclusive safety bounds for one DCF field."""

    key: str
    label: str
    family: str
    min_value: float
    max_value: float


# WACC is derived from its CAPM inputs and has no directly writable field.
DRIVER_FIELDS: tuple[DriverField, ...] = (
    DriverField("near_op_margin", "Near-term operating margin", PROPORTION, -0.5, 0.95),
    DriverField("terminal_op_margin", "Terminal operating margin", PROPORTION, -0.5, 0.95),
    DriverField("tax_rate", "Tax rate", PROPORTION, 0.0, 0.6),
    DriverField("risk_free_rate", "Risk-free rate (10Y)", PROPORTION, 0.0, 0.2),
    DriverField("equity_risk_premium", "Equity risk premium", PROPORTION, 0.0, 0.15),
    DriverField("cost_of_debt", "Pre-tax cost of debt", PROPORTION, 0.0, 0.3),
    DriverField("beta", "Beta (levered)", RAW, 0.0, 4.0),
    DriverField("exit_multiple", "Exit multiple (turns)", RAW, 0.5, 75.0),
    DriverField("terminal_growth_g", "Terminal growth (g)", PROPORTION, -0.05, 0.1),
    DriverField("terminal_capex_da", "Terminal capex / D&A", PROPORTION, 0.0, 5.0),
    DriverField("capex_2026_m", "2026 capex ($M)", MONEY, 0.0, 2000000.0),
    DriverField(
        "segment_growth_near", "Segment growth — near-term (all segments)", PROPORTION, -0.5, 1.0
    ),
    DriverField(
        "segment_growth_term", "Segment growth — terminal (all segments)", PROPORTION, -0.1, 0.2
    ),
)

DRIVER_FIELDS_BY_KEY: dict[str, DriverField] = {f.key: f for f in DRIVER_FIELDS}


@dataclass(frozen=True, slots=True)
class ConvertedValue:
    """A fact value rescaled into a driver field's units, with a human note
    describing the scale change (for the result chip + provenance)."""

    value: float
    note: str
    source_unit: str | None
    target_unit: str | None


def _parse_unit(raw: str | None) -> Unit | None:
    """A stored unit string → :class:`Unit`, or None when absent/out-of-vocab."""
    if raw is None:
        return None
    try:
        return Unit(raw.strip().lower())
    except ValueError:
        return None


def _fmt_unit(unit: Unit) -> str:
    return {
        Unit.RATIO: "ratio",
        Unit.PERCENT: "percent",
        Unit.BPS: "bps",
        Unit.ACTUAL: "$",
        Unit.THOUSANDS: "$K",
        Unit.MILLIONS: "$M",
        Unit.BILLIONS: "$B",
        Unit.COUNT: "count",
    }.get(unit, unit.value)


def convert_to_driver(raw_value: float, raw_unit: str | None, field: DriverField) -> ConvertedValue:
    """Rescale ``raw_value`` (in ``raw_unit``) into ``field``'s target units.

    The correctness contract, in order:

    * A non-finite value is rejected (NaN/inf would poison the model).
    * PROPORTION / MONEY fields require a known, family-compatible source unit
      and rescale through :func:`~models.unit_convert.convert_unit`
      (percent 4.3 → ratio 0.043; $ → $M). An ABSENT unit is rejected, not
      guessed — the 4.3-vs-0.043 trap. A cross-family unit (e.g. a dollar level
      offered to a margin) is rejected rather than blindly rescaled.
    * RAW fields (beta, an exit multiple in turns) take the number as-is, but
      reject a percent/bps source (that fact is a rate — it belongs in a rate
      field, and using it raw would be a 100×/10000× error).
    * The converted value is bounds-checked against the field's sane range; an
      out-of-range value (which ``derive_over_under`` would silently map to
      ``None``, or which would value the model absurdly) is rejected here.

    Raises :class:`FactDriverError` with a user-facing message on any failure.
    """
    if not math.isfinite(raw_value):
        raise FactDriverError("the fact's value is not a finite number")
    src = _parse_unit(raw_unit)

    if field.family == RAW:
        if src is not None and src in (Unit.PERCENT, Unit.BPS):
            raise FactDriverError(
                f"{field.label} takes a plain number, but the fact is a "
                f"{src.value} rate — pick a rate/margin field instead"
            )
        value = raw_value
        note = "used as-is (no unit scaling)"
        target_unit: str | None = None
    else:
        target = Unit.RATIO if field.family == PROPORTION else Unit.MILLIONS
        if src is None:
            raise FactDriverError(
                "the fact has no stored unit, so it cannot be scaled safely "
                "(a percent stored as 4.3 vs 0.043 would mis-scale the model) — "
                "edit the DCF cell directly instead"
            )
        try:
            converted = convert_unit(Decimal(str(raw_value)), src, target)
        except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
            raise FactDriverError(f"cannot convert the fact value: {exc}") from exc
        if converted is None:
            wanted = "a rate / percentage" if field.family == PROPORTION else "a dollar amount"
            raise FactDriverError(
                f"{field.label} expects {wanted}, but the fact's unit is "
                f"{src.value} — incompatible units, refusing to mis-scale"
            )
        value = float(converted)
        note = f"{_fmt_unit(src)} → {_fmt_unit(target)}"
        target_unit = target.value

    if not field.min_value <= value <= field.max_value:
        raise FactDriverError(
            f"converted value {value:g} is outside the safe range for "
            f"{field.label} [{field.min_value:g}, {field.max_value:g}] — "
            "refusing to write a degenerate driver"
        )
    return ConvertedValue(
        value=value,
        note=note,
        source_unit=src.value if src is not None else None,
        target_unit=target_unit,
    )
