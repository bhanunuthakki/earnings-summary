"""
src/evals/ — the LLM eval harness (directives/llm_evals_plan.md).

Scores LLM-call purposes against ground truth so prompt/model changes become
measurable regressions. Three modes share this package:

    A. golden-set deterministic (PR 1) — checked-in cases under
       evals/golden/, graded by code (does it validate, execute, match?),
       with a cheap judge model resolving only ambiguous near-misses;
    B. rubric judge (PR 2) — per-purpose versioned rubric under
       evals/rubrics/, a cheap judge scoring REAL production outputs
       facet-by-facet (bear_case, transcript_summary, advisor_next_dollar);
    C. outcome calibration — already live in execution/grade_*.py.

Layout:
    harness          — shared dataclasses (CaseResult, EvalRunSummary) +
                       run math + the calibration-score bridge.
    store            — eval_runs / eval_case_results writes (alembic 0083).
                       Loud, not best-effort: an eval you can't persist
                       should fail, unlike production telemetry.
    judge            — the `eval_judge` model call + fail-closed verdict
                       parsing for mode A's divergence question.
    viewspec_compile — the pilot grader: compile -> execute -> compare ->
                       judge-on-divergence over the production
                       `compile_nl_to_viewspec` path.
    corpora          — mode-B corpus loaders: which production artifacts
                       each audit purpose grades, newest first.
    rubric_judge     — mode-B engine: rubric parse/validate, facet-scored
                       judging (fail-closed, hard stops abort), run
                       orchestration.

CLI: execution/run_llm_evals.py. Judge agreement is itself audited by
execution/spot_check_eval_judge.py. Scores land in eval_runs and bridge into
prompt_calibration_scores so summarize_by_prompt_version compares prompt
versions with no new read-side code.
"""

from __future__ import annotations

from evals.corpora import CORPUS_LOADERS, AuditItem
from evals.harness import CaseResult, EvalAbortError, EvalRunSummary, persist_summary
from evals.judge import JudgeOutcome, JudgeVerdict, run_judge
from evals.rubric_judge import AUDIT_SPECS, Rubric, RubricVerdict, load_rubric, run_rubric_eval
from evals.viewspec_compile import GoldenCase, load_golden, run_viewspec_eval, spec_diff

__all__ = [
    "AUDIT_SPECS",
    "CORPUS_LOADERS",
    "AuditItem",
    "CaseResult",
    "EvalAbortError",
    "EvalRunSummary",
    "GoldenCase",
    "JudgeOutcome",
    "JudgeVerdict",
    "Rubric",
    "RubricVerdict",
    "load_golden",
    "load_rubric",
    "persist_summary",
    "run_judge",
    "run_rubric_eval",
    "run_viewspec_eval",
    "spec_diff",
]
