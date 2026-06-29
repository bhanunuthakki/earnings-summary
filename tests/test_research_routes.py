"""Phase-1 W1-5d + W1-7: the research run route, the 4-action route, and the
Ledger → Research inbox lane fragment."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from research.proposals import create_proposal, create_task, get_proposal  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_db(db_path: Path) -> tuple[int, int]:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")
    task_id = create_task(
        note_id=None, claim="do NU's margins still hold?", ticker="NU", db_path=db_path
    )
    proposal_id = create_proposal(
        task_id=task_id,
        kind="memo",
        ticker="NU",
        title="NU margins hold",
        body_md="The book looks stable.",
        budget_tier="cheap",
        db_path=db_path,
    )
    return task_id, proposal_id


@pytest.fixture
def ctx(tmp_path: Path) -> tuple[FlaskClient, Path, int, int]:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    task_id, proposal_id = _build_db(db)
    client = comments_server.create_app(tmp_path).test_client()
    return client, db, task_id, proposal_id


def test_research_fragment_renders_proposals_and_wonderings(ctx) -> None:
    client, _db, _task_id, _pid = ctx
    resp = client.get("/api/panel/musings?fragment=research")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "NU margins hold" in body  # the proposal card
    assert "Approve" in body and "Reject" in body  # the 4-action footer
    assert (
        "do NU&#x27;s margins still hold?" in body or "do NU's margins" in body
    )  # the open wondering
    assert "detection only" in body  # run flag off by default → no Research button


def test_research_run_disabled_by_default(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _db, task_id, _pid = ctx
    monkeypatch.delenv("LEDGER_RESEARCH_RUN", raising=False)
    resp = client.post(f"/api/research/task/{task_id}/run")
    assert resp.status_code == 403


def test_research_run_enabled_invokes_engine(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _db, task_id, _pid = ctx
    monkeypatch.setenv("LEDGER_RESEARCH_RUN", "1")
    import research.run as run_mod

    monkeypatch.setattr(run_mod, "run_research_task", lambda tid, **kw: 4242)
    resp = client.post(f"/api/research/task/{task_id}/run")
    assert resp.status_code == 200
    assert resp.get_json()["proposal_id"] == 4242


def test_approve_action_flips_status_inert(ctx) -> None:
    client, db, _task_id, pid = ctx
    resp = client.post(f"/api/research/proposal/{pid}/approve")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "approved"
    prop = get_proposal(pid, db_path=db)
    assert prop is not None and prop.status == "approved"


def test_steer_action_records_direction(ctx) -> None:
    client, db, _task_id, pid = ctx
    resp = client.post(
        f"/api/research/proposal/{pid}/steer", json={"steer_text": "focus on the credit book"}
    )
    assert resp.status_code == 200
    prop = get_proposal(pid, db_path=db)
    assert prop is not None and "focus on the credit book" in prop.body_md


def test_unknown_verb_rejected(ctx) -> None:
    client, _db, _task_id, pid = ctx
    assert client.post(f"/api/research/proposal/{pid}/delete").status_code == 400
