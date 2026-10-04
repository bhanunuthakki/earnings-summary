"""Fixed population, period arithmetic, and receipt tampering checks."""

import sqlite3
from datetime import UTC, date, datetime

import pytest

from dcf import onon_inputs
from dcf.input_evidence import (
    AssumptionBasis,
    AssumptionReview,
    DriverReview,
    FactBinding,
    InputEvidenceError,
    ModelInputReceipt,
    ModelInputRequest,
    VerifiedInput,
    canonical_digest,
)
from tests.test_onon_model import memo_inputs

NOW = datetime(2026, 10, 3, 20, tzinfo=UTC)
END = date(2026, 6, 30)


def proof() -> ModelInputReceipt:
    flow = {
        "revenue": 3014.0,
        "adjusted_ebitda": 567.0,
        "operating_income": 377.0,
        "depreciation_amortization": 120.0,
        "rou_depreciation": -70.0,
        "sbc": -62.6,
        "ppe_purchases": -80.0,
        "intangible_purchases": -10.0,
        "lease_principal": -70.0,
        "lease_interest_expense": -12.0,
        "operating_cash_flow": 400.0,
    }
    point = {
        "reported_cash": 1205.6,
        "restricted_cash": 0.9,
        "receivables": 374.0,
        "inventory": 472.9,
        "payables": 211.0,
        "current_lease_liabilities": 90.0,
        "noncurrent_lease_liabilities": 472.5,
        "reported_other_nonlease_financial_liabilities": 52.0,
        "class_a_outstanding": 301.715535,
        "class_b_outstanding": 324.991680,
        "class_a_dilutive_awards": 1.644629,
        "class_b_dilutive_awards": 2.493692,
    }
    reqs = onon_inputs.requirements_for(END)
    ref = FactBinding(
        canonical_metric_cell_id="cell",
        metric_id="metric",
        metric_definition_revision_id="definition",
        canonical_resolution_revision_id="resolution",
        observation_id="observation",
        observation_payload_sha256="a" * 64,
    )
    entries: list[VerifiedInput] = []
    for req in reqs:
        key = req.role.removeprefix("onon.")
        value = (
            point[key]
            if req.period_kind == "instant"
            else flow[key]
            * (1 if req.key.endswith("_fy") else 0.6 if req.key.endswith("_prior_ytd") else 0.7)
        )
        entries.append(
            VerifiedInput(
                key=req.key,
                value=value,
                reference=ref,
                period_start=req.period_start,
                period_end=req.period_end or END,
                document_version_id="doc",
                reporting_entity_id="entity",
                unit_key=req.unit_key,
                currency=req.currency,
                observation_kind="reported",
                knowledge_at=NOW,
                recorded_at=NOW,
            )
        )
    request = ModelInputRequest(
        recipe=onon_inputs.RECIPE,
        ticker="ONON",
        research_snapshot_id="snapshot",
        financial_period_end=END,
        facts={req.key: ref for req in reqs},
        assumptions={
            key: AssumptionBasis(
                value=value,
                attribution="analyst",
                rationale="Synthetic reviewed assumption",
                source_reference="synthetic:fixture",
                source_as_of=NOW.date(),
                recorded_at=NOW,
            )
            for key, value in memo_inputs().items()
            if key in onon_inputs.ASSUMPTION_KEYS
        },
    )
    return ModelInputReceipt(
        recipe=onon_inputs.RECIPE,
        request=request,
        verified_at=NOW,
        snapshot_member_sha256="b" * 64,
        effective_inputs_sha256="c" * 64,
        required_keys=tuple(sorted(request.facts)),
        inputs=tuple(entries),
        inventory_snapshot_ids=("inventory",),
    )


def test_complete_fixed_population_periods_and_scale() -> None:
    reqs = onon_inputs.requirements_for(END)
    assert len(reqs) == 45
    by_key = {r.key: r for r in reqs}
    assert by_key["revenue_fy"].period_start == date(2025, 1, 1)
    assert by_key["revenue_ytd"].period_end == END
    assert by_key["revenue_prior_ytd"].period_end == date(2025, 6, 30)
    assert float(by_key["revenue_fy"].scale) == 1e-6
    assert by_key["class_b_outstanding"].period_kind == "instant"
    with pytest.raises(InputEvidenceError, match="unsupported_onon"):
        onon_inputs.requirements_for(date(2026, 7, 1))


