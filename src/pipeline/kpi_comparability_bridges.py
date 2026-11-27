"""Typed comparability bridge specs, overlap witnesses, and cadence/diff guards.

This module defines the deterministic, provenance-backed contracts for the five
common issuer KPI comparability break patterns:
1. Pure label rename verified by comparative prior-period overlap.
2. Stock split / share-count rescale via an exact rational factor.
3. Additive segment combine or split across explicit member revisions.
4. Divestiture / M&A / spin-off continuing-ops recast (comparative-only).
5. Cumulative vs standalone reporting-cadence separation and deterministic diff.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from models.facts import Currency, FiscalPeriodType, Unit

_DEFAULT_MAX_RELATIVE_DELTA = Decimal("0.005")
_WITNESS_RELATIVE_EPSILON = Decimal("1e-9")


class OverlapVerificationError(ValueError):
    """Raised when a comparative overlap fails tolerance or precondition checks."""


def _validate_iso_date(value: str, field_name: str) -> str:
    text = value.strip()
    if len(text) != 10:
        raise ValueError(f"{field_name} must be an ISO date YYYY-MM-DD")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO date YYYY-MM-DD") from exc
    return text


def _require_finite_decimal(value: Decimal, field_name: str) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")
    return value


class OverlapTolerancePolicy(BaseModel):
    """Explicit Decimal tolerance governing comparative overlap verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_relative_delta: Decimal = Field(
        default=_DEFAULT_MAX_RELATIVE_DELTA,
        ge=Decimal("0"),
        le=Decimal("1"),
    )
    rounding_unit: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))

    @field_validator("max_relative_delta", "rounding_unit")
    @classmethod
    def _finite(cls, value: Decimal, info: object) -> Decimal:
        field_name = str(getattr(info, "field_name", "tolerance"))
        return _require_finite_decimal(value, field_name)


