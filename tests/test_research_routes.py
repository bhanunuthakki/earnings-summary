"""Phase-1 W1-5d + W1-7: the research run route, the 4-action route, and the
Ledger → Research inbox lane fragment."""

from __future__ import annotations

import logging
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
ResearchContext = tuple[FlaskClient, Path, int, int]


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
def ctx(tmp_path: Path) -> ResearchContext:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    task_id, proposal_id = _build_db(db)
    client = comments_server.create_app(tmp_path).test_client()
    return client, db, task_id, proposal_id


def test_research_fragment_renders_proposals_and_wonderings(
    ctx: ResearchContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _db, _task_id, _pid = ctx
    # Pin the run flag OFF regardless of the machine .env (a bare load_dotenv()
    # in production imports resolves the MAIN repo's .env from any nested
    # worktree and injects LEDGER_RESEARCH_RUN=1 — the conftest FMP_TIER note).
    monkeypatch.delenv("LEDGER_RESEARCH_RUN", raising=False)
    resp = client.get("/api/panel/musings?fragment=research")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "NU margins hold" in body  # the proposal card
    assert "Approve" in body and "Reject" in body  # the 4-action footer
    assert (
        "do NU&#x27;s margins still hold?" in body or "do NU's margins" in body
    )  # the open wondering
    # Run flag off by default -> no per-card "Research it" button, just Dismiss.
    # (PR9: the "runs are off" explanation moved to ONE section-level line —
    # see test_ledger_panel.py's copy tests — so this fragment, which is just
    # the list, carries no env-var-shaped text at all.)
    assert "data-run-task" not in body
    assert "Dismiss" in body
    assert "LEDGER_RESEARCH_RUN" not in body


def test_research_run_disabled_by_default(
    ctx: ResearchContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _db, task_id, _pid = ctx
    monkeypatch.delenv("LEDGER_RESEARCH_RUN", raising=False)
    resp = client.post(f"/api/research/task/{task_id}/run")
    assert resp.status_code == 403


def test_research_run_enabled_invokes_engine_async(
    ctx: ResearchContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run route returns immediately ({started: true}) and drives the
    engine on a background thread — the engine took seconds-to-minutes inline,
    which pinned the request (and the 'Research it' button) for that window."""
    import threading as _threading

    client, _db, task_id, _pid = ctx
    monkeypatch.setenv("LEDGER_RESEARCH_RUN", "1")
    import research.run as run_mod

    ran = _threading.Event()
    seen: list[int] = []

    def _fake_run(tid: int, **kw: object) -> int:
        seen.append(tid)
        ran.set()
        return 4242

    monkeypatch.setattr(run_mod, "run_research_task", _fake_run)
    resp = client.post(f"/api/research/task/{task_id}/run")
    assert resp.status_code == 200
    assert resp.get_json() == {"started": True}
    assert ran.wait(timeout=5), "background research thread never ran"
    assert seen == [task_id]


def test_research_run_conflict_when_not_proposed(
    ctx: ResearchContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research.proposals import set_task_status

    client, db, task_id, _pid = ctx
    monkeypatch.setenv("LEDGER_RESEARCH_RUN", "1")
    set_task_status(task_id, "drafted", db_path=db)
    resp = client.post(f"/api/research/task/{task_id}/run")
    assert resp.status_code == 409


def test_research_task_status_endpoint(ctx: ResearchContext) -> None:
    client, _db, task_id, _pid = ctx
    resp = client.get(f"/api/research/task/{task_id}/status")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "proposed"}
    assert client.get("/api/research/task/999999/status").status_code == 404


def test_approve_action_flips_status_inert(ctx: ResearchContext) -> None:
    client, db, _task_id, pid = ctx
    resp = client.post(f"/api/research/proposal/{pid}/approve")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "approved"
    prop = get_proposal(pid, db_path=db)
    assert prop is not None and prop.status == "approved"


def test_approve_apply_failure_is_redacted_and_correlated(
    ctx: ResearchContext,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import research.apply as apply_mod

    client, _db, _task_id, pid = ctx

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("apply failed?api_key=secret-value")

    monkeypatch.setattr(apply_mod, "apply_approved_proposal", _boom)
    with caplog.at_level(logging.ERROR, logger=client.application.logger.name):
        resp = client.post(
            f"/api/research/proposal/{pid}/approve",
            headers={"X-Correlation-ID": "research-apply-test"},
        )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "approved"
    assert body["applied"] == ""
    assert body["apply_error"] == "approved proposal could not be applied; retry the request"
    assert body["correlation_id"] == "research-apply-test"
    assert "secret-value" not in resp.get_data(as_text=True)
    assert "secret-value" not in caplog.text
    assert "Traceback" not in caplog.text


def test_steer_action_records_direction(ctx: ResearchContext) -> None:
    client, db, _task_id, pid = ctx
    resp = client.post(
        f"/api/research/proposal/{pid}/steer", json={"steer_text": "focus on the credit book"}
    )
    assert resp.status_code == 200
    prop = get_proposal(pid, db_path=db)
    assert prop is not None and "focus on the credit book" in prop.body_md


def test_unknown_verb_rejected(ctx: ResearchContext) -> None:
    client, _db, _task_id, pid = ctx
    assert client.post(f"/api/research/proposal/{pid}/delete").status_code == 400


def test_reject_task_drops_it_from_wonderings(ctx: ResearchContext) -> None:
    client, _db, task_id, _pid = ctx
    resp = client.post(f"/api/research/task/{task_id}/reject")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    body = client.get("/api/panel/musings?fragment=research").data.decode()
    assert f'data-reject-task="{task_id}"' not in body


def test_reject_task_cross_site_rejected(ctx: ResearchContext) -> None:
    client, _db, task_id, _pid = ctx
    resp = client.post(
        f"/api/research/task/{task_id}/reject", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert resp.status_code == 403


def test_reject_unknown_task_404(ctx: ResearchContext) -> None:
    client, _db, _task_id, _pid = ctx
    assert client.post("/api/research/task/999999/reject").status_code == 404