def test_ttm_and_economic_shares_never_use_weighted_average_eps_counts() -> None:
    actuals, calculations = onon_inputs.calculate_actuals(proof())
    assert actuals["revenue"] == pytest.approx(3014 * 1.1)
    assert actuals["shares"] == pytest.approx(336.1087012)
    assert actuals["cash"] == pytest.approx(1204.7)
    assert actuals["sbc_expense"] == pytest.approx(62.6 * 1.1)
    assert actuals["nonlease_da"] == pytest.approx(55)
    assert actuals["capex"] == pytest.approx(99)
    assert actuals["cash_rent_proxy"] == pytest.approx(90.2)
    assert actuals["nwc"] == pytest.approx(635.9)
    assert "shares" in {c.key for c in calculations}


def test_incomplete_or_wrong_signed_reported_population_fails() -> None:
    original = proof()
    with pytest.raises(InputEvidenceError, match="population"):
        onon_inputs.calculate_actuals(original.model_copy(update={"inputs": original.inputs[:-1]}))
    bad = tuple(
        item.model_copy(update={"value": 80.0}) if item.key == "ppe_purchases_fy" else item
        for item in original.inputs
    )
    with pytest.raises(InputEvidenceError, match="reported_cash_outflow_sign"):
        onon_inputs.calculate_actuals(original.model_copy(update={"inputs": bad}))


def test_missing_canonical_bindings_fail_before_snapshot_queries() -> None:
    request = proof().request.model_copy(update={"facts": {}})
    with (
        sqlite3.connect(":memory:") as conn,
        pytest.raises(InputEvidenceError, match="required_input_population"),
    ):
        onon_inputs.prepare_onon_inputs(conn, request, effective_inputs=memo_inputs(), as_of=NOW)


def test_receipt_replays_shared_verification_and_binds_full_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = proof()
    actuals, _ = onon_inputs.calculate_actuals(source)
    numeric = {
        **{
            key: value for key, value in memo_inputs().items() if key in onon_inputs.ASSUMPTION_KEYS
        },
        **{key: actuals[key] for key in onon_inputs.REPORTED_DRIVER_KEYS},
    }
    review = AssumptionReview(
        reviewed_at=NOW,
        reviewer="fixture",
        effective_inputs_sha256=canonical_digest(numeric),
        actuals_sha256=canonical_digest(actuals),
        drivers={
            key: DriverReview(
                observed=a,
                forecast=f,
                variance=f - a,
                rationale="Fixture accepts this explicitly modeled variance",
            )
            for key, (a, f) in onon_inputs.review_drivers(actuals, numeric).items()
        },
    )
    source = source.model_copy(
        update={"request": source.request.model_copy(update={"assumption_review": review})}
    )
    calls: list[dict[str, object]] = []

    def verified(
        conn: sqlite3.Connection, request: ModelInputRequest, **kwargs: object
    ) -> ModelInputReceipt:
        calls.append(kwargs)
        assert kwargs["requirements"] == onon_inputs.requirements_for(END)
        assert kwargs["recipe"] == onon_inputs.RECIPE
        return source.model_copy(update={"request": request})

    monkeypatch.setattr(onon_inputs, "verify_model_inputs", verified)
    with sqlite3.connect(":memory:") as conn:
        numeric, receipt = onon_inputs.prepare_onon_inputs(
            conn, source.request, effective_inputs=numeric, as_of=NOW
        )
        assert receipt.model_output_sha256 == canonical_digest(onon_inputs.model_output(numeric))
        assert onon_inputs.verify_onon_inputs(conn, receipt, effective_inputs=numeric, as_of=NOW)
        with pytest.raises(InputEvidenceError, match="receipt_mismatch"):
            onon_inputs.verify_onon_inputs(
                conn,
                receipt.model_copy(update={"model_output_sha256": "f" * 64}),
                effective_inputs=numeric,
                as_of=NOW,
            )
        assert len(calls) == 3


@pytest.mark.parametrize("key,value", [("shares", True), ("base_rent", float("inf"))])
def test_nonnumeric_effective_inputs_fail(key: str, value: object) -> None:
    with pytest.raises(InputEvidenceError, match="effective_input_missing_or_invalid"):
        onon_inputs.effective_numeric_inputs({**memo_inputs(), key: value})


def bridge_proof() -> tuple[dict[str, float], ModelInputReceipt]:
    source = proof()
    actuals, calculations = onon_inputs.calculate_actuals(source)
    values = {**memo_inputs(), **{key: actuals[key] for key in onon_inputs.REPORTED_DRIVER_KEYS}}
    return values, source.model_copy(
        update={
            "calculations": calculations,
            "actuals_sha256": canonical_digest(actuals),
            "effective_inputs_sha256": canonical_digest(values),
            "model_output_sha256": canonical_digest(onon_inputs.model_output(values)),
        }
    )


