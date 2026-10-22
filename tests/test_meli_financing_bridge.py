"""Financing arithmetic and closed selectors; fixtures do not qualify source evidence."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from dcf import input_recipes, meli_inputs
from dcf.input_evidence import (
    AssumptionBasis,
    FactBinding,
    InputEvidenceError,
    ModelInputReceipt,
    ModelInputRequest,
    ValuationRoleSelector,
    VerifiedInput,
    input_role_matches,
)

AS_OF = datetime(2026, 10, 19, tzinfo=UTC)
END = date(2026, 6, 30)


def _request(
    *, reserve: float | None = 2.0, context_updates: dict[str, object] | None = None
) -> ModelInputRequest:
    context: dict[str, object] = {
        "schema_version": "meli_financing_bridge.v1",
        "event_key": "synthetic-notes-event",
        "contract_period_end": "2026-09-09",
        "closing_period_end": "2026-09-14",
    }
    context.update(context_updates or {})
    assumptions = {
        key: AssumptionBasis(
            value=value, attribution="analyst", rationale="Synthetic arithmetic case"
        )
        for key, value in {
            "credit_cash_allocation": 10.0,
            "operating_cash_reserve": 20.0,
            "credit_funding_debt_allocation": 30.0,
        }.items()
    }
    if reserve is not None:
        assumptions["financing_other_expenses_reserve"] = AssumptionBasis(
            value=reserve, attribution="analyst", rationale="Explicit synthetic expense reserve"
        )
    return ModelInputRequest(
        recipe=meli_inputs.FINANCING_RECIPE,
        ticker="MELI",
        research_snapshot_id="synthetic",
        financial_period_end=END,
        facts={},
        assumptions=assumptions,
        recipe_context=context,
    )


def _receipt(request: ModelInputRequest, *, fraction: float = 0.97364) -> ModelInputReceipt:
    values = {
        **{
            f"{key}_{suffix}": value
            for key in meli_inputs.FLOW_KEYS
            for suffix, value in (("fy", 100.0), ("ytd", 60.0), ("prior_ytd", 40.0))
        },
        "cb0": 100.0,
        "shares": 50.0,
        "reported_available_cash_and_investments": 500.0,
        "reported_total_financial_debt_and_leases": 300.0,
        "reported_current_operating_lease_liabilities": 20.0,
        "reported_noncurrent_operating_lease_liabilities": 30.0,
        "nimal_actual": 0.2,
        "financing_issued_principal": 1000.0,
        "financing_underwriting_purchase_fraction": fraction,
    }
    ref = FactBinding(
        canonical_metric_cell_id="synthetic-cell",
        metric_id="synthetic-metric",
        metric_definition_revision_id="synthetic-definition",
        canonical_resolution_revision_id="synthetic-resolution",
        observation_id="synthetic-observation",
        observation_payload_sha256="0" * 64,
    )
    requirements = meli_inputs.requirements_for_request(request, as_of=AS_OF)
    return ModelInputReceipt(
        recipe=request.recipe,
        request=request,
        verified_at=AS_OF,
        snapshot_member_sha256="0" * 64,
        effective_inputs_sha256="0" * 64,
        required_keys=tuple(values),
        inventory_snapshot_ids=(),
        inputs=tuple(
            VerifiedInput(
                key=req.key,
                value=values[req.key],
                reference=ref,
                period_start=req.period_start,
                period_end=req.period_end or END,
                document_version_id=None,
                reporting_entity_id="synthetic",
                unit_key=req.unit_key,
                currency=req.currency,
                observation_kind="reported",
                knowledge_at=AS_OF,
                recorded_at=AS_OF,
            )
            for req in requirements
        ),
    )


def test_v5_uses_discounted_underwriter_proceeds_and_explicit_reserve() -> None:
    request = _request()
    actuals, calculations = meli_inputs.calculate_actuals(_receipt(request))
    # Baseline =500-10-20-(300-20-30)+30=250. New cash-debt change=973.64-1000-2.
    assert actuals["baseline_net_cash"] == 250.0
    assert actuals["financing_cash_before_other_expenses"] == pytest.approx(973.64)
    assert actuals["net_cash"] == pytest.approx(221.64)
    assert next(item for item in calculations if item.key == "net_cash").operands == (
        "baseline_net_cash",
        "financing_cash_before_other_expenses",
        "financing_issued_principal",
        "financing_other_expenses_reserve",
    )


def test_v4_baseline_remains_unchanged_and_rejects_financing_context() -> None:
    request = _request().model_copy(update={"recipe": meli_inputs.RECIPE, "recipe_context": None})
    receipt = _receipt(request)
    actuals, _ = meli_inputs.calculate_actuals(receipt)
    assert actuals["net_cash"] == 250.0
    assert "baseline_net_cash" not in actuals
    assert len(meli_inputs.requirements_for_request(request, as_of=AS_OF)) == 28
    with pytest.raises(InputEvidenceError):
        meli_inputs.requirements_for_request(
            request.model_copy(update={"recipe_context": _request().recipe_context}), as_of=AS_OF
        )


def test_event_requirements_pin_dates_event_identity_and_underwriter_basis() -> None:
    reqs = meli_inputs.requirements_for_request(_request(), as_of=AS_OF)
    assert len(reqs) == 30
    principal, fraction = reqs[-2:]
    assert principal.period_end == date(2026, 9, 14)
    assert fraction.period_end == date(2026, 9, 9)
    assert (
        principal.definition_constraints["event_key"]
        == fraction.definition_constraints["event_key"]
    )
    assert fraction.definition_constraints["purchase_basis"] == "underwriter_purchase_price"
    assert principal.unit_key == "USD" and fraction.unit_key == "pure"
    assert principal.accounting_basis == fraction.accounting_basis == "other"
    assert principal.consolidation_scope == fraction.consolidation_scope == "other"
    assert input_recipes.model_engine(meli_inputs.FINANCING_RECIPE) == "meli_platform_sotp"


@pytest.mark.parametrize(
    "updates",
    [
        {"closing_period_end": "2026-06-30"},
        {"closing_period_end": "2026-10-20"},
        {"contract_period_end": "2026-09-15"},
        {"contract_period_end": "2026-06-30"},
        {"extra": "unreviewed"},
    ],
)
def test_context_does_not_admit_wrong_future_or_ambiguous_event_periods(
    updates: dict[str, object],
) -> None:
    with pytest.raises(InputEvidenceError):
        meli_inputs.requirements_for_request(_request(context_updates=updates), as_of=AS_OF)


@pytest.mark.parametrize("reserve", [None, -1.0])
def test_expense_reserve_is_explicit_and_nonnegative(reserve: float | None) -> None:
    with pytest.raises(InputEvidenceError):
        meli_inputs.calculate_actuals(_receipt(_request(reserve=reserve)))


@pytest.mark.parametrize("fraction", [0.0, 97.364, 1.01])
def test_purchase_fraction_has_ratio_units_and_nonzero_proceeds(fraction: float) -> None:
    with pytest.raises(InputEvidenceError):
        meli_inputs.calculate_actuals(_receipt(_request(), fraction=fraction))


def test_v5_effective_hash_population_includes_expense_reserve_without_default() -> None:
    values = {key: 1.0 for key in meli_inputs.REPORTED_DRIVER_KEYS | meli_inputs.ASSUMPTION_KEYS}
    assert "financing_other_expenses_reserve" not in meli_inputs.effective_numeric_inputs(values)
    with pytest.raises(InputEvidenceError):
        input_recipes.effective_numeric_inputs(meli_inputs.FINANCING_RECIPE, values)
    values["financing_other_expenses_reserve"] = 2.0
    assert (
        input_recipes.effective_numeric_inputs(meli_inputs.FINANCING_RECIPE, values)[
            "financing_other_expenses_reserve"
        ]
        == 2.0
    )


def test_public_offer_price_and_other_event_do_not_match_underwriter_fact() -> None:
    requirement = meli_inputs.requirements_for_request(_request(), as_of=AS_OF)[-1]
    for changed in (
        {"purchase_basis": "public_offer_price"},
        {"event_key": "different-notes-event"},
    ):
        scope: dict[str, object] = {
            "valuation_role_selectors": {
                requirement.role: ValuationRoleSelector(
                    canonical_dimensions=(),
                    semantic_constraints={**requirement.definition_constraints, **changed},
                ).model_dump(mode="json")
            }
        }
        assert not input_role_matches(scope, requirement, ())


def test_absent_reported_principal_does_not_become_net_neutral_financing() -> None:
    receipt = _receipt(_request())
    missing = receipt.model_copy(
        update={
            "inputs": tuple(
                item for item in receipt.inputs if item.key != "financing_issued_principal"
            )
        }
    )
    with pytest.raises(InputEvidenceError, match="reported_inputs_invalid"):
        meli_inputs.calculate_actuals(missing)


def _diagnostic_inputs() -> tuple[ModelInputReceipt, dict[str, float], dict[str, float]]:
    from dataclasses import asdict

    from dcf.meli_model import Assum, implied_credit_terminal_roe

    request = _request()
    values = asdict(Assum())
    assumptions = {
        key: AssumptionBasis(value=values[key], attribution="analyst", rationale="Synthetic driver")
        for key in meli_inputs.ASSUMPTION_KEYS
    }
    assumptions.update(request.assumptions)
    request = request.model_copy(update={"assumptions": assumptions})
    receipt = _receipt(request)
    receipt = receipt.model_copy(
        update={
            "inputs": tuple(
                item.model_copy(update={"value": 1000.0})
                if item.key == "cb0"
                else item.model_copy(update={"value": item.value * 2})
                if item.key.startswith("revenue_total_")
                else item.model_copy(update={"value": item.value / 2})
                if item.key.startswith("revenue_credit_")
                else item.model_copy(update={"value": 20000.0})
                if item.key == "reported_available_cash_and_investments"
                else item.model_copy(update={"value": 15000.0})
                if item.key == "reported_total_financial_debt_and_leases"
                else item
                for item in receipt.inputs
            )
        }
    )
    actuals, _ = meli_inputs.calculate_actuals(receipt)
    numeric = meli_inputs.effective_numeric_inputs(
        {
            **{key: item.value for key, item in assumptions.items()},
            **{key: actuals[key] for key in meli_inputs.REPORTED_DRIVER_KEYS},
        },
        recipe=meli_inputs.FINANCING_RECIPE,
    )
    numeric["credit_terminal_roe"] = implied_credit_terminal_roe(numeric)
    assumptions["credit_terminal_roe"] = assumptions["credit_terminal_roe"].model_copy(
        update={"value": numeric["credit_terminal_roe"]}
    )
    receipt = receipt.model_copy(
        update={"request": receipt.request.model_copy(update={"assumptions": assumptions})}
    )
    return receipt, actuals, numeric


def test_diagnostics_replay_cash_variants_without_changing_reported_inputs() -> None:
    receipt, actuals, numeric = _diagnostic_inputs()
    original = receipt.model_dump_json()
    rows = {row.key: row for row in meli_inputs.driver_diagnostics(receipt, actuals, numeric)}
    base_value = meli_inputs.model_output(numeric)["vps"]
    assert isinstance(base_value, (int, float))
    base = float(base_value)
    assert rows["funding_0_vps"].value == pytest.approx(base - 30 / 50)
    assert rows["financing_reserve_0_vps"].value == pytest.approx(base + 2 / 50)
    assert rows["credit_liquidity_0_vps"].value == pytest.approx(base + 10 / 50)
    assert rows["operating_liquidity_0_vps"].value == pytest.approx(base + 20 / 50)
    assert rows["calibrated_profit_variance"].value == pytest.approx(0, abs=1e-9)
    assert rows["profit_proxy_variance"].value == pytest.approx(
        rows["profit_proxy_year0"].value - actuals["operating_income"]
    )
    assert rows["credit_cost_600bp_vps"].value > rows["credit_cost_1200bp_vps"].value
    assert set(rows) == set(meli_inputs.MELI_DIAGNOSTIC_UNITS)
    assert receipt.model_dump_json() == original


def test_source_bound_diagnostics_survive_real_workbook_recalculation(tmp_path: Path) -> None:
    import os

    from dcf.meli_model import Assum, mirror
    from dcf.meli_workbook import WorkbookRuntime, build_workbook, workbook_spec

    receipt, actuals, numeric = _diagnostic_inputs()
    _, calculations = meli_inputs.calculate_actuals(receipt)
    context = dict(receipt.request.recipe_context or {}) | {
        "diagnostics": "meli-report-driver-checks/v1"
    }
    receipt = receipt.model_copy(
        update={
            "request": receipt.request.model_copy(update={"recipe_context": context}),
            "calculations": calculations
            + meli_inputs.driver_diagnostics(receipt, actuals, numeric),
        }
    )
    base = Assum()
    for key, value in numeric.items():
        setattr(base, key, int(value) if key in {"years", "derive_capm"} else value)
    cases = {key: base for key in ("base", "bear", "bull")}
    spec = workbook_spec(base, mirror(base), cases=cases, input_receipt=receipt)
    assert len(spec["data_tables"]) == 3
    assert spec["expected"]["Scenario!B36"] == pytest.approx(0, abs=1e-9)
    assert spec["expected"]["Dashboard!E65"] > spec["expected"]["Dashboard!E68"]
    if not os.environ.get("DCF_ARTIFACT_NODE"):
        pytest.skip("Real workbook engine requires explicit installed runtime paths")
    path = tmp_path / "diagnostics.xlsx"
    build_workbook(
        base,
        mirror(base),
        path,
        cases=cases,
        input_receipt=receipt,
        runtime=WorkbookRuntime.from_environment(),
    )
    import json

    proof = json.loads(path.with_suffix(".xlsx.recalculation.json").read_text())
    assert proof["status"] == "verified"
    assert proof["after_reopen"]["checked"] == len(spec["expected"])
