"""Next-dollar allocation model — quantitative "where the next dollar goes".

Blends DCF fair-value upside, marginal-volatility diversification off a
Ledoit–Wolf shrunk covariance of daily returns, and a macro sentiment tilt
(betas × series momentum) into a softmax allocation distribution across the
portfolio holdings. Inspectable by construction: visible blend weights, a
per-holding factor waterfall, hide-don't-stub for factors without data.

Model documentation: directives/next_dollar_model.md.

Public surface:
  build_next_dollar_model — score the holdings, return the distribution.
  NextDollarModel / HoldingScore / FactorReading — the result shape.
  BLEND_WEIGHTS / FACTOR_LABELS — the visible blend.

Also re-exports ``allocation.concentration`` — the PRD §7.2 (P0.2) soft
concentration-zone policy shared by the position-review service.
"""

from __future__ import annotations

from allocation.concentration import (
    ENTRY_APPRECIATION,
    ENTRY_INTENTIONAL,
    TRIM_ASSESSMENT_THRESHOLD_PCT,
    ZONE_BOUNDS,
    Zone,
    ZoneAssessment,
    classify_entry_method,
    classify_zone,
    zone_at_least,
)
from allocation.model import (
    BLEND_WEIGHTS,
    FACTOR_LABELS,
    FactorReading,
    HoldingScore,
    NextDollarModel,
    build_next_dollar_model,
)

__all__ = [
    "BLEND_WEIGHTS",
    "ENTRY_APPRECIATION",
    "ENTRY_INTENTIONAL",
    "FACTOR_LABELS",
    "TRIM_ASSESSMENT_THRESHOLD_PCT",
    "ZONE_BOUNDS",
    "FactorReading",
    "HoldingScore",
    "NextDollarModel",
    "Zone",
    "ZoneAssessment",
    "build_next_dollar_model",
    "classify_entry_method",
    "classify_zone",
    "zone_at_least",
]
