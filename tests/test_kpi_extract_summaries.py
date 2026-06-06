# These tests exercise internal extraction seams (_build_manifest, _llm_extract, …).
# pyright: reportPrivateUsage=false
"""Tests for compute.kpi_extract_summaries value parsing.

Regression for the VEEV extraction abort: Haiku returned a non-numeric KPI value
("Q1 2026 ... InvalidOperation: ConversionSyntax"), and `_build_manifest` caught
only (TypeError, ValueError) — but `Decimal("N/A")` raises decimal.InvalidOperation
(an ArithmeticError), so one bad value aborted the whole ticker. `parse_decimal_value`
must degrade at the per-KPI scope (skip the value, keep extracting).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

import compute.kpi_extract_summaries as kes
from compute.kpi_extract_summaries import (
    _build_manifest,
    _canonical_units_from_holdings,
    _llm_extract,
    parse_decimal_value,
)
from models.facts import FiscalPeriodType, Unit


def test_parse_decimal_value_parses_clean_numbers() -> None:
    assert parse_decimal_value(12.5) == Decimal("12.5")
    assert parse_decimal_value("17.8") == Decimal("17.8")
    assert parse_decimal_value(0) == Decimal("0")
    assert parse_decimal_value("-3.2") == Decimal("-3.2")


def test_parse_decimal_value_returns_none_for_unparseable() -> None:
    # Each of these raised decimal.InvalidOperation (or TypeError) and used to
    # abort the ticker; they must now skip just that KPI.
    for bad in ("N/A", "~17%", "n.m.", "", "1,200", "TBD", None):
        assert parse_decimal_value(bad) is None


# --- Prompt unit vocabulary (problem 1: out-of-enum unit tokens) --------------


def test_llm_extract_prompt_offers_only_valid_unit_enum_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prompt must not instruct the model to return tokens absent from the
    `Unit` enum. The old prompt offered "usd" and "ratio_per_unit", which
    `Unit(...)` then coerced to ACTUAL — storing dollar KPIs as raw `actual` and
    breaking every break-rule declared in `millions`."""
    captured: dict[str, str] = {}

    def fake_call(prompt: str, model: str | None = None) -> str:
        captured["prompt"] = prompt
        return '{"GMV": {"value": 1200000000, "unit": "actual", "confidence": 0.9}}'

    monkeypatch.setattr(kes, "_call_claude", fake_call)
    out = _llm_extract("MELI", "Q1 2026", ["GMV"], "GMV reached $1.2 billion this quarter.")

    prompt = captured["prompt"]
    # The invalid tokens are gone entirely.
    assert "ratio_per_unit" not in prompt
    assert '"usd"' not in prompt
    # Every offered token is a real Unit enum value.
    for token in ('"percent"', '"actual"', '"count"', '"ratio"', '"bps"'):
        assert token in prompt
        assert token.strip('"') in {u.value for u in Unit}
    # The money convention is spelled out with a full-figure example.
    assert "1200000000" in prompt
    # Sanity: the canned response still parses through the existing path.
    assert out == {"GMV": {"value": 1200000000, "unit": "actual", "confidence": 0.9}}


# --- Canonical-unit mapping from holdings break-rules (problem 2) -------------


def _holdings(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {"break_rules": [], "business_model_rules": []}
    base.update(kw)
    return base


def test_canonical_units_matches_break_rule_unit_across_name_spellings() -> None:
    """The break-rule spells the metric differently from the tier_1 label, but
    both normalize equal — the canonical unit still attaches. This is the RBRK
    net-new-ARR case ("($)" tier_1 label vs "(USD millions)" rule label)."""
    holdings = _holdings(
        break_rules=[
            {
                "rule_id": "r1",
                "kpi_name": "Net New Subscription ARR (USD millions)",
                "comparator": "lt",
                "threshold": 80,
                "unit": "millions",
                "narrative": "x",
            }
        ]
    )
    out = _canonical_units_from_holdings(holdings, ["Net new subscription ARR ($)"])
    assert out == {"Net new subscription ARR ($)": Unit.MILLIONS}


def test_canonical_units_skips_non_enum_rule_units() -> None:
    """A rule unit outside the `Unit` enum (e.g. KVYO's derived "percent_decel")
    has no dimensional meaning for reconciliation and is skipped, leaving the
    metric with no canonical override (LLM unit used as-is)."""
    holdings = _holdings(
        break_rules=[
            {
                "rule_id": "kvyo_revenue_decel",
                "kpi_name": "Revenue YoY Growth",
                "comparator": "lt",
                "threshold": 50,
                "unit": "percent_decel",
                "narrative": "x",
            }
        ]
    )
    out = _canonical_units_from_holdings(holdings, ["Revenue YoY Growth"])
    assert out == {}


def test_canonical_units_reads_both_rule_arrays_and_ignores_unlisted_names() -> None:
    holdings = _holdings(
        break_rules=[
            {
                "rule_id": "a",
                "kpi_name": "Gross margin",
                "comparator": "lt",
                "threshold": 70,
                "unit": "percent",
                "narrative": "x",
            },
        ],
        business_model_rules=[
            {
                "rule_id": "b",
                "kpi_name": "Monthly ARPAC (USD)",
                "comparator": "lt",
                "threshold": 9,
                "unit": "actual",
                "narrative": "x",
            },
        ],
    )
    out = _canonical_units_from_holdings(
        holdings, ["Gross margin", "Monthly ARPAC", "Untracked KPI"]
    )
    assert out == {"Gross margin": Unit.PERCENT, "Monthly ARPAC": Unit.ACTUAL}
    assert "Untracked KPI" not in out


def test_build_manifest_threads_canonical_units_onto_manifest() -> None:
    """`_build_manifest` carries the canonical-unit map onto the manifest so
    persist_manifest can reconcile; the extracted unit itself is left intact at
    build time (reconciliation happens at persist)."""
    extracted: dict[str, dict[str, object]] = {
        "Net new subscription ARR ($)": {"value": 115000000, "unit": "actual", "confidence": 0.9},
    }
    canonical = {"Net new subscription ARR ($)": Unit.MILLIONS}
    manifest = _build_manifest(
        "RBRK",
        datetime(2027, 1, 31),
        FiscalPeriodType.Q4,
        7140,
        extracted,
        canonical,
    )
    assert manifest.canonical_units == canonical
    # Build time keeps the extracted unit; reconciliation is persist's job.
    assert manifest.values[0].unit is Unit.ACTUAL
    assert manifest.values[0].value == Decimal("115000000")


def test_build_manifest_canonical_units_default_empty() -> None:
    """Called without a canonical map (back-compat), the manifest carries none."""
    extracted: dict[str, dict[str, object]] = {
        "GMV": {"value": 5, "unit": "actual", "confidence": 0.9}
    }
    manifest = _build_manifest("YY", datetime(2026, 3, 31), FiscalPeriodType.Q1, 1, extracted)
    assert manifest.canonical_units == {}
