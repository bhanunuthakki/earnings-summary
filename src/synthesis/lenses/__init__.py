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
from .bull_case import LENS as _LENS_BULL_CASE
from .catalyst_calendar import LENS as _LENS_CATALYST_CALENDAR
from .cross_portfolio_synthesis import LENS as _LENS_CROSS_PORTFOLIO_SYNTHESIS
from .customer_concentration_risk import LENS as _LENS_CUSTOMER_CONCENTRATION_RISK
from .filing_diff_narrative import LENS as _LENS_FILING_DIFF_NARRATIVE
from .five_min_reread import LENS as _LENS_FIVE_MIN_REREAD
from .footnote_anomaly import LENS as _LENS_FOOTNOTE_ANOMALY
from .llm_calibration import LENS as _LENS_LLM_CALIBRATION
from .macro_scenario import run_macro_scenario_lens
from .mgmt_credibility_score import LENS as _LENS_MGMT_CREDIBILITY_SCORE
from .portfolio_macro_stress import run_portfolio_macro_stress_lens
from .reverse_dcf import LENS as _LENS_REVERSE_DCF
from .thesis_drift_qoq import LENS as _LENS_THESIS_DRIFT_QOQ
from .underweighted_facts import LENS as _LENS_UNDERWEIGHTED_FACTS

LENSES: dict[str, Lens] = {
    _LENS_THESIS_DRIFT_QOQ.name: _LENS_THESIS_DRIFT_QOQ,
    _LENS_FIVE_MIN_REREAD.name: _LENS_FIVE_MIN_REREAD,
    _LENS_BULL_CASE.name: _LENS_BULL_CASE,
    _LENS_REVERSE_DCF.name: _LENS_REVERSE_DCF,
    _LENS_UNDERWEIGHTED_FACTS.name: _LENS_UNDERWEIGHTED_FACTS,
    _LENS_CATALYST_CALENDAR.name: _LENS_CATALYST_CALENDAR,
    _LENS_FILING_DIFF_NARRATIVE.name: _LENS_FILING_DIFF_NARRATIVE,
    _LENS_FOOTNOTE_ANOMALY.name: _LENS_FOOTNOTE_ANOMALY,
    _LENS_CROSS_PORTFOLIO_SYNTHESIS.name: _LENS_CROSS_PORTFOLIO_SYNTHESIS,
    _LENS_MGMT_CREDIBILITY_SCORE.name: _LENS_MGMT_CREDIBILITY_SCORE,
    _LENS_LLM_CALIBRATION.name: _LENS_LLM_CALIBRATION,
    _LENS_CUSTOMER_CONCENTRATION_RISK.name: _LENS_CUSTOMER_CONCENTRATION_RISK,
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
