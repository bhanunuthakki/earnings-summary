"""ViewSpec — deterministic slice-and-dice over the fact tables (P5.1).

Public surface:
  spec.ViewSpec / spec.MetricRef / spec.ViewSpecError — the saveable spec
  engine.execute_view / engine.metric_catalog          — run it / list axes
  render.render_view_fragment                          — HTML w/ chips + chart
"""

from viewspec.engine import ViewResult, execute_view, metric_catalog
from viewspec.render import render_view_fragment
from viewspec.spec import MetricRef, ViewSpec, ViewSpecError

__all__ = [
    "MetricRef",
    "ViewResult",
    "ViewSpec",
    "ViewSpecError",
    "execute_view",
    "metric_catalog",
    "render_view_fragment",
]
