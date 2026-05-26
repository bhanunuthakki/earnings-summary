"""Pre-defined macro scenarios for the stress engine.

Each Scenario is a named shock package — a set of MacroShocks applied
to the same series_ids that live in src/macro_series.py. The LLM lens
joins these to per-ticker betas (macro_sensitivities) to narrate
read-throughs.

Shocks are intentionally coarse — the scenario engine isn't trying to
out-predict a real macro model; it's prompting structured analytical
thinking. Use `--scenario fed_cuts_50bps` as a fast way to ask
"would a cutting cycle press on the thesis?" rather than "exactly what
return does this scenario produce."

Add a new scenario by appending to SCENARIOS. Keep the id slug-case,
keep title human-readable, keep the historical_analog brief but
specific so the LLM has a concrete frame.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class MacroShock:
    """One shock to one series. `unit` declares how `magnitude` is interpreted:

      - "bps"      : basis points (rates: fed_funds, us_10y)
      - "pct"      : percent move (FX, commodities, indices)
      - "absolute" : move TO this exact value (e.g. brent → $50)
    """

    series_id: str
    magnitude: float
    unit: str  # "bps" | "pct" | "absolute"
    direction: str = "up"  # "up" | "down"


@dataclass(slots=True, frozen=True)
class Scenario:
    id: str
    title: str
    description: str
    historical_analog: str
    timeframe_days: int
    shocks: tuple[MacroShock, ...]


# ---------------------------------------------------------------------------
# Scenario registry — the six mandated by the spec
# ---------------------------------------------------------------------------

SCENARIOS: dict[str, Scenario] = {
    "fed_cuts_50bps": Scenario(
        id="fed_cuts_50bps",
        title="Fed cuts 50bps in one move",
        description=(
            "Inter-meeting easing — Fed delivers a 50bp cut, the front end of "
            "the curve drops materially and the long end rallies modestly as "
            "growth expectations decline. Equity multiple expansion likely; "
            "FX path: USD weakens broadly. Net dovish."
        ),
        historical_analog="July 2019 mid-cycle cut + March 2020 emergency cut",
        timeframe_days=90,
        shocks=(
            MacroShock(series_id="fed_funds", magnitude=50, unit="bps", direction="down"),
            MacroShock(series_id="us_10y", magnitude=25, unit="bps", direction="down"),
            MacroShock(series_id="usd_eur", magnitude=2.5, unit="pct", direction="down"),
            MacroShock(series_id="gold", magnitude=4.0, unit="pct", direction="up"),
        ),
    ),
    "fed_hikes_25bps": Scenario(
        id="fed_hikes_25bps",
        title="Fed hikes 25bps unexpectedly",
        description=(
            "Hawkish surprise — Fed extends or reignites a tightening cycle. "
            "Front end repriced higher, curve bull-flattens. USD strengthens. "
            "Long-duration assets and FX-exposed names take the hit."
        ),
        historical_analog="May 2023 surprise pause-then-skip cycle",
        timeframe_days=90,
        shocks=(
            MacroShock(series_id="fed_funds", magnitude=25, unit="bps", direction="up"),
            MacroShock(series_id="us_10y", magnitude=15, unit="bps", direction="up"),
            MacroShock(series_id="usd_eur", magnitude=2.0, unit="pct", direction="up"),
            MacroShock(series_id="usd_brl", magnitude=3.0, unit="pct", direction="up"),
        ),
    ),
    "usd_brl_up_15pct": Scenario(
        id="usd_brl_up_15pct",
        title="BRL devalues 15% vs USD",
        description=(
            "Brazilian fiscal-policy stress or commodity-cycle dislocation "
            "drives a sharp BRL devaluation. Direct hit to USD-reporting "
            "Brazilian names (NU especially); positive translation for "
            "USD-cost / BRL-revenue exporters."
        ),
        historical_analog="2014–15 commodity collapse / 2020 COVID dislocation",
        timeframe_days=180,
        shocks=(
            MacroShock(series_id="usd_brl", magnitude=15.0, unit="pct", direction="up"),
            MacroShock(series_id="usd_eur", magnitude=3.0, unit="pct", direction="up"),
        ),
    ),
    "oil_to_50": Scenario(
        id="oil_to_50",
        title="Brent crashes to ~$50/bbl",
        description=(
            "Demand-side weakness OR OPEC+ supply discipline collapse drives "
            "Brent to the mid-$50s. Disinflationary for the median consumer; "
            "pain for E&P + commodity-leveraged sovereigns; tailwind for "
            "logistics-heavy consumer franchises."
        ),
        historical_analog="2014–15 oil crash / Q1 2020 demand shock",
        timeframe_days=180,
        shocks=(
            MacroShock(series_id="brent", magnitude=30.0, unit="pct", direction="down"),
            MacroShock(series_id="us_10y", magnitude=20, unit="bps", direction="down"),
            MacroShock(series_id="usd_brl", magnitude=5.0, unit="pct", direction="up"),
        ),
    ),
    "copper_doubles": Scenario(
        id="copper_doubles",
        title="Copper doubles on supply-demand squeeze",
        description=(
            "Structural copper deficit (electrification + capex underinvestment) "
            "drives copper to multi-decade highs. Industrial-capex-leveraged "
            "names benefit; manufacturers face input-cost squeeze."
        ),
        historical_analog="2020–21 post-COVID demand snapback + grid-build cycles",
        timeframe_days=365,
        shocks=(
            MacroShock(series_id="copper", magnitude=100.0, unit="pct", direction="up"),
            MacroShock(series_id="gold", magnitude=15.0, unit="pct", direction="up"),
            MacroShock(series_id="usd_eur", magnitude=3.0, unit="pct", direction="down"),
        ),
    ),
    "recession_2026": Scenario(
        id="recession_2026",
        title="US recession in 2026",
        description=(
            "Composite scenario — broad demand contraction triggers a Fed easing "
            "cycle, oil + copper drop on demand weakness, VIX spikes, semis "
            "underperform on capex retrenchment. Tests every consumer- and "
            "capex-cyclical exposure simultaneously."
        ),
        historical_analog="2008 broad credit / 2001 capex unwind",
        timeframe_days=270,
        shocks=(
            MacroShock(series_id="fed_funds", magnitude=100, unit="bps", direction="down"),
            MacroShock(series_id="us_10y", magnitude=60, unit="bps", direction="down"),
            MacroShock(series_id="brent", magnitude=25.0, unit="pct", direction="down"),
            MacroShock(series_id="copper", magnitude=30.0, unit="pct", direction="down"),
            MacroShock(series_id="vix", magnitude=50.0, unit="pct", direction="up"),
            MacroShock(series_id="sox", magnitude=25.0, unit="pct", direction="down"),
        ),
    ),
}


def all_scenario_ids() -> list[str]:
    return list(SCENARIOS.keys())


def get(scenario_id: str) -> Scenario | None:
    return SCENARIOS.get(scenario_id)