class OverlapWitness(BaseModel):
    """Provenance-backed witness proving two reported values agree within tolerance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_end: str = Field(min_length=10, max_length=10)
    fiscal_period_type: FiscalPeriodType | None = None
    predecessor_value: Decimal
    successor_value: Decimal
    expected_value: Decimal | None = None
    relative_delta: Decimal = Field(ge=Decimal("0"))
    rounding_tolerance: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    max_relative_delta: Decimal = Field(
        default=_DEFAULT_MAX_RELATIVE_DELTA,
        ge=Decimal("0"),
        le=Decimal("1"),
    )
    source_locator: dict[str, object] = Field(min_length=1)

    @field_validator("period_end")
    @classmethod
    def _period_end_iso(cls, value: str) -> str:
        return _validate_iso_date(value, "period_end")

    @field_validator(
        "predecessor_value",
        "successor_value",
        "relative_delta",
        "rounding_tolerance",
        "max_relative_delta",
    )
    @classmethod
    def _finite_required(cls, value: Decimal, info: object) -> Decimal:
        field_name = str(getattr(info, "field_name", "witness value"))
        return _require_finite_decimal(value, field_name)

    @field_validator("expected_value")
    @classmethod
    def _finite_optional(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        return _require_finite_decimal(value, "expected_value")

    @model_validator(mode="after")
    def _witness_satisfies_tolerance(self) -> Self:
        target = self.predecessor_value if self.expected_value is None else self.expected_value
        abs_delta = abs(self.successor_value - target)
        if target == Decimal("0"):
            if abs_delta == Decimal("0"):
                if self.relative_delta != Decimal("0"):
                    raise ValueError("zero delta requires relative_delta == 0")
                return self
            if self.rounding_tolerance > Decimal("0") and abs_delta <= self.rounding_tolerance:
                return self
            raise ValueError("overlap witness exceeds zero-baseline rounding tolerance")
        computed_relative = abs_delta / abs(target)
        if abs(computed_relative - self.relative_delta) > _WITNESS_RELATIVE_EPSILON:
            raise ValueError("overlap witness relative_delta does not match reported values")
        within_relative = computed_relative <= self.max_relative_delta
        within_rounding = (
            self.rounding_tolerance > Decimal("0") and abs_delta <= self.rounding_tolerance
        )
        if not (within_relative or within_rounding):
            raise ValueError("overlap witness disagrees beyond configured tolerance")
        return self


def evaluate_overlap_witness(
    *,
    period_end: str | date,
    predecessor_value: Decimal,
    successor_value: Decimal,
    source_locator: dict[str, object],
    expected_value: Decimal | None = None,
    fiscal_period_type: FiscalPeriodType | None = None,
    policy: OverlapTolerancePolicy | None = None,
) -> tuple[bool, OverlapWitness | None, str | None]:
    """Deterministically check whether comparative values agree within policy tolerance."""
    active_policy = policy or OverlapTolerancePolicy()
    iso_period = period_end.isoformat() if isinstance(period_end, date) else str(period_end)[:10]
    _validate_iso_date(iso_period, "period_end")
    _require_finite_decimal(predecessor_value, "predecessor_value")
    _require_finite_decimal(successor_value, "successor_value")
    target = predecessor_value if expected_value is None else expected_value
    _require_finite_decimal(target, "expected_value")
    if not source_locator:
        return False, None, "overlap witness requires a non-empty source_locator"

    abs_delta = abs(successor_value - target)
    if target == Decimal("0"):
        if abs_delta == Decimal("0") or (
            active_policy.rounding_unit > Decimal("0") and abs_delta <= active_policy.rounding_unit
        ):
            rel_delta = Decimal("0")
        else:
            return (
                False,
                None,
                f"overlap mismatch at {iso_period}: expected {target}, observed {successor_value} "
                f"(abs_delta={abs_delta} > rounding_unit={active_policy.rounding_unit})",
            )
    else:
        rel_delta = abs_delta / abs(target)
        within_relative = rel_delta <= active_policy.max_relative_delta
        within_rounding = (
            active_policy.rounding_unit > Decimal("0") and abs_delta <= active_policy.rounding_unit
        )
        if not (within_relative or within_rounding):
            return (
                False,
                None,
                f"overlap mismatch at {iso_period}: expected {target}, observed {successor_value} "
                f"(rel_delta={rel_delta:.6f} > {active_policy.max_relative_delta}, "
                f"abs_delta={abs_delta} > {active_policy.rounding_unit})",
            )

    witness = OverlapWitness(
        period_end=iso_period,
        fiscal_period_type=fiscal_period_type,
        predecessor_value=predecessor_value,
        successor_value=successor_value,
        expected_value=expected_value,
        relative_delta=rel_delta,
        rounding_tolerance=active_policy.rounding_unit,
        max_relative_delta=active_policy.max_relative_delta,
        source_locator=dict(source_locator),
    )
    return True, witness, None


def build_overlap_witness(
    *,
    period_end: str | date,
    predecessor_value: Decimal,
    successor_value: Decimal,
    source_locator: dict[str, object],
    expected_value: Decimal | None = None,
    fiscal_period_type: FiscalPeriodType | None = None,
    policy: OverlapTolerancePolicy | None = None,
) -> OverlapWitness:
    """Build a verified ``OverlapWitness`` or raise ``OverlapVerificationError``."""
    ok, witness, reason = evaluate_overlap_witness(
        period_end=period_end,
        predecessor_value=predecessor_value,
        successor_value=successor_value,
        source_locator=source_locator,
        expected_value=expected_value,
        fiscal_period_type=fiscal_period_type,
        policy=policy,
    )
    if not ok or witness is None:
        raise OverlapVerificationError(reason or "overlap verification failed")
    return witness


class RenameOverlapBridge(BaseModel):
    """Overlap witness proving a pure label rename is numerically continuous."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["rename_overlap"] = "rename_overlap"
    overlap_witness: OverlapWitness
    additional_witnesses: tuple[OverlapWitness, ...] = ()

    @model_validator(mode="after")
    def _rename_expected_matches_predecessor(self) -> Self:
        for witness in (self.overlap_witness, *self.additional_witnesses):
            if (
                witness.expected_value is not None
                and witness.expected_value != witness.predecessor_value
            ):
                raise ValueError("rename overlap witness cannot apply a scale transform")
        return self


