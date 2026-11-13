from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import cast

import openpyxl
import pytest

from dcf import equity_bridge, redesign, redesign_evidence, redesign_inputs
from dcf.input_evidence import InputEvidenceError, ModelInputReceipt
from tests.test_redesign_input_evidence import BASE, CLOCK, synthetic_redesign_proof


@pytest.fixture
def receipt(monkeypatch: pytest.MonkeyPatch) -> ModelInputReceipt:
    request, inputs = synthetic_redesign_proof(monkeypatch)
    with sqlite3.connect(":memory:") as conn:
        _, verified = redesign_inputs.prepare_redesign_inputs(
            conn, request, effective_inputs=inputs, as_of=CLOCK
        )
    return verified


def test_workbook_receipt_roundtrip_and_replay(tmp_path: Path, receipt: ModelInputReceipt) -> None:
    path = tmp_path / "model.xlsx"
    workbook = openpyxl.Workbook()
    redesign_evidence.write_workbook_receipt(workbook, receipt)
    assert workbook["Model Input Receipt"].sheet_state == "hidden"
    workbook.save(path)
    workbook.close()
    assert redesign_evidence.read_workbook_receipt(path) == receipt
    with sqlite3.connect(":memory:") as conn:
        assert redesign_evidence.verify_workbook_receipt(conn, path, BASE, as_of=CLOCK) == receipt
        changed = dataclasses.replace(BASE, cash_m=BASE.cash_m + 1)
        with pytest.raises(
            InputEvidenceError, match="workbook_model_input_receipt_display_mismatch"
        ):
            redesign_evidence.verify_workbook_receipt(conn, path, changed, as_of=CLOCK)


def test_absent_receipt_is_distinct_from_invalid_sheet(tmp_path: Path) -> None:
    path = tmp_path / "old.xlsx"
    workbook = openpyxl.Workbook()
    workbook.save(path)
    assert redesign_evidence.read_workbook_receipt(path) is None
    workbook.create_sheet("Model Input Receipt")
    workbook.save(path)
    workbook.close()
    with pytest.raises(InputEvidenceError, match="workbook_model_input_receipt"):
        redesign_evidence.read_workbook_receipt(path)


@pytest.mark.parametrize("damage", ["order", "digest", "json", "oversize", "recipe", "schema"])
def test_present_invalid_receipt_cannot_fall_back(
    tmp_path: Path, receipt: ModelInputReceipt, damage: str
) -> None:
    path = tmp_path / "invalid.xlsx"
    workbook = openpyxl.Workbook()
    redesign_evidence.write_workbook_receipt(workbook, receipt)
    sheet = workbook["Model Input Receipt"]
    if damage == "order":
        sheet.cell(2, 1, 1)
    elif damage == "digest":
        sheet.cell(1, 3, "0" * 64)
    elif damage == "oversize":
        sheet.cell(1, 2, 262_145)
    else:
        payload = receipt.model_dump(mode="json")
        if damage == "recipe":
            payload["recipe"] = "other_model.v1"
        elif damage == "schema":
            payload["schema_version"] = "dcf_model_inputs.v2"
        encoded = "not JSON" if damage == "json" else json.dumps(payload, separators=(",", ":"))
        sheet.cell(1, 2, len(encoded.encode()))
        sheet.cell(1, 3, hashlib.sha256(encoded.encode()).hexdigest())
        sheet.delete_rows(2, sheet.max_row)
        for index, offset in enumerate(range(0, len(encoded), 16_000)):
            sheet.append([index, encoded[offset : offset + 16_000]])
    workbook.save(path)
    workbook.close()
    with pytest.raises(InputEvidenceError, match="workbook_model_input_receipt"):
        redesign_evidence.read_workbook_receipt(path)


