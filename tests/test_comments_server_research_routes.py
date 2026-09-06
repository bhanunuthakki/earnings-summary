"""Parity tests for the extracted research task routes."""

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
from comments_server_research_routes import (  # noqa: E402
    ResearchTaskRouteContext,
    register_research_task_routes,
)

from research.proposals import create_task  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


def _noop_activation_count(panel_id: str) -> None:
    return None


def _noop_redacted_failure(message: str, exc: object, *, level: str = "error") -> None:
    return None


def _start_inline_background_task(task: Callable[[], None], name: str) -> None:
    task()


@pytest.fixture
def db_file(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "data" / "portfolio.db", stamp=_PRIOR_HEAD)


@pytest.fixture
def client(tmp_path: Path, db_file: Path) -> FlaskClient:
    return comments_server.create_app(tmp_path).test_client()


def _seed_research(db: Path) -> int:
    return create_task(
        note_id=None,
        claim="do NU's margins still hold?",
        ticker="NU",
        db_path=db,
    )


def test_research_task_routes_register_expected_endpoints(tmp_path: Path) -> None:
    app = Flask(__name__)
    register_research_task_routes(
        app,
        ResearchTaskRouteContext(
            repo_root=tmp_path,
            db_path=tmp_path / "portfolio.db",
            start_background_task=_start_inline_background_task,
            bump_activation_count=_noop_activation_count,
            log_redacted_failure=_noop_redacted_failure,
        ),
    )
    routes = {rule.rule for rule in app.url_map.iter_rules()}
    assert {
        "/api/research/task/<int:task_id>/run",
        "/api/research/task/<int:task_id>/status",
        "/api/research/task/<int:task_id>/reject",
    }.issubset(routes)


def test_research_task_lifecycle_routes_keep_http_contract(
    client: FlaskClient, db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _seed_research(db_file)

    assert client.open(f"/api/research/task/{task_id}/run", method="OPTIONS").status_code == 204
    assert client.post(f"/api/research/task/{task_id}/run").status_code == 403

    monkeypatch.setenv("LEDGER_RESEARCH_RUN", "1")
    run_mod = pytest.importorskip("research.run")
    started = []

    def _fake_run(tid: int, **kwargs: object) -> int:
        started.append(tid)
        return task_id

    monkeypatch.setattr(run_mod, "run_research_task", _fake_run)
    resp = client.post(f"/api/research/task/{task_id}/run")
    assert resp.status_code == 200
    assert resp.get_json() == {"started": True}
    assert started == [task_id]

    status = client.get(f"/api/research/task/{task_id}/status")
    assert status.status_code == 200
    assert status.get_json() == {"status": "proposed"}
    assert client.get("/api/research/task/999999/status").status_code == 404

    rejected = client.post(f"/api/research/task/{task_id}/reject")
    assert rejected.status_code == 200
    assert rejected.get_json() == {"ok": True}
    assert (
        client.post(
            f"/api/research/task/{task_id}/reject", headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )
    assert client.post("/api/research/task/999999/reject").status_code == 404
