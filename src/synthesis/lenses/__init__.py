"""Synthesis lenses — public API + registry.

Each lens lives in its own sibling module and exports a module-level
`LENS` instance of `Lens`; this package collects them into the `LENSES`
dispatcher dict and re-exports the runner/helpers consumers reach for.

Backwards-compat shim at `src/synthesis_lenses.py` re-exports everything
here so existing callers (execution/run_lens.py, execution/run_due_lenses.py,
execution/run_scenario.py, src/report/sections/synthesis.py) keep working
without import changes.
"""

from __future__ import annotations

from ._shared import (
    Lens,
    LensContext,
    read_lens_artifact,
    run_lens,
)
from .bull_case import LENS as _LENS_bull_case
from .catalyst_calendar import LENS as _LENS_catalyst_calendar
from .cross_portfolio_synthesis import LENS as _LENS_cross_portfolio_synthesis
from .customer_concentration_risk import LENS as _LENS_customer_concentration_risk
from .filing_diff_narrative import LENS as _LENS_filing_diff_narrative
from .five_min_reread import LENS as _LENS_five_min_reread
from .footnote_anomaly import LENS as _LENS_footnote_anomaly
from .llm_calibration import LENS as _LENS_llm_calibration
from .macro_scenario import run_macro_scenario_lens
from .mgmt_credibility_score import LENS as _LENS_mgmt_credibility_score
from .portfolio_macro_stress import run_portfolio_macro_stress_lens
from .reverse_dcf import LENS as _LENS_reverse_dcf
from .thesis_drift_qoq import LENS as _LENS_thesis_drift_qoq
from .underweighted_facts import LENS as _LENS_underweighted_facts

LENSES: dict[str, Lens] = {
    _LENS_thesis_drift_qoq.name: _LENS_thesis_drift_qoq,
    _LENS_five_min_reread.name: _LENS_five_min_reread,
    _LENS_bull_case.name: _LENS_bull_case,
    _LENS_reverse_dcf.name: _LENS_reverse_dcf,
    _LENS_underweighted_facts.name: _LENS_underweighted_facts,
    _LENS_catalyst_calendar.name: _LENS_catalyst_calendar,
    _LENS_filing_diff_narrative.name: _LENS_filing_diff_narrative,
    _LENS_footnote_anomaly.name: _LENS_footnote_anomaly,
    _LENS_cross_portfolio_synthesis.name: _LENS_cross_portfolio_synthesis,
    _LENS_mgmt_credibility_score.name: _LENS_mgmt_credibility_score,
    _LENS_llm_calibration.name: _LENS_llm_calibration,
    _LENS_customer_concentration_risk.name: _LENS_customer_concentration_risk,
}

# Macro lenses run via dedicated entry points (run_macro_scenario_lens /
# run_portfolio_macro_stress_lens) rather than the generic LENSES dispatcher
# because they take a scenario_obj parameter the generic run_lens doesn't
# carry. Names exposed here so callers can enumerate them.
MACRO_LENS_NAMES: tuple[str, ...] = ("macro_scenario", "portfolio_macro_stress")


def list_lenses_for_ticker() -> list[str]:
    """Lenses that operate on a single ticker."""
    return sorted(name for name, lens in LENSES.items() if lens.scope == "ticker")


def list_portfolio_lenses() -> list[str]:
    """Lenses that operate on the whole portfolio."""
    return sorted(name for name, lens in LENSES.items() if lens.scope == "portfolio")


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