SplitScaleAppliesTo = Literal["per_share_divide", "share_count_multiply"]


class SplitScaleBridge(BaseModel):
    """Exact rational stock-split or share-count rescale bridge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["scale_ratio"] = "scale_ratio"
    scale_numerator: int = Field(gt=0)
    scale_denominator: int = Field(default=1, gt=0)
    applies_to: SplitScaleAppliesTo
    effective_date: str = Field(min_length=10, max_length=10)
    overlap_witness: OverlapWitness

    @field_validator("effective_date")
    @classmethod
    def _effective_date_iso(cls, value: str) -> str:
        return _validate_iso_date(value, "effective_date")

    def apply_to_predecessor(self, value: Decimal) -> Decimal:
        """Transform a pre-split predecessor value into the post-split basis."""
        _require_finite_decimal(value, "value")
        num = Decimal(self.scale_numerator)
        den = Decimal(self.scale_denominator)
        if self.applies_to == "per_share_divide":
            return (value * den) / num
        return (value * num) / den

    @property
    def scale_factor(self) -> Decimal:
        """Multiplicative factor converting predecessor values to successor basis."""
        num = Decimal(self.scale_numerator)
        den = Decimal(self.scale_denominator)
        if self.applies_to == "per_share_divide":
            return den / num
        return num / den

    @model_validator(mode="after")
    def _verify_split_witness(self) -> Self:
        if self.scale_numerator == self.scale_denominator:
            raise ValueError("split scale ratio must differ from 1:1")
        expected = self.apply_to_predecessor(self.overlap_witness.predecessor_value)
        if self.overlap_witness.expected_value is None:
            raise ValueError("split scale overlap_witness requires expected_value")
        if abs(self.overlap_witness.expected_value - expected) > _WITNESS_RELATIVE_EPSILON * max(
            Decimal("1"), abs(expected)
        ):
            raise ValueError(
                "split scale overlap_witness expected_value does not match rational scale factor"
            )
        return self


AdditiveBridgeDirection = Literal["combine", "split"]


class AdditiveCompositeBridge(BaseModel):
    """Verified additive segment combine (many -> one) or split (one -> many)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["sum_of_revisions"] = "sum_of_revisions"
    direction: AdditiveBridgeDirection
    composite_definition_revision_id: str = Field(min_length=1, max_length=128)
    member_definition_revision_ids: tuple[str, ...] = Field(min_length=2)
    component_values: dict[str, Decimal] = Field(min_length=2)
    overlap_witness: OverlapWitness

    @model_validator(mode="after")
    def _verify_additive_contract(self) -> Self:
        if len(set(self.member_definition_revision_ids)) != len(
            self.member_definition_revision_ids
        ):
            raise ValueError("member_definition_revision_ids must be unique")
        if any(not rev_id.strip() for rev_id in self.member_definition_revision_ids):
            raise ValueError("member_definition_revision_ids must be non-empty")
        if self.composite_definition_revision_id in self.member_definition_revision_ids:
            raise ValueError(
                "composite_definition_revision_id cannot appear in member_definition_revision_ids"
            )
        if set(self.component_values.keys()) != set(self.member_definition_revision_ids):
            raise ValueError("component_values keys must match member_definition_revision_ids")
        for member_id, amount in self.component_values.items():
            _require_finite_decimal(amount, f"component_values[{member_id}]")
        member_sum = sum(self.component_values.values(), Decimal("0"))
        if self.direction == "combine":
            if self.overlap_witness.predecessor_value != member_sum:
                raise ValueError(
                    "combine overlap_witness predecessor_value must equal sum(component_values)"
                )
        else:
            if self.overlap_witness.successor_value != member_sum:
                raise ValueError(
                    "split overlap_witness successor_value must equal sum(component_values)"
                )
        return self


