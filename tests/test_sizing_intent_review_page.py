"""Contracts for the standalone sizing-intent review page."""

from __future__ import annotations

import sys
from pathlib import Path

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
        "action": "add", "source_event_id": "bha85-page-wix-v1", "expected_prior_sizing_intent_id": None,
        "holdings_basis": {"source": "materialized_holdings_snapshot", "as_of": "2026-08-23T12:00:00", "source_content_sha256": "a" * 64, "embedded_positions": [{"ticker": "WIX", "availability": "observed", "weight_pct": 4.75}]},
        "leg": {"leg_id": "wix-target", "ticker": "WIX", "action": "add", "proposed_delta_pct": 0.25, "target_band": {"minimum_pct": 4.5, "maximum_pct": 5.0}, "price_level": 85.0, "account": "tax_deferred_ira", "instrument": "equity", "horizon": "not_provided", "thesis_state": "intact", "thesis_content_sha256": "b" * 64, "thesis_excerpt": "Recorded owner context.", "changed_since_prior": "Recorded", "why_now": "Owner confirmed", "conviction": "medium", "falsifier": "not_provided", "portfolio_role": "prosumer software", "qualitative_stress_implication": "duration exposure", "alternative_use_of_capital": "not_provided", "target_verification": "verified"},
        "sizing_intent": {"leg_id": "wix-target", "ticker": "WIX", "intent_kind": "target_weight_pct", "intent_value": 4.75, "narrative": "Owner-recorded target", "target_band": {"minimum_pct": 4.5, "maximum_pct": 5.0}},
    }


def test_page_truthfully_labels_unencoded_when_no_ticker_intent(tmp_path: Path, migrated_db) -> None:
    database = migrated_db(tmp_path / "review-page-empty.db")
    html = render_sizing_intent_review_page(database, "NU")
    assert "NU sizing-intent review" in html
    assert "unencoded" in html
    assert "stale" in html
    assert "No broker connection" in html
    assert "/api/sizing-intents/${encodeURIComponent(config.ticker)}/checkpoint" in html


def test_page_renders_ratified_revision_provenance_and_form_contract(tmp_path: Path, migrated_db) -> None:
    database = migrated_db(tmp_path / "review-page-ratified.db")
    created = confirm_sizing_intent_checkpoint(SizingIntentCheckpointRequest.model_validate(_body()), ticker="WIX", db_path=database)
    html = render_sizing_intent_review_page(database, "WIX")
    assert "ratified" in html and f">{created.projection.sizing_intent_id}<" in html
    assert "materialized_holdings_snapshot" in html
    assert "Provenance digest" in html
    assert "Expected current revision" in html
    assert "option value=\"add\"" in html and "option value=\"revise\"" in html and "option value=\"ratify\"" in html


def test_page_route_is_a_local_full_page_and_preserves_no_broker_boundary(tmp_path: Path, migrated_db) -> None:
    database = migrated_db(tmp_path / "review-page-route.db")
    client = comments_server.create_app(tmp_path, db_path=database).test_client()
    response = client.get("/advisor/sizing-intents/nu")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert "Owner checkpoint" in response.get_data(as_text=True)
