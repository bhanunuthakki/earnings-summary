"""Deterministic evidence for conclusion-driving DCF equity bridges."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

AggregateBasis = Literal["reported_aggregate", "complete_component_sum"]
ReceiptStatus = Literal["verified", "unverified"]

_MILLION = Decimal("1000000")
_ARITHMETIC_TOLERANCE = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class ResolvedAggregate:
    """A reported aggregate or an exact sum of every required component."""

    value: float
    basis: AggregateBasis
    component_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EquityBridgeReceipt:
    """Proof that the inputs and arithmetic behind value/share are auditable."""

    ticker: str
    status: ReceiptStatus
    arithmetic_status: ReceiptStatus
    operating_value_usd_m: float
    cash_m: float
    total_debt_m: float
    diluted_shares_m: float
    fx_to_usd: float
    stored_value_per_share_usd: float
    recomputed_value_per_share_usd: float | None
    arithmetic_delta: float | None
    reporting_currency: str | None
    bridge_period_end: str | None
    bridge_fiscal_period_type: str | None
    bridge_context: Mapping[str, object] | None
    cash_lineage: Mapping[str, object] | None
    total_debt_lineage: Mapping[str, object] | None
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "dcf_equity_bridge_receipt.v2",
            "ticker": self.ticker,
            "status": self.status,
            "arithmetic_status": self.arithmetic_status,
            "operating_value_usd_m": self.operating_value_usd_m,
            "cash_m": self.cash_m,
            "total_debt_m": self.total_debt_m,
            "diluted_shares_m": self.diluted_shares_m,
            "fx_to_usd": self.fx_to_usd,
            "stored_value_per_share_usd": self.stored_value_per_share_usd,
            "recomputed_value_per_share_usd": self.recomputed_value_per_share_usd,
            "arithmetic_delta": self.arithmetic_delta,
            "reporting_currency": self.reporting_currency,
            "bridge_period_end": self.bridge_period_end,
            "bridge_fiscal_period_type": self.bridge_fiscal_period_type,
            "bridge_context": (
                dict(self.bridge_context) if self.bridge_context is not None else None
            ),
            "cash_lineage": dict(self.cash_lineage) if self.cash_lineage is not None else None,
            "total_debt_lineage": (
                dict(self.total_debt_lineage) if self.total_debt_lineage is not None else None
            ),
            "reasons": list(self.reasons),
        }


def resolve_complete_aggregate(
    record: Mapping[str, object],
    *,
    aggregate_field: str,
    component_fields: Sequence[str],
) -> ResolvedAggregate | None:
    """Resolve an aggregate without truthiness defaults or partial sums."""
    aggregate = _decimal(record.get(aggregate_field))
    if aggregate is not None:
        return ResolvedAggregate(float(aggregate), "reported_aggregate", ())
    components = tuple(_decimal(record.get(field)) for field in component_fields)
    if not components or any(value is None for value in components):
        return None
    complete = cast("tuple[Decimal, ...]", components)
    return ResolvedAggregate(
        float(sum(complete, start=Decimal(0))),
        "complete_component_sum",
        tuple(component_fields),
    )


def build_equity_bridge_receipt(
    *,
    ticker: str,
    operating_value_usd_m: float,
    cash_m: float,
    total_debt_m: float,
    diluted_shares_m: float,
    fx_to_usd: float,
    value_per_share_usd: float,
    reporting_currency: str | None,
    primary_fact_overlay: Mapping[str, object] | None,
    bridge_context: Mapping[str, object] | None,
) -> EquityBridgeReceipt:
    """Build a fail-closed receipt from the exact inputs used by the DCF."""
    reasons: set[str] = set()
    op = _required_decimal(operating_value_usd_m, "operating_value_usd_m", reasons)
    cash = _required_decimal(cash_m, "cash_m", reasons)
    debt = _required_decimal(total_debt_m, "total_debt_m", reasons)
    shares = _required_decimal(diluted_shares_m, "diluted_shares_m", reasons)
    fx = _required_decimal(fx_to_usd, "fx_to_usd", reasons)
    stored = _required_decimal(value_per_share_usd, "value_per_share_usd", reasons)

    recomputed: Decimal | None = None
    delta: Decimal | None = None
    if shares is not None and shares <= 0:
        reasons.add("non_positive_diluted_shares")
    if fx is not None and fx <= 0:
        reasons.add("non_positive_fx_to_usd")
    if None not in (op, cash, debt, shares, fx, stored) and shares and shares > 0:
        assert op is not None and cash is not None and debt is not None
        assert fx is not None and stored is not None
        recomputed = (op + (cash - debt) * fx) / shares
        delta = recomputed - stored
        if abs(delta) > _ARITHMETIC_TOLERANCE:
            reasons.add("equity_bridge_arithmetic_mismatch")

    currency = reporting_currency.upper() if isinstance(reporting_currency, str) else None
    if currency is None:
        reasons.add("missing_reporting_currency")
    validated_context, bridge_period, bridge_fiscal = _validate_bridge_context(
        bridge_context,
        ticker=ticker,
        cash_m=cash_m,
        total_debt_m=total_debt_m,
        diluted_shares_m=diluted_shares_m,
        reporting_currency=currency,
        reasons=reasons,
    )
    cash_lineage = _matching_lineage(
        primary_fact_overlay,
        field="cashAndShortTermInvestments",
        expected_m=cash_m,
        expected_period=bridge_period,
        expected_fiscal_period=bridge_fiscal,
    )
    debt_lineage = _matching_lineage(
        primary_fact_overlay,
        field="totalDebt",
        expected_m=total_debt_m,
        expected_period=bridge_period,
        expected_fiscal_period=bridge_fiscal,
    )
    if cash_lineage is None:
        reasons.add("missing_primary_cash_lineage")
    if debt_lineage is None:
        reasons.add("missing_primary_total_debt_lineage")

    _validate_lineage_currency(cash_lineage, currency, "cash", reasons)
    _validate_lineage_currency(debt_lineage, currency, "total_debt", reasons)
    _validate_lineage_unit(cash_lineage, "cash", reasons)
    _validate_lineage_unit(debt_lineage, "total_debt", reasons)

    arithmetic_status: ReceiptStatus = (
        "verified"
        if recomputed is not None and delta is not None and abs(delta) <= _ARITHMETIC_TOLERANCE
        else "unverified"
    )
    status: ReceiptStatus = "verified" if not reasons else "unverified"
    return EquityBridgeReceipt(
        ticker=ticker.upper(),
        status=status,
        arithmetic_status=arithmetic_status,
        operating_value_usd_m=operating_value_usd_m,
        cash_m=cash_m,
        total_debt_m=total_debt_m,
        diluted_shares_m=diluted_shares_m,
        fx_to_usd=fx_to_usd,
        stored_value_per_share_usd=value_per_share_usd,
        recomputed_value_per_share_usd=float(recomputed) if recomputed is not None else None,
        arithmetic_delta=float(delta) if delta is not None else None,
        reporting_currency=currency,
        bridge_period_end=bridge_period,
        bridge_fiscal_period_type=bridge_fiscal,
        bridge_context=validated_context,
        cash_lineage=cash_lineage,
        total_debt_lineage=debt_lineage,
        reasons=tuple(sorted(reasons)),
    )


def _matching_lineage(
    overlay: Mapping[str, object] | None,
    *,
    field: str,
    expected_m: float,
    expected_period: str | None,
    expected_fiscal_period: str | None,
) -> Mapping[str, object] | None:
    if overlay is None or expected_period is None or expected_fiscal_period is None:
        return None
    statements = overlay.get("statements")
    if not isinstance(statements, Mapping):
        return None
    balance_raw = cast("Mapping[str, object]", statements).get("balance")
    if not isinstance(balance_raw, Mapping):
        return None
    applied = cast("Mapping[str, object]", balance_raw).get("applied")
    if not isinstance(applied, list):
        return None
    expected = Decimal(str(expected_m)) * _MILLION
    matches: list[Mapping[str, object]] = []
    for raw in cast("list[object]", applied):
        if not isinstance(raw, Mapping):
            continue
        item = cast("Mapping[str, object]", raw)
        if item.get("fmp_field") != field:
            continue
        if _text(item, "period_end") != expected_period:
            continue
        if _text(item, "fiscal_period_type") != expected_fiscal_period:
            continue
        primary = _decimal(item.get("primary_value"))
        if primary == expected:
            matches.append(item)
    if not matches:
        return None
    return max(matches, key=lambda item: _text(item, "period_end") or "")


def _validate_bridge_context(
    context: Mapping[str, object] | None,
    *,
    ticker: str,
    cash_m: float,
    total_debt_m: float,
    diluted_shares_m: float,
    reporting_currency: str | None,
    reasons: set[str],
) -> tuple[Mapping[str, object] | None, str | None, str | None]:
    if context is None:
        reasons.add("missing_equity_bridge_context")
        return None, None, None
    copied = dict(context)
    if copied.get("schema_version") != "dcf_equity_bridge_context.v1":
        reasons.add("invalid_equity_bridge_context_schema")
    observed_ticker = _text(copied, "ticker")
    if observed_ticker is None or observed_ticker.upper() != ticker.upper():
        reasons.add("equity_bridge_context_ticker_mismatch")
    period = _text(copied, "period_end")
    fiscal = _text(copied, "fiscal_period_type")
    if period is None:
        reasons.add("missing_equity_bridge_context_period")
    if fiscal is None:
        reasons.add("missing_equity_bridge_context_fiscal_period")
    observed_currency = _text(copied, "reporting_currency")
    if reporting_currency is None or observed_currency is None:
        reasons.add("missing_equity_bridge_context_currency")
    elif observed_currency.upper() != reporting_currency:
        reasons.add("equity_bridge_context_currency_mismatch")
    for field, expected in (
        ("cash_m", cash_m),
        ("total_debt_m", total_debt_m),
        ("diluted_shares_m", diluted_shares_m),
    ):
        observed = _decimal(copied.get(field))
        if observed is None or observed != Decimal(str(expected)):
            reasons.add(f"equity_bridge_context_{field}_mismatch")
    for field in ("cash_basis", "total_debt_basis"):
        if copied.get(field) not in {"reported_aggregate", "complete_component_sum"}:
            reasons.add(f"invalid_equity_bridge_context_{field}")
    return copied, period, fiscal


def _validate_lineage_currency(
    lineage: Mapping[str, object] | None,
    expected: str | None,
    label: str,
    reasons: set[str],
) -> None:
    if lineage is None or expected is None:
        return
    observed = _text(lineage, "currency")
    if observed is None or observed.upper() != expected:
        reasons.add(f"{label}_currency_mismatch")


def _validate_lineage_unit(
    lineage: Mapping[str, object] | None,
    label: str,
    reasons: set[str],
) -> None:
    if lineage is None:
        return
    observed = _text(lineage, "unit")
    if observed is None or observed.lower() != "actual":
        reasons.add(f"{label}_unit_mismatch")


def _required_decimal(value: object, field: str, reasons: set[str]) -> Decimal | None:
    parsed = _decimal(value)
    if parsed is None:
        reasons.add(f"invalid_{field}")
    return parsed


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _text(value: Mapping[str, object] | None, key: str) -> str | None:
    if value is None:
        return None
    raw = value.get(key)
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


__all__ = [
    "EquityBridgeReceipt",
    "ResolvedAggregate",
    "build_equity_bridge_receipt",
    "resolve_complete_aggregate",
]
