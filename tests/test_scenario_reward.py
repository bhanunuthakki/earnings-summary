"""Asymmetry-aware DCF reward (the single producer feeding the next-dollar
model, the sizing audit, and the risk-budget allocator).

Pins: the bull/base/bear parse, the probability-weighted expectation (and its
renormalization when only one tail is on file), the symmetric → base identity,
the downside-skew case, and the point-estimate fallback.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf.scenario_reward import (  # noqa: E402
    SCENARIO_PROBABILITIES,
    parse_scenario_fair_values,
    scenario_reward,
)


def _snap(bull: object, base: object, bear: object) -> str:
    return json.dumps(
        {
            "format": "redesign",
            "scenarios": {
                "bull": {"fair_value_per_share_usd": bull},
                "base": {"fair_value_per_share_usd": base},
                "bear": {"fair_value_per_share_usd": bear},
            },
        }
    )


# --------------------------------------------------------------------------- #
# parse
# --------------------------------------------------------------------------- #


def test_parse_extracts_three_legs() -> None:
    fv = parse_scenario_fair_values(_snap(150.0, 100.0, 60.0))
    assert fv == {"bull": 150.0, "base": 100.0, "bear": 60.0}


def test_parse_tolerates_malformed_and_nonpositive() -> None:
    assert parse_scenario_fair_values(None) == {}
    assert parse_scenario_fair_values("") == {}
    assert parse_scenario_fair_values("{not json") == {}
    assert parse_scenario_fair_values(json.dumps({"format": "redesign"})) == {}
    assert parse_scenario_fair_values(json.dumps({"scenarios": "oops"})) == {}
    # bool is not a fair value; None / non-positive legs are dropped.
    assert parse_scenario_fair_values(_snap(True, 100.0, -5.0)) == {"base": 100.0}
    assert parse_scenario_fair_values(_snap(None, 100.0, 0.0)) == {"base": 100.0}


# --------------------------------------------------------------------------- #
# expectation
# --------------------------------------------------------------------------- #


def test_no_scenarios_is_base_point_estimate() -> None:
    r = scenario_reward(price=100.0, base_fv=120.0, snapshot_json=None)
    assert r is not None
    assert r.has_scenarios is False
    assert r.expected_return == pytest.approx(0.20)
    assert r.base_return == pytest.approx(0.20)
    assert r.skew == pytest.approx(0.0)
    assert r.bull_return is None and r.bear_return is None
    assert r.detail == "fair $120.00 vs $100.00"


def test_symmetric_range_equals_base() -> None:
    # bull/bear equidistant from base in fair-value space, 0.25/0.25 → expectation
    # collapses to the base return.
    r = scenario_reward(price=100.0, base_fv=100.0, snapshot_json=_snap(140.0, 100.0, 60.0))
    assert r is not None
    assert r.has_scenarios is True
    assert r.expected_return == pytest.approx(0.0, abs=1e-9)
    assert r.skew == pytest.approx(0.0, abs=1e-9)
    assert r.probabilities == {
        "bull": pytest.approx(0.25),
        "base": pytest.approx(0.50),
        "bear": pytest.approx(0.25),
    }


def test_downside_skew_drags_expectation_below_base() -> None:
    # bear far below, bull only modestly above → expected < base (negative skew).
    r = scenario_reward(price=100.0, base_fv=110.0, snapshot_json=_snap(130.0, 110.0, 40.0))
    assert r is not None
    # 0.25*0.30 + 0.5*0.10 + 0.25*(-0.60) = -0.025 → -2.5% expected vs +10% base.
    assert r.expected_return == pytest.approx(-0.025)
    assert r.base_return == pytest.approx(0.10)
    assert r.skew == pytest.approx(-0.125)
    assert r.bull_return == pytest.approx(0.30)
    assert r.bear_return == pytest.approx(-0.60)
    assert "exp -2%" in r.detail
    assert "$40.00" in r.detail and "$130.00" in r.detail


def test_single_tail_renormalizes_probabilities() -> None:
    # Only the bull tail present (bear null): mass over {bull, base} renormalizes.
    r = scenario_reward(price=100.0, base_fv=100.0, snapshot_json=_snap(200.0, 100.0, None))
    assert r is not None
    assert r.has_scenarios is True
    assert r.bear_return is None
    assert r.probabilities == {"bull": pytest.approx(1 / 3), "base": pytest.approx(2 / 3)}
    # (1/3)*1.0 + (2/3)*0.0 = 0.333…
    assert r.expected_return == pytest.approx(1 / 3)


def test_value_of_record_base_overrides_snapshot_base() -> None:
    # The snapshot's own base differs from npv_per_share; the value-of-record wins.
    r = scenario_reward(price=100.0, base_fv=110.0, snapshot_json=_snap(150.0, 999.0, 70.0))
    assert r is not None
    assert r.base_return == pytest.approx(0.10)  # 110/100 - 1, not 999
    assert r.expected_return == pytest.approx(0.25 * 0.50 + 0.50 * 0.10 + 0.25 * -0.30)


def test_guards_return_none() -> None:
    assert scenario_reward(price=None, base_fv=100.0) is None
    assert scenario_reward(price=0.0, base_fv=100.0) is None
    assert scenario_reward(price=100.0, base_fv=None) is None
    assert scenario_reward(price=100.0, base_fv=-1.0) is None


def test_probability_prior_is_visible_and_sums_to_one() -> None:
    assert SCENARIO_PROBABILITIES == {"bull": 0.25, "base": 0.50, "bear": 0.25}
    assert sum(SCENARIO_PROBABILITIES.values()) == pytest.approx(1.0)
