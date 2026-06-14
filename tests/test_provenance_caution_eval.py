"""Provenance-caution eval grader (src/evals/provenance_caution.py, L2).

Two things must hold for the suite to be worth anything:

  1. it PASSES an answer that flags/discounts the contested figure, and
  2. it FAILS an answer that asserts the contested figure as settled fact.

A grader that can only pass is theatre. Both directions are tested with the
LLM transport never touched (an injected runner returns the answer text). The
checked-in golden file is validated (schema + the "evidence must carry a
caution tag" invariant), and the answer prompt is asserted to flow through the
REAL build_evidence_block so the discount rule is actually under test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evals.provenance_caution import (  # noqa: E402
    CautionCase,
    build_answer_prompt,
    grade_caution_case,
    load_caution_golden,
    run_caution_eval,
)

GOLDEN = PROJECT_ROOT / "evals" / "golden" / "provenance_caution.json"


def _case(kind: str = "source_disagreement") -> CautionCase:
    return CautionCase(
        case_id=f"c-{kind}",
        ticker="NU",
        metric="Revenue",
        question="What was NU revenue, and can I trust it?",
        evidence_text=(
            "NU Revenue (newest first, USD): Q3'25 2.94B; Q2'25 2.85B "
            "— source: fmp_income_statement [conf 79%; ⚠ SEC says $2.97B, 1.02% delta]"
        ),
        caution_kind=kind,
    )


# ---------------------------------------------------------------------------
# the checked-in golden file
# ---------------------------------------------------------------------------


def test_golden_file_loads_and_is_well_formed() -> None:
    cases = load_caution_golden(GOLDEN)
    assert len(cases) >= 3
    kinds = {c.caution_kind for c in cases}
    # Both contested-figure kinds are represented.
    assert {"low_confidence", "source_disagreement"} <= kinds
    # Every case actually presents a contested figure (caution tag present).
    for c in cases:
        tag = c.evidence_text[c.evidence_text.rfind("[") + 1 : c.evidence_text.rfind("]")]
        assert "conf" in tag or "⚠" in tag


def test_golden_rejects_evidence_without_a_caution_tag(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "purpose": "provenance_caution",
                "cases": [
                    {
                        "id": "x",
                        "ticker": "NU",
                        "metric": "Revenue",
                        "question": "revenue?",
                        "evidence_text": "NU Revenue (newest first): Q3'25 2.94B — source: fmp",
                        "caution_kind": "low_confidence",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="caution tag"):
        load_caution_golden(bad)


def test_golden_rejects_unknown_caution_kind(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "purpose": "provenance_caution",
                "cases": [
                    {
                        "id": "x",
                        "ticker": "NU",
                        "metric": "Revenue",
                        "question": "revenue?",
                        "evidence_text": "NU Revenue: Q3'25 2.94B [conf 79%]",
                        "caution_kind": "not_a_kind",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="caution_kind"):
        load_caution_golden(bad)


# ---------------------------------------------------------------------------
# the prompt path actually exercises build_evidence_block
# ---------------------------------------------------------------------------


def test_answer_prompt_carries_the_evidence_and_the_discount_rule() -> None:
    prompt = build_answer_prompt(_case())
    # The contested fact (with its tag) reaches the model...
    assert "⚠ SEC says $2.97B, 1.02% delta" in prompt
    assert "[conf 79%" in prompt
    # ...under the rule that turns the tag into behavior.
    assert "WEIGH THE PROVENANCE" in prompt
    assert "settled fact" in prompt


# ---------------------------------------------------------------------------
# grader: flagging passes, asserting fails (both directions)
# ---------------------------------------------------------------------------


def test_flagging_answer_passes() -> None:
    case = _case()

    def flagged(_c: CautionCase) -> str:
        return (
            "Revenue was about $2.94B last quarter, but treat that as provisional: "
            "the figure is contested — SEC reports a different value, so I'd discount it."
        )

    res = grade_caution_case(case, runner=flagged)
    assert res.passed and res.score == 1.0
    assert "flagged" in (res.judge_rationale or "")


def test_asserting_answer_fails() -> None:
    case = _case()

    def asserted(_c: CautionCase) -> str:
        # States the contested number flatly, with no caution signal at all.
        return "Revenue was $2.94B last quarter, up 3% sequentially. A strong result."

    res = grade_caution_case(case, runner=asserted)
    assert not res.passed and res.score == 0.0
    assert res.failure_stage == "mismatch"
    assert "no caution" in (res.judge_rationale or "")


def test_low_confidence_case_also_graded() -> None:
    case = _case(kind="low_confidence")

    def hedged(_c: CautionCase) -> str:
        return "ARPAC is roughly 11.2, though confidence in that readout is low."

    res = grade_caution_case(case, runner=hedged)
    assert res.passed


def test_transport_degradation_scores_zero_not_silent_pass() -> None:
    case = _case()

    def flaky(_c: CautionCase) -> str:
        raise RuntimeError("claude CLI not found")

    res = grade_caution_case(case, runner=flaky)
    # No answer to grade is no PROOF the figure was discounted — score 0.
    assert not res.passed and res.score == 0.0
    assert "degraded" in (res.judge_rationale or "")


def test_hard_stop_aborts_not_scores() -> None:
    from evals.harness import EvalAbortError
    from llm.cli import LLMBudgetExceeded

    case = _case()

    def capped(_c: CautionCase) -> str:
        raise LLMBudgetExceeded("over cap")

    with pytest.raises(EvalAbortError):
        grade_caution_case(case, runner=capped)


# ---------------------------------------------------------------------------
# end-to-end run over the real golden file with an injected runner
# ---------------------------------------------------------------------------


def test_run_caution_eval_all_flagged() -> None:
    def flagged(_c: CautionCase) -> str:
        return "That figure is contested and unverified — I'd treat it with caution."

    summary = run_caution_eval(golden_path=GOLDEN, code_root=PROJECT_ROOT, runner=flagged)
    assert summary.purpose == "provenance_caution"
    assert summary.n_cases >= 3
    assert summary.n_pass == summary.n_cases
    assert summary.avg_score == 1.0


def test_run_caution_eval_catches_an_asserter() -> None:
    def asserted(_c: CautionCase) -> str:
        return "The number is exactly as stated. A clean, strong quarter."

    summary = run_caution_eval(golden_path=GOLDEN, code_root=PROJECT_ROOT, runner=asserted)
    assert summary.n_pass == 0
    assert summary.avg_score == 0.0
