"""Parity tests for the extracted research proposal routes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import comments_server
import comments_server_proposal_routes as proposal_routes
import pytest
from comments_server_proposal_routes import (
    ResearchProposalRouteContext,
    register_research_proposal_routes,
)
from flask import Flask
from flask.testing import FlaskClient

from research.proposal_approval import AskProposalDecisionV1

_PRIOR_HEAD = "0059_kpi_facts_restatement"


def _noop_activation_count(panel_id: str) -> None:
    return None


def _noop_redacted_failure(message: str, exc: object, *, level: str = "error") -> None:
    return None


def _return_none(*_args: object, **_kwargs: object) -> None:
    return None


class _FakeReceipt:
    def model_dump(self, mode: str = "json") -> dict[str, object]:
        return {
            "schema_version": "ask_proposal_decision_receipt.v1",
            "proposal_id": 7,
            "proposal_revision": 1,
            "status": "approved",
            "applied": True,
            "message": "saved",
            "replayed": False,
            "canonical_content_sha256": "a" * 64,
            "target_postcondition_sha256": "b" * 64,
        }


@pytest.fixture
def db_file(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "data" / "portfolio.db", stamp=_PRIOR_HEAD)


@pytest.fixture
def client(tmp_path: Path, db_file: Path) -> FlaskClient:
    return comments_server.create_app(tmp_path).test_client()


def test_research_proposal_routes_register_expected_endpoints(tmp_path: Path) -> None:
    app = Flask(__name__)
    register_research_proposal_routes(
        app,
        ResearchProposalRouteContext(
            repo_root=tmp_path,
            db_path=tmp_path / "portfolio.db",
            bump_activation_count=_noop_activation_count,
            log_redacted_failure=_noop_redacted_failure,
        ),
    )
    routes = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/api/research/proposal/<int:proposal_id>/<verb>" not in routes
    assert {
        "/api/research/proposals/<int:proposal_id>",
        "/api/research/proposals/<int:proposal_id>/decision",
    }.issubset(routes)


def test_research_proposal_routes_keep_http_contract(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proposal_routes, "get_ask_proposal_detail", _return_none)
    detail = client.get("/api/research/proposals/999999")
    assert detail.status_code == 404
    assert detail.get_json()["schema_version"] == "ask_proposal_error.v1"

    def _fake_decide(*_args: object, **_kwargs: object) -> _FakeReceipt:
        return _FakeReceipt()

    monkeypatch.setattr(proposal_routes, "decide_ask_proposal", _fake_decide)
    decision = AskProposalDecisionV1(
        proposal_id=7,
        decision="approve",
        expected_proposal_revision=0,
        decision_request_id="research-decision-1",
    )
    decision_resp = client.post("/api/research/proposals/7/decision", json=decision.model_dump())
    assert decision_resp.status_code == 200
    assert decision_resp.get_json()["status"] == "approved"

    assert (
        client.post(
            "/api/research/proposals/7/decision",
            json=decision.model_dump(),
            headers={"Sec-Fetch-Site": "cross-site"},
        ).status_code
        == 403
    )


def test_retired_research_entrypoints_stay_unavailable(client: FlaskClient) -> None:
    assert client.post("/api/research/proposal/7/approve").status_code == 404
    assert client.post("/api/research/task/7/run").status_code == 404
    assert client.get("/api/panel/musings?fragment=research").status_code == 400
