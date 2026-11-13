"""Workbook-bound input proof and replay for redesigned FCFF valuations."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from openpyxl import Workbook, load_workbook
from openpyxl.compat.strings import safe_string
from pydantic import ValidationError

from dcf import equity_bridge, redesign, redesign_inputs
from dcf.grade_evidence import MAX_VERIFICATION_EVIDENCE_BYTES
from dcf.input_evidence import (
    InputEvidenceError,
    ModelInputReceipt,
    VerifiedInput,
    canonical_digest,
)
from provenance.financial_statement_admission import financial_statement_scale
from provenance.source_read_context import SourceReadContext

_SHEET = "Model Input Receipt"
_SCHEMA = "dcf_workbook_model_input_receipt.v1"
_CHUNK_CHARS = 16_000
_MAX_CHUNKS = (MAX_VERIFICATION_EVIDENCE_BYTES + _CHUNK_CHARS - 1) // _CHUNK_CHARS


def _receipt(value: object) -> ModelInputReceipt:
    try:
        receipt = ModelInputReceipt.model_validate(value)
    except ValidationError as exc:
        raise InputEvidenceError("workbook_model_input_receipt_invalid") from exc
    if (
        receipt.schema_version != "dcf_model_inputs.v3"
        or receipt.source_integrity != "present_bytes_verified"
        or receipt.recipe != redesign_inputs.RECIPE
        or receipt.request.recipe != redesign_inputs.RECIPE
    ):
        raise InputEvidenceError("workbook_model_input_receipt_schema_or_recipe_invalid")
    return receipt


def write_workbook_receipt(wb: Workbook, receipt: ModelInputReceipt) -> None:
    encoded = _receipt(receipt.model_dump(mode="json")).model_dump_json()
    raw = encoded.encode("utf-8")
    if len(raw) > MAX_VERIFICATION_EVIDENCE_BYTES:
        raise InputEvidenceError("workbook_model_input_receipt_byte_limit")
    if _SHEET in wb.sheetnames:
        del wb[_SHEET]
    sheet = wb.create_sheet(_SHEET)
    sheet.sheet_state = "hidden"
    sheet.append([_SCHEMA, len(raw), hashlib.sha256(raw).hexdigest()])
    for index, offset in enumerate(range(0, len(encoded), _CHUNK_CHARS)):
        sheet.append([index, encoded[offset : offset + _CHUNK_CHARS]])


def read_workbook_receipt(path: Path) -> ModelInputReceipt | None:
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        if _SHEET not in workbook.sheetnames:
            return None
        sheet = workbook[_SHEET]
        if not 2 <= sheet.max_row <= _MAX_CHUNKS + 1 or sheet.max_column != 3:
            raise InputEvidenceError("workbook_model_input_receipt_population_invalid")
        rows = iter(sheet.iter_rows(values_only=True))
        schema, byte_size, digest = next(rows)
        if (
            schema != _SCHEMA
            or type(byte_size) is not int
            or not 0 < byte_size <= MAX_VERIFICATION_EVIDENCE_BYTES
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise InputEvidenceError("workbook_model_input_receipt_header_invalid")
        chunks: list[str] = []
        consumed = 0
        for index, row in enumerate(rows):
            order, chunk, extra = row
            if (
                type(order) is not int
                or order != index
                or not isinstance(chunk, str)
                or not 0 < len(chunk) <= _CHUNK_CHARS
                or extra is not None
            ):
                raise InputEvidenceError("workbook_model_input_receipt_chunk_invalid")
            consumed += len(chunk.encode("utf-8"))
            if consumed > MAX_VERIFICATION_EVIDENCE_BYTES:
                raise InputEvidenceError("workbook_model_input_receipt_byte_limit")
            chunks.append(chunk)
        if any(len(chunk) != _CHUNK_CHARS for chunk in chunks[:-1]):
            raise InputEvidenceError("workbook_model_input_receipt_chunk_invalid")
        encoded = "".join(chunks)
        if consumed != byte_size or hashlib.sha256(encoded.encode("utf-8")).hexdigest() != digest:
            raise InputEvidenceError("workbook_model_input_receipt_digest_mismatch")
        try:
            return _receipt(json.loads(encoded))
        except (ValueError, RecursionError) as exc:
            raise InputEvidenceError("workbook_model_input_receipt_invalid") from exc
    finally:
        workbook.close()


def _source_actual_value(item: VerifiedInput, concept: str) -> float:
    try:
        value = float(
            Decimal(item.native_value or "")
            * financial_statement_scale(item.unit_key, item.currency, concept)
        )
    except (ValueError, InvalidOperation) as exc:
        raise InputEvidenceError("workbook_model_input_receipt_native_value_invalid") from exc
    if not math.isfinite(value):
        raise InputEvidenceError("workbook_model_input_receipt_native_value_invalid")
    return value


def _source_cell_millions(item: VerifiedInput, concept: str) -> float:
    value = (
        _source_actual_value(item, concept) / 1_000_000
        if item.native_value is not None
        else item.value
    )
    return float(safe_string(value))


def _sealed_inputs(inp: redesign.RedesignInputs, receipt: ModelInputReceipt) -> dict[str, float]:
    flat = redesign_inputs.effective_inputs_from_receipt(receipt)
    context = redesign_inputs.context_for(receipt.request)
    if inp.segments != context.segments:
        raise InputEvidenceError("workbook_model_input_receipt_segment_mismatch")
    displayed = redesign_inputs.serialize_flat_inputs(inp)
    if displayed == flat:
        return flat
    expected = {key: float(safe_string(value)) for key, value in flat.items()}
    sources = {item.key: item for item in receipt.inputs}
    bridge_fields = redesign_inputs.bridge_source_fields(context)
    if all(sources[key].native_value is not None for key, _concept, _field in bridge_fields):
        physical = {
            field: _source_actual_value(sources[key], concept)
            for key, concept, field in bridge_fields
        }
        cash_field = (
            "cashAndCashEquivalents"
            if context.cash_scope == "cash_and_equivalents"
            else "cashAndShortTermInvestments"
        )
        cash = equity_bridge.resolve_complete_aggregate(
            physical,
            aggregate_field=cash_field,
            component_fields=(),
        )
        debt = equity_bridge.resolve_debt_scope(physical, scope=context.debt_scope)
        if cash is None or debt is None:
            raise InputEvidenceError("workbook_model_input_receipt_native_bridge_invalid")
        expected["cash_m"] = float(safe_string(cash.value / 1_000_000))
        expected["total_debt_m"] = float(safe_string(debt.value / 1_000_000))
    expected["diluted_shares_m"] = _source_cell_millions(
        sources["diluted_shares_m"], "weighted_avg_shares_diluted"
    )
    expected["wacc"] = redesign.derive_wacc(
        **{
            key: expected[key]
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
    values = {
        item.key: _source_cell_millions(item, item.key.rsplit("_", 1)[0])
        for item in receipt.inputs
        if item.key.rsplit("_", 1)[0] in redesign_inputs.FLOW_CONCEPTS
    }
    revenue = sum(value for key, value in values.items() if key.startswith("revenue_"))
    depreciation = sum(
        value for key, value in values.items() if key.startswith("depreciation_and_amortization_")
    )
    if revenue <= 0:
        raise InputEvidenceError("workbook_model_input_receipt_revenue_invalid")
    expected["da_ratio"] = depreciation / revenue
    if len(context.segments) == 1:
        expected["segment_0_revenue_m"] = revenue
    normalized = redesign.normalize_scenario_weights(
        expected["weight_bull"], expected["weight_base"], expected["weight_bear"]
    )
    expected.update(zip(("weight_bull", "weight_base", "weight_bear"), normalized, strict=True))
    if any(
        value != expected[key]
        for key, value in displayed.items()
        if not (key.startswith("segment_") and key.endswith("_allocation"))
    ):
        raise InputEvidenceError("workbook_model_input_receipt_display_mismatch")
    return flat


def verify_workbook_receipt(
    conn: sqlite3.Connection,
    path: Path,
    inp: redesign.RedesignInputs,
    *,
    as_of: datetime,
    source_context: SourceReadContext | None = None,
) -> ModelInputReceipt | None:
    receipt = read_workbook_receipt(path)
    if receipt is None:
        return None
    return redesign_inputs.verify_redesign_inputs(
        conn,
        receipt,
        effective_inputs=_sealed_inputs(inp, receipt),
        as_of=as_of,
        source_context=source_context,
    )


def snapshot_with_receipt(
    snapshot_json: str, inp: redesign.RedesignInputs, receipt: ModelInputReceipt
) -> str:
    _receipt(receipt.model_dump(mode="json"))
    flat = _sealed_inputs(inp, receipt)
    output = redesign_inputs.model_output(flat)
    if (
        canonical_digest(flat) != receipt.effective_inputs_sha256
        or canonical_digest(output) != receipt.model_output_sha256
    ):
        raise InputEvidenceError("workbook_model_input_receipt_replay_mismatch")
    try:
        raw: object = json.loads(snapshot_json)
    except (ValueError, RecursionError) as exc:
        raise InputEvidenceError("workbook_model_input_receipt_snapshot_invalid") from exc
    if not isinstance(raw, dict):
        raise InputEvidenceError("workbook_model_input_receipt_snapshot_invalid")
    snapshot = cast("dict[str, object]", raw)
    if "scenarios" in snapshot:
        scenarios = snapshot["scenarios"]
        if not isinstance(scenarios, dict):
            raise InputEvidenceError("workbook_model_input_receipt_scenarios_mismatch")
        scenario_values = cast("dict[str, object]", scenarios)
        if set(scenario_values) != {"base", "bull", "bear"}:
            raise InputEvidenceError("workbook_model_input_receipt_scenarios_mismatch")
        nested = [isinstance(entry, dict) for entry in scenario_values.values()]
        if any(nested) and not all(nested):
            raise InputEvidenceError("workbook_model_input_receipt_scenarios_mismatch")
        projected: dict[str, float] = {}
        for name, entry in scenario_values.items():
            value = (
                cast("dict[str, object]", entry).get("fair_value_per_share_usd")
                if isinstance(entry, dict)
                else entry
            )
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise InputEvidenceError("workbook_model_input_receipt_scenarios_mismatch")
            projected[name] = float(value)
        if canonical_digest(projected) != canonical_digest(output["scenarios"]):
            displayed_output = redesign_inputs.model_output(
                redesign_inputs.serialize_flat_inputs(inp)
            )
            if canonical_digest(projected) != canonical_digest(displayed_output["scenarios"]):
                raise InputEvidenceError("workbook_model_input_receipt_scenarios_mismatch")
        sealed_scenarios = cast("dict[str, float]", output["scenarios"])
        for name, entry in scenario_values.items():
            scenario_values[name] = (
                {
                    **cast("dict[str, object]", entry),
                    "fair_value_per_share_usd": sealed_scenarios[name],
                }
                if isinstance(entry, dict)
                else sealed_scenarios[name]
            )
    else:
        snapshot["scenarios"] = output["scenarios"]
    snapshot.update(
        model=redesign_inputs.ENGINE,
        effective_model_inputs=flat,
        model_output=output,
        value_per_share=output["vps"],
        equity_value_m=output["equity_value"],
        operating_ev_m=output["operating_ev"],
    )
    return json.dumps(
        snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
