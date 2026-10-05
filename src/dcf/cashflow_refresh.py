"""Prepare or retain a source-certified, analyst-attributed equity cash-flow model."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import tempfile
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from dcf.artifact_promotion import StagedFilePromotion
from dcf.cashflow_inputs import ENGINE, RECIPE, prepare_cashflow_inputs
from dcf.cashflow_scenarios import (
    AnalystCashflowScenarioReview,
    verify_analyst_cashflow_scenarios,
)
from dcf.input_evidence import FrozenModel, InputEvidenceError, ModelInputRequest
from dcf.input_recipes import model_output
from dcf.persist import DcfRunRow, upsert
from dcf.provenance import build_file_provenance


class PreparedCashflowDcfRequest(FrozenModel):
    schema_version: Literal["prepared_cashflow_dcf.v1"] = "prepared_cashflow_dcf.v1"
    model_inputs: ModelInputRequest
    effective_inputs: dict[str, float]
    as_of: AwareDatetime
    valuation_date: date
    market_price: float = Field(gt=0)
    market_observed_at: AwareDatetime
    market_source: str = Field(min_length=1)
    currency: Literal["USD"] = "USD"
    analyst_scenario_review: AnalystCashflowScenarioReview | None = None

    @model_validator(mode="after")
    def _clocks(self) -> PreparedCashflowDcfRequest:
        if self.model_inputs.recipe != RECIPE:
            raise ValueError("prepared cash-flow request requires the registered cash-flow recipe")
        return self


class PreparedCashflowDcfResult(FrozenModel):
    mode: Literal["dry_run", "apply"]
    ticker: str
    engine: Literal["operating_cashflow_equity"] = ENGINE
    model_input_receipt: dict[str, object]
    effective_inputs: dict[str, float]
    model_output: dict[str, object]
    calculated_at: AwareDatetime
    source_observed_at: AwareDatetime
    artifact_path: str | None = None
    artifact_sha256: str | None = None
    version_created: bool | None = None


def prepare_cashflow_dcf(
    conn: sqlite3.Connection,
    request: PreparedCashflowDcfRequest,
    *,
    request_path: Path,
    expected_request_sha256: str,
    repo_root: Path,
    artifact_path: Path,
    apply: bool = False,
) -> PreparedCashflowDcfResult:
    """Reconstruct inputs, then optionally save through the existing atomic owner.

    The exact request bytes are analyst evidence. A dry run creates no files or
    database rows. Apply retains a JSON calculation artifact and a versioned run.
    Analyst scenario reviews certify replay, not owner allocation permission.
    """
    raw = request_path.read_bytes()
    source_stat = request_path.stat()
    captured_at = datetime.now(UTC)
    digest = hashlib.sha256(raw).hexdigest()
    if (
        digest != expected_request_sha256
        or PreparedCashflowDcfRequest.model_validate_json(raw) != request
    ):
        raise InputEvidenceError("prepared_cashflow_request_bytes_mismatch")
    if request_path.resolve() == artifact_path.resolve():
        raise InputEvidenceError("prepared_cashflow_request_artifact_collision")
    source_at = source_stat.st_mtime
    if (
        source_at > captured_at.timestamp()
        or request.as_of > captured_at
        or request.market_observed_at > captured_at
        or request.valuation_date > captured_at.date()
    ):
        raise InputEvidenceError("prepared_cashflow_request_clock_in_future")
    if not all(math.isfinite(value) for value in request.effective_inputs.values()):
        raise InputEvidenceError("prepared_cashflow_request_nonfinite_input")
    effective, receipt = prepare_cashflow_inputs(
        conn, request.model_inputs, effective_inputs=request.effective_inputs, as_of=request.as_of
    )
    output = model_output(RECIPE, effective)
    calculated_at = datetime.now(UTC)
    if calculated_at < captured_at:
        raise InputEvidenceError("prepared_cashflow_calculation_clock_invalid")
    scenario_json = None
    if request.analyst_scenario_review is not None:
        if request.effective_inputs != effective:
            raise InputEvidenceError("analyst_scenario_prepared_inputs_incomplete")
        verify_analyst_cashflow_scenarios(
            request.analyst_scenario_review,
            receipt,
            base_effective_inputs=effective,
            as_of=calculated_at,
            calculated_at=calculated_at,
        )
        scenario_json = request.analyst_scenario_review.model_dump(mode="json")
    equity_value, value_per_share = output["equity_value"], output["vps"]
    if not isinstance(equity_value, (int, float)) or not isinstance(value_per_share, (int, float)):
        raise InputEvidenceError("cashflow_model_output_invalid")
    snapshot: dict[str, object] = {
        "model": ENGINE,
        "recipe": RECIPE,
        "artifact_kind": "json_calculation",
        "valuation_scope": "equity",
        "effective_model_inputs": effective,
        "recipe_context": request.model_inputs.recipe_context,
        "financial_period_end": request.model_inputs.financial_period_end.isoformat(),
        "value_per_share": output["vps"],
        "equity_value_m": output["equity_value"],
        "operating_ev_m": None,
        "credit_equity_value_m": None,
        "analyst_assumptions": {
            key: item.model_dump(mode="json")
            for key, item in request.model_inputs.assumptions.items()
        },
        "analyst_scenario_review": scenario_json,
    }
    receipt_json = receipt.model_dump(mode="json")
    result = PreparedCashflowDcfResult(
        mode="apply" if apply else "dry_run",
        ticker=request.model_inputs.ticker,
        model_input_receipt=receipt_json,
        effective_inputs=effective,
        model_output=output,
        calculated_at=calculated_at,
        source_observed_at=captured_at,
    )
    if not apply:
        return result
    if conn.in_transaction:
        raise InputEvidenceError("cashflow_persistence_requires_own_transaction")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        {
            "schema_version": "cashflow_calculation.v1",
            "request_sha256": digest,
            "data_cutoff_at": request.as_of.isoformat(),
            "source_observed_at": captured_at.isoformat(),
            "calculated_at": calculated_at.isoformat(),
            "snapshot": snapshot,
            "model_input_receipt": receipt_json,
            "output": output,
            "analyst_scenario_review": scenario_json,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    with tempfile.NamedTemporaryFile(
        dir=artifact_path.parent, prefix=f".{artifact_path.name}.", suffix=".staged", delete=False
    ) as handle:
        staged = Path(handle.name)
        handle.write(encoded)
    try:
        provenance = build_file_provenance(
            ticker=request.model_inputs.ticker,
            repo_root=repo_root,
            workbook_path=staged,
            workbook_locator_path=artifact_path,
            engine_version=RECIPE,
            effective_inputs=effective,
            assumption_snapshot=snapshot,
            live_price=request.market_price,
            live_price_at=request.market_observed_at,
            live_price_source=request.market_source,
            source_files=((request_path, "prepared_analyst_model_request"),),
            equity_direct_archetype=ENGINE,
            model_input_receipt=receipt_json,
        )
        if scenario_json is not None:
            provenance = replace(
                provenance,
                detail={
                    **(provenance.detail or {}),
                    "analyst_scenario_review": scenario_json,
                    "analyst_scenario_source": {
                        "path": str(request_path.resolve()),
                        "sha256": digest,
                        "byte_size": len(raw),
                        "observed_at": captured_at.isoformat(),
                        "data_cutoff_at": request.as_of.isoformat(),
                        "mtime": source_at,
                    },
                },
            )
        if hashlib.sha256(request_path.read_bytes()).hexdigest() != digest:
            raise InputEvidenceError("prepared_cashflow_request_changed_during_build")
        # Hold the write transaction across canonical recheck and model retention.
        conn.execute("BEGIN IMMEDIATE")
        prepare_cashflow_inputs(
            conn, request.model_inputs, effective_inputs=effective, as_of=request.as_of
        )
        created = upsert(
            conn,
            DcfRunRow(
                ticker=request.model_inputs.ticker,
                valuation_date=request.valuation_date,
                horizon_years=int(effective["years"]),
                wacc=effective["cost_of_equity"],
                npv=float(equity_value),
                npv_per_share=float(value_per_share),
                shares_outstanding=(effective["reported_shares"] + effective["dilution_shares"])
                * 1_000_000,
                currency="USD",
                live_price=request.market_price,
                live_price_at=request.market_observed_at,
                mos_bar_used=None,
                assumption_snapshot_json=json.dumps(snapshot, sort_keys=True, allow_nan=False),
                notes="Equity cash-flow method. Legacy wacc field stores cost of equity. Analyst adjustments are explicit; scenario acceptance remains separate.",
                provenance=provenance,
                calculated_at=calculated_at,
            ),
            artifact_promotion=StagedFilePromotion(staged, artifact_path),
        )
        return result.model_copy(
            update={
                "artifact_path": str(artifact_path),
                "artifact_sha256": hashlib.sha256(encoded).hexdigest(),
                "version_created": created,
            }
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        staged.unlink(missing_ok=True)