class RecastComparativeBridge(BaseModel):
    """Scope-change recast where only management-recast comparatives are comparable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["recast_comparative_only"] = "recast_comparative_only"
    recast_reason: str = Field(min_length=1, max_length=256)
    scope_change_summary: str = Field(min_length=1, max_length=1024)
    comparative_periods_present: tuple[str, ...] = ()

    @field_validator("recast_reason", "scope_change_summary")
    @classmethod
    def _nonblank_text(cls, value: str, info: object) -> str:
        if not value.strip():
            field_name = str(getattr(info, "field_name", "text"))
            raise ValueError(f"{field_name} must be non-blank")
        return value.strip()

    @field_validator("comparative_periods_present")
    @classmethod
    def _periods_iso(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_validate_iso_date(v, "comparative_periods_present") for v in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("comparative_periods_present must not contain duplicates")
        return normalized


KpiComparabilityBridgeSpec = Annotated[
    RenameOverlapBridge | SplitScaleBridge | AdditiveCompositeBridge | RecastComparativeBridge,
    Field(discriminator="kind"),
]

_BRIDGE_SPEC_ADAPTER: TypeAdapter[KpiComparabilityBridgeSpec] = TypeAdapter(
    KpiComparabilityBridgeSpec
)


def parse_bridge_spec(payload: object) -> KpiComparabilityBridgeSpec:
    """Validate a raw JSON object into a typed comparability bridge spec."""
    return _BRIDGE_SPEC_ADAPTER.validate_python(payload)


def parse_bridge_spec_json(raw_json: str) -> KpiComparabilityBridgeSpec:
    """Validate a raw JSON string into a typed comparability bridge spec."""
    return _BRIDGE_SPEC_ADAPTER.validate_json(raw_json)


class KpiPeriodCadenceKind(StrEnum):
    """Strict cadence bucket preventing cumulative vs standalone mixing."""

    STANDALONE_QUARTER = "standalone_quarter"
    CUMULATIVE_HALF = "cumulative_half"
    CUMULATIVE_NINE_MONTHS = "cumulative_nine_months"
    ANNUAL = "annual"
    TRAILING_TWELVE_MONTHS = "ttm"
    UNKNOWN = "unknown"


_CADENCE_BY_FISCAL_PERIOD: dict[str, KpiPeriodCadenceKind] = {
    "Q1": KpiPeriodCadenceKind.STANDALONE_QUARTER,
    "Q2": KpiPeriodCadenceKind.STANDALONE_QUARTER,
    "Q3": KpiPeriodCadenceKind.STANDALONE_QUARTER,
    "Q4": KpiPeriodCadenceKind.STANDALONE_QUARTER,
    "3M": KpiPeriodCadenceKind.STANDALONE_QUARTER,
    "H1": KpiPeriodCadenceKind.CUMULATIVE_HALF,
    "H2": KpiPeriodCadenceKind.CUMULATIVE_HALF,
    "6M": KpiPeriodCadenceKind.CUMULATIVE_HALF,
    "9M": KpiPeriodCadenceKind.CUMULATIVE_NINE_MONTHS,
    "YTD": KpiPeriodCadenceKind.CUMULATIVE_NINE_MONTHS,
    "FY": KpiPeriodCadenceKind.ANNUAL,
    "ANNUAL": KpiPeriodCadenceKind.ANNUAL,
    "TTM": KpiPeriodCadenceKind.TRAILING_TWELVE_MONTHS,
}


def classify_period_cadence(
    fiscal_period_type: FiscalPeriodType | str | None,
) -> KpiPeriodCadenceKind:
    """Classify a reported fiscal period token into its strict cadence bucket."""
    if fiscal_period_type is None:
        return KpiPeriodCadenceKind.UNKNOWN
    raw = (
        fiscal_period_type.value
        if isinstance(fiscal_period_type, FiscalPeriodType)
        else str(fiscal_period_type)
    )
    key = raw.strip().upper()
    return _CADENCE_BY_FISCAL_PERIOD.get(key, KpiPeriodCadenceKind.UNKNOWN)


class CumulativeDiffInput(BaseModel):
    """One admitted reported observation supplied to deterministic cumulative diff."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: int = Field(gt=0)
    ticker: str = Field(min_length=1, max_length=32)
    kpi_definition_id: int = Field(gt=0)
    kpi_definition_revision_id: str = Field(min_length=1, max_length=128)
    period_end: str = Field(min_length=10, max_length=10)
    fiscal_period_type: FiscalPeriodType
    value: Decimal
    unit: Unit
    currency: Currency | None = None
    accounting_basis: str = Field(min_length=1)
    consolidation_scope: str = Field(min_length=1)
    dimensions: dict[str, str] = Field(default_factory=dict)
    unit_scale: str = Field(min_length=1)
    stock_flow_behavior: str = Field(default="flow", min_length=1)
    source_doc_id: int | None = None

    @field_validator("period_end")
    @classmethod
    def _period_iso(cls, value: str) -> str:
        return _validate_iso_date(value[:10], "period_end")

    @field_validator("value")
    @classmethod
    def _finite_value(cls, value: Decimal) -> Decimal:
        return _require_finite_decimal(value, "value")


