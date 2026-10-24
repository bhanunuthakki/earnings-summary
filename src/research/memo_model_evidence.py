"""Finite memo displays of verified ONON and MELI models, never a fact writer.

The shared readiness verifier owns source, bridge and scenario qualification.
This boundary binds an exact run and selects existing replay values. It adds no
financial formula, inferred actual, arbitrary expression or JSON path.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dcf import meli_inputs
from dcf.grade_evidence import DcfGradeEvidence, load_dcf_verification_evidence
from dcf.input_evidence import ModelInputReceipt, SourceReadContext, canonical_digest
from dcf.onon_inputs import (
    FLOW_KEYS,
    calculate_actuals,
    effective_numeric_inputs,
    model_output,
    verify_onon_inputs,
)
from dcf.onon_model import ASSUMPTION_KEYS, SCALAR_FIELDS, SCENARIOS
from dcf.readiness import load_valuation_readiness, replay_model_scenarios
from dcf.scenario_acceptance import ScenarioAcceptance, verify_scenario_acceptance

Unit = Literal[
    "CHF_m",
    "CHF_bn",
    "USD_m",
    "USD_bn",
    "USD_per_share",
    "shares_m",
    "fraction",
    "percent",
    "year",
    "multiple",
    "CHF_per_USD",
]


class MemoModelEvidenceError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoModelCommitment(_Closed):
    ticker: Literal["ONON"] = "ONON"
    run_id: int = Field(gt=0, strict=True)
    model: Literal["onon_economic_fcff"] = "onon_economic_fcff"
    engine_version: Literal["onon_economic_fcff_v1"] = "onon_economic_fcff_v1"
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    research_snapshot_id: str = Field(min_length=1)
    snapshot_member_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_inputs_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario_acceptance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MeliMemoModelCommitment(_Closed):
    ticker: Literal["MELI"] = "MELI"
    run_id: int = Field(gt=0, strict=True)
    model: Literal["meli_platform_sotp"] = "meli_platform_sotp"
    engine_version: Literal["meli_platform_sotp_v1"] = "meli_platform_sotp_v1"
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    research_snapshot_id: str = Field(min_length=1)
    snapshot_member_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_inputs_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario_acceptance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sensitivity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


MemoModelCommitmentReference = MemoModelCommitment | MeliMemoModelCommitment


_CALC_UNITS: dict[str, str] = dict.fromkeys(FLOW_KEYS, "CHF_m") | {
    "sbc_expense": "CHF_m",
    "shares": "shares_m",
    "cash": "CHF_m",
    "financial_debt": "CHF_m",
    "lease_liabilities": "CHF_m",
    "nonlease_da": "CHF_m",
    "capex": "CHF_m",
    "cash_rent_proxy": "CHF_m",
    "nwc": "CHF_m",
}
_OUTPUT_UNITS = {"vps": "USD_per_share", "equity_value": "USD_m", "operating_ev": "USD_m"}
_ROW_UNITS = {
    "year": "year",
    "revenue_chf_m": "CHF_m",
    "growth": "fraction",
    "adjusted_ebitda_margin": "fraction",
    "post_rent_sbc_ebit_chf_m": "CHF_m",
    "nopat_chf_m": "CHF_m",
    "nonlease_da_chf_m": "CHF_m",
    "capex_chf_m": "CHF_m",
    "incremental_nwc_chf_m": "CHF_m",
    "economic_fcff_chf_m": "CHF_m",
}
_SCENARIO_UNITS = _OUTPUT_UNITS | {
    "rate": "fraction",
    "terminal_g": "fraction",
    "sustainable_2032_fcff_chf_m": "CHF_m",
    "exit_multiple_fair_value_usd": "USD_per_share",
    "gordon_fair_value_usd": "USD_per_share",
    "pv_fcff_usd_m": "USD_m",
    "exit_multiple_pv_terminal_usd_m": "USD_m",
    "gordon_pv_terminal_usd_m": "USD_m",
    "gordon_terminal_weight": "fraction",
    "exit_multiple_terminal_weight": "fraction",
    "cash_bridge_usd_m": "USD_m",
    "entry_25pct_mos_usd": "USD_per_share",
}
_REVERSE_UNITS = {
    "required_constant_2027_2031_growth": "fraction",
    "required_2031_revenue_chf_m": "CHF_m",
    "required_margin_shift": "fraction",
    "required_2031_ebitda_margin": "fraction",
}
_BRIDGE_UNITS = {
    "diluted_economic_shares_m": "shares_m",
    "stub_years": "year",
    "equity_value_at_quote_usd_m": "USD_m",
    "ev_with_leases_chf_m": "CHF_m",
    "ev_to_fy26_adj_ebitda_range": "multiple",
}
_STRESS_UNITS: dict[str, str] = dict.fromkeys(
    (
        "ebitda_margin_minus_200bp",
        "rent_plus_100bp",
        "tax_plus_200bp",
        "incremental_nwc_plus_3pp",
        "cash_haircut_chf_500m",
        "translation_chf_per_usd_plus_10pct",
        "translation_chf_per_usd_minus_10pct",
        "combined_margin_rent_nwc",
    ),
    "USD_per_share",
)
_ASSUMPTION_UNITS = (
    {
        "price_usd": "USD_per_share",
        "chf_per_usd": "CHF_per_USD",
        "revenue_anchor_chf_m": "CHF_m",
        "wacc": "fraction",
        "forecast_start_year": "year",
        "guidance_revenue_low_chf_m": "CHF_m",
        "guidance_revenue_high_chf_m": "CHF_m",
        "guidance_ebitda_margin_low": "fraction",
        "guidance_ebitda_margin_high": "fraction",
    }
    | {
        f"{scenario}_{field}_{year}": "fraction"
        for scenario in SCENARIOS
        for field in ("growth", "ebitda_margin")
        for year in range(1, 6)
    }
    | {
        f"{scenario}_{field}": "multiple" if field == "terminal_fcf_multiple" else "fraction"
        for scenario in SCENARIOS
        for field in SCALAR_FIELDS
    }
)
_UNITS: dict[str, dict[str, str]] = {
    "receipt_calculation": _CALC_UNITS,
    "effective_assumption": _ASSUMPTION_UNITS,
    "output": _OUTPUT_UNITS,
    "scenario": _SCENARIO_UNITS,
    "scenario_row": _ROW_UNITS,
    "reverse": _REVERSE_UNITS,
    "bridge": _BRIDGE_UNITS,
    "stress": _STRESS_UNITS,
    "sensitivity": {"vps": "USD_per_share"},
}


class MemoModelValue(_Closed):
    source: Literal[
        "receipt_calculation",
        "effective_assumption",
        "output",
        "scenario",
        "scenario_row",
        "reverse",
        "bridge",
        "stress",
        "sensitivity",
    ]
    key: str
    scenario: Literal["bear", "base", "bull"] | None = None
    year: int | None = Field(default=None, ge=2027, le=2031, strict=True)
    discount_rate: Literal["0.095", "0.105", "0.12"] | None = None
    terminal_growth: Literal["0.02", "0.03", "0.04"] | None = None
    range_endpoint: int | None = Field(default=None, ge=0, le=1, strict=True)
    unit: Unit
    displayed_unit: Unit
    display_format: Literal[
        "number0",
        "number1",
        "number2",
        "number3",
        "number4",
        "number7",
        "percentage1",
        "percentage2",
    ]
    rounding: Literal["half_even"] = "half_even"
    displayed_value: str = Field(min_length=1)

    @model_validator(mode="after")
    def finite_selector(self) -> MemoModelValue:
        if self.key not in _UNITS[self.source]:
            raise ValueError("memo_model_selector_unsupported")
        if (self.source in {"scenario", "scenario_row"}) != (self.scenario is not None):
            raise ValueError("memo_model_scenario_selector_mismatch")
        if (self.source == "scenario_row") != (self.year is not None):
            raise ValueError("memo_model_year_selector_mismatch")
        if (self.source == "sensitivity") != (
            self.discount_rate is not None and self.terminal_growth is not None
        ):
            raise ValueError("memo_model_sensitivity_selector_mismatch")
        if self.source != "sensitivity" and (
            self.discount_rate is not None or self.terminal_growth is not None
        ):
            raise ValueError("memo_model_sensitivity_selector_mismatch")
        if (self.source == "bridge" and self.key == "ev_to_fy26_adj_ebitda_range") != (
            self.range_endpoint is not None
        ):
            raise ValueError("memo_model_range_selector_mismatch")
        return self


_MELI_INPUT_UNITS = {
    f"{key}_{period}": "USD_m"
    for key in meli_inputs.FLOW_KEYS
    for period in ("fy", "ytd", "prior_ytd")
} | {
    "cb0": "USD_m",
    "shares": "shares_m",
    "reported_available_cash_and_investments": "USD_m",
    "reported_total_financial_debt_and_leases": "USD_m",
    "reported_current_operating_lease_liabilities": "USD_m",
    "reported_noncurrent_operating_lease_liabilities": "USD_m",
    "nimal_actual": "fraction",
    "financing_issued_principal": "USD_m",
    "financing_underwriting_purchase_fraction": "fraction",
}
_MELI_CALC_UNITS = {
    str(key): "USD_m"
    for key in (
        *meli_inputs.FLOW_KEYS,
        "fpay_rev0",
        "operating_lease_liabilities",
        "financial_debt_pool",
        "net_cash",
        "baseline_net_cash",
        "financing_cash_before_other_expenses",
    )
} | meli_inputs.MELI_DIAGNOSTIC_UNITS
_MELI_ASSUMPTION_UNITS = dict.fromkeys(
    (
        "comm_g_near",
        "comm_g_term",
        "comm_margin_near",
        "comm_margin_term",
        "fpay_g_near",
        "fpay_g_term",
        "fpay_margin_near",
        "fpay_margin_term",
        "da_pct",
        "capex_pct_near",
        "capex_pct_term",
        "nwc_pct",
        "tax",
        "wacc",
        "cbg_near",
        "cbg_term",
        "nimal_near",
        "nimal_term",
        "credit_opex_ratio",
        "cap_ratio",
        "credit_ke",
        "credit_g_term",
        "credit_terminal_roe",
        "g_term",
        "country_risk_premium",
    ),
    "fraction",
) | {
    "credit_cash_allocation": "USD_m",
    "operating_cash_reserve": "USD_m",
    "credit_funding_debt_allocation": "USD_m",
    "financing_other_expenses_reserve": "USD_m",
    "op_exit_ebitda_mult": "multiple",
    "years": "year",
    "beta_op": "multiple",
    "beta_credit": "multiple",
    "derive_capm": "flag",
}
_MELI_OUTPUT_UNITS = dict.fromkeys(
    (
        "pv_op_fcff",
        "op_terminal_ev",
        "pv_op_terminal",
        "operating_ev",
        "pv_credit_fcfe",
        "credit_terminal",
        "pv_credit_terminal",
        "credit_equity_value",
        "equity_value",
        "op_terminal_revenue",
    ),
    "USD_m",
) | {
    "vps": "USD_per_share",
    "credit_terminal_roe": "fraction",
    "terminal_blended_op_margin": "fraction",
}
_MELI_ROW_UNITS = dict.fromkeys(
    (
        "comm_rev",
        "fpay_rev",
        "op_rev",
        "op_ebit",
        "op_da",
        "op_capex",
        "op_fcff",
        "cb",
        "credit_ni",
        "reqcap",
        "credit_fcfe",
    ),
    "USD_m",
) | {"t": "year", "df_op": "fraction", "df_cr": "fraction"}
_MELI_UNITS: dict[str, dict[str, str]] = {
    "receipt_input": _MELI_INPUT_UNITS,
    "receipt_calculation": _MELI_CALC_UNITS,
    "effective_assumption": _MELI_ASSUMPTION_UNITS,
    "output": _MELI_OUTPUT_UNITS,
    "scenario_input": _MELI_ASSUMPTION_UNITS,
    "scenario": _MELI_OUTPUT_UNITS,
    "scenario_row": _MELI_ROW_UNITS,
    "sensitivity": {"vps": "USD_per_share", "wacc": "fraction", "credit_ke": "fraction"},
}
MeliUnit = Literal[
    "USD_m",
    "USD_bn",
    "USD_per_share",
    "shares_m",
    "fraction",
    "percent",
    "year",
    "multiple",
    "flag",
]
MeliSensitivityDelta = Literal["-0.01", "0", "0.01"]


class MeliMemoModelValue(_Closed):
    model: Literal["meli_platform_sotp"]
    source: Literal[
        "receipt_input",
        "receipt_calculation",
        "effective_assumption",
        "output",
        "scenario_input",
        "scenario",
        "scenario_row",
        "sensitivity",
    ]
    key: str
    scenario: Literal["bear", "base", "bull"] | None = None
    period: int | None = Field(default=None, ge=1, le=100, strict=True)
    wacc_delta: MeliSensitivityDelta | None = None
    credit_ke_delta: MeliSensitivityDelta | None = None
    unit: MeliUnit
    displayed_unit: MeliUnit
    display_format: Literal[
        "number0",
        "number1",
        "number2",
        "number3",
        "number4",
        "number7",
        "percentage1",
        "percentage2",
    ]
    rounding: Literal["half_even"] = "half_even"
    displayed_value: str = Field(min_length=1)

    @model_validator(mode="after")
    def finite_selector(self) -> MeliMemoModelValue:
        if self.key not in _MELI_UNITS[self.source]:
            raise ValueError("memo_model_selector_unsupported")
        if (self.source in {"scenario_input", "scenario", "scenario_row"}) != (
            self.scenario is not None
        ):
            raise ValueError("memo_model_scenario_selector_mismatch")
        if (self.source == "scenario_row") != (self.period is not None):
            raise ValueError("memo_model_period_selector_mismatch")
        if self.source == "sensitivity":
            if self.wacc_delta is None or self.credit_ke_delta is None:
                raise ValueError("memo_model_sensitivity_selector_mismatch")
        elif self.wacc_delta is not None or self.credit_ke_delta is not None:
            raise ValueError("memo_model_sensitivity_selector_mismatch")
        return self


MemoModelValueReference = MemoModelValue | MeliMemoModelValue


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MemoModelEvidenceError("memo_model_value_unavailable")
    return cast(dict[str, object], value)


def model_value_display(
    reference: MemoModelValueReference,
    *,
    calculations: dict[str, float],
    assumptions: dict[str, float],
    output: dict[str, object],
) -> str:
    """Check one finite selector after the caller has qualified the full model."""
    if isinstance(reference, MeliMemoModelValue):
        return _meli_model_value_display(reference, calculations, assumptions, output)
    # Revalidate even model_copy/construct values; neither grants a selector.
    reference = MemoModelValue.model_validate(reference.model_dump())
    if reference.unit != _UNITS[reference.source][reference.key]:
        raise MemoModelEvidenceError("memo_model_value_unit_mismatch")
    selected: object
    if reference.source == "receipt_calculation":
        selected = calculations.get(reference.key)
    elif reference.source == "effective_assumption":
        selected = assumptions.get(reference.key)
    elif reference.source == "output":
        selected = output.get(reference.key)
    elif reference.source == "reverse":
        selected = _object(output.get("reverse_dcf")).get(reference.key)
    elif reference.source == "stress":
        selected = _object(output.get("one_factor_stresses")).get(reference.key)
    elif reference.source == "sensitivity":
        selected = _object(
            _object(output.get("gordon_sensitivity")).get(str(reference.discount_rate))
        ).get(str(reference.terminal_growth))
    elif reference.source == "bridge":
        selected = _object(output.get("bridge")).get(reference.key)
        if reference.range_endpoint is not None:
            if not isinstance(selected, list):
                raise MemoModelEvidenceError("memo_model_value_unavailable")
            members = cast(list[object], selected)
            if len(members) != 2:
                raise MemoModelEvidenceError("memo_model_value_unavailable")
            selected = members[reference.range_endpoint]
    else:
        scenario = _object(_object(output.get("scenarios")).get(str(reference.scenario)))
        if reference.source == "scenario":
            selected = scenario.get(reference.key)
        else:
            rows = scenario.get("rows")
            if not isinstance(rows, list):
                raise MemoModelEvidenceError("memo_model_value_unavailable")
            matches = [
                row
                for row in cast(list[object], rows)
                if _object(row).get("year") == reference.year
            ]
            if len(matches) != 1:
                raise MemoModelEvidenceError("memo_model_year_unavailable_or_duplicate")
            selected = _object(matches[0]).get(reference.key)
    return _display_numeric(reference, selected)


def _display_numeric(reference: MemoModelValueReference, selected: object) -> str:
    if (
        isinstance(selected, bool)
        or not isinstance(selected, (int, float))
        or not math.isfinite(selected)
    ):
        raise MemoModelEvidenceError("memo_model_value_unavailable")
    value = Decimal(str(selected))
    if (reference.unit, reference.displayed_unit) in {("CHF_m", "CHF_bn"), ("USD_m", "USD_bn")}:
        value /= Decimal(1000)
    elif (reference.unit, reference.displayed_unit) == ("fraction", "percent"):
        value *= Decimal(100)
    elif reference.unit != reference.displayed_unit:
        raise MemoModelEvidenceError("memo_model_display_unit_mismatch")
    digits = int(reference.display_format[-1])
    expected = format(
        value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_EVEN), f".{digits}f"
    )
    if reference.display_format.startswith("percentage"):
        if reference.unit != "fraction" or reference.displayed_unit != "percent":
            raise MemoModelEvidenceError("memo_model_display_unit_mismatch")
        expected += "%"
    if reference.displayed_value != expected:
        raise MemoModelEvidenceError("memo_model_display_mismatch")
    return expected


def _meli_model_value_display(
    reference: MeliMemoModelValue,
    calculations: dict[str, float],
    assumptions: dict[str, float],
    output: dict[str, object],
) -> str:
    reference = MeliMemoModelValue.model_validate(reference.model_dump())
    if reference.unit != _MELI_UNITS[reference.source][reference.key]:
        raise MemoModelEvidenceError("memo_model_value_unit_mismatch")
    selected: object
    if reference.source == "receipt_input":
        selected = _object(output.get("reported_inputs")).get(reference.key)
    elif reference.source == "receipt_calculation":
        selected = calculations.get(reference.key)
    elif reference.source == "effective_assumption":
        selected = assumptions.get(reference.key)
    elif reference.source == "output":
        selected = output.get(reference.key)
    elif reference.source == "sensitivity":
        selected = _object(
            _object(_object(output.get("sensitivity")).get(str(reference.wacc_delta))).get(
                str(reference.credit_ke_delta)
            )
        ).get(reference.key)
    else:
        scenario = _object(_object(output.get("scenarios")).get(str(reference.scenario)))
        if reference.source == "scenario_input":
            selected = _object(scenario.get("effective_inputs")).get(reference.key)
        else:
            scenario_output = _object(scenario.get("model_output"))
            if reference.source == "scenario":
                selected = scenario_output.get(reference.key)
            else:
                rows = scenario_output.get("rows")
                if not isinstance(rows, list):
                    raise MemoModelEvidenceError("memo_model_value_unavailable")
                matches = [
                    row
                    for row in cast(list[object], rows)
                    if _object(row).get("t") == reference.period
                ]
                if len(matches) != 1:
                    raise MemoModelEvidenceError("memo_model_period_unavailable_or_duplicate")
                selected = _object(matches[0]).get(reference.key)
    return _display_numeric(reference, selected)


def meli_sensitivity_grid(inputs: dict[str, float]) -> dict[str, dict[str, dict[str, float]]]:
    """Replay the workbook's finite WACC/credit-Ke grid through its financial owner."""
    grid: dict[str, dict[str, dict[str, float]]] = {}
    for dw in ("-0.01", "0", "0.01"):
        grid[dw] = {}
        for dk in ("-0.01", "0", "0.01"):
            variant = inputs | {
                "wacc": inputs["wacc"] + float(dw),
                "credit_ke": inputs["credit_ke"] + float(dk),
            }
            if variant["wacc"] <= 0 or variant["credit_ke"] <= variant["credit_g_term"]:
                raise MemoModelEvidenceError("memo_model_sensitivity_economics_invalid")
            value = meli_inputs.model_output(variant)["vps"]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise MemoModelEvidenceError("memo_model_sensitivity_output_invalid")
            grid[dw][dk] = {
                "wacc": variant["wacc"],
                "credit_ke": variant["credit_ke"],
                "vps": value,
            }
    return grid


