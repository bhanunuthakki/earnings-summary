"""Synthetic volume x price model. No issuer facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import cast

import pytest

from dcf.driver_model import (
    Anchor,
    Change,
    DriverAssumption,
    DriverModelError,
    IdentityPeriod,
    Params,
    Shock,
    assumption_issues,
    grade_blockers,
    identity_gaps,
    rank_drivers,
    render_markdown,
)
from dcf.reverse_valuation import solve_monotonic

BASE: dict[str, object] = {"volume_growth": 0.10, "price_growth": 0.02, "fee": 1.0, "years": 5}


def run(p: Mapping[str, object]) -> float:
    growth = (1 + cast("float", p["volume_growth"])) * (1 + cast("float", p["price_growth"]))
    return 100.0 * growth ** cast("int", p["years"]) - cast("float", p["fee"])


def shift(key: str, delta: float) -> Change:
    def change(p: Params) -> None:
        p[key] = cast("float", p[key]) + delta

    return change


def record(driver: str, key: str, delta: float, anchor: Anchor) -> DriverAssumption:
    return DriverAssumption(
        driver=driver,
        role="revenue = volume x price",
        base_path="constant rate for five years",
        anchor=anchor,
        counter_argument="The recent rate includes a one-time mix effect.",
        confidence="medium",
        confidence_reason="Three reported years agree.",
        observable="Quarterly release, table 2.",
        adverse=Shock(f"{-delta:+.0%} a year", shift(key, -delta)),
        favourable=Shock(f"{delta:+.0%} a year", shift(key, delta)),
        shock_reason="Range of the last five reported years.",
    )


REPORTED = Anchor("reported_fact", "Synthetic 10-K", "2026-02-01", "p. 30", resolver_admitted=True)
EXTERNAL = Anchor(
    "external_estimate",
    "Synthetic panel",
    "2026-10-01",
    "chart 3",
    publisher="Panel Co",
    measured="app sessions",
    error="+/- 20%",
)
RECORDS = [
    record("Volume growth", "volume_growth", 0.02, EXTERNAL),
    record("Price growth", "price_growth", 0.01, REPORTED),
    record("Fixed fee", "fee", -0.5, REPORTED),
]


def test_ranks_drivers_and_stresses_material_set_together() -> None:
    ranking = rank_drivers(run, BASE, RECORDS)
    assert [e.driver for e in ranking.effects] == ["Volume growth", "Price growth", "Fixed fee"]
    assert ranking.material_drivers == ("Volume growth", "Price growth")
    volume, price = ranking.effects[0], ranking.effects[1]
    low, high = volume.deltas(ranking.base_output)
    assert low < 0 < high and abs(high) > abs(low)  # compounding makes effects asymmetric
    assert ranking.joint_adverse_output is not None
    single_sum = low + price.deltas(ranking.base_output)[0]
    assert ranking.joint_adverse_output < ranking.base_output
    assert ranking.joint_adverse_output - ranking.base_output != pytest.approx(single_sum)
    assert dict(BASE) == {"volume_growth": 0.10, "price_growth": 0.02, "fee": 1.0, "years": 5}


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (replace(RECORDS[0], counter_argument=" "), "counter_argument is empty"),
        (replace(RECORDS[0], anchor=replace(EXTERNAL, error="")), "external estimate needs error"),
        (replace(RECORDS[1], anchor=replace(REPORTED, locator="")), "needs a locator"),
        (replace(RECORDS[1], favourable=None), "both shock directions"),
        (replace(RECORDS[1], observable=""), "observable is empty"),
        (
            replace(
                RECORDS[1], anchor=Anchor("judgment", "builder", "2026-11-16"), confidence="high"
            ),
            "judgment anchor cannot carry high confidence",
        ),
    ],
)
def test_incomplete_record_fails_closed(bad: DriverAssumption, message: str) -> None:
    assert any(message in issue for issue in assumption_issues([bad]))
    with pytest.raises(DriverModelError, match=message):
        rank_drivers(run, BASE, [bad])


def test_mislabelled_shock_direction_is_rejected() -> None:
    flipped = replace(RECORDS[1], adverse=RECORDS[1].favourable, favourable=RECORDS[1].adverse)
    with pytest.raises(DriverModelError, match="wrong way"):
        rank_drivers(run, BASE, [flipped])


def test_constant_parameter_can_record_no_shock() -> None:
    constant = replace(
        RECORDS[2],
        driver="Share of channel",
        adverse=None,
        favourable=None,
        shock_reason="A constant share cancels out of the growth index.",
    )
    ranking = rank_drivers(run, BASE, [RECORDS[0], constant])
    assert ranking.unshocked == ("Share of channel",)


def test_identity_check_flags_a_period_the_tree_does_not_explain() -> None:
    gaps = identity_gaps(
        [IdentityPeriod("FY1", (0.08, 0.04), 0.120), IdentityPeriod("FY2", (0.05, 0.07), 0.160)]
    )
    assert [ok for _, _, ok in gaps] == [True, False]


def test_blockers_and_argument_first_report() -> None:
    ranking = rank_drivers(run, BASE, RECORDS)
    blockers = grade_blockers(
        RECORDS, ranking, model_as_of="2026-08-25", latest_reported_period_end="2026-09-30"
    )
    assert "Volume growth: material driver rests on external_estimate" in blockers
    assert "model is older than the latest reported period" in blockers

    def report(not_modeled: list[str]) -> str:
        return render_markdown(
            question="Can revenue grow while share falls?",
            conclusion="Yes; volume growth carries the result.",
            output_name="year-5 income",
            unit="USD m",
            records=RECORDS,
            ranking=ranking,
            blockers=blockers,
            not_modeled=not_modeled,
            model_as_of="2026-08-25",
            latest_reported_period="Q3 2026",
        )

    text = report(["Purchase fees (upside)"])
    assert text.index("## Drivers ranked") < text.index("## Joint stress")
    assert text.index("## Joint stress") < text.index("## Assumption records")
    assert "Fixed fee (minor)" in text
    with pytest.raises(DriverModelError, match="not modeled"):
        report([])


def test_market_implied_driver_uses_existing_solver() -> None:
    def value_at(x: float) -> float:
        params: dict[str, object] = dict(BASE)
        params["volume_growth"] = x
        return run(params)

    solved = solve_monotonic(value_at, 150.0, 0.0, 0.3)
    assert solved.status == "solved"
    assert solved.implied_value is not None
    assert value_at(solved.implied_value) == pytest.approx(150.0, rel=1e-4)
