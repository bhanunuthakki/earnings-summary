"""Deterministic evidence for conclusion-driving DCF equity bridges."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

AggregateBasis = Literal["reported_aggregate", "complete_component_sum"]
DebtScope = Literal["interest_bearing_debt_only", "debt_and_lease_obligations"]
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
class ResolvedDebtScope:
    """A fail-closed DCF liability set, separate from its debt aggregation basis."""

    value: float
    scope: DebtScope
    debt_basis: AggregateBasis
    calculation: str
    # (input field, sign).  A negative sign is explicit evidence of a
    # subtraction, never an implicit omission of a liability.
    operations: tuple[tuple[str, int], ...]
    component_lineage: tuple[Mapping[str, object], ...] = ()


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
    debt_scope: DebtScope | None
    debt_component_lineage: tuple[Mapping[str, object], ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "dcf_equity_bridge_receipt.v3",
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
            "debt_scope": self.debt_scope,
            "debt_component_lineage": [dict(item) for item in self.debt_component_lineage],
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


def resolve_debt_scope(
    record: Mapping[str, object], *, scope: DebtScope
) -> ResolvedDebtScope | None:
    """Resolve the approved liability scope without absence-to-zero defaults.

    ``totalDebt`` is the SEC DebtAndCapitalLeaseObligations-style aggregate.
    Finance leases are therefore explicitly removed to obtain pure
    interest-bearing debt, then explicitly restored with operating leases for
    the debt-and-lease scope.  The exact primary-source lineage is validated by
    the receipt layer; this resolver only performs deterministic arithmetic.
    """
    debt = resolve_complete_aggregate(
        record,
        aggregate_field="totalDebt",
        component_fields=("longTermDebt", "shortTermDebt"),
    )
    if debt is None:
        return None
    if scope == "debt_and_lease_obligations":
        operating = resolve_complete_aggregate(
            record,
            aggregate_field="operatingLeaseLiability",
            component_fields=(
                "operatingLeaseLiabilityCurrent",
                "operatingLeaseLiabilityNoncurrent",
            ),
        )
        if operating is None:
            return None
        return ResolvedDebtScope(
            value=float(Decimal(str(debt.value)) + Decimal(str(operating.value))),
            scope=scope,
            debt_basis=debt.basis,
            calculation="debt_and_capital_lease_obligations + operating_lease_liability",
            operations=(("totalDebt", 1), ("operatingLeaseLiability", 1)),
        )
    finance = resolve_complete_aggregate(
        record,
        aggregate_field="financeLeaseLiability",
        component_fields=("financeLeaseLiabilityCurrent", "financeLeaseLiabilityNoncurrent"),
    )
    if finance is None:
        return None
    pure_debt = Decimal(str(debt.value)) - Decimal(str(finance.value))
    if pure_debt < 0:
        return None
    return ResolvedDebtScope(
        value=float(pure_debt),
        scope=scope,
        debt_basis=debt.basis,
        calculation="debt_and_capital_lease_obligations - finance_lease_liability",
        operations=(("totalDebt", 1), ("financeLeaseLiability", -1)),
    )


def resolve_primary_debt_scope(
    record: Mapping[str, object],
    *,
    scope: DebtScope,
    overlay: Mapping[str, object] | None,
    period_end: str,
    fiscal_period_type: str,
    currency: str,
) -> ResolvedDebtScope | None:
    """Resolve a scope only when every signed input has exact primary lineage."""
    resolved = resolve_debt_scope(record, scope=scope)
    if resolved is None or overlay is None:
        return None
    statements = overlay.get("statements")
    if not isinstance(statements, Mapping):
        return None
    statements_map = cast("Mapping[str, object]", statements)
    balance = statements_map.get("balance")
    if not isinstance(balance, Mapping) or balance.get("status") != "ok":
        return None
    balance_map = cast("Mapping[str, object]", balance)
    applied = balance_map.get("applied")
    if not isinstance(applied, list):
        return None
    lineages: list[Mapping[str, object]] = []
    for field, _sign in resolved.operations:
        matches = [
            cast("Mapping[str, object]", raw)
            for raw in cast("list[object]", applied)
            if isinstance(raw, Mapping)
            and cast("Mapping[str, object]", raw).get("fmp_field") == field
            and _text(cast("Mapping[str, object]", raw), "period_end") == period_end
            and _text(cast("Mapping[str, object]", raw), "fiscal_period_type") == fiscal_period_type
            and isinstance(cast("Mapping[str, object]", raw).get("currency"), str)
            and cast(str, cast("Mapping[str, object]", raw).get("currency")).upper()
            == currency.upper()
            and isinstance(cast("Mapping[str, object]", raw).get("unit"), str)
            and cast(str, cast("Mapping[str, object]", raw).get("unit")).lower() == "actual"
        ]
        if len(matches) != 1:
            return None
        if field == "totalDebt" and matches[0].get("derivation") is not None:
            # The approved subtraction policy requires the semantically exact
            # DebtAndCapitalLeaseObligations aggregate.  A generic LT+ST sum
            # cannot prove the same lease perimeter and therefore stays HOLD.
            return None
        lineages.append(matches[0])
    return ResolvedDebtScope(
        value=resolved.value,
        scope=resolved.scope,
        debt_basis=resolved.debt_basis,
        calculation=resolved.calculation,
        operations=resolved.operations,
        component_lineage=tuple(lineages),
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
    (
        validated_context,
        bridge_period,
        bridge_fiscal,
        debt_scope,
        debt_component_lineage,
    ) = _validate_bridge_context(
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
    if cash_lineage is None:
        reasons.add("missing_primary_cash_lineage")
    _validate_debt_component_lineage(
        debt_component_lineage,
        overlay=primary_fact_overlay,
        expected_m=total_debt_m,
        expected_period=bridge_period,
        expected_fiscal_period=bridge_fiscal,
        expected_currency=currency,
        reasons=reasons,
    )
    debt_lineage = next(
        (item for item in debt_component_lineage if item.get("fmp_field") == "totalDebt"),
        None,
    )

    _validate_lineage_currency(cash_lineage, currency, "cash", reasons)
    _validate_lineage_unit(cash_lineage, "cash", reasons)

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
        debt_scope=debt_scope,
        debt_component_lineage=debt_component_lineage,
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
) -> tuple[
    Mapping[str, object] | None,
    str | None,
    str | None,
    DebtScope | None,
    tuple[Mapping[str, object], ...],
]:
    if context is None:
        reasons.add("missing_equity_bridge_context")
        return None, None, None, None, ()
    copied = dict(context)
    if copied.get("schema_version") != "dcf_equity_bridge_context.v2":
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
    scope_raw = copied.get("debt_scope")
    scope: DebtScope | None = (
        cast("DebtScope", scope_raw)
        if scope_raw in {"interest_bearing_debt_only", "debt_and_lease_obligations"}
        else None
    )
    if scope is None:
        reasons.add("invalid_equity_bridge_context_debt_scope")
    calculation = copied.get("debt_calculation")
    if not isinstance(calculation, str) or not calculation.strip():
        reasons.add("missing_equity_bridge_context_debt_calculation")
    operations_raw = copied.get("debt_operations")
    operations: tuple[tuple[str, int], ...] = ()
    if isinstance(operations_raw, list):
        parsed_operations: list[tuple[str, int]] = []
        for raw in cast("list[object]", operations_raw):
            if not isinstance(raw, Mapping):
                parsed_operations = []
                break
            raw_map = cast("Mapping[str, object]", raw)
            field = _text(raw_map, "field")
            sign = raw_map.get("sign")
            if field is None or sign not in {-1, 1}:
                parsed_operations = []
                break
            parsed_operations.append((field, cast("int", sign)))
        operations = tuple(parsed_operations)
    expected_operations = {
        "interest_bearing_debt_only": (("totalDebt", 1), ("financeLeaseLiability", -1)),
        "debt_and_lease_obligations": (("totalDebt", 1), ("operatingLeaseLiability", 1)),
    }
    expected_calculations = {
        "interest_bearing_debt_only": (
            "debt_and_capital_lease_obligations - finance_lease_liability"
        ),
        "debt_and_lease_obligations": (
            "debt_and_capital_lease_obligations + operating_lease_liability"
        ),
    }
    if scope is None or operations != expected_operations.get(scope):
        reasons.add("invalid_equity_bridge_context_debt_operations")
    if scope is not None and calculation != expected_calculations[scope]:
        reasons.add("invalid_equity_bridge_context_debt_calculation")
    if copied.get("total_debt_basis") != "reported_aggregate":
        reasons.add("unverified_debt_aggregate_semantics")
    raw_lineage = copied.get("debt_component_lineage")
    raw_lineage_items = cast("list[object]", raw_lineage) if isinstance(raw_lineage, list) else []
    component_lineage = (
        tuple(cast("Mapping[str, object]", item) for item in raw_lineage_items)
        if raw_lineage_items and all(isinstance(item, Mapping) for item in raw_lineage_items)
        else ()
    )
    if not component_lineage:
        reasons.add("missing_debt_component_lineage")
    elif operations != tuple(
        (_text(item, "fmp_field") or "", cast("int", item.get("operation_sign")))
        for item in component_lineage
        if item.get("operation_sign") in {-1, 1}
    ):
        reasons.add("debt_operations_lineage_mismatch")
    return copied, period, fiscal, scope, component_lineage


def _validate_debt_component_lineage(
    lineages: tuple[Mapping[str, object], ...],
    *,
    overlay: Mapping[str, object] | None,
    expected_m: float,
    expected_period: str | None,
    expected_fiscal_period: str | None,
    expected_currency: str | None,
    reasons: set[str],
) -> None:
    if not lineages or expected_period is None or expected_fiscal_period is None:
        return
    statements = overlay.get("statements") if overlay is not None else None
    statements_map: Mapping[str, object] = (
        cast("Mapping[str, object]", statements) if isinstance(statements, Mapping) else {}
    )
    balance: object | None = statements_map.get("balance")
    balance_map: Mapping[str, object] = (
        cast("Mapping[str, object]", balance) if isinstance(balance, Mapping) else {}
    )
    applied: object | None = balance_map.get("applied")
    applied_values = cast("list[object]", applied) if isinstance(applied, list) else []
    applied_items = (
        [cast("Mapping[str, object]", item) for item in applied_values if isinstance(item, Mapping)]
        if applied_values
        else []
    )
    total = Decimal(0)
    seen_fields: set[str] = set()
    for item in lineages:
        field = _text(item, "fmp_field")
        sign = item.get("operation_sign")
        value = _decimal(item.get("primary_value"))
        if field is None or sign not in {-1, 1} or value is None:
            reasons.add("invalid_debt_component_lineage")
            continue
        if field in seen_fields:
            reasons.add("duplicate_debt_component_lineage")
        seen_fields.add(field)
        if _text(item, "period_end") != expected_period:
            reasons.add("debt_component_period_mismatch")
        if _text(item, "fiscal_period_type") != expected_fiscal_period:
            reasons.add("debt_component_fiscal_period_mismatch")
        _validate_lineage_currency(item, expected_currency, "debt_component", reasons)
        _validate_lineage_unit(item, "debt_component", reasons)
        fact_id = item.get("fact_id")
        if not any(
            candidate.get("fact_id") == fact_id
            and candidate.get("fmp_field") == field
            and _text(candidate, "period_end") == expected_period
            and _text(candidate, "fiscal_period_type") == expected_fiscal_period
            and _decimal(candidate.get("primary_value")) == value
            and (
                expected_currency is not None
                and (_text(candidate, "currency") or "").upper() == expected_currency
            )
            for candidate in applied_items
        ):
            reasons.add("debt_component_not_in_primary_overlay")
        total += value * cast("int", sign)
    if total != Decimal(str(expected_m)) * _MILLION:
        reasons.add("debt_component_arithmetic_mismatch")


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
    "DebtScope",
    "EquityBridgeReceipt",
    "ResolvedAggregate",
    "ResolvedDebtScope",
    "build_equity_bridge_receipt",
    "resolve_complete_aggregate",
    "resolve_debt_scope",
    "resolve_primary_debt_scope",
]
