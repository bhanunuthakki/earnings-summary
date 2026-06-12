"""Single source of truth for calibration prompt versions.

This is NOT a cache key. The LLM artifact CACHE is invalidated by the rendered
prompt's *content hash* (see ``src/llm/style.py`` and
``llm_artifact_store.compute_input_sha256``) — editing a prompt regenerates its
cached artifact automatically, with no version bump anywhere.

This module is the A/B dimension for the *calibration* loop. Scores land in
``prompt_calibration_scores`` tagged with ``(purpose, prompt_version)`` and
``llm.calibration.summarize_by_prompt_version`` groups by that tuple so the
dashboard can answer "is the rewritten prompt producing better-graded output
than the old one?".

The problem this fixes: every grader hardcoded ``prompt_version="v1"``, so every
score was ``v1`` and there was never a second version to compare — the A/B
machinery was *structurally* dead. Versions now live here, in one place. When
you MATERIALLY iterate a graded/cached prompt, bump its entry below (``"v1"`` ->
``"v2"``); subsequent ``record_score()`` rows (and, for the trigger sensors, new
artifacts) carry the new version and the calibration panel compares the two. A
purpose with no entry defaults to ``"v1"``.

The four trigger sensors now source their artifact ``_PROMPT_VERSION`` from this
registry (see the per-purpose comment below) rather than a hardcoded literal, so
the registry is the single bump-point for both the calibration A/B dimension and
the triggers' cache/version. Producing the *scores themselves* for the
prediction purpose is wired in ``execution/grade_predictions.py`` (a sound,
available-now signal: the fraction of due predictions the extraction prompt left
well-formed enough to grade against realized ``kpi_facts``), and all three
graders are now run on a schedule by ``execution/run_calibration_grading.py``.
"""

from __future__ import annotations

# purpose -> current prompt version. Bump when a graded/cached prompt is
# materially rewritten. Purposes absent here default to "v1".
#
# Two kinds of consumer read this, both keyed on the same per-purpose version so
# ONE bump governs both:
#   * the calibration graders (bear_case, decision_audit, management_prediction)
#     tag every ``record_score`` row with ``prompt_version_for(purpose)`` so the
#     A/B comparison has a version dimension;
#   * the four Personal-CIO trigger sensors (earnings_tone_diff,
#     kpi_inflection_context, material_news_classification, saydo_due_context)
#     source their artifact ``_PROMPT_VERSION`` here instead of a hardcoded
#     literal — so a materially rewritten trigger prompt is bumped in ONE place,
#     which both busts that artifact's cache key and tags new artifacts with the
#     new version. (Before, each trigger hardcoded ``"v1"`` and never consulted
#     this registry, so every artifact was permanently ``v1`` — v6 re-grade.)
_PROMPT_VERSIONS: dict[str, str] = {
    # Graded calibration purposes.
    "bear_case": "v1",
    "decision_audit": "v1",
    "management_prediction": "v1",
    # Personal-CIO trigger artifact purposes.
    "earnings_tone_diff": "v1",
    "kpi_inflection_context": "v1",
    "material_news_classification": "v1",
    "saydo_due_context": "v1",
    # Pairwise backend judge (src/llm/backend_judge.py). Bump when the A/B judge
    # rubric is materially reworded so a re-grade of the same corpus is comparable
    # to the prior verdict instead of being silently confounded by the prompt.
    "backend_compare_judge": "v1",
    # Eval-harness purposes (src/evals/, directives/llm_evals_plan.md): every
    # eval run records prompt_version_for(purpose), so bumping here is what
    # makes a prompt rewrite show up as a comparable A/B slice in
    # summarize_by_prompt_version. Bump when nl_compile._build_prompt changes
    # materially.
    # v2 (2026-06-11): no transform-stacking on pre-transformed kpi tokens +
    # most-specific colloquialism resolution — the first live eval run's
    # failure modes (vs-002/vs-016 YoY-of-YoY, vs-010 R&D->opex).
    "viewspec_compile": "v2",
    # Ask pack router (src/ask/router.py, S4). Bump when _build_prompt's
    # catalog/rules are materially rewritten, then re-run
    # `run_llm_evals.py --purpose ask_pack_router`.
    "ask_pack_router": "v1",
    # Rubric-audited prose purposes (mode B, PR 2): their audit runs are keyed
    # to these versions, so bump when generate_summary's prompt /
    # _NEXT_DOLLAR_PROMPT is materially rewritten and the score history forks
    # cleanly. (bear_case is already registered above with the outcome
    # graders — one entry governs both its grading modes.)
    "transcript_summary": "v1",
    "advisor_next_dollar": "v1",
    # Golden-set classifier purposes (mode A, PR 4). Bump when the prompt in
    # identify_transcript_metadata / classify_intake_document /
    # structure_recent_news_json is materially rewritten, then re-run
    # `run_llm_evals.py --purpose <p> --min-score ...` per the prompt-change
    # workflow in directives/llm_calls.md.
    "transcript_metadata": "v1",
    "intake_classifier": "v1",
    "news_structuring": "v1",
    # Falsifiable-condition extraction (src/decision_conditions.py, mode-A
    # golden set). Bump when _EXTRACTION_PROMPT is materially rewritten, then
    # re-run `run_llm_evals.py --purpose decision_conditions_extract`.
    "decision_conditions_extract": "v1",
    # The eval judge itself (src/evals/judge.py + rubric_judge.py prompt
    # templates). Bump when either judge prompt is materially reworded so
    # spot-check agreement rates (execution/spot_check_eval_judge.py) stay
    # comparable within a version.
    "eval_judge": "v1",
}

_DEFAULT_VERSION = "v1"


def prompt_version_for(purpose: str) -> str:
    """Current prompt version for ``purpose`` (default ``"v1"``).

    The single bump-point for both the calibration A/B dimension and the trigger
    sensors' artifact version — see the registry comment above.
    """
    return _PROMPT_VERSIONS.get(purpose, _DEFAULT_VERSION)


def registered_purposes() -> frozenset[str]:
    """The purposes with an explicit registry entry (vs. silently defaulting to
    ``"v1"``) — lets callers enumerate without touching the private dict."""
    return frozenset(_PROMPT_VERSIONS)