@dataclass(frozen=True)
class VerifiedMemoModel:
    calculations: dict[str, float]
    assumptions: dict[str, float]
    output: dict[str, object]

    def display(self, reference: MemoModelValueReference) -> str:
        if not isinstance(reference, MemoModelValue):
            raise MemoModelEvidenceError("memo_model_reference_model_mismatch")
        return model_value_display(
            reference,
            calculations=self.calculations,
            assumptions=self.assumptions,
            output=self.output,
        )


@dataclass(frozen=True)
class VerifiedMeliMemoModel:
    calculations: dict[str, float]
    assumptions: dict[str, float]
    output: dict[str, object]

    def display(self, reference: MemoModelValueReference) -> str:
        if not isinstance(reference, MeliMemoModelValue):
            raise MemoModelEvidenceError("memo_model_reference_model_mismatch")
        return model_value_display(
            reference,
            calculations=self.calculations,
            assumptions=self.assumptions,
            output=self.output,
        )


def load_verified_memo_model(
    conn: sqlite3.Connection,
    commitment: MemoModelCommitmentReference,
    *,
    primary_snapshot_id: str,
    primary_member_sha256: str,
    ticker: str,
    as_of: datetime,
    source_context: SourceReadContext | None,
) -> VerifiedMemoModel | VerifiedMeliMemoModel:
    """Replay the committed model; current revision checks belong to publication."""
    commitment = (
        MeliMemoModelCommitment.model_validate(commitment.model_dump())
        if isinstance(commitment, MeliMemoModelCommitment)
        else MemoModelCommitment.model_validate(commitment.model_dump())
    )
    if (ticker, primary_snapshot_id, primary_member_sha256) != (
        commitment.ticker,
        commitment.research_snapshot_id,
        commitment.snapshot_member_sha256,
    ):
        raise MemoModelEvidenceError("memo_model_primary_context_mismatch")
    readiness = load_valuation_readiness(
        conn,
        ticker,
        as_of=as_of,
        purpose="analyst_memo",
        source_context=source_context,
        run_id=commitment.run_id,
    )
    if not readiness.ready or readiness.run_id != commitment.run_id:
        raise MemoModelEvidenceError("memo_model_readiness_unverified")
    grade = load_dcf_verification_evidence(conn, ticker, run_id=commitment.run_id)
    if (
        grade.status != "available"
        or grade.projection_status != "complete"
        or grade.run_id != commitment.run_id
    ):
        raise MemoModelEvidenceError("memo_model_evidence_incomplete_or_changed")
    if isinstance(commitment, MeliMemoModelCommitment):
        return _load_verified_meli_memo_model(
            conn, commitment, grade, as_of=as_of, source_context=source_context
        )
    snapshot, provenance = grade.assumption_snapshot or {}, grade.provenance or {}
    receipt = ModelInputReceipt.model_validate(provenance.get("model_input_receipt"))
    numeric = effective_numeric_inputs(_object(snapshot.get("effective_model_inputs")))
    verify_onon_inputs(
        conn, receipt, effective_inputs=numeric, as_of=as_of, source_context=source_context
    )
    output = model_output(numeric)
    acceptance = provenance.get("scenario_acceptance")
    if acceptance is None:
        raise MemoModelEvidenceError("memo_model_scenario_acceptance_missing")
    expected = MemoModelCommitment(
        run_id=commitment.run_id,
        input_sha256=grade.input_sha256 or "",
        receipt_sha256=canonical_digest(receipt.model_dump(mode="json")),
        research_snapshot_id=receipt.request.research_snapshot_id,
        snapshot_member_sha256=receipt.snapshot_member_sha256,
        effective_inputs_sha256=canonical_digest(numeric),
        model_output_sha256=canonical_digest(output),
        scenario_acceptance_sha256=canonical_digest(acceptance),
    )
    if (
        commitment != expected
        or grade.engine_version != commitment.engine_version
        or snapshot.get("model") != commitment.model
        or canonical_digest(snapshot.get("model_output")) != commitment.model_output_sha256
    ):
        raise MemoModelEvidenceError("memo_model_commitment_mismatch")
    _actuals, calculations = calculate_actuals(receipt)
    return VerifiedMemoModel(
        calculations={item.key: item.value for item in calculations},
        assumptions={key: numeric[key] for key in ASSUMPTION_KEYS},
        output=output,
    )


