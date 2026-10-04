"""Canonical receipt bodies participate in the specialized model input hash."""

from pathlib import Path

import pytest

from dcf.provenance import DcfInputProvenance, EquityDirectValuationArchetype, build_file_provenance


def provenance(
    *,
    equity_bridge_receipt: dict[str, object] | None = None,
    scenario_acceptance: dict[str, object] | None = None,
    equity_direct_archetype: EquityDirectValuationArchetype | None = None,
) -> DcfInputProvenance:
    return build_file_provenance(
        ticker="ONON",
        repo_root=Path("/nonexistent"),
        workbook_path=Path("/nonexistent/ONON.xlsx"),
        engine_version="onon_economic_fcff_v1",
        effective_inputs={"cash": 1204.7},
        assumption_snapshot={"model": "onon_economic_fcff"},
        live_price=30.85,
        live_price_at=None,
        live_price_source="analyst_fixture",
        source_files=(),
        equity_bridge_receipt=equity_bridge_receipt,
        scenario_acceptance=scenario_acceptance,
        equity_direct_archetype=equity_direct_archetype,
    )


def test_new_receipt_parameters_preserve_default_hash() -> None:
    assert (
        provenance().input_sha256
        == "d0f842fce4f5d1b99670317c6cec3c5d366ad43799898b4fba612300cdf06781"
    )
    assert (
        provenance().input_sha256
        == provenance(equity_bridge_receipt=None, scenario_acceptance=None).input_sha256
    )


@pytest.mark.parametrize("field", ["equity_bridge_receipt", "scenario_acceptance"])
def test_canonical_receipt_mutation_changes_model_input_commitment(field: str) -> None:
    first = provenance(
        equity_bridge_receipt={"fixture_commitment": "a"}
        if field == "equity_bridge_receipt"
        else None,
        scenario_acceptance={"fixture_commitment": "a"} if field == "scenario_acceptance" else None,
    )
    second = provenance(
        equity_bridge_receipt={"fixture_commitment": "b"}
        if field == "equity_bridge_receipt"
        else None,
        scenario_acceptance={"fixture_commitment": "b"} if field == "scenario_acceptance" else None,
    )
    assert first.input_sha256 != second.input_sha256
    assert first.detail is not None
    assert first.detail[field] == {"fixture_commitment": "a"}


def test_canonical_bridge_cannot_be_combined_with_equity_direct_bypass() -> None:
    with pytest.raises(ValueError, match="equity_bridge_authority_conflict"):
        provenance(
            equity_bridge_receipt={"status": "verified"}, equity_direct_archetype="platform_sotp"
        )


def test_existing_equity_direct_receipt_branch_is_preserved() -> None:
    result = provenance(equity_direct_archetype="platform_sotp")
    assert result.detail is not None
    assert result.detail["equity_bridge_receipt"] == {
        "schema_version": "dcf_equity_bridge_receipt.v3",
        "status": "not_applicable",
        "reason_code": "equity_direct_valuation",
        "valuation_scope": "equity",
        "valuation_archetype": "platform_sotp",
    }
