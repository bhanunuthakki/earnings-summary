"""Integration tests for POST /api/red_team/<id>/respond
(execution/comments_server.py, PR6 — monthly_red_team.md Phase 2 "Forced
response"). Exercises the route end to end (Flask test client) against a
fully-migrated fixture DB, delegating all state-machine assertions to
tests/test_redteam_response.py — this file is about the HTTP contract
(status codes, JSON shape, action validation) layered on top.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from redteam import store  # noqa: E402
from redteam.models import Kind, RedTeamLLMItem  # noqa: E402

PRIOR_HEAD = "0059_kpi_facts_restatement"


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def app_repo(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "portfolio.db"
    cfg = _cfg(db_path)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return tmp_path


@pytest.fixture
def db_path(app_repo: Path) -> Path:
    return app_repo / "data" / "portfolio.db"


@pytest.fixture
def client(app_repo: Path):
    app = comments_server.create_app(app_repo)
    return app.test_client()


def _seed_item(db_path: Path, *, ticker: str | None = "NU", kind: Kind = "per_name") -> int:
    return store.insert_item(
        db_path=db_path,
        run_key="red_team_2026_08",
        ticker=ticker,
        lens="fx_translation" if ticker else "factor_block",
        kind=kind,
        item=RedTeamLLMItem(
            attack_md="Attack text.",
            question_md="Question text?",
            proposed_change_md="Trim the position given the FX risk.",
            severity="high",
        ),
    )


def test_refute_without_response_md_returns_400(client, db_path: Path) -> None:
    item_id = _seed_item(db_path)
    resp = client.post(f"/api/red_team/{item_id}/respond", json={"action": "refute"})
    assert resp.status_code == 400
    assert "response_md" in resp.get_json()["error"]


def test_refute_with_text_returns_200_and_ledger_artifact(client, db_path: Path) -> None:
    item_id = _seed_item(db_path)
    resp = client.post(
        f"/api/red_team/{item_id}/respond",
        json={"action": "refute", "response_md": "Already hedged per the 10-K."},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["status"] == "refuted"
    assert body["artifact_kind"] == "ledger_entry"
    assert body["artifact_id"] is not None


def test_accept_sizing_item_returns_sizing_intent_artifact(client, db_path: Path) -> None:
    item_id = _seed_item(db_path)  # proposed_change_md mentions "Trim"
    resp = client.post(f"/api/red_team/{item_id}/respond", json={"action": "accept"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "accepted"
    assert body["artifact_kind"] == "sizing_intent"


def test_first_defer_returns_200_second_defer_returns_409(client, db_path: Path) -> None:
    item_id = _seed_item(db_path)
    first = client.post(f"/api/red_team/{item_id}/respond", json={"action": "defer"})
    assert first.status_code == 200
    assert first.get_json()["status"] == "deferred"

    second = client.post(f"/api/red_team/{item_id}/respond", json={"action": "defer"})
    assert second.status_code == 409
    body = second.get_json()
    assert body["escalated"] is True
    assert body["status"] == "deferred"
    assert body["defer_count"] == 1


def test_responding_to_already_terminal_item_returns_409(client, db_path: Path) -> None:
    item_id = _seed_item(db_path)
    client.post(f"/api/red_team/{item_id}/respond", json={"action": "accept"})
    resp = client.post(f"/api/red_team/{item_id}/respond", json={"action": "accept"})
    assert resp.status_code == 409


def test_unknown_item_id_returns_404(client, db_path: Path) -> None:
    resp = client.post("/api/red_team/999999/respond", json={"action": "accept"})
    assert resp.status_code == 404


def test_invalid_action_returns_400(client, db_path: Path) -> None:
    item_id = _seed_item(db_path)
    resp = client.post(f"/api/red_team/{item_id}/respond", json={"action": "bogus"})
    assert resp.status_code == 400


def test_options_preflight_returns_204(client, db_path: Path) -> None:
    item_id = _seed_item(db_path)
    resp = client.options(f"/api/red_team/{item_id}/respond")
    assert resp.status_code == 204