def test_snapshot_replay_keeps_owner_settings_and_rejects_tampered_input(
    receipt: ModelInputReceipt,
) -> None:
    flat = redesign_inputs.serialize_flat_inputs(BASE)
    output = redesign_inputs.model_output(flat)
    values = output["scenarios"]
    assert isinstance(values, dict)
    original = {
        "owner_note": "Keep current assumptions",
        "wacc": BASE.wacc,
        "scenarios": {
            name: {
                "fair_value_per_share_usd": value,
                "deltas": {"margin": 0.02},
                "bear_provenance": "owner",
            }
            for name, value in cast("dict[str, float]", values).items()
        },
    }
    snapshot = json.loads(
        redesign_evidence.snapshot_with_receipt(json.dumps(original), BASE, receipt)
    )
    flat = redesign_inputs.serialize_flat_inputs(BASE)
    assert snapshot["owner_note"] == original["owner_note"]
    assert snapshot["wacc"] == original["wacc"]
    assert snapshot["model"] == "redesign_fcff"
    assert snapshot["effective_model_inputs"] == flat
    assert snapshot["model_output"] == redesign_inputs.model_output(flat)
    assert snapshot["scenarios"] == original["scenarios"]
    assert "scenario_display" not in snapshot
    assert (
        json.loads(redesign_evidence.snapshot_with_receipt(json.dumps(snapshot), BASE, receipt))
        == snapshot
    )
    with pytest.raises(InputEvidenceError, match="workbook_model_input_receipt"):
        redesign_evidence.snapshot_with_receipt(
            json.dumps(original), dataclasses.replace(BASE, near_op_margin=0.5), receipt
        )


def test_oversized_receipt_cannot_replace_existing_proof(receipt: ModelInputReceipt) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.create_sheet("Model Input Receipt")
    sheet.append(["existing proof"])
    oversized = receipt.model_copy(update={"assumptions_source_path": "x" * 262_144})
    with pytest.raises(InputEvidenceError, match="workbook_model_input_receipt_byte_limit"):
        redesign_evidence.write_workbook_receipt(workbook, oversized)
    assert workbook["Model Input Receipt"]["A1"].value == "existing proof"
    workbook.close()


def test_workbook_verifier_rejects_tampered_calculation_receipt(
    tmp_path: Path, receipt: ModelInputReceipt
) -> None:
    path = tmp_path / "tampered.xlsx"
    workbook = openpyxl.Workbook()
    redesign_evidence.write_workbook_receipt(
        workbook, receipt.model_copy(update={"model_output_sha256": "0" * 64})
    )
    workbook.save(path)
    workbook.close()
    with (
        sqlite3.connect(":memory:") as conn,
        pytest.raises(InputEvidenceError, match="model_input_receipt_mismatch"),
    ):
        redesign_evidence.verify_workbook_receipt(conn, path, BASE, as_of=CLOCK)


def test_excel_display_rounding_preserves_exact_sealed_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openpyxl.compat.strings import safe_string

    from dcf import redesign

    precise = dataclasses.replace(BASE, near_op_margin=0.12345678901234567)
    displayed = dataclasses.replace(
        precise,
        near_op_margin=float(safe_string(precise.near_op_margin)),
        wacc=redesign.derive_wacc(
            risk_free_rate=precise.risk_free_rate,
            beta=precise.beta,
            equity_risk_premium=precise.equity_risk_premium,
            country_risk_premium=precise.country_risk_premium,
            cost_of_debt=precise.cost_of_debt,
            tax_rate=precise.tax_rate,
            current_price=precise.current_price,
            diluted_shares_m=precise.diluted_shares_m,
            total_debt_m=precise.total_debt_m,
        ),
    )
    precise = dataclasses.replace(precise, wacc=displayed.wacc)
    request, inputs = synthetic_redesign_proof(monkeypatch, model_inputs=precise)
    with sqlite3.connect(":memory:") as conn:
        _, receipt = redesign_inputs.prepare_redesign_inputs(
            conn, request, effective_inputs=inputs, as_of=CLOCK
        )
    assert displayed.near_op_margin != precise.near_op_margin
    displayed_output = redesign_inputs.model_output(
        redesign_inputs.serialize_flat_inputs(displayed)
    )
    displayed_scenarios = cast("dict[str, float]", displayed_output["scenarios"])
    sealed_scenarios = cast("dict[str, float]", redesign_inputs.model_output(inputs)["scenarios"])
    assert displayed_scenarios != sealed_scenarios
    rich_scenarios = {
        name: {"fair_value_per_share_usd": value, "deltas": {"margin": 0.02}, "provenance": "owner"}
        for name, value in displayed_scenarios.items()
    }
    snapshot = json.loads(
        redesign_evidence.snapshot_with_receipt(
            json.dumps({"scenarios": rich_scenarios}), displayed, receipt
        )
    )
    assert snapshot["effective_model_inputs"]["near_op_margin"] == precise.near_op_margin
    assert snapshot["model_output"] == redesign_inputs.model_output(inputs)
    assert {
        name: entry["fair_value_per_share_usd"] for name, entry in snapshot["scenarios"].items()
    } == sealed_scenarios
    assert all(
        entry["deltas"] == {"margin": 0.02} and entry["provenance"] == "owner"
        for entry in snapshot["scenarios"].values()
    )
    direction = math.inf if displayed_scenarios["bull"] > sealed_scenarios["bull"] else -math.inf
    rich_scenarios["bull"]["fair_value_per_share_usd"] = math.nextafter(
        displayed_scenarios["bull"], direction
    )
    with pytest.raises(InputEvidenceError, match="workbook_model_input_receipt_scenarios_mismatch"):
        redesign_evidence.snapshot_with_receipt(
            json.dumps({"scenarios": rich_scenarios}), displayed, receipt
        )


