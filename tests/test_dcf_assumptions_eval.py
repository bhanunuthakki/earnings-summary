"""Golden grader for dcf_assumptions. Uses a fake generator; no model call."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from dcf.assumption_prompt import build_prompt
from evals.dcf_assumptions import (
    DEFAULT_GOLDEN_RELPATH,
    DcfAssumptionsCase,
    load_dcf_assumptions_golden,
    run_dcf_assumptions_eval,
)
from llm.contracts import (
    DcfAnchorClass,
    DcfAssumptionRecord,
    DcfAssumptionsPayload,
    DcfSegmentGrowth,
)
from llm.structured import StructuredParseError

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / DEFAULT_GOLDEN_RELPATH


def _mid(case: DcfAssumptionsCase, key: str, default: float, *, use_bounds: bool = True) -> float:
    bound = case.bounds.get(key)
    return default if bound is None or not use_bounds else (bound[0] + bound[1]) / 2


def _segment(near: float) -> DcfSegmentGrowth:
    terminal = 0.02 if near > 0.02 else (-0.02 if near < -0.02 else near - 0.01)
    return DcfSegmentGrowth(near_term_growth=near, terminal_growth=terminal)


def _payload(
    case: DcfAssumptionsCase,
    *,
    growth: float,
    anchor_class: DcfAnchorClass = "reported_fact",
    anchor: str = "Reported history above.",
    confidence: Literal["high", "medium", "low"] = "medium",
    use_bounds: bool = True,
) -> DcfAssumptionsPayload:
    fields = [f"segments.{s}.near_term_growth" for s in case.seg_keys] + [
        "near_term_op_margin",
        "terminal_op_margin",
        "exit_multiple",
    ]
    return DcfAssumptionsPayload.model_validate(
        {
            "dcf_applicable": True,
            "business_model": "operating",
            "valuation_model": "fcff_dcf",
            "valuation_model_suggestion": "",
            "segments": {
                s: _segment(
                    _mid(case, f"segments.{s}.near_term_growth", growth, use_bounds=use_bounds)
                )
                for s in case.seg_keys
            },
            "near_term_op_margin": _mid(case, "near_term_op_margin", 0.30),
            "terminal_op_margin": _mid(case, "terminal_op_margin", 0.33),
            "tax_rate": 0.21,
            "capex_pct_revenue_2026": 0.04,
            "terminal_capex_da": 1.05,
            "terminal_method": "Exit multiple",
            "exit_basis": "EV/EBITDA",
            "exit_multiple": 12.0,
            "terminal_growth_g": 0.025,
            "narrative": "Synthetic.",
            "reasoning": "Synthetic.",
            "consensus_comparison": "Growth is 6 pts below consensus because units grow 5%.",
            "assumption_records": [
                DcfAssumptionRecord(
                    field=f,
                    anchor_class=anchor_class,
                    anchor=anchor,
                    counter_argument="Could be wrong.",
                    confidence=confidence,
                    observable="10-Q.",
                )
                for f in fields
            ],
        }
    )


def test_golden_set_loads_and_every_context_renders() -> None:
    cases = load_dcf_assumptions_golden(GOLDEN)
    assert len(cases) >= 5
    for case in cases:
        prompt = build_prompt(case.ticker, case.context, case.seg_keys)
        assert case.context in prompt
        assert "anchor near-term to consensus" not in prompt


def test_evidence_supported_answers_pass_and_consensus_echo_fails() -> None:
    def good(case: DcfAssumptionsCase) -> DcfAssumptionsPayload:
        return _payload(case, growth=0.10, confidence="low")

    summary = run_dcf_assumptions_eval(golden_path=GOLDEN, code_root=ROOT, generate_fn=good)
    assert [c.passed for c in summary.cases] == [True] * len(summary.cases), [
        c.judge_rationale for c in summary.cases
    ]

    def consensus_echo(case: DcfAssumptionsCase) -> DcfAssumptionsPayload:
        return _payload(case, growth=0.18, use_bounds=False)

    echo = run_dcf_assumptions_eval(
        golden_path=GOLDEN, code_root=ROOT, generate_fn=consensus_echo, limit=1
    )
    assert echo.cases[0].passed is False
    assert "bound:segments.Total company.near_term_growth" in (echo.cases[0].judge_rationale or "")


def test_capture_gap_cited_as_fact_fails() -> None:
    cases = {c.case_id: c for c in load_dcf_assumptions_golden(GOLDEN)}
    case = cases["dcfa-capture-gap-not-fact"]

    def cites_gap(_case: DcfAssumptionsCase) -> DcfAssumptionsPayload:
        return _payload(_case, growth=0.06, anchor="Paid clicks grew 6% in FY2025.")

    summary = run_dcf_assumptions_eval(golden_path=GOLDEN, code_root=ROOT, generate_fn=cites_gap)
    result = next(r for r in summary.cases if r.case_id == case.case_id)
    assert result.passed is False and "gap:paid click" in (result.judge_rationale or "")


def test_thin_evidence_high_confidence_fails() -> None:
    def overconfident(case: DcfAssumptionsCase) -> DcfAssumptionsPayload:
        return _payload(case, growth=0.10, confidence="high")

    summary = run_dcf_assumptions_eval(
        golden_path=GOLDEN, code_root=ROOT, generate_fn=overconfident
    )
    result = next(r for r in summary.cases if r.case_id == "dcfa-thin-evidence-confidence")
    assert result.passed is False and "confidence:" in (result.judge_rationale or "")


def test_missing_records_fail_the_records_check() -> None:
    def no_records(case: DcfAssumptionsCase) -> DcfAssumptionsPayload:
        return _payload(case, growth=0.10).model_copy(update={"assumption_records": []})

    summary = run_dcf_assumptions_eval(
        golden_path=GOLDEN, code_root=ROOT, generate_fn=no_records, limit=1
    )
    assert summary.cases[0].passed is False
    assert "records: missing record" in (summary.cases[0].judge_rationale or "")


def test_parse_failure_on_first_case_twice_aborts() -> None:
    from evals.harness import EvalAbortError

    def broken(_case: DcfAssumptionsCase) -> DcfAssumptionsPayload:
        raise StructuredParseError("bad json")

    with pytest.raises(EvalAbortError):
        run_dcf_assumptions_eval(golden_path=GOLDEN, code_root=ROOT, generate_fn=broken)


def test_invalid_golden_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "g.json"
    bad.write_text(
        '{"purpose": "dcf_assumptions", "cases": [{"id": "x", "ticker": "T", "seg_keys": ["A"], "context": "c"}]}'
    )
    with pytest.raises(ValueError, match="needs at least one"):
        load_dcf_assumptions_golden(bad)
