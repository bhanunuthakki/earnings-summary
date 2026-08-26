from __future__ import annotations

from dcf.equity_bridge import build_equity_bridge_receipt, resolve_complete_aggregate


def _lineage(field: str, value: float, *, period: str = "2026-06-30") -> dict[str, object]:
    return {
        "line_item": field,
        "fmp_field": field,
        "period_end": period,
        "fiscal_period_type": "Q2",
        "currency": "USD",
        "unit": "actual",
        "source_doc_id": 12,
        "source_tier": "sec_official",
        "source_type": "sec_xbrl",
        "source_url": "https://www.sec.gov/example",
        "as_of": "2026-08-20T12:00:00+00:00",
        "fact_id": 34,
        "primary_value": value,
    }


def _overlay(*items: dict[str, object]) -> dict[str, object]:
    return {
        "status": "ok",
        "statements": {
            "balance": {
                "status": "ok",
                "applied": list(items),
                "conflicts": [],
                "rejected": [],
            }
        },
    }


def _context(*, period: str = "2026-06-30", currency: str = "USD") -> dict[str, object]:
    return {
        "schema_version": "dcf_equity_bridge_context.v1",
        "ticker": "TEST",
        "period_end": period,
        "fiscal_period_type": "Q2",
        "reporting_currency": currency,
        "cash_m": 200.0,
        "total_debt_m": 100.0,
        "diluted_shares_m": 10.0,
        "cash_basis": "reported_aggregate",
        "total_debt_basis": "reported_aggregate",
    }


def test_complete_aggregate_preserves_zero_and_requires_every_component() -> None:
    exact = resolve_complete_aggregate(
        {"totalDebt": 0, "longTermDebt": 9, "shortTermDebt": 3},
        aggregate_field="totalDebt",
        component_fields=("longTermDebt", "shortTermDebt"),
    )
    assert exact is not None
    assert exact.value == 0
    assert exact.basis == "reported_aggregate"

    derived = resolve_complete_aggregate(
        {"totalDebt": None, "longTermDebt": 9, "shortTermDebt": 0},
        aggregate_field="totalDebt",
        component_fields=("longTermDebt", "shortTermDebt"),
    )
    assert derived is not None
    assert derived.value == 9
    assert derived.basis == "complete_component_sum"

    assert (
        resolve_complete_aggregate(
            {"totalDebt": None, "longTermDebt": 9},
            aggregate_field="totalDebt",
            component_fields=("longTermDebt", "shortTermDebt"),
        )
        is None
    )


def test_equity_bridge_receipt_verifies_arithmetic_and_primary_lineage() -> None:
    receipt = build_equity_bridge_receipt(
        ticker="TEST",
        operating_value_usd_m=1_000,
        cash_m=200,
        total_debt_m=100,
        diluted_shares_m=10,
        fx_to_usd=1,
        value_per_share_usd=110,
        reporting_currency="USD",
        primary_fact_overlay=_overlay(
            _lineage("cashAndShortTermInvestments", 200_000_000),
            _lineage("totalDebt", 100_000_000),
        ),
        bridge_context=_context(),
    )

    payload = receipt.to_dict()
    assert payload["schema_version"] == "dcf_equity_bridge_receipt.v2"
    assert payload["status"] == "verified"
    assert payload["recomputed_value_per_share_usd"] == 110.0
    assert payload["arithmetic_delta"] == 0.0
    assert payload["bridge_period_end"] == "2026-06-30"
    assert payload["bridge_fiscal_period_type"] == "Q2"
    assert payload["bridge_context"] == _context()
    assert receipt.cash_lineage is not None
    assert receipt.total_debt_lineage is not None
    assert receipt.cash_lineage["fact_id"] == 34
    assert receipt.total_debt_lineage["source_tier"] == "sec_official"


def test_equity_bridge_receipt_does_not_average_away_missing_lineage() -> None:
    receipt = build_equity_bridge_receipt(
        ticker="TEST",
        operating_value_usd_m=1_000,
        cash_m=200,
        total_debt_m=100,
        diluted_shares_m=10,
        fx_to_usd=1,
        value_per_share_usd=110,
        reporting_currency="USD",
        primary_fact_overlay=_overlay(
            _lineage("cashAndShortTermInvestments", 200_000_000),
        ),
        bridge_context=_context(),
    )

    assert receipt.status == "unverified"
    assert receipt.arithmetic_status == "verified"
    assert "missing_primary_total_debt_lineage" in receipt.reasons


def test_equity_bridge_receipt_requires_same_balance_period_and_currency() -> None:
    receipt = build_equity_bridge_receipt(
        ticker="TEST",
        operating_value_usd_m=1_000,
        cash_m=200,
        total_debt_m=100,
        diluted_shares_m=10,
        fx_to_usd=1,
        value_per_share_usd=110,
        reporting_currency="USD",
        primary_fact_overlay=_overlay(
            _lineage("cashAndShortTermInvestments", 200_000_000),
            {**_lineage("totalDebt", 100_000_000, period="2026-03-31"), "currency": "EUR"},
        ),
        bridge_context=_context(),
    )

    assert receipt.status == "unverified"
    assert "missing_primary_total_debt_lineage" in receipt.reasons


def test_equity_bridge_receipt_applies_fx_to_cash_and_debt() -> None:
    receipt = build_equity_bridge_receipt(
        ticker="TEST",
        operating_value_usd_m=500,
        cash_m=100,
        total_debt_m=50,
        diluted_shares_m=5,
        fx_to_usd=2,
        value_per_share_usd=120,
        reporting_currency="EUR",
        primary_fact_overlay=_overlay(
            {**_lineage("cashAndShortTermInvestments", 100_000_000), "currency": "EUR"},
            {**_lineage("totalDebt", 50_000_000), "currency": "EUR"},
        ),
        bridge_context={
            **_context(currency="EUR"),
            "cash_m": 100.0,
            "total_debt_m": 50.0,
            "diluted_shares_m": 5.0,
        },
    )

    assert receipt.status == "verified"
    assert receipt.recomputed_value_per_share_usd == 120


def test_receipt_requires_model_input_context_and_rejects_an_older_matching_value() -> None:
    no_context = build_equity_bridge_receipt(
        ticker="TEST",
        operating_value_usd_m=1_000,
        cash_m=200,
        total_debt_m=100,
        diluted_shares_m=10,
        fx_to_usd=1,
        value_per_share_usd=110,
        reporting_currency="USD",
        primary_fact_overlay=_overlay(
            _lineage("cashAndShortTermInvestments", 200_000_000),
            _lineage("totalDebt", 100_000_000),
        ),
        bridge_context=None,
    )
    assert no_context.status == "unverified"
    assert "missing_equity_bridge_context" in no_context.reasons

    stale_match = build_equity_bridge_receipt(
        ticker="TEST",
        operating_value_usd_m=1_000,
        cash_m=200,
        total_debt_m=100,
        diluted_shares_m=10,
        fx_to_usd=1,
        value_per_share_usd=110,
        reporting_currency="USD",
        primary_fact_overlay=_overlay(
            _lineage("cashAndShortTermInvestments", 200_000_000, period="2026-03-31"),
            _lineage("totalDebt", 100_000_000, period="2026-03-31"),
        ),
        bridge_context=_context(period="2026-06-30"),
    )
    assert stale_match.status == "unverified"
    assert "missing_primary_cash_lineage" in stale_match.reasons
    assert "missing_primary_total_debt_lineage" in stale_match.reasons
