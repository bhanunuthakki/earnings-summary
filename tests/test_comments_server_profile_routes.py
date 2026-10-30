"""Tests for the manual tenet lifecycle routes and retired profile endpoints."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import comments_server
import pytest
from comments_server_profile_routes import (
    ProfileRouteContext,
    register_profile_routes,
)
from flask import Flask
from flask.testing import FlaskClient

from synthesis.tenets import record_tenet, supersede_tenet

_PRIOR_HEAD = "0059_kpi_facts_restatement"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db_file(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "data" / "portfolio.db", stamp=_PRIOR_HEAD)


@pytest.fixture
def client(tmp_path: Path, db_file: Path) -> FlaskClient:
    return comments_server.create_app(tmp_path).test_client()


def test_profile_routes_register_expected_endpoints(tmp_path: Path) -> None:
    app = Flask(__name__)
    register_profile_routes(
        app,
        ProfileRouteContext(
            db_path=tmp_path / "portfolio.db",
        ),
    )
    routes = {rule.rule for rule in app.url_map.iter_rules()}
    assert {
        "/api/tenets",
        "/api/tenets/<int:tenet_id>/<action>",
    }.issubset(routes)
    assert "/api/tenets/distill" not in routes
    assert not any(route.startswith("/api/profile/fact/") for route in routes)


def test_tenets_routes_keep_http_contract(client: FlaskClient, db_file: Path) -> None:
    assert client.open("/api/tenets", method="OPTIONS").status_code == 204

    created = client.post(
        "/api/tenets",
        json={"body_md": "I sell my winners too early.", "scope_key": "exit-discipline"},
    )
    assert created.status_code == 200
    created_body = created.get_json()
    assert created_body == {
        "ok": True,
        "id": created_body["id"],
        "scope_key": "tenet:exit-discipline",
    }

    prop = record_tenet(
        body_md="Trim winners on a double.",
        scope_key="exit-discipline",
        status="proposed",
        source_note_ids=[1],
        db_path=db_file,
    )
    approved = client.post(f"/api/tenets/{prop.id}/approve")
    assert approved.status_code == 200
    assert approved.get_json() == {
        "ok": True,
        "status": "current",
        "receipt": "Adopted — now a standing Tenet in your decision prompts",
    }

    rejected_tenet = record_tenet(
        body_md="Chase momentum.",
        status="proposed",
        source_note_ids=[1],
        db_path=db_file,
    )
    rejected = client.post(f"/api/tenets/{rejected_tenet.id}/reject")
    assert rejected.status_code == 200
    assert rejected.get_json() == {
        "ok": True,
        "receipt": "Retired — this Tenet was not adopted",
    }

    old = record_tenet(body_md="Let winners run.", scope_key="exit-discipline", db_path=db_file)
    new = supersede_tenet(old.id, body_md="Trim on a double.", db_path=db_file)
    assert new is not None
    reverted = client.post(f"/api/tenets/{new.id}/revert")
    assert reverted.status_code == 200
    assert reverted.get_json() == {
        "ok": True,
        "status": "current",
        "receipt": "Reverted — restores your prior belief",
    }
