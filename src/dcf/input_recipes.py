"""Closed dispatch for reviewed input recipes; no automatic method selection."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime

from dcf import cashflow_inputs, meli_inputs
from dcf.input_evidence import (
    InputEvidenceError,
    ModelInputReceipt,
    ModelInputRequest,
    SourceReadContext,
)


def model_engine(recipe: str) -> str:
    if recipe == meli_inputs.RECIPE:
        return "meli_platform_sotp"
    if recipe == cashflow_inputs.RECIPE:
        return cashflow_inputs.ENGINE
    raise InputEvidenceError("input_recipe_unsupported")


def effective_numeric_inputs(recipe: str, values: Mapping[str, object]) -> dict[str, float]:
    model_engine(recipe)
    return (
        meli_inputs.effective_numeric_inputs(values)
        if recipe == meli_inputs.RECIPE
        else cashflow_inputs.effective_numeric_inputs(values)
    )


def prepare_model_inputs(
    conn: sqlite3.Connection,
    request: ModelInputRequest,
    *,
    effective_inputs: Mapping[str, float],
    as_of: datetime,
    source_context: SourceReadContext | None = None,
) -> tuple[dict[str, float], ModelInputReceipt]:
    model_engine(request.recipe)
    if request.recipe == meli_inputs.RECIPE:
        return meli_inputs.prepare_meli_inputs(
            conn,
            request,
            effective_inputs=effective_inputs,
            as_of=as_of,
            source_context=source_context,
        )
    return cashflow_inputs.prepare_cashflow_inputs(
        conn,
        request,
        effective_inputs=effective_inputs,
        as_of=as_of,
        source_context=source_context,
    )


def verify_model_input_receipt(
    conn: sqlite3.Connection,
    receipt: ModelInputReceipt,
    *,
    effective_inputs: Mapping[str, float],
    as_of: datetime,
    source_context: SourceReadContext | None = None,
) -> ModelInputReceipt:
    model_engine(receipt.recipe)
    if receipt.recipe == meli_inputs.RECIPE:
        return meli_inputs.verify_meli_inputs(
            conn,
            receipt,
            effective_inputs=effective_inputs,
            as_of=as_of,
            source_context=source_context,
        )
    return cashflow_inputs.verify_cashflow_inputs(
        conn,
        receipt,
        effective_inputs=effective_inputs,
        as_of=as_of,
        source_context=source_context,
    )


def model_output(recipe: str, inputs: Mapping[str, float]) -> dict[str, object]:
    model_engine(recipe)
    return (
        meli_inputs.model_output(inputs)
        if recipe == meli_inputs.RECIPE
        else cashflow_inputs.model_output(inputs)
    )
