"""Reconstruct memo values and exact source wording without judging paraphrases.

The caller must independently verify issuer, snapshot and sealed extraction
membership. This module verifies numerical display and retained wording. It
cannot certify the meaning of an arbitrary paraphrase or analyst inference.
"""

from __future__ import annotations

import ast
import html
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


_LIGATURE = re.compile("[\ufb00-\ufb06]")


def readable_source_text(text: str) -> str:
    """Reader form of retained source text; the stored bytes never change.

    Extraction can retain HTML entities, table-cell separators and typographic
    ligatures. Decoding them is presentation, not rewording, so verification
    accepts either this form or the normalized raw form.
    """
    decoded = html.unescape(text).replace("\u241f", " | ").replace("\u00a0", " ")
    decoded = _LIGATURE.sub(lambda match: unicodedata.normalize("NFKC", match.group()), decoded)
    return _normalized(decoded)


def _source_forms(texts: list[str]) -> tuple[str, ...]:
    # Adjacent extraction nodes can split a word ("S", "&atilde;", "o Paulo").
    joined = ("".join(texts), " ".join(texts))
    return tuple(
        dict.fromkeys(form(item) for item in joined for form in (_normalized, readable_source_text))
    )


class MemoSourceExcerpt(BaseModel):
    """A verbatim contiguous excerpt of ordered retained evidence nodes."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence_node_ids: tuple[str, ...] = Field(min_length=1)
    text: str = Field(min_length=1)


class MemoDerivedFigure(BaseModel):
    """Analyst arithmetic over figures already bound to the same claim."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    expression: str = Field(min_length=1, max_length=200)
    operands: dict[str, str] = Field(min_length=1, max_length=8)
    displayed_value: str = Field(min_length=1)
    decimals: int = Field(ge=0, le=3)
    percent: bool = False


# Calendar references are not financial figures; every other number must bind.
_CALENDAR = re.compile(r"\b(?:Q[1-4]|[12]H|H[12]|FY|(?:19|20)\d{2})\b")
_QUOTED = re.compile(r"[\u201c\"]([^\u201d\"]{12,})[\u201d\"]")


def _figure(token: str) -> str:
    """Compare magnitude only: sign, grouping and unit markers stay in wording."""
    digits = re.sub(r"[^0-9.]", "", token).strip(".")
    if "." in digits:
        digits = digits.rstrip("0").rstrip(".")
    return digits.lstrip("0") or "0"


def _figures(text: str) -> set[str]:
    return {_figure(match.group()) for match in _NUMBER.finditer(text)}


def _evaluate(node: ast.AST, operands: dict[str, Decimal]) -> Decimal:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, operands)
    if isinstance(node, ast.Name) and node.id in operands:
        return operands[node.id]
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        return Decimal(str(node.value))
    if isinstance(node, ast.BinOp):
        left, right = _evaluate(node.left, operands), _evaluate(node.right, operands)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_evaluate(node.operand, operands)
    raise MemoClaimSupportError("memo_derived_expression_unsupported")


def derived_figure_display(figure: MemoDerivedFigure) -> str:
    """Replay the arithmetic and return its canonical display."""
    try:
        operands = {
            name: Decimal(re.sub(r"[,%\s]", "", token).strip("()"))
            for name, token in figure.operands.items()
        }
        value = _evaluate(ast.parse(figure.expression, mode="eval"), operands)
    except (SyntaxError, ArithmeticError, ValueError) as exc:
        raise MemoClaimSupportError("memo_derived_expression_invalid") from exc
    if figure.percent:
        value *= 100
    return f"{value:,.{figure.decimals}f}" + ("%" if figure.percent else "")


def verify_memo_analysis_text(
    passage: str,
    excerpts: tuple[tuple[str, tuple[str, ...]], ...],
    declared_values: tuple[str, ...] = (),
    derived: tuple[MemoDerivedFigure, ...] = (),
) -> None:
    """Bind analyst prose to verbatim excerpts, admitted values and replayed arithmetic.

    Each excerpt arrives with its pinned source texts in order: retained
    evidence nodes, or the owner's hash-pinned thesis. Every quoted
    span must occur in a cited excerpt; every non-calendar number must equal a
    figure in a cited excerpt, an admitted value or a replayed derivation whose
    own operands are bound the same way. Semantics remain analyst judgment.
    """
    cited: list[str] = []
    for excerpt, texts in excerpts:
        wanted = readable_source_text(excerpt)
        if not texts or not any(wanted in form for form in _source_forms(list(texts))):
            raise MemoClaimSupportError("memo_excerpt_not_in_source")
        cited.append(wanted)
    supported: set[str] = set()
    for text in (*cited, *declared_values):
        supported |= _figures(text)
    for figure in derived:
        if any(_figure(token) not in supported for token in figure.operands.values()):
            raise MemoClaimSupportError("memo_derived_operand_unbound")
        if derived_figure_display(figure) != figure.displayed_value:
            raise MemoClaimSupportError("memo_derived_value_mismatch")
        supported.add(_figure(figure.displayed_value))
        # Written-out formulas show their own constants, such as 1 - rate.
        supported.update(
            _figure(str(node.value))
            for node in ast.walk(ast.parse(figure.expression, mode="eval"))
            if isinstance(node, ast.Constant)
        )
    text = readable_source_text(passage)
    for quoted in _QUOTED.findall(text):
        if not any(quoted.strip() in item for item in cited):
            raise MemoClaimSupportError("memo_quoted_span_not_in_excerpt")
    if _figures(_CALENDAR.sub(" ", text)) - supported:
        raise MemoClaimSupportError("memo_analysis_number_unbound")


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

    source_text = read_evidence_node_texts(conn, evidence_node_ids)
    if source_text and text not in _source_forms(source_text):
        raise MemoClaimSupportError("memo_reported_passage_not_reconstructed")
    declared = [*calculated_values, *(verified_value_display(conn, item) for item in values)]
    verify_memo_numeric_population(text, tuple(declared), source_wording_verified=bool(source_text))


def read_evidence_node_texts(conn: sqlite3.Connection, node_ids: tuple[str, ...]) -> list[str]:
    texts: list[str] = []
    for node_id in node_ids:
        row = conn.execute("SELECT text FROM evidence_nodes WHERE node_id=?", (node_id,)).fetchone()
        if row is None or not isinstance(row[0], str) or not _normalized(row[0]):
            raise MemoClaimSupportError("memo_reported_source_text_unavailable")
        texts.append(row[0])
    return texts


def verified_value_display(conn: sqlite3.Connection, item: MemoReportedValue) -> str:
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
    return expected
