"""Synthetic financial parity through the configured real spreadsheet engine."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import openpyxl
import pytest

from dcf.input_evidence import ModelInputReceipt, ModelInputRequest, canonical_digest
from dcf.meli_inputs import FINANCING_RECIPE
from dcf.scenario_acceptance import ScenarioAcceptance, ScenarioReview
from execution import build_meli_platform_dcf as meli


def synthetic_cases() -> dict[str, meli.Assum]:
    base = meli.Assum(derive_capm=0, credit_terminal_roe=0.4992, net_cash=1000, price=1000)
    return {
        "base": base,
        "bear": replace(base, comm_g_near=0.12, nimal_near=0.15, wacc=0.15, credit_ke=0.18),
        "bull": replace(base, comm_g_near=0.30, nimal_near=0.20, wacc=0.12, credit_ke=0.15),
    }


def runtime_or_skip() -> meli.WorkbookRuntime:
    if not os.environ.get("DCF_ARTIFACT_NODE") and not os.environ.get("DCF_ARTIFACT_NODE_MODULES"):
        pytest.skip("Real artifact engine requires explicit loader-supplied runtime paths")
    return meli.WorkbookRuntime.from_environment()


@pytest.mark.parametrize("key", ["comm_rev0", "fpay_rev0", "cb0", "shares", "net_cash"])
def test_case_cannot_change_reported_or_reviewed_baseline(key: str) -> None:
    cases = synthetic_cases()
    base = cases["base"]
    cases["bear"] = replace(cases["bear"], **{key: getattr(base, key) + 1})
    with pytest.raises(meli.InputEvidenceError, match="case_reported_anchor_changed"):
        meli.workbook_spec(base, meli.mirror(base), cases=cases)


def test_missing_runtime_preserves_prior_workbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DCF_ARTIFACT_NODE", raising=False)
    monkeypatch.delenv("DCF_ARTIFACT_NODE_MODULES", raising=False)
    target = tmp_path / "existing.xlsx"
    target.write_bytes(b"prior workbook")
    cases = synthetic_cases()
    base = cases["base"]
    with pytest.raises(meli.InputEvidenceError, match="runtime_not_configured"):
        meli.build(base, meli.mirror(base), target, cases=cases)
    assert target.read_bytes() == b"prior workbook"


def synthetic_scenario_transport(
    tmp_path: Path,
    *,
    recipe: str = meli.RECIPE,
) -> tuple[dict[str, meli.Assum], Path, ModelInputReceipt, Path]:
    """Transport commitments only; this fixture is not admitted source evidence."""
    cases = synthetic_cases()
    if recipe == FINANCING_RECIPE:
        cases = {
            name: replace(case, financing_other_expenses_reserve=5.0)
            for name, case in cases.items()
        }
    effective = meli.effective_numeric_inputs(meli.asdict(cases["base"]), recipe=recipe)
    vectors = {
        name: meli.effective_numeric_inputs(meli.asdict(case), recipe=recipe)
        for name, case in cases.items()
    }
    request = ModelInputRequest(
        recipe=recipe,
        ticker="MELI",
        research_snapshot_id="synthetic",
        financial_period_end=date(2026, 6, 30),
        facts={},
        assumptions={},
    )
    source = tmp_path / "assumptions.json"
    source.write_text(
        json.dumps(
            {"input_evidence": request.model_dump(mode="json"), "reviewed_scenario_inputs": vectors}
        )
    )
    receipt = ModelInputReceipt(
        recipe=recipe,
        request=request,
        verified_at=datetime(2026, 10, 18, tzinfo=UTC),
        snapshot_member_sha256="a" * 64,
        effective_inputs_sha256=canonical_digest(effective),
        required_keys=(),
        inputs=(),
        inventory_snapshot_ids=("synthetic",),
        actuals_sha256="b" * 64,
        model_output_sha256=canonical_digest(meli.model_output(effective)),
        assumptions_source_path=str(source.resolve()),
        assumptions_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    output = meli.replay_model_scenarios(
        "MELI",
        effective,
        meli.model_output(effective),
        {"reviewed_scenario_inputs": vectors},
        recipe=recipe,
    )
    scenario_outputs = output["scenarios"]
    assert isinstance(scenario_outputs, dict)
    verified_outputs = cast(dict[str, object], scenario_outputs)
    acceptance = ScenarioAcceptance(
        ticker="MELI",
        recipe=recipe,
        model="meli_platform_sotp",
        engine_version="meli_platform_sotp_v1",
        reviewer="synthetic transport fixture",
        attribution="analyst",
        reviewed_at=receipt.verified_at,
        model_input_receipt_sha256=canonical_digest(receipt.model_dump(mode="json")),
        effective_inputs_sha256=canonical_digest(effective),
        actuals_sha256="b" * 64,
        snapshot_member_sha256=receipt.snapshot_member_sha256,
        inventory_snapshot_ids=receipt.inventory_snapshot_ids,
        model_output_sha256=canonical_digest(output),
        scenarios={
            name: ScenarioReview(
                output_sha256=canonical_digest(value),
                accepted=True,
                rationale="Synthetic exact-vector transport test; no admission claim.",
            )
            for name, value in verified_outputs.items()
        },
    )
    acceptance_path = tmp_path / "acceptance.json"
    acceptance_path.write_text(acceptance.model_dump_json())
    return cases, source, receipt, acceptance_path


@pytest.mark.parametrize("recipe", [meli.RECIPE, FINANCING_RECIPE])
def test_separate_acceptance_and_retained_receipt_commit_without_hash_cycle(
    tmp_path: Path, recipe: str
) -> None:
    cases, source, receipt, acceptance = synthetic_scenario_transport(tmp_path, recipe=recipe)
    loaded, review = meli.load_reviewed_cases(
        cases["base"],
        source,
        receipt,
        scenario_acceptance_path=acceptance,
        expected_sha256=hashlib.sha256(acceptance.read_bytes()).hexdigest(),
    )
    assert loaded == cases
    assert review.model_input_receipt_sha256 == canonical_digest(receipt.model_dump(mode="json"))
    retained = tmp_path / "retained.json"
    retained.write_text(receipt.model_dump_json())
    assert (
        meli.load_retained_input_receipt(
            retained, hashlib.sha256(retained.read_bytes()).hexdigest()
        )
        == receipt
    )
    with pytest.raises(meli.InputEvidenceError, match="changed_after_dispatch"):
        meli.load_retained_input_receipt(retained, "0" * 64)


def test_tampered_scenario_authorities_fail_closed(tmp_path: Path) -> None:
    cases, source, receipt, acceptance = synthetic_scenario_transport(tmp_path)
    with pytest.raises(meli.InputEvidenceError, match="acceptance_changed_after_dispatch"):
        meli.load_reviewed_cases(
            cases["base"],
            source,
            receipt,
            scenario_acceptance_path=acceptance,
            expected_sha256="0" * 64,
        )
    source.write_text(source.read_text() + " ")
    with pytest.raises(meli.InputEvidenceError, match="assumptions_authority_changed"):
        meli.load_reviewed_cases(
            cases["base"], source, receipt, scenario_acceptance_path=acceptance
        )


def test_explicit_projection_uses_same_vectors_as_native_replay() -> None:
    cases = synthetic_cases()
    base = cases["base"]
    block = meli.scenarios_block(base, meli.mirror(base), None, cases=cases)
    effective = meli.effective_numeric_inputs(meli.asdict(base))
    result = meli.replay_model_scenarios(
        "MELI",
        effective,
        meli.model_output(effective),
        {
            "reviewed_scenario_inputs": {
                name: meli.effective_numeric_inputs(meli.asdict(case))
                for name, case in cases.items()
            },
            "scenarios": block,
        },
    )
    assert result["base_model_output"] == meli.model_output(effective)


def test_financing_vector_cannot_fill_reserve_from_model_default(tmp_path: Path) -> None:
    cases, source, receipt, acceptance = synthetic_scenario_transport(
        tmp_path, recipe=FINANCING_RECIPE
    )
    payload = json.loads(source.read_text())
    del payload["reviewed_scenario_inputs"]["bear"]["financing_other_expenses_reserve"]
    source.write_text(json.dumps(payload))
    changed = receipt.model_copy(
        update={"assumptions_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    )
    with pytest.raises(meli.InputEvidenceError, match="vector_population_invalid"):
        meli.load_reviewed_cases(
            cases["base"], source, changed, scenario_acceptance_path=acceptance
        )


def test_v4_parameter_identity_does_not_gain_v5_reserve() -> None:
    base = synthetic_cases()["base"]
    assert "financing_other_expenses_reserve" not in meli.model_parameters(base, meli.RECIPE)
    assert meli.model_parameters(base, FINANCING_RECIPE)["financing_other_expenses_reserve"] == 0


def test_financing_workbook_preserves_explicit_bridge_input(tmp_path: Path) -> None:
    runtime = runtime_or_skip()
    cases, _, receipt, _ = synthetic_scenario_transport(tmp_path, recipe=FINANCING_RECIPE)
    base = cases["base"]
    path = tmp_path / "financing.xlsx"
    meli.build(base, meli.mirror(base), path, cases=cases, runtime=runtime, input_receipt=receipt)
    actuals, _ = cached_cells(path, 6)
    assert actuals["B41"] == 5.0
    recalculation = json.loads(path.with_suffix(".xlsx.recalculation.json").read_text())
    assert recalculation["financial_acceptance"] == "not_verified"
    cases["bear"] = replace(cases["bear"], financing_other_expenses_reserve=6.0)
    with pytest.raises(meli.InputEvidenceError, match="case_reported_anchor_changed"):
        meli.workbook_spec(base, meli.mirror(base), cases=cases, input_receipt=receipt)


def cached_cells(path: Path, sheet: int) -> tuple[dict[str, float], ET.Element]:
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(path) as archive:
        tree = ET.fromstring(archive.read(f"xl/worksheets/sheet{sheet}.xml"))
    cells: dict[str, float] = {}
    for cell in tree.findall(".//s:c", ns):
        value = cell.find("s:v", ns)
        if (
            value is not None
            and value.text is not None
            and cell.attrib.get("t") not in {"s", "str", "e"}
        ):
            cells[cell.attrib["r"]] = float(value.text)
    return cells, tree


@pytest.mark.parametrize("years", [1, 10])
def test_real_recalculation_and_saved_native_tables(tmp_path: Path, years: int) -> None:
    runtime = runtime_or_skip()
    cases = {name: replace(case, years=years) for name, case in synthetic_cases().items()}
    base = cases["base"]
    path = tmp_path / "synthetic.xlsx"
    meli.build(base, meli.mirror(base), path, cases=cases, runtime=runtime)
    receipt = json.loads(path.with_suffix(".xlsx.recalculation.json").read_text())
    assert receipt["status"] == "verified"
    assert receipt["financial_acceptance"] == "not_verified"
    assert receipt["native_excel_verified"] is False
    assert receipt["after_reopen"]["checked"] > 30
    cells, tree = cached_cells(path, 1)
    for address, name in (("E48", "base"), ("F48", "bear"), ("G48", "bull")):
        assert cells[address] == pytest.approx(meli.mirror(cases[name]).vps, rel=1e-9)
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    tables = [node.attrib for node in tree.findall(".//s:f[@t='dataTable']", ns)]
    assert tables == [
        {"t": "dataTable", "ref": "E47:G49", "dt2D": "1", "r1": "B44", "r2": "B32"},
        {"t": "dataTable", "ref": "E55:G57", "dt2D": "1", "r1": "D19", "r2": "D27"},
    ]
    scenario_cells, scenario_tree = cached_cells(path, 5)
    for address, name, col in (("E3", "bear", "F"), ("E4", "base", "E"), ("E5", "bull", "G")):
        expected = meli.mirror(cases[name]).vps
        assert scenario_cells[address] == pytest.approx(expected, rel=1e-9)
        formula = scenario_tree.find(f".//s:c[@r='{address}']/s:f", ns)
        assert formula is not None
        assert formula.text == f"'Dashboard'!{col}48"
        for stage in ("before_export", "after_reopen"):
            assert receipt[stage]["outputs"][f"Scenario!{address}"] == pytest.approx(
                expected, rel=1e-9
            )
            assert (
                receipt[stage]["bear_provenance"]
                == "Explicit vectors; acceptance recorded separately"
            )
    saved = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        assert saved["Scenario"]["A6"].value == "Bear provenance"
        assert saved["Scenario"]["B6"].value == "Explicit vectors; acceptance recorded separately"
    finally:
        saved.close()
    actuals, _ = cached_cells(path, 6)
    assert actuals["B32"] == base.shares


def test_reopened_workbook_independent_and_combined_edits(tmp_path: Path) -> None:
    runtime = runtime_or_skip()
    cases = synthetic_cases()
    base = cases["base"]
    path = tmp_path / "synthetic.xlsx"
    meli.build(base, meli.mirror(base), path, cases=cases, runtime=runtime)
    requests: list[dict[str, object]] = []
    for label, growth, nimal, wacc, ke, denominator, active in (
        ("growth", 0.05, 0, None, None, base.shares, "base"),
        ("credit_margin", 0, 0.02, None, None, base.shares, "base"),
        ("wacc", 0, 0, 0.16, None, base.shares, "base"),
        ("credit_ke", 0, 0, None, 0.20, base.shares, "base"),
        ("valuation_shares", 0, 0, None, None, base.shares * 1.1, "base"),
        ("combined_and_case_selection", 0.05, 0.02, 0.16, 0.20, base.shares * 1.1, "bear"),
    ):
        changed = {
            name: replace(
                case, shares=denominator, wacc=wacc or case.wacc, credit_ke=ke or case.credit_ke
            )
            for name, case in cases.items()
        }
        changed["base"] = replace(
            changed["base"],
            comm_g_near=base.comm_g_near + growth,
            nimal_near=base.nimal_near + nimal,
        )
        edits: list[list[object]] = [
            ["Assumptions", "B4", base.comm_g_near + growth],
            ["Assumptions", "B23", base.nimal_near + nimal],
            ["Dashboard", "B32", denominator],
            ["Dashboard", "B44", 2 if active == "bear" else 1],
        ]
        if wacc is not None:
            edits.append(["Dashboard", "D19", wacc])
        if ke is not None:
            edits.append(["Dashboard", "D27", ke])
        expected = {
            f"Dashboard!{col}48": meli.mirror(changed[name]).vps
            for col, name in (("E", "base"), ("F", "bear"), ("G", "bull"))
        }
        for row, name in ((3, "bear"), (4, "base"), (5, "bull")):
            expected[f"Scenario!E{row}"] = meli.mirror(changed[name]).vps
        expected["Dashboard!B40"] = meli.mirror(changed[active]).vps
        for col, dw in (("E", -0.01), ("F", 0), ("G", 0.01)):
            for row, dk in ((55, -0.01), (56, 0), (57, 0.01)):
                selected = changed[active]
                expected[f"Dashboard!{col}{row}"] = meli.mirror(
                    replace(selected, wacc=selected.wacc + dw, credit_ke=selected.credit_ke + dk)
                ).vps
        requests.append({"label": label, "edits": edits, "expected": expected})
    (tmp_path / "requests.json").write_text(json.dumps(requests))
    (tmp_path / "node_modules").symlink_to(runtime.node_modules, target_is_directory=True)
    shutil.copyfile(
        Path(meli.__file__).with_name("meli_workbook_adapter.mjs"), tmp_path / "adapter.mjs"
    )
    script = tmp_path / "edits.mjs"
    script.write_text(
        'import fs from "node:fs/promises";\n'
        'import {FileBlob,SpreadsheetFile} from "@oai/artifact-tool";\n'
        'import {recalculateAndVerify} from "./adapter.mjs";\n'
        'const requests=JSON.parse(await fs.readFile("requests.json","utf8"));\n'
        "const receipts=[];\n"
        "for(const request of requests){\n"
        ' const wb=await SpreadsheetFile.importXlsx(await FileBlob.load("synthetic.xlsx"));\n'
        ' const actuals=JSON.stringify(wb.worksheets.getItem("Actuals").getUsedRange().values);\n'
        " for(const [name,address,value] of request.edits) wb.worksheets.getItem(name).getRange(address).values=[[value]];\n"
        ' const result=recalculateAndVerify(wb,request.expected,"Explicit vectors; acceptance recorded separately");\n'
        ' if(actuals!==JSON.stringify(wb.worksheets.getItem("Actuals").getUsedRange().values)) throw new Error("actuals_changed");\n'
        ' await(await SpreadsheetFile.exportXlsx(wb)).save("edited.xlsx");\n'
        ' const reopened=await SpreadsheetFile.importXlsx(await FileBlob.load("edited.xlsx"));\n'
        ' recalculateAndVerify(reopened,request.expected,"Explicit vectors; acceptance recorded separately");\n'
        " receipts.push({label:request.label,...result});\n"
        "}\n"
        'await fs.writeFile("edit-receipts.json",JSON.stringify(receipts));\n'
    )
    result = subprocess.run(
        [str(runtime.node), str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    receipts = json.loads((tmp_path / "edit-receipts.json").read_text())
    assert len(receipts) == 6
    final, _ = cached_cells(tmp_path / "edited.xlsx", 1)
    expected_final = requests[-1]["expected"]
    assert isinstance(expected_final, dict)
    verified_expected = cast(dict[str, float], expected_final)
    scenario_final, _ = cached_cells(tmp_path / "edited.xlsx", 5)
    for address, expected in verified_expected.items():
        sheet, cell = address.split("!")
        cached = scenario_final if sheet == "Scenario" else final
        assert cached[cell] == pytest.approx(expected, rel=1e-9)
    saved = openpyxl.load_workbook(tmp_path / "edited.xlsx", read_only=True, data_only=True)
    try:
        assert saved["Scenario"]["A6"].value == "Bear provenance"
        assert saved["Scenario"]["B6"].value == "Explicit vectors; acceptance recorded separately"
    finally:
        saved.close()
    actuals_final, _ = cached_cells(tmp_path / "edited.xlsx", 6)
    assert actuals_final["B32"] == base.shares
