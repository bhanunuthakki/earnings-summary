"""Synthetic owner-model boundary: validate at the owner, export only allowed fields."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from execution.import_owner_capacity import stage_wealthplan_facts


class GuardedOwner(SimpleNamespace):
    def __getattribute__(self, name: str) -> object:
        if name in {
            "base_comp",
            "bonus_comp",
            "equity_comp",
            "base_bonus_annual",
            "equity_annual",
            "price",
            "gross_amount",
            "cost_basis",
        }:
            raise AssertionError(f"Excluded owner field accessed: {name}")
        return super().__getattribute__(name)


@dataclass
class OwnerValidator:
    result: object
    received: list[object]

    def model_validate(self, value: object) -> object:
        self.received.append(value)
        return self.result


def test_owner_validators_and_actual_event_types_preserve_allowed_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"household": {"fixture": "household"}, "baseline": {"fixture": "baseline"}}
    plan = tmp_path / "data" / "plan.local.json"
    plan.parent.mkdir()
    plan.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(sys, "path", sys.path.copy())
    models = ModuleType("wealthplan.models")
    day = date(2030, 1, 2)
    events: list[object] = []
    for name in (
        "BabyEvent",
        "BuyHouseEvent",
        "MoveCityEvent",
        "WorkBreakEvent",
        "StartupEvent",
        "ExitPayoutEvent",
        "ParentCareEvent",
    ):
        owner_type = type(name, (GuardedOwner,), {})
        setattr(models, name, owner_type)
        events.append(
            owner_type(
                label=name,
                birth_date=day,
                purchase_date=day,
                move_date=day,
                start_date=day,
                end_date=None,
                payout_date=day,
                person=SimpleNamespace(value="person_a"),
                to_city="Fixture City",
                start_age=65,
                end_age=70,
            )
        )
    # Matching attributes alone do not turn an unknown owner event into a baby.
    events.append(GuardedOwner(label="Unknown event", birth_date=day))
    household = GuardedOwner(
        starting=GuardedOwner(balances={}, as_of=day, equity_fraction=0.6),
        retirement=GuardedOwner(cash_buffer_months=12, target_retirement_age=60, horizon_age=90),
        glide=GuardedOwner(equity_accumulation=0.8, equity_retirement=0.5, derisk_years=10),
        home_city="Fixture City",
        person_a=GuardedOwner(
            name="Fixture Person",
            promotions=[
                GuardedOwner(label="Career transition", effective=day),
                GuardedOwner(label="Promotion", effective=day),
            ],
        ),
        person_b=GuardedOwner(name="Other Fixture Person", promotions=[]),
    )
    household_inputs: list[object] = []
    scenario_inputs: list[object] = []
    setattr(models, "Household", OwnerValidator(household, household_inputs))
    setattr(models, "Scenario", OwnerValidator(SimpleNamespace(events=events), scenario_inputs))
    monkeypatch.setitem(sys.modules, "wealthplan.models", models)

    facts = stage_wealthplan_facts(tmp_path)

    assert household_inputs == [payload["household"]]
    assert scenario_inputs == [payload["baseline"]]
    assert len(facts) == 14  # Six summaries, seven supported events, one career change.
    by_key = {fact.key: fact for fact in facts}
    assert by_key["life_event.work_break_person_a_2030_01_02"].value == {
        "kind": "work_break",
        "label": "WorkBreakEvent",
        "date": "2030-01-02",
        "end_date": None,
        "person": "person_a",
    }
    assert by_key["life_event.parent_care_65_70"].value == {
        "label": "ParentCareEvent",
        "start_age": 65,
        "end_age": 70,
    }
    assert all(fact.status == "affirmed" and fact.review_horizon_days is None for fact in facts)
    assert all(
        fact.source_detail == "wealthplan/data/plan.local.json as_of=2030-01-02" for fact in facts
    )
    assert not any("Unknown event" in fact.narrative for fact in facts)
