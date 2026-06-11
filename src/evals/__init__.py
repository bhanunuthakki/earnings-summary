"""
src/evals/ — the LLM eval harness (directives/llm_evals_plan.md).

Scores LLM-call purposes against ground truth so prompt/model changes become
measurable regressions. Three modes share this package; PR 1 ships mode A:

    A. golden-set deterministic — checked-in cases under evals/golden/,
       graded by code (does it validate, execute, match?), with a cheap
       judge model resolving only ambiguous near-misses;
    B. rubric judge (PR 2) — per-purpose rubric scoring of real outputs;
    C. outcome calibration — already live in execution/grade_*.py.

Layout:
    harness          — shared dataclasses (CaseResult, EvalRunSummary) +
                       run math + the calibration-score bridge.
    store            — eval_runs / eval_case_results writes (alembic 0083).
                       Loud, not best-effort: an eval you can't persist
                       should fail, unlike production telemetry.
    judge            — the `eval_judge` model call + fail-closed verdict
                       parsing.
    viewspec_compile — the pilot grader: compile -> execute -> compare ->
                       judge-on-divergence over the production
                       `compile_nl_to_viewspec` path.

CLI: execution/run_llm_evals.py. Scores land in eval_runs and bridge into
prompt_calibration_scores so summarize_by_prompt_version compares prompt
versions with no new read-side code.
"""

from __future__ import annotations

from evals.harness import CaseResult, EvalAbortError, EvalRunSummary, persist_summary
from evals.judge import JudgeOutcome, JudgeVerdict, run_judge
from evals.viewspec_compile import GoldenCase, load_golden, run_viewspec_eval, spec_diff

__all__ = [
    "CaseResult",
    "EvalAbortError",
    "EvalRunSummary",
    "GoldenCase",
    "JudgeOutcome",
    "JudgeVerdict",
    "load_golden",
    "persist_summary",
    "run_judge",
    "run_viewspec_eval",
    "spec_diff",
]
