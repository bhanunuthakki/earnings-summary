"""Canonical purpose projections for executable LLM eval surfaces.

The model registry remains owned by :mod:`llm.cli`; this module only projects
the checked-in eval declarations into the runner, coverage report, and panel.
Order is intentional because it is also the operator-facing run-bar order.
"""

from __future__ import annotations

from evals.capture_quality_specs import CAPTURE_QUALITY_PURPOSES
from evals.golden_classifiers import CLASSIFIER_PURPOSES
from evals.rubric_judge import AUDIT_SPECS

# Golden sets with dedicated graders, rather than classifier purposes.
_SPECIAL_GOLDEN_PURPOSES: tuple[str, ...] = (
    "viewspec_compile",
    "ask_pack_router",
    "ask_evidence_followup",
    "ask_claim_grounding",
    "ask_claim_audit",
    "injection_canaries",
    "provenance_caution",
    "key_metrics",
    "podcast_takeaway_summary",
    "scenario_prior",
    "sector_benchmark_proposal",
)

GOLDEN_PURPOSES: tuple[str, ...] = _SPECIAL_GOLDEN_PURPOSES + tuple(
    purpose for purpose in CLASSIFIER_PURPOSES if purpose not in _SPECIAL_GOLDEN_PURPOSES
)
AUDIT_PURPOSES: tuple[str, ...] = tuple(AUDIT_SPECS)
CAPTURE_PURPOSES: tuple[str, ...] = CAPTURE_QUALITY_PURPOSES
CAPTURE_AUDIT_PURPOSES: tuple[str, ...] = CAPTURE_PURPOSES

# Preserve declaration order while making the run surface a true union.
RUNNABLE_PURPOSES: tuple[str, ...] = tuple(
    dict.fromkeys(GOLDEN_PURPOSES + AUDIT_PURPOSES + CAPTURE_PURPOSES)
)

__all__ = [
    "AUDIT_PURPOSES",
    "CAPTURE_AUDIT_PURPOSES",
    "CAPTURE_PURPOSES",
    "GOLDEN_PURPOSES",
    "RUNNABLE_PURPOSES",
]
