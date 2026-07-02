"""Model-version provenance & self-updating freshness.

Two orthogonal, reusable conventions for the recurring problem that *new models and
valuations supersede old ones*:

- ``versioning`` — the producer side: a model-output table versions instead of
  overwriting (is_latest / superseded_at / superseded_by_id + append-and-supersede).
- ``freshness`` — the consumer side: a decision records the model-version it rests
  on (basis_kind / basis_ref_id / basis_value / basis_as_of), and a self-updating
  VIEW (``v_decision_freshness``) derives whether that basis has been superseded.

First instance is the DCF → decision chain (migration 0137). KPI, thesis, and
allocation kinds plug into the same shape.

Distinct from ``provenance`` (that package is the FMP source-of-truth *override*
layer — company-published docs supersede FMP data). This one is about *model
versions* superseding each other over time.
"""

from __future__ import annotations

from model_provenance.freshness import (
    BASIS_STATUSES,
    MATERIAL_DRIFT_PCT,
    DecisionFreshness,
    decision_freshness,
    stale_material_decisions,
)
from model_provenance.versioning import mark_superseded_by, supersede_current

BASIS_KINDS: tuple[str, ...] = ("dcf", "kpi", "thesis", "allocation")

__all__ = [
    "BASIS_KINDS",
    "BASIS_STATUSES",
    "MATERIAL_DRIFT_PCT",
    "DecisionFreshness",
    "decision_freshness",
    "mark_superseded_by",
    "stale_material_decisions",
    "supersede_current",
]
