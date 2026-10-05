"""Reconstruct memo values and exact source wording without judging paraphrases.

The caller must independently verify issuer, snapshot and sealed extraction
membership. This module verifies numerical display and retained wording. It
cannot certify the meaning of an arbitrary paraphrase or analyst inference.
"""

from __future__ import annotations

import math
import re
import sqlite3
import unicodedata
from collections import Counter
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sources.report_financials import (
    FinancialEvidenceReference,
    FinancialTableCell,
    read_financial_evidence,
)

# Include signs, grouped digits, exponents and percentage markers. An unbound
# date, fiscal year or percentage must not disappear from the claim population.
_NUMERIC_TOKEN = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?(?:\s*%)?"
_NUMBER = re.compile(rf"\({_NUMERIC_TOKEN}\)|{_NUMERIC_TOKEN}")


class MemoClaimSupportError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class MemoReportedValue(BaseModel):
    """An exact financial reference and its declared numerical display."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    reference: FinancialEvidenceReference
    displayed_value: str = Field(min_length=1)
    display_format: Literal["number1", "number2", "financial_reader"]
    scale: Literal[1, 1000000] = 1


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def financial_reader_value(value: Decimal, concept: str) -> str:
    """Reconstruct the existing financial table's float and sign display."""
    displayed = float(value)
    if not math.isfinite(displayed):
        raise MemoClaimSupportError("memo_reported_value_not_finite")
    if concept == "eps_diluted":
        return f"{displayed:.2f}"
    return f"({abs(displayed):.1f})" if displayed < 0 else f"{displayed:.1f}"


def verify_memo_numeric_population(
    passage: str, declared_values: tuple[str, ...], *, source_wording_verified: bool = False
) -> None:
    """Bind every displayed number and unit marker to a verified declaration."""
    declared = Counter(declared_values)
    displayed = Counter(match.group() for match in _NUMBER.finditer(_normalized(passage)))
    if declared - displayed:
        raise MemoClaimSupportError("memo_reported_value_not_in_passage")
    if not source_wording_verified and displayed != declared:
        raise MemoClaimSupportError("memo_reported_numeric_population_unverified")


def verify_reported_memo_claim(
    conn: sqlite3.Connection,
    passage: str,
    evidence_node_ids: tuple[str, ...] = (),
    values: tuple[MemoReportedValue, ...] = (),
    *,
    calculated_values: tuple[str, ...] = (),
) -> None:
    """Reject unsupported numbers and non-exact node-backed reported wording.

    Node support reconstructs the complete passage from the supplied nodes in
    order. Whitespace and canonical Unicode composition can change; words,
    punctuation, numbers and qualification cannot. Source quotation permits
    dates and reported figures without inventing a canonical financial role.

    With value support alone, every numerical occurrence must match a declared
    admitted value. This proves the numerical display, not free-form semantics.
    The caller owns claim classification and exact snapshot membership.
    Calculated values must already pass the caller's operand and replay checks.
    """
    text = _normalized(passage)
    if not text:
        raise MemoClaimSupportError("memo_reported_passage_empty")
    if not evidence_node_ids and not values and not calculated_values:
        raise MemoClaimSupportError("memo_reported_claim_support_missing")
    if len(set(evidence_node_ids)) != len(evidence_node_ids):
        raise MemoClaimSupportError("memo_reported_node_membership_duplicate")

    source_text: list[str] = []
    for node_id in evidence_node_ids:
        row = conn.execute("SELECT text FROM evidence_nodes WHERE node_id=?", (node_id,)).fetchone()
        if row is None or not isinstance(row[0], str) or not _normalized(row[0]):
            raise MemoClaimSupportError("memo_reported_source_text_unavailable")
        source_text.append(row[0])
    if source_text and text != _normalized(" ".join(source_text)):
        raise MemoClaimSupportError("memo_reported_passage_not_reconstructed")

    declared: list[str] = list(calculated_values)
    for item in values:
        # Series values use native units, outside the memo's table display contract.
        if item.reference.reader_kind != "report_table":
            raise MemoClaimSupportError("memo_financial_series_reference_unsupported")
        cell = read_financial_evidence(conn, item.reference)
        if cell is not None and not isinstance(cell, FinancialTableCell):
            raise MemoClaimSupportError("memo_financial_series_reference_unsupported")
        if cell is None or cell.display_value is None:
            raise MemoClaimSupportError("memo_reported_value_not_admitted")
        value = cell.display_value * Decimal(item.scale)
        if not value.is_finite():
            raise MemoClaimSupportError("memo_reported_value_not_finite")
        if item.display_format == "financial_reader":
            if item.scale != 1:
                raise MemoClaimSupportError("memo_reader_scale_mismatch")
            expected = financial_reader_value(value, item.reference.concept)
        else:
            expected = format(value, ".2f" if item.display_format == "number2" else ".1f")
        if item.displayed_value != expected:
            raise MemoClaimSupportError("memo_reported_value_mismatch")
        declared.append(expected)
    verify_memo_numeric_population(text, tuple(declared), source_wording_verified=bool(source_text))
