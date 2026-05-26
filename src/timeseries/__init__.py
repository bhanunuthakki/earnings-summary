"""Time-series intelligence layer over financial_facts / kpi_facts / segment_facts.

The synthesis layer (src/synthesis_lenses.py) and the per-quarter LLM prompts
historically asked the LLM to "eyeball" trends from raw tables — fragile when
the table is 16 quarters tall and the analyst wants the second-derivative of
YoY growth, not the level. This package replaces eyeballing with numerical
analysis: trend regression, changepoint detection, rolling-window anomalies,
seasonal decomposition, cross-KPI correlation, YoY acceleration.

Public surface:
  primitives — six pure-statistics functions over a Series.
  loaders    — DB-aware functions to materialize a Series for a given
               (ticker, kpi_name) or (ticker, line_item).

All functions are best-effort: a series too short for the requested test
returns a structured "insufficient_data" result rather than raising. The
loaders return [] when the DB is missing or the query returns nothing.
"""

from __future__ import annotations

from timeseries.loaders import (
    load_financial_series,
    load_kpi_series,
    load_segment_series,
)
from timeseries.primitives import (
    Observation,
    correlation_matrix,
    detect_inflection,
    detect_trend,
    rolling_zscore_anomalies,
    seasonal_decompose,
    yoy_acceleration,
)
from timeseries.signal_writer import compute_and_persist_signals

__all__ = [
    "Observation",
    "compute_and_persist_signals",
    "correlation_matrix",
    "detect_inflection",
    "detect_trend",
    "load_financial_series",
    "load_kpi_series",
    "load_segment_series",
    "rolling_zscore_anomalies",
    "seasonal_decompose",
    "yoy_acceleration",
]