def test_canonical_bridge_has_exact_source_and_calculation_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values, receipt = bridge_proof()

    def verified(*args: object, **kwargs: object) -> ModelInputReceipt:
        return receipt

    monkeypatch.setattr(onon_inputs, "verify_onon_inputs", verified)
    with sqlite3.connect(":memory:") as conn:
        bridge = onon_inputs.build_onon_equity_bridge(
            conn, receipt, effective_inputs=values, as_of=NOW
        )
    assert bridge.status == "verified"
    assert bridge.debt_scope == "other_nonlease_financial_liabilities"
    assert bridge.total_debt_m == 52.0
    assert bridge.arithmetic_delta == pytest.approx(0, abs=1e-8)
    assert bridge.cash_m == pytest.approx(1204.7)
    assert bridge.diluted_shares_m == pytest.approx(336.1087012)
    assert bridge.cash_lineage is not None
    assert "fmp_field" not in bridge.cash_lineage
    assert bridge.bridge_context is not None
    assert bridge.bridge_context["lease_policy"] == "cash_rent_and_sbc_expensed_leases_excluded"
    assert bridge.bridge_context["model_input_receipt_sha256"] == canonical_digest(
        receipt.model_dump(mode="json")
    )


@pytest.mark.parametrize("key", ["cash", "shares", "financial_debt"])
def test_canonical_bridge_rejects_wrong_economic_operand(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    values, receipt = bridge_proof()

    def verified(*args: object, **kwargs: object) -> ModelInputReceipt:
        return receipt

    monkeypatch.setattr(onon_inputs, "verify_onon_inputs", verified)
    with (
        sqlite3.connect(":memory:") as conn,
        pytest.raises(InputEvidenceError, match="operand_mismatch"),
    ):
        onon_inputs.build_onon_equity_bridge(
            conn, receipt, effective_inputs={**values, key: values[key] + 1}, as_of=NOW
        )


@pytest.mark.parametrize(
    "update", [{"period_end": date(2025, 6, 30)}, {"unit_key": "USD"}, {"currency": "USD"}]
)
def test_canonical_bridge_rejects_wrong_reported_coordinate(
    monkeypatch: pytest.MonkeyPatch, update: dict[str, object]
) -> None:
    values, receipt = bridge_proof()
    bad = receipt.model_copy(
        update={
            "inputs": tuple(
                item.model_copy(update=update) if item.key == "reported_cash" else item
                for item in receipt.inputs
            )
        }
    )

    def verified(*args: object, **kwargs: object) -> ModelInputReceipt:
        return bad

    monkeypatch.setattr(onon_inputs, "verify_onon_inputs", verified)
    with (
        sqlite3.connect(":memory:") as conn,
        pytest.raises(InputEvidenceError, match="coordinate_mismatch"),
    ):
        onon_inputs.build_onon_equity_bridge(conn, bad, effective_inputs=values, as_of=NOW)


@pytest.mark.parametrize(
    "update,reason",
    [
        ({"source_reference": None}, "source_clock_required"),
        ({"source_as_of": date(2026, 10, 4)}, "source_clock_invalid"),
        ({"recorded_at": datetime(2026, 10, 4, tzinfo=UTC)}, "source_clock_invalid"),
    ],
)
def test_onon_assumption_source_clock_cannot_be_missing_or_future(
    monkeypatch: pytest.MonkeyPatch, update: dict[str, object], reason: str
) -> None:
    source = proof()
    assumptions = {
        **source.request.assumptions,
        "chf_per_usd": source.request.assumptions["chf_per_usd"].model_copy(update=update),
    }
    source = source.model_copy(
        update={"request": source.request.model_copy(update={"assumptions": assumptions})}
    )

    def verified(*args: object, **kwargs: object) -> ModelInputReceipt:
        return source

    monkeypatch.setattr(onon_inputs, "verify_model_inputs", verified)
    actuals, _ = onon_inputs.calculate_actuals(source)
    values = {**memo_inputs(), **{key: actuals[key] for key in onon_inputs.REPORTED_DRIVER_KEYS}}
    with sqlite3.connect(":memory:") as conn, pytest.raises(InputEvidenceError, match=reason):
        onon_inputs.prepare_onon_inputs(conn, source.request, effective_inputs=values, as_of=NOW)


def test_canonical_nonlease_liability_scope_cannot_use_legacy_debt_fields() -> None:
    from dcf.equity_bridge import resolve_debt_scope

    assert (
        resolve_debt_scope(
            {"totalDebt": 52, "financeLeaseLiability": 0},
            scope="other_nonlease_financial_liabilities",
        )
        is None
    )