class DerivedCumulativeDiffObservation(BaseModel):
    """Derived standalone observation computed from two same-revision reported facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    derivation_kind: Literal["cumulative_diff"] = "cumulative_diff"
    ticker: str = Field(min_length=1, max_length=32)
    kpi_definition_id: int = Field(gt=0)
    kpi_definition_revision_id: str = Field(min_length=1, max_length=128)
    period_end: str = Field(min_length=10, max_length=10)
    fiscal_period_type: FiscalPeriodType
    cadence_kind: KpiPeriodCadenceKind
    value: Decimal
    unit: Unit
    currency: Currency | None = None
    accounting_basis: str = Field(min_length=1)
    consolidation_scope: str = Field(min_length=1)
    dimensions: dict[str, str] = Field(default_factory=dict)
    unit_scale: str = Field(min_length=1)
    formula: str = Field(min_length=1)
    minuend_fact_id: int = Field(gt=0)
    subtrahend_fact_id: int = Field(gt=0)
    input_fact_ids: tuple[int, int]
    minuend_period_end: str = Field(min_length=10, max_length=10)
    subtrahend_period_end: str = Field(min_length=10, max_length=10)
    minuend_source_doc_id: int | None = None
    subtrahend_source_doc_id: int | None = None


_ALLOWED_CUMULATIVE_DIFF_PAIRS: dict[
    tuple[FiscalPeriodType, FiscalPeriodType], FiscalPeriodType
] = {
    (FiscalPeriodType.H1, FiscalPeriodType.Q1): FiscalPeriodType.Q2,
    (FiscalPeriodType.NINE_MONTHS, FiscalPeriodType.H1): FiscalPeriodType.Q3,
    (FiscalPeriodType.FY, FiscalPeriodType.NINE_MONTHS): FiscalPeriodType.Q4,
    (FiscalPeriodType.FY, FiscalPeriodType.H1): FiscalPeriodType.H2,
}


def derive_standalone_from_cumulative(
    minuend: CumulativeDiffInput,
    subtrahend: CumulativeDiffInput,
) -> DerivedCumulativeDiffObservation:
    """Deterministically derive a standalone period from a cumulative minus prior cumulative/quarter.

    Fails closed unless both inputs belong to the exact same definition revision,
    identical semantic axes/currency/unit/scale, flow behavior, valid period pair,
    and chronological same-fiscal-year spacing.
    """
    if minuend.fact_id == subtrahend.fact_id:
        raise ValueError("cumulative_diff requires two distinct source facts")
    if minuend.ticker.upper() != subtrahend.ticker.upper():
        raise ValueError("cumulative_diff inputs must belong to the same ticker")
    if minuend.kpi_definition_id != subtrahend.kpi_definition_id:
        raise ValueError("cumulative_diff inputs must belong to the same kpi_definition_id")
    if minuend.kpi_definition_revision_id != subtrahend.kpi_definition_revision_id:
        raise ValueError(
            "cumulative_diff requires identical kpi_definition_revision_id on both inputs"
        )
    if minuend.accounting_basis != subtrahend.accounting_basis:
        raise ValueError("cumulative_diff requires identical accounting_basis")
    if minuend.consolidation_scope != subtrahend.consolidation_scope:
        raise ValueError("cumulative_diff requires identical consolidation_scope")
    if minuend.dimensions != subtrahend.dimensions:
        raise ValueError("cumulative_diff requires identical dimensions")
    if minuend.currency != subtrahend.currency:
        raise ValueError("cumulative_diff requires identical currency")
    if minuend.unit is not subtrahend.unit or minuend.unit_scale != subtrahend.unit_scale:
        raise ValueError("cumulative_diff requires identical unit and unit_scale")
    if minuend.stock_flow_behavior != "flow" or subtrahend.stock_flow_behavior != "flow":
        raise ValueError("cumulative_diff is only valid for additive flow metrics")

    pair = (minuend.fiscal_period_type, subtrahend.fiscal_period_type)
    derived_period_type = _ALLOWED_CUMULATIVE_DIFF_PAIRS.get(pair)
    if derived_period_type is None:
        raise ValueError(
            f"unsupported cumulative_diff period pair: {minuend.fiscal_period_type.value} - "
            f"{subtrahend.fiscal_period_type.value}"
        )

    minuend_date = date.fromisoformat(minuend.period_end)
    subtrahend_date = date.fromisoformat(subtrahend.period_end)
    day_gap = (minuend_date - subtrahend_date).days
    max_gap = 200 if derived_period_type is FiscalPeriodType.H2 else 120
    if day_gap <= 0 or day_gap > max_gap:
        raise ValueError(
            f"cumulative_diff period gap ({day_gap} days) is invalid for "
            f"{minuend.fiscal_period_type.value} - {subtrahend.fiscal_period_type.value}"
        )

    return DerivedCumulativeDiffObservation(
        ticker=minuend.ticker.upper(),
        kpi_definition_id=minuend.kpi_definition_id,
        kpi_definition_revision_id=minuend.kpi_definition_revision_id,
        period_end=minuend.period_end,
        fiscal_period_type=derived_period_type,
        cadence_kind=classify_period_cadence(derived_period_type),
        value=minuend.value - subtrahend.value,
        unit=minuend.unit,
        currency=minuend.currency,
        accounting_basis=minuend.accounting_basis,
        consolidation_scope=minuend.consolidation_scope,
        dimensions=dict(minuend.dimensions),
        unit_scale=minuend.unit_scale,
        formula=(
            f"{minuend.fiscal_period_type.value} - {subtrahend.fiscal_period_type.value} "
            f"-> {derived_period_type.value}"
        ),
        minuend_fact_id=minuend.fact_id,
        subtrahend_fact_id=subtrahend.fact_id,
        input_fact_ids=(minuend.fact_id, subtrahend.fact_id),
        minuend_period_end=minuend.period_end,
        subtrahend_period_end=subtrahend.period_end,
        minuend_source_doc_id=minuend.source_doc_id,
        subtrahend_source_doc_id=subtrahend.source_doc_id,
    )


cumulative_diff = derive_standalone_from_cumulative