def _load_verified_meli_memo_model(
    conn: sqlite3.Connection,
    commitment: MeliMemoModelCommitment,
    grade: DcfGradeEvidence,
    *,
    as_of: datetime,
    source_context: SourceReadContext | None,
) -> VerifiedMeliMemoModel:
    snapshot, provenance = grade.assumption_snapshot or {}, grade.provenance or {}
    receipt = ModelInputReceipt.model_validate(provenance.get("model_input_receipt"))
    numeric = meli_inputs.effective_numeric_inputs(
        _object(snapshot.get("effective_model_inputs")), recipe=receipt.recipe
    )
    meli_inputs.verify_meli_inputs(
        conn, receipt, effective_inputs=numeric, as_of=as_of, source_context=source_context
    )
    output = meli_inputs.model_output(numeric)
    scenarios = replay_model_scenarios("MELI", numeric, output, snapshot, recipe=receipt.recipe)
    raw_acceptance = provenance.get("scenario_acceptance")
    if raw_acceptance is None or grade.created_at is None:
        raise MemoModelEvidenceError("memo_model_scenario_acceptance_missing")
    acceptance = verify_scenario_acceptance(
        ScenarioAcceptance.model_validate(raw_acceptance),
        input_receipt=receipt,
        model=commitment.model,
        engine_version=commitment.engine_version,
        effective_inputs=numeric,
        output=scenarios,
        as_of=as_of,
        calculated_at=datetime.fromisoformat(grade.created_at.replace("Z", "+00:00")),
    )
    grid = meli_sensitivity_grid(numeric)
    expected = MeliMemoModelCommitment(
        run_id=commitment.run_id,
        input_sha256=grade.input_sha256 or "",
        receipt_sha256=canonical_digest(receipt.model_dump(mode="json")),
        research_snapshot_id=receipt.request.research_snapshot_id,
        snapshot_member_sha256=receipt.snapshot_member_sha256,
        effective_inputs_sha256=canonical_digest(numeric),
        model_output_sha256=canonical_digest(output),
        scenario_acceptance_sha256=canonical_digest(acceptance.model_dump(mode="json")),
        sensitivity_sha256=canonical_digest(grid),
    )
    if (
        commitment != expected
        or grade.engine_version != commitment.engine_version
        or snapshot.get("model") != commitment.model
        or canonical_digest(snapshot.get("model_output")) != commitment.model_output_sha256
    ):
        raise MemoModelEvidenceError("memo_model_commitment_mismatch")
    return VerifiedMeliMemoModel(
        calculations={item.key: item.value for item in receipt.calculations},
        assumptions={
            key: numeric[key] for key in meli_inputs.assumption_keys_for_recipe(receipt.recipe)
        },
        output=output
        | {
            "reported_inputs": {item.key: item.value for item in receipt.inputs},
            "scenarios": scenarios["scenarios"],
            "sensitivity": grid,
        },
    )
