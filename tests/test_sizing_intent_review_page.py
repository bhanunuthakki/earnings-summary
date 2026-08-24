"""Contracts for the standalone sizing-intent review page."""

from __future__ import annotations

import sys
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import cast

from advisor.sizing_intent_checkpoint_api import (
    SizingIntentCheckpointRequest,
    confirm_sizing_intent_checkpoint,
)
from advisor.sizing_intent_review_page import render_sizing_intent_review_page

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
import comments_server  # noqa: E402


def _body() -> dict[str, object]:
    return {
        "action": "add",
        "source_event_id": "bha85-page-wix-v1",
        "expected_prior_sizing_intent_id": None,
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
            "thesis_excerpt": "Recorded owner context.",
            "changed_since_prior": "Recorded",
            "why_now": "Owner confirmed",
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


def test_page_truthfully_labels_unencoded_when_no_ticker_intent(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "review-page-empty.db")
    html = render_sizing_intent_review_page(database, "NU")
    assert "NU sizing-intent review" in html
    assert "unencoded" in html
    assert "stale" in html
    assert "No broker connection" in html
    assert "/api/sizing-intents/${encodeURIComponent(config.ticker)}/checkpoint" in html


def test_page_renders_ratified_revision_provenance_and_form_contract(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "review-page-ratified.db")
    created = confirm_sizing_intent_checkpoint(
        SizingIntentCheckpointRequest.model_validate(_body()), ticker="WIX", db_path=database
    )
    html = render_sizing_intent_review_page(database, "WIX")
    assert "ratified" in html and f">{created.projection.sizing_intent_id}<" in html
    assert "materialized_holdings_snapshot" in html
    assert "Provenance digest" in html
    assert "Expected current revision" in html
    assert '"currentRevisions":{"target_weight_pct":' in html
    assert (
        'option value="add"' in html
        and 'option value="revise"' in html
        and 'option value="ratify"' in html
    )


def test_page_renders_all_current_intent_kinds_and_derives_the_matching_revision(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "review-page-multi-kind.db")
    first = confirm_sizing_intent_checkpoint(
        SizingIntentCheckpointRequest.model_validate(_body()), ticker="WIX", db_path=database
    )
    second_body = deepcopy(_body())
    second_body["source_event_id"] = "bha85-page-wix-risk-v1"
    second_intent = second_body["sizing_intent"]
    assert isinstance(second_intent, dict)
    second_intent["intent_kind"] = "risk_budget_pct"
    second_intent["intent_value"] = 3.0
    second_intent["narrative"] = "Owner-recorded risk budget"
    second_intent["target_band"] = {"minimum_pct": 2.5, "maximum_pct": 3.5}
    second_leg = second_body["leg"]
    assert isinstance(second_leg, dict)
    second_leg["target_band"] = {"minimum_pct": 2.5, "maximum_pct": 3.5}
    second_holdings = second_body["holdings_basis"]
    assert isinstance(second_holdings, dict)
    second_positions = cast(list[dict[str, object]], second_holdings["embedded_positions"])
    assert isinstance(second_positions, list)
    assert isinstance(second_positions[0], dict)
    second_positions[0]["weight_pct"] = 3.0
    second = confirm_sizing_intent_checkpoint(
        SizingIntentCheckpointRequest.model_validate(second_body), ticker="WIX", db_path=database
    )

    html = render_sizing_intent_review_page(database, "WIX")

    assert "Persisted evidence · target_weight_pct" in html
    assert "Persisted evidence · risk_budget_pct" in html
    assert f'"target_weight_pct":{first.projection.sizing_intent_id}' in html
    assert f'"risk_budget_pct":{second.projection.sizing_intent_id}' in html
    assert "const intentKind = String(body.sizing_intent.intent_kind || '').trim();" in html
    assert "body.expected_prior_sizing_intent_id = currentRevision;" in html


def test_page_route_is_a_local_full_page_and_preserves_no_broker_boundary(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "review-page-route.db")
    client = comments_server.create_app(tmp_path, db_path=database).test_client()
    response = client.get("/advisor/sizing-intents/nu")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert "Owner checkpoint" in response.get_data(as_text=True)
