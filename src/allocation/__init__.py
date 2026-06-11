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
"""

from __future__ import annotations

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
    "FACTOR_LABELS",
    "FactorReading",
    "HoldingScore",
    "NextDollarModel",
    "build_next_dollar_model",
]
