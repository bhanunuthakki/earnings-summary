"""Parity tests for the extracted research proposal routes."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402
import comments_server_proposal_routes as proposal_routes  # noqa: E402
from comments_server_proposal_routes import (  # noqa: E402
    ResearchProposalRouteContext,
    register_research_proposal_routes,
)

from research.proposal_approval import AskProposalDecisionV1  # noqa: E402
from research.proposals import create_proposal, create_task, get_proposal  # noqa: E402

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


def _seed_research(db: Path) -> tuple[int, int]:
    task_id = create_task(
        note_id=None,
        claim="do NU's margins still hold?",
        ticker="NU",
        db_path=db,
    )
    proposal_id = create_proposal(
        task_id=task_id,
        kind="memo",
        ticker="NU",
        title="NU margins hold",
        body_md="The book looks stable.",
        budget_tier="cheap",
        db_path=db,
    )
    return task_id, proposal_id


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
    assert {
        "/api/research/proposal/<int:proposal_id>/<verb>",
        "/api/research/proposals/<int:proposal_id>",
        "/api/research/proposals/<int:proposal_id>/decision",
    }.issubset(routes)


def test_research_proposal_routes_keep_http_contract(
    client: FlaskClient, db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _task_id, proposal_id = _seed_research(db_file)

    approve = client.post(f"/api/research/proposal/{proposal_id}/approve")
    assert approve.status_code == 200
    assert approve.get_json()["status"] == "approved"
    assert get_proposal(proposal_id, db_path=db_file) is not None

    steer = client.post(
        f"/api/research/proposal/{proposal_id}/steer",
        json={"steer_text": "focus on the credit book"},
    )
    assert steer.status_code == 200
    assert steer.get_json()["status"] == "steered"

    assert client.post(f"/api/research/proposal/{proposal_id}/delete").status_code == 400

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
