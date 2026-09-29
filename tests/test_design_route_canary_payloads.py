"""The design route canary must not reintroduce retired Evaluation scalars.

BHA-102 retired the six scalar compatibility fields (``score``/``fit`` and their
explanation/partial companions). The Evaluation canary payload is a
hand-maintained shape, so this guards it directly rather than relying only on
the hydrated API projection tests.
"""

from __future__ import annotations

from execution.design_route_canaries import render_route_canary

_RETIRED_SCALAR_FIELDS = ("score", "score_why", "score_partial", "fit", "fit_why", "fit_partial")


def test_evaluation_canary_omits_retired_scalar_compatibility_fields() -> None:
    html = render_route_canary(route="evaluation", viewport="desktop")

    assert all(f'"{field}":' not in html for field in _RETIRED_SCALAR_FIELDS)