@pytest.mark.parametrize("damage", ["value", "extra", "missing", "boolean", "nonfinite", "mixed"])
def test_snapshot_rejects_corrupt_scenario_projection(
    receipt: ModelInputReceipt, damage: str
) -> None:
    output = redesign_inputs.model_output(redesign_inputs.serialize_flat_inputs(BASE))
    values = output["scenarios"]
    assert isinstance(values, dict)
    numeric_values = cast("dict[str, float]", values)
    scenarios: dict[str, object] = {
        name: {"fair_value_per_share_usd": value} for name, value in numeric_values.items()
    }
    if damage == "value":
        scenarios["bull"] = {"fair_value_per_share_usd": numeric_values["bull"] + 1}
    elif damage == "extra":
        scenarios["other"] = {"fair_value_per_share_usd": 1}
    elif damage == "boolean":
        scenarios["base"] = {"fair_value_per_share_usd": True}
    elif damage == "nonfinite":
        scenarios["base"] = {"fair_value_per_share_usd": float("nan")}
    elif damage == "mixed":
        scenarios["bull"] = numeric_values["bull"]
    else:
        scenarios.pop("bear")
    with pytest.raises(InputEvidenceError, match="workbook_model_input_receipt_scenarios_mismatch"):
        redesign_evidence.snapshot_with_receipt(json.dumps({"scenarios": scenarios}), BASE, receipt)


