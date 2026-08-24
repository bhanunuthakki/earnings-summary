"""Focused contracts for the governed sizing-intent checkpoint boundary."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from advisor.sizing_intent_checkpoint_api import (
    SizingIntentCheckpointConflictError,
    SizingIntentCheckpointRequest,
    confirm_sizing_intent_checkpoint,
)
from advisor.sizing_intent_review import load_sizing_intent_review
from user_state.sizing import append_intent

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
import comments_server  # noqa: E402


def _body(*, action: str = "add", expected: int | None = None) -> dict[str, object]:
    return {
        "action": action,
        "source_event_id": "bha85-wix-target-v1",
        "expected_prior_sizing_intent_id": expected,
        "holdings_basis": {
            "source": "materialized_holdings_snapshot",
            "as_of": "2026-08-23T12:00:00",
            "source_content_sha256": "a" * 64,
            "embedded_positions": [
                {"ticker": "WIX", "availability": "observed", "weight_pct": 4.75}
            ],
        },
        "leg": {
            "leg_id": "wix-target",
            "ticker": "WIX",
            "action": "add",
            "proposed_delta_pct": 0.25,
            "target_band": {"minimum_pct": 4.5, "maximum_pct": 5.0},
            "price_level": 85.0,
            "account": "tax_deferred_ira",
            "instrument": "equity",
            "horizon": "not_provided",
            "thesis_state": "intact",
            "thesis_content_sha256": "b" * 64,
            "thesis_excerpt": "The operating thesis remains intact.",
            "changed_since_prior": "Target remains recorded",
            "why_now": "Owner confirmed the checkpoint",
            "conviction": "medium",
            "falsifier": "not_provided",
            "portfolio_role": "prosumer software",
            "qualitative_stress_implication": "duration exposure",
            "alternative_use_of_capital": "not_provided",
            "target_verification": "verified",
        },
        "sizing_intent": {
            "leg_id": "wix-target",
            "ticker": "WIX",
            "intent_kind": "target_weight_pct",
            "intent_value": 4.75,
            "narrative": "Owner-recorded target",
            "target_band": {"minimum_pct": 4.5, "maximum_pct": 5.0},
        },
    }


def test_add_is_atomic_idempotent_and_returns_checkpointed_projection(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "checkpoint-api.db")
    request = SizingIntentCheckpointRequest.model_validate(_body())

    first = confirm_sizing_intent_checkpoint(request, ticker="wix", db_path=database)
    second = confirm_sizing_intent_checkpoint(request, ticker="WIX", db_path=database)

    assert first.receipt.created is True
    assert second.receipt.created is False
    assert second.receipt.checkpoint_id == first.receipt.checkpoint_id
    assert first.projection.sizing_intent_id == first.receipt.sizing_intent_ids[0]
    assert first.projection.checkpoint_id == first.receipt.checkpoint_id
    assert first.projection.checkpoint_evidence_available is True


def test_add_conflicts_when_the_expected_empty_state_has_changed(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "checkpoint-api-conflict.db")
    confirm_sizing_intent_checkpoint(
        SizingIntentCheckpointRequest.model_validate(_body()), ticker="WIX", db_path=database
    )

    with pytest.raises(SizingIntentCheckpointConflictError, match="already exists"):
        confirm_sizing_intent_checkpoint(
            SizingIntentCheckpointRequest.model_validate(
                {**_body(), "source_event_id": "bha85-wix-target-v2"}
            ),
            ticker="WIX",
            db_path=database,
        )


def test_revise_appends_a_new_immutable_revision(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "checkpoint-api-revise-ratify.db")
    added = confirm_sizing_intent_checkpoint(
        SizingIntentCheckpointRequest.model_validate(_body()), ticker="WIX", db_path=database
    )
    prior_id = added.projection.sizing_intent_id
    revise_body = _body(action="revise", expected=prior_id)
    revise_body["source_event_id"] = "bha85-wix-target-v2"
    revised = confirm_sizing_intent_checkpoint(
        SizingIntentCheckpointRequest.model_validate(revise_body), ticker="WIX", db_path=database
    )

    assert revised.projection.sizing_intent_id != prior_id
    assert revised.receipt.sizing_intent_ids == (revised.projection.sizing_intent_id,)


def test_late_idempotent_replay_projects_its_original_intent_not_the_latest(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "checkpoint-api-late-replay.db")
    v1_request = SizingIntentCheckpointRequest.model_validate(_body())
    v1 = confirm_sizing_intent_checkpoint(v1_request, ticker="WIX", db_path=database)
    v2_body = _body(action="revise", expected=v1.projection.sizing_intent_id)
    v2_body["source_event_id"] = "bha85-wix-target-v2"
    v2 = confirm_sizing_intent_checkpoint(
        SizingIntentCheckpointRequest.model_validate(v2_body), ticker="WIX", db_path=database
    )

    replay = confirm_sizing_intent_checkpoint(v1_request, ticker="WIX", db_path=database)
    latest = load_sizing_intent_review(database).entries[0]

    assert replay.receipt.created is False
    assert replay.receipt.checkpoint_id == v1.receipt.checkpoint_id
    assert replay.projection.sizing_intent_id == v1.projection.sizing_intent_id
    assert replay.projection.checkpoint_id == v1.receipt.checkpoint_id
    assert latest.intent.id == v2.projection.sizing_intent_id


def test_ratify_links_an_existing_uncheckpointed_revision(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "checkpoint-api-ratify.db")
    existing = append_intent(
        ticker="WIX",
        intent_kind="target_weight_pct",
        intent_value=4.75,
        narrative="Owner-recorded target",
        db_path=database,
    )
    ratify_body = _body(action="ratify", expected=existing.id)
    ratify_body["source_event_id"] = "bha85-wix-target-v3"
    sizing_intent = cast(dict[str, object], ratify_body["sizing_intent"]).copy()
    sizing_intent["existing_sizing_intent_id"] = existing.id
    ratify_body["sizing_intent"] = sizing_intent
    ratified = confirm_sizing_intent_checkpoint(
        SizingIntentCheckpointRequest.model_validate(ratify_body), ticker="WIX", db_path=database
    )

    assert ratified.projection.sizing_intent_id == existing.id
    assert ratified.receipt.created is True


def test_route_ticker_cannot_be_rebound_to_a_different_checkpoint_ticker(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "checkpoint-api-ticker.db")
    with pytest.raises(ValueError, match="route ticker"):
        confirm_sizing_intent_checkpoint(
            SizingIntentCheckpointRequest.model_validate(_body()),
            ticker="NU",
            db_path=database,
        )


def test_localhost_api_has_distinct_validation_conflict_and_success_responses(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "checkpoint-api-route.db")
    client = comments_server.create_app(tmp_path, db_path=database).test_client()

    invalid = client.post("/api/sizing-intents/WIX/checkpoint", json={})
    created = client.post("/api/sizing-intents/WIX/checkpoint", json=_body())
    conflict = client.post(
        "/api/sizing-intents/WIX/checkpoint",
        json={**_body(), "source_event_id": "bha85-wix-target-v2"},
    )

    assert invalid.status_code == 400
    assert invalid.get_json()["error"].startswith("validation_error:")
    assert created.status_code == 201
    assert created.get_json()["projection"]["ticker"] == "WIX"
    assert created.get_json()["projection"]["checkpoint_evidence_available"] is True
    assert conflict.status_code == 409
    assert conflict.get_json()["error"].startswith("conflict_error:")


def test_localhost_api_reports_an_unavailable_checkpoint_source(tmp_path: Path) -> None:
    database = tmp_path / "unavailable.db"
    sqlite3.connect(database).close()
    client = comments_server.create_app(tmp_path, db_path=database).test_client()

    unavailable = client.post("/api/sizing-intents/WIX/checkpoint", json=_body())

    assert unavailable.status_code == 503
    assert unavailable.get_json()["error"].startswith("unavailable_error:")
