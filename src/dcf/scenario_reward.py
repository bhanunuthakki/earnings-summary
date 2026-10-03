"""Probability-weighted upside from price to present DCF fair value.

The stored base NPV per share and scenario tails are present values. Their gap
to price is not a forward holding-period return or Sharpe numerator. No holding
horizon, exit price, shareholder payouts or accepted probability model is supplied.

The legacy analytical convention uses per-name weights when present, otherwise
25/50/25. Missing tails are omitted and weights are renormalized. With no tails,
the calculation retains the base point estimate. These conventions do not grant
scenario or prior acceptance. The module is pure and does not refresh models.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import cast

# The reward prior, in plain sight. A coarse, symmetric-by-default weighting over
# the three scenarios — NOT fitted, just a documented stance that the tails carry
# a quarter of the probability mass each. Renormalized over whichever legs are
# actually on file (a run may persist only one tail). Changing this changes every
# DCF-reward surface at once, which is the point.
SCENARIO_PROBABILITIES: dict[str, float] = {"bull": 0.25, "base": 0.50, "bear": 0.25}


@dataclass(frozen=True, slots=True)
class ScenarioReward:
    """Present-value gap for one name at a given price.

    ``valuation_upside`` is Σp_s(V_0,s/P_0−1), as a fraction. The legacy
    ``expected_return`` and per-leg ``*_return`` fields remain for compatibility;
    all represent valuation gaps, with no time horizon or annualization.
    ``skew`` is the weighted gap minus the base gap, not return uncertainty.
    """

    expected_return: float
    base_return: float
    bull_return: float | None
    bear_return: float | None
    has_scenarios: bool
    probabilities: dict[str, float]
    detail: str
    # Prior origin only; neither value establishes accepted scenario authority.
    weights_source: str = "global"

    @property
    def valuation_upside(self) -> float:
        """Probability-weighted gap to present fair value; not forward return."""
        return self.expected_return

    @property
    def skew(self) -> float:
        """Weighted valuation gap minus the base gap. Zero with no tails."""
        return self.expected_return - self.base_return


def parse_scenario_fair_values(snapshot_json: object) -> dict[str, float]:
    """Bull / base / bear per-share fair values from a ``dcf_runs`` assumption
    snapshot. Mirrors ``snapshot._scenario_range`` parsing but returns all three
    legs that are present + positive. Tolerates absent / malformed / null /
    non-redesign snapshots (returns an empty dict)."""
    if not isinstance(snapshot_json, str) or not snapshot_json:
        return {}
    try:
        data: object = json.loads(snapshot_json)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    scenarios = cast("dict[str, object]", data).get("scenarios")
    if not isinstance(scenarios, dict):
        return {}
    out: dict[str, float] = {}
    for key in ("bull", "base", "bear"):
        block = cast("dict[str, object]", scenarios).get(key)
        if not isinstance(block, dict):
            continue
        v = cast("dict[str, object]", block).get("fair_value_per_share_usd")
        # bool is an int subclass — exclude it (matches snapshot._scenario_range).
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0:
            out[key] = float(v)
    return out


def parse_scenario_bear_provenance(snapshot_json: object) -> str | None:
    """The bear leg's provenance from a ``dcf_runs`` assumption snapshot's
    ``scenarios.bear.provenance`` field (written by ``refresh_dcf._redesign_snapshot``,
    Monthly Red Team Phase 1 guard 3): ``"seed"`` (the generic BEAR_SEED offsets,
    untouched), ``"thesis"`` (a holdings-JSON ``bear_deltas`` override), or
    ``"owner"`` (a hand-edited workbook Dashboard cell). ``None`` when absent,
    malformed, or an unrecognized value — tolerant of pre-provenance snapshots and
    non-redesign formats, mirroring :func:`parse_scenario_fair_values`."""
    if not isinstance(snapshot_json, str) or not snapshot_json:
        return None
    try:
        data: object = json.loads(snapshot_json)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    scenarios = cast("dict[str, object]", data).get("scenarios")
    if not isinstance(scenarios, dict):
        return None
    bear = cast("dict[str, object]", scenarios).get("bear")
    if not isinstance(bear, dict):
        return None
    prov = cast("dict[str, object]", bear).get("provenance")
    return prov if prov in ("seed", "thesis", "owner") else None


def parse_scenario_prior_weights(snapshot_json: object) -> dict[str, float] | None:
    """Per-name Bull/Base/Bear probability weights from a ``dcf_runs`` assumption
    snapshot's ``scenario_prior`` block (written by ``execution/refresh_dcf``), or
    ``None`` when absent/malformed.

    A valid result is a positive simplex over all three legs, normalized defensively
    to sum 1. Anything else returns ``None`` so :func:`scenario_reward` falls back to
    the documented global prior — a run without a per-name prior behaves exactly as
    before this shipped."""
    if not isinstance(snapshot_json, str) or not snapshot_json:
        return None
    try:
        data: object = json.loads(snapshot_json)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    block = cast("dict[str, object]", data).get("scenario_prior")
    if not isinstance(block, dict):
        return None
    weights = cast("dict[str, object]", block).get("weights")
    if not isinstance(weights, dict):
        return None
    w = cast("dict[str, object]", weights)
    out: dict[str, float] = {}
    for key in ("bull", "base", "bear"):
        v = w.get(key)
        if not (
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
        ):
            return None
        out[key] = float(v)
    total = out["bull"] + out["base"] + out["bear"]
    if not math.isfinite(total) or total <= 0:
        return None
    return {k: v / total for k, v in out.items()}


def scenario_reward(
    *, price: float | None, base_fv: float | None, snapshot_json: object = None
) -> ScenarioReward | None:
    """Probability-weighted upside to present fair value for one name.

    ``base_fv`` is the value-of-record fair value (``dcf_runs.npv_per_share``) —
    authoritative for the base leg even if the snapshot's own ``base`` differs;
    the bull/bear tails come from ``snapshot_json``. Returns None when the inputs
    are not finite positive numbers. Boolean operands are invalid.

    The valuation gap is ``Σ p_s · (fv_s / price − 1)`` over the scenarios on file,
    with ``p_s`` from :data:`SCENARIO_PROBABILITIES` renormalized to the present
    legs. With no tails it is exactly the base point estimate.
    """
    if (
        price is None
        or isinstance(price, bool)
        or not math.isfinite(price)
        or price <= 0
        or base_fv is None
        or isinstance(base_fv, bool)
        or not math.isfinite(base_fv)
        or base_fv <= 0
    ):
        return None

    fair_values = dict(parse_scenario_fair_values(snapshot_json))
    fair_values["base"] = float(base_fv)  # value-of-record wins for the base leg

    def ret(key: str) -> float | None:
        fv = fair_values.get(key)
        return fv / price - 1.0 if fv is not None else None

    base_return = base_fv / price - 1.0
    bull_return = ret("bull")
    bear_return = ret("bear")
    # Every consumer displays a percentage; reject overflow in that conversion too.
    if any(
        value is not None and (not math.isfinite(value) or not math.isfinite(value * 100.0))
        for value in (base_return, bull_return, bear_return)
    ):
        return None
    has_scenarios = bull_return is not None or bear_return is not None

    # Per-name prior (LLM/owner) when the run carries one, else the global 25/50/25.
    # Both are renormalized over whichever legs are actually on file.
    prior = parse_scenario_prior_weights(snapshot_json)
    weights = prior if prior is not None else SCENARIO_PROBABILITIES
    weights_source = "per_name" if prior is not None else "global"
    present = [s for s in weights if s in fair_values]
    mass = sum(weights[s] for s in present)
    if not math.isfinite(mass) or mass <= 0:
        return None
    probabilities = {s: weights[s] / mass for s in present}
    expected_return = sum(probabilities[s] * (fair_values[s] / price - 1.0) for s in present)
    if not math.isfinite(expected_return):
        return None

    if has_scenarios:
        legs = " / ".join(
            f"${fair_values[s]:,.2f}" for s in ("bear", "base", "bull") if s in fair_values
        )
        prior_label = (
            "per-name prior (unaccepted)" if prior is not None else "default prior (unaccepted)"
        )
        partial = "; partial scenarios; weights renormalized" if len(fair_values) < 3 else ""
        detail = (
            f"upside to present fair value {expected_return * 100.0:+.0f}% · {legs} vs ${price:,.2f}"
            f" · {prior_label}{partial}"
        )
    else:
        detail = (
            f"upside to present fair value {base_return * 100.0:+.0f}% · fair ${base_fv:,.2f}"
            f" vs ${price:,.2f} · base point estimate; scenarios unavailable"
        )

    return ScenarioReward(
        expected_return=expected_return,
        base_return=base_return,
        bull_return=bull_return,
        bear_return=bear_return,
        has_scenarios=has_scenarios,
        probabilities=probabilities,
        detail=detail,
        weights_source=weights_source,
    )