def test_native_debt_operand_arithmetic_matches_workbook_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openpyxl.compat.strings import safe_string

    native_debt = Decimal("5161619741.549224")
    native_lease = Decimal("40088479.60759101")
    reported_debt = float(native_debt / Decimal(1_000_000))
    reported_lease = float(native_lease / Decimal(1_000_000))
    model_resolution = equity_bridge.resolve_debt_scope(
        {"totalDebt": reported_debt, "financeLeaseLiability": reported_lease},
        scope="interest_bearing_debt_only",
    )
    native_resolution = equity_bridge.resolve_debt_scope(
        {"totalDebt": float(native_debt), "financeLeaseLiability": float(native_lease)},
        scope="interest_bearing_debt_only",
    )
    assert model_resolution is not None and native_resolution is not None
    sealed_debt = model_resolution.value
    physical_debt = float(safe_string(native_resolution.value / 1_000_000))
    assert physical_debt != float(safe_string(sealed_debt))
    precise = dataclasses.replace(BASE, total_debt_m=sealed_debt)
    request, inputs = synthetic_redesign_proof(
        monkeypatch,
        model_inputs=precise,
        reported_bridge_values={
            "reported_cash_m": BASE.cash_m,
            "reported_total_debt_m": reported_debt,
            "reported_finance_lease_liability_m": reported_lease,
        },
    )
    with sqlite3.connect(":memory:") as conn:
        _, receipt = redesign_inputs.prepare_redesign_inputs(
            conn, request, effective_inputs=inputs, as_of=CLOCK
        )
    native = {
        "reported_cash_m": str(Decimal(str(BASE.cash_m)) * 1_000_000),
        "reported_total_debt_m": str(native_debt),
        "reported_finance_lease_liability_m": str(native_lease),
    }
    receipt = receipt.model_copy(
        update={
            "inputs": tuple(
                item.model_copy(
                    update={"native_value": native[item.key], "scale_factor": "0.000001"}
                )
                if item.key in native
                else item
                for item in receipt.inputs
            )
        }
    )
    displayed = dataclasses.replace(
        precise,
        total_debt_m=physical_debt,
        wacc=redesign.derive_wacc(
            risk_free_rate=precise.risk_free_rate,
            beta=precise.beta,
            equity_risk_premium=precise.equity_risk_premium,
            country_risk_premium=precise.country_risk_premium,
            cost_of_debt=precise.cost_of_debt,
            tax_rate=precise.tax_rate,
            current_price=precise.current_price,
            diluted_shares_m=precise.diluted_shares_m,
            total_debt_m=physical_debt,
        ),
    )
    snapshot = json.loads(redesign_evidence.snapshot_with_receipt("{}", displayed, receipt))
    assert snapshot["effective_model_inputs"]["total_debt_m"] == sealed_debt
    assert snapshot["model_output"] == redesign_inputs.model_output(inputs)
    with pytest.raises(InputEvidenceError, match="workbook_model_input_receipt_display_mismatch"):
        redesign_evidence.snapshot_with_receipt(
            "{}", dataclasses.replace(displayed, cash_m=BASE.cash_m + 1), receipt
        )


@pytest.mark.parametrize(
    ("source_key", "native_text"),
    [
        ("revenue_0", "4958626601.577125"),
        ("depreciation_and_amortization_0", "49586266.01577125"),
        ("diluted_shares_m", "49586266.01577125"),
    ],
)
def test_native_statement_cell_rounding_preserves_sealed_inputs(
    receipt: ModelInputReceipt, source_key: str, native_text: str
) -> None:
    from openpyxl.compat.strings import safe_string

    native = Decimal(native_text)
    reported = float(native / Decimal(1_000_000))
    physical = float(safe_string(float(native) / 1_000_000))
    assert physical != float(safe_string(reported))
    receipt = receipt.model_copy(
        update={
            "inputs": tuple(
                item.model_copy(
                    update={
                        "value": reported,
                        "native_value": native_text,
                        "scale_factor": "0.000001",
                    }
                )
                if item.key == source_key
                else item
                for item in receipt.inputs
            )
        }
    )
    flat = redesign_inputs.effective_inputs_from_receipt(receipt)
    output = redesign_inputs.model_output(flat)
    receipt = receipt.model_copy(
        update={
            "effective_inputs_sha256": redesign_inputs.canonical_digest(flat),
            "model_output_sha256": redesign_inputs.canonical_digest(output),
        }
    )
    displayed_values = {key: float(safe_string(value)) for key, value in flat.items()}
    source_values = {item.key: float(safe_string(item.value)) for item in receipt.inputs}
    source_values[source_key] = physical
    displayed_values["segment_0_revenue_m"] = source_values["revenue_0"]
    displayed_values["da_ratio"] = (
        source_values["depreciation_and_amortization_0"] / source_values["revenue_0"]
    )
    displayed_values["diluted_shares_m"] = source_values["diluted_shares_m"]
    displayed_values["wacc"] = redesign.derive_wacc(
        **{
            key: displayed_values[key]
            for key in (
                "risk_free_rate",
                "beta",
                "equity_risk_premium",
                "country_risk_premium",
                "cost_of_debt",
                "tax_rate",
                "current_price",
                "diluted_shares_m",
                "total_debt_m",
            )
        }
    )
    displayed = redesign_inputs.reconstruct_inputs(displayed_values, segments=BASE.segments)
    snapshot = json.loads(redesign_evidence.snapshot_with_receipt("{}", displayed, receipt))
    assert snapshot["effective_model_inputs"] == flat
    assert snapshot["model_output"] == output
