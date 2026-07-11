"""Asymmetric expected return from a DCF's bull/base/bear scenario range.

The redesigned FCFF refresher persists a ``scenarios`` block into
``dcf_runs.assumption_snapshot_json`` — bull / base / bear fair values per
share (the same block ``report.sections.snapshot._scenario_range`` reads for the
valuation card). Three allocation surfaces consume DCF reward — the next-dollar
``ret`` factor, the sizing audit's valuation-tension flag, and the risk-budget
allocator's reward leg — and all three historically read only the *base* point
estimate (``npv_per_share / price − 1``), throwing away the asymmetry the
scenario range encodes. A name whose bear case is far below today's price while
its bull case is only modestly above is *not* the same reward as a symmetric one
with the same midpoint, and a point estimate hides that.

This module is the single producer of the asymmetry-aware reward, so the three
surfaces can never diverge. It is pure (stdlib only) — give it a live price, the
value-of-record base fair value, and the snapshot JSON, and it returns the
probability-weighted expected return plus the per-leg returns for display. The
scenario probabilities are a visible, documented prior (not fitted) — a coarse
25 / 50 / 25 over bull / base / bear, renormalized when only one tail is on
file. When no tails are present it degrades to exactly the base point estimate
(``has_scenarios=False``), so a row without a scenario range behaves exactly as
before.
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
    """Asymmetry-aware DCF reward for one name at a given price.

    ``expected_return`` is the probability-weighted return over the scenarios
    actually on file (a fraction; +0.12 = +12% expected upside on price). When no
    tail scenarios are present it equals ``base_return`` exactly and
    ``has_scenarios`` is False — the honest point-estimate fallback. ``skew`` is
    ``expected_return − base_return``: negative means the bear tail drags the
    expectation below the midpoint (downside-skewed reward), the asymmetry a
    point estimate would have hidden."""

    expected_return: float
    base_return: float
    bull_return: float | None
    bear_return: float | None
    has_scenarios: bool
    probabilities: dict[str, float]
    detail: str
    # Where ``probabilities`` came from: ``"per_name"`` (the LLM/owner scenario_prior
    # on the run's snapshot) or ``"global"`` (the symmetric 25/50/25 fallback). Lets
    # a surface show whether a name carries a real per-name prior. Defaulted so any
    # external constructor / older test keeps working.
    weights_source: str = "global"

    @property
    def skew(self) -> float:
        """How far the asymmetric expectation sits below (−) or above (+) the
        base point estimate. 0.0 when there are no tails."""
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
    if total <= 0:
        return None
    return {k: v / total for k, v in out.items()}


def scenario_reward(
    *, price: float | None, base_fv: float | None, snapshot_json: object = None
) -> ScenarioReward | None:
    """Asymmetry-aware expected return for one name.

    ``base_fv`` is the value-of-record fair value (``dcf_runs.npv_per_share``) —
    authoritative for the base leg even if the snapshot's own ``base`` differs;
    the bull/bear tails come from ``snapshot_json``. Returns None when the inputs
    can't yield a meaningful return (no price, non-positive base fair value).

    The expectation is ``Σ p_s · (fv_s / price − 1)`` over the scenarios on file,
    with ``p_s`` from :data:`SCENARIO_PROBABILITIES` renormalized to the present
    legs. With no tails it is exactly the base point estimate.
    """
    if price is None or price <= 0 or base_fv is None or base_fv <= 0:
        return None

    fair_values = dict(parse_scenario_fair_values(snapshot_json))
    fair_values["base"] = float(base_fv)  # value-of-record wins for the base leg

    def ret(key: str) -> float | None:
        fv = fair_values.get(key)
        return fv / price - 1.0 if fv is not None else None

    base_return = base_fv / price - 1.0
    bull_return = ret("bull")
    bear_return = ret("bear")
    has_scenarios = bull_return is not None or bear_return is not None

    # Per-name prior (LLM/owner) when the run carries one, else the global 25/50/25.
    # Both are renormalized over whichever legs are actually on file.
    prior = parse_scenario_prior_weights(snapshot_json)
    weights = prior if prior is not None else SCENARIO_PROBABILITIES
    weights_source = "per_name" if prior is not None else "global"
    present = [s for s in weights if s in fair_values]
    mass = sum(weights[s] for s in present)
    probabilities = {s: weights[s] / mass for s in present} if mass > 0 else {}
    expected_return = sum(probabilities[s] * (fair_values[s] / price - 1.0) for s in present)

    if has_scenarios:
        legs = " / ".join(
            f"${fair_values[s]:,.2f}" for s in ("bear", "base", "bull") if s in fair_values
        )
        detail = f"exp {expected_return * 100.0:+.0f}% · {legs} vs ${price:,.2f}"
    else:
        detail = f"fair ${base_fv:,.2f} vs ${price:,.2f}"

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
