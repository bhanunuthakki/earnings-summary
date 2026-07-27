"""Structural smoke test for the src/synthesis/lenses/ split.

Asserts that every per-lens module exposes a module-level `LENS`
instance of `Lens` with a callable `build_context`, that the package
registry matches the per-module set, and that the backwards-compat
shim re-exports the public API.
"""

from __future__ import annotations

import importlib

import pytest

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

PER_LENS_MODULES = [
    "bull_case",
    "catalyst_calendar",
    "cross_portfolio_synthesis",
    "customer_concentration_risk",
    "filing_diff_narrative",
    "five_min_reread",
    "footnote_anomaly",
    "llm_calibration",
    "mgmt_credibility_score",
    "reverse_dcf",
    "thesis_drift_qoq",
    "underweighted_facts",
]

MACRO_LENS_MODULES = {
    "macro_scenario": "run_macro_scenario_lens",
    "portfolio_macro_stress": "run_portfolio_macro_stress_lens",
}


@pytest.mark.parametrize("lens_name", PER_LENS_MODULES)
def test_per_lens_module_exports_LENS(lens_name: str) -> None:
    mod = importlib.import_module(f"synthesis.lenses.{lens_name}")
    assert hasattr(mod, "LENS"), f"{lens_name} missing module-level LENS"
    lens = mod.LENS
    assert isinstance(lens, Lens), f"{lens_name}.LENS is not a Lens instance"
    assert lens.name == lens_name, f"{lens_name}.LENS.name mismatch: {lens.name!r}"
    assert lens.scope in {"ticker", "portfolio"}
    assert callable(lens.build_context)
    assert isinstance(lens.prompt_template, str) and lens.prompt_template.strip()


def test_registry_matches_per_lens_modules() -> None:
    """LENSES dict in the package __init__ collects exactly the per-module LENS instances."""
    assert sorted(LENSES.keys()) == sorted(PER_LENS_MODULES)
    for name, lens in LENSES.items():
        mod = importlib.import_module(f"synthesis.lenses.{name}")
        assert lens is mod.LENS, f"LENSES[{name!r}] is not the module's LENS"


@pytest.mark.parametrize("lens_name,runner_attr", sorted(MACRO_LENS_MODULES.items()))
def test_macro_lens_module_exports_runner(lens_name: str, runner_attr: str) -> None:
    mod = importlib.import_module(f"synthesis.lenses.{lens_name}")
    assert hasattr(mod, runner_attr), f"{lens_name} missing {runner_attr}"
    assert callable(getattr(mod, runner_attr))


def test_package_public_api_is_callable() -> None:
    assert callable(run_lens)
    assert callable(read_lens_artifact)
    assert callable(run_macro_scenario_lens)
    assert callable(run_portfolio_macro_stress_lens)
    assert callable(list_lenses_for_ticker)
    assert callable(list_portfolio_lenses)
    assert LensContext.__name__ == "LensContext"
    assert MACRO_LENS_NAMES == ("macro_scenario", "portfolio_macro_stress")


def test_shim_reexports_match_package() -> None:
    """src/synthesis_lenses.py must re-export the same names callers used pre-split."""
    import synthesis.lenses as pkg
    import synthesis_lenses as shim

    for attr in (
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
    ):
        assert getattr(shim, attr) is getattr(pkg, attr), f"shim.{attr} diverges from package"
