"""Canonical eval-machinery scopes — the single source of truth for "measurement
traffic, not production workload" (``directives/meta_eval_governance.md`` §0).

Every meta/eval LLM call carries one of these ``llm_calls.scope`` values; every
workload or per-purpose *production* cost rollup EXCLUDES them, so the optimizer
never observes itself and a purpose's real cost is never inflated by the cost of
measuring it (isolation invariant I5).

  * ``model_eval``    — the downgrade sweep re-running a purpose's prompt on a candidate
  * ``backend_judge`` — the brand-blind pairwise judge (``backend_compare_judge``)
  * ``eval``          — the golden/rubric grading harness (``eval_judge``, ``*_grading``)
  * ``prompt_ab``     — prompt A/B variant runs (meta_eval_governance.md §4; not built yet)
  * ``meta_eval``     — the optimizer's own steering calls (nominator, difficulty
                        classifier, criteria deriver, frontier research; §1-§4, §10.1)

``prompt_ab`` / ``meta_eval`` are reserved here BEFORE their subsystems land so the
inventory that ships first (PR1) already excludes them by construction. This set is
the UNION of every measurement scope: it deliberately INCLUDES ``eval``, which
meta_eval_governance.md §0's literal set omitted — see §10.2. ``pipeline.
model_eval_panel`` keeps a local copy today; PR6 unifies it onto this constant.
"""

from __future__ import annotations

EVAL_SCOPES: frozenset[str] = frozenset(
    {"model_eval", "backend_judge", "eval", "prompt_ab", "meta_eval"}
)
