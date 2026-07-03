"""scenario_prior — the governed per-name Bull/Base/Bear weight setter + its grader.

Covers:
  * coerce_weights — the non-degenerate simplex enforcement (decimals, percent
    form, renormalization, and every rejection path);
  * propose vs generate — propose raises StructuredParseError (the eval scores it);
    generate degrades it to the global prior (production); an empty anchor block
    short-circuits to global with no call;
  * the global fallback stays in lockstep with dcf.scenario_reward's prior;
  * the mode-A grader — skew + grounded scoring, golden loading, run orchestration.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf import scenario_prior as sp  # noqa: E402
from dcf.scenario_reward import SCENARIO_PROBABILITIES  # noqa: E402
from evals import scenario_prior as sp_eval  # noqa: E402
from llm.structured import StructuredParseError  # noqa: E402

_TODAY = date(2026, 7, 2)


def _payload(
    bull: float, base: float, bear: float, rationale: str = "because"
) -> dict[str, object]:
    return {"bull": bull, "base": base, "bear": bear, "rationale": rationale}


def _fixed(payload: dict[str, object]) -> sp.ScenarioPriorCall:
    def call(_prompt: str) -> dict[str, object]:
        return payload

    return call


# --------------------------------------------------------------------------- #
# 1. coerce_weights — the simplex enforcement
# --------------------------------------------------------------------------- #
def test_coerce_accepts_valid_decimals() -> None:
    assert sp.coerce_weights(_payload(0.2, 0.5, 0.3)) == (0.2, 0.5, 0.3)


def test_coerce_rescales_percent_form() -> None:
    w = sp.coerce_weights(_payload(20, 50, 30))
    assert w is not None
    bull, base, bear = w
    assert abs(bull - 0.2) < 1e-9 and abs(base - 0.5) < 1e-9 and abs(bear - 0.3) < 1e-9


def test_coerce_renormalizes_near_one_sum() -> None:
    # Sums to 1.01 — inside tolerance, renormalized to exactly 1.
    w = sp.coerce_weights(_payload(0.21, 0.50, 0.30))
    assert w is not None
    assert abs(sum(w) - 1.0) < 1e-9


def test_coerce_rejects_degenerate() -> None:
    assert sp.coerce_weights(_payload(0.7, 0.25, 0.05)) is None  # base below 0.30 plurality
    assert sp.coerce_weights(_payload(0.02, 0.5, 0.48)) is None  # a leg below 0.05
    assert sp.coerce_weights(_payload(0.95, 0.03, 0.02)) is None  # a leg above 0.90 (+ base low)
    assert sp.coerce_weights(_payload(0.1, 0.1, 0.1)) is None  # sum far from 1
    assert sp.coerce_weights({"bull": "x", "base": 0.5, "bear": 0.3}) is None  # non-numeric
    assert sp.coerce_weights({"base": 0.5, "bear": 0.3}) is None  # missing bull


# --------------------------------------------------------------------------- #
# 2. propose vs generate — raise vs degrade
# --------------------------------------------------------------------------- #
def test_propose_returns_llm_prior_on_valid() -> None:
    prior = sp.propose_scenario_prior(
        "NU",
        anchor_block="thesis...",
        today=_TODAY,
        call=_fixed(_payload(0.2, 0.5, 0.3, "risk skew")),
    )
    assert prior.set_by == "llm"
    assert prior.is_per_name
    assert (prior.bull, prior.base, prior.bear) == (0.2, 0.5, 0.3)
    assert prior.rationale == "risk skew"
    assert prior.as_of == "2026-07-02"


def test_propose_degrades_degenerate_weights_to_global() -> None:
    prior = sp.propose_scenario_prior(
        "NU", anchor_block="thesis", today=_TODAY, call=_fixed(_payload(0.8, 0.15, 0.05))
    )
    assert prior.set_by == "global"
    assert prior.weights() == sp.GLOBAL_WEIGHTS


def test_propose_degrades_empty_rationale_to_global() -> None:
    prior = sp.propose_scenario_prior(
        "NU", anchor_block="thesis", today=_TODAY, call=_fixed(_payload(0.2, 0.5, 0.3, "   "))
    )
    assert prior.set_by == "global"


def test_propose_propagates_parse_error() -> None:
    def boom(_prompt: str) -> dict[str, object]:
        raise StructuredParseError("bad json", raw_head="...")

    import pytest

    with pytest.raises(StructuredParseError):
        sp.propose_scenario_prior("NU", anchor_block="thesis", today=_TODAY, call=boom)


def test_generate_degrades_parse_error_to_global() -> None:
    def boom(_prompt: str) -> dict[str, object]:
        raise StructuredParseError("bad json", raw_head="...")

    prior = sp.generate_scenario_prior("NU", anchor_block="thesis", today=_TODAY, call=boom)
    assert prior.set_by == "global"


def test_generate_short_circuits_empty_anchor_without_call() -> None:
    def must_not_call(_prompt: str) -> dict[str, object]:
        raise AssertionError("should not spend a call with no anchors")

    prior = sp.generate_scenario_prior("NU", anchor_block="   ", today=_TODAY, call=must_not_call)
    assert prior.set_by == "global"


def test_generate_propagates_hard_stop() -> None:
    # generate only catches StructuredParseError — a budget/setup hard stop (any
    # other exception) propagates as configuration, not parse quality.
    def hard(_prompt: str) -> dict[str, object]:
        raise RuntimeError("budget exceeded")

    import pytest

    with pytest.raises(RuntimeError):
        sp.generate_scenario_prior("NU", anchor_block="thesis", today=_TODAY, call=hard)


def test_global_prior_matches_scenario_reward() -> None:
    # The "no per-name prior" reward must be exactly today's global 25/50/25.
    assert sp.GLOBAL_WEIGHTS == SCENARIO_PROBABILITIES
    g = sp.global_prior(_TODAY)
    assert not g.is_per_name and g.set_by == "global"


# --------------------------------------------------------------------------- #
# 3. The mode-A grader
# --------------------------------------------------------------------------- #
def _prior(
    bull: float, base: float, bear: float, set_by: str = "llm", rationale: str = "r"
) -> sp.ScenarioPrior:
    return sp.ScenarioPrior(bull, base, bear, rationale, set_by, "2026-01-01")


def _case(skew: str) -> sp_eval.ScenarioPriorCase:
    return sp_eval.ScenarioPriorCase("c1", "NU", "## THESIS ...", skew)


def test_grade_passes_correct_skew_and_grounded() -> None:
    r = sp_eval.grade_scenario_prior_case(
        _case("bear"), generate_fn=lambda _c: _prior(0.15, 0.50, 0.35)
    )
    assert r.passed and r.score == 1.0


def test_grade_fails_wrong_skew() -> None:
    r = sp_eval.grade_scenario_prior_case(
        _case("bull"), generate_fn=lambda _c: _prior(0.15, 0.50, 0.35)
    )
    assert not r.passed
    assert r.score == 0.3  # grounded but wrong direction (0.7*0 + 0.3*1)


def test_grade_global_fallback_is_not_grounded() -> None:
    # A global fallback is balanced (25/50/25): skew_ok for a "balanced" case but
    # NOT grounded (set_by != llm), so it can't score a full pass.
    r = sp_eval.grade_scenario_prior_case(
        _case("balanced"), generate_fn=lambda _c: sp.global_prior(_TODAY)
    )
    assert not r.passed
    assert r.score == 0.7  # skew OK, grounded fails


def test_grade_scores_parse_error_as_call_failure() -> None:
    def boom(_c: sp_eval.ScenarioPriorCase) -> sp.ScenarioPrior:
        raise StructuredParseError("bad", raw_head="x")

    r = sp_eval.grade_scenario_prior_case(_case("bear"), generate_fn=boom)
    assert r.failure_stage == "call" and r.score == 0.0


def test_golden_file_loads_and_runs() -> None:
    golden = PROJECT_ROOT / sp_eval.DEFAULT_GOLDEN_RELPATH
    cases = sp_eval.load_scenario_prior_golden(golden)
    assert len(cases) >= 5
    assert {c.expected_skew for c in cases} <= {"bull", "bear", "balanced"}

    # Run the harness with a fake generator that answers each case correctly.
    def perfect(c: sp_eval.ScenarioPriorCase) -> sp.ScenarioPrior:
        if c.expected_skew == "bear":
            return _prior(0.15, 0.50, 0.35)
        if c.expected_skew == "bull":
            return _prior(0.35, 0.50, 0.15)
        return _prior(0.25, 0.50, 0.25)

    summary = sp_eval.run_scenario_prior_eval(
        golden_path=golden, code_root=PROJECT_ROOT, generate_fn=perfect
    )
    assert summary.purpose == "scenario_prior"
    assert all(c.passed for c in summary.cases)


def test_golden_rejects_malformed(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"purpose": "scenario_prior", "cases": []}), encoding="utf-8")
    import pytest

    with pytest.raises(ValueError, match="non-empty"):
        sp_eval.load_scenario_prior_golden(bad)
