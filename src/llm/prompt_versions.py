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
you MATERIALLY iterate a graded prompt, bump its entry below (``"v1"`` ->
``"v2"``); subsequent ``record_score()`` rows carry the new version and the
calibration panel compares the two. A purpose with no entry defaults to ``"v1"``.

(Extending grading to the trigger LLM purposes — earnings_tone, material_news,
kpi_inflection — needs a sound ground-truth signal per purpose and is tracked
in the separate predictions/grading lane; this registry is the knob that makes
their A/B comparison possible once that grading exists.)
"""

from __future__ import annotations

# purpose -> current calibration prompt version. Bump when a graded prompt is
# materially rewritten. Purposes absent here default to "v1".
_PROMPT_VERSIONS: dict[str, str] = {
    "bear_case": "v1",
    "decision_audit": "v1",
}

_DEFAULT_VERSION = "v1"


def prompt_version_for(purpose: str) -> str:
    """Current calibration prompt version for ``purpose`` (default ``"v1"``)."""
    return _PROMPT_VERSIONS.get(purpose, _DEFAULT_VERSION)
