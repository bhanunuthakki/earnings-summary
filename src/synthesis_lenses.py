"""Backwards-compat shim — the lens implementations now live in
`src/synthesis/lenses/`.

This module re-exports the public API from the new package so existing
callers (execution/run_lens.py, execution/run_due_lenses.py,
execution/run_scenario.py, src/report/sections/synthesis.py) keep their
top-of-file imports unchanged.

See `src/synthesis/lenses/__init__.py` for the registry, runner, and
the per-lens sibling modules.
"""

from __future__ import annotations

from synthesis.lenses import (
    LENSES,
    MACRO_LENS_NAMES,
    Lens,
    LensContext,
    list_lenses_for_ticker,
    list_portfolio_lenses,
    read_lens_artifact,
    run_lens,
    run_macro_scenario_lens,
    run_portfolio_macro_stress_lens,
)

__all__ = [
    "LENSES",
    "MACRO_LENS_NAMES",
    "Lens",
    "LensContext",
    "list_lenses_for_ticker",
    "list_portfolio_lenses",
    "read_lens_artifact",
    "run_lens",
    "run_macro_scenario_lens",
    "run_portfolio_macro_stress_lens",
]
