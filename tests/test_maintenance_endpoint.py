"""Follow-up I — /actions/maintenance dispatches repo-wide chores as jobs."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from dispatch_registry import Registry  # noqa: E402
from pipeline.dashboard_html import render_dashboard_html  # noqa: E402


class _NonSpawningRegistry(Registry):
    def __init__(self) -> None:
        # Non-spawning jobs never "finish", so raise the concurrency cap above
        # the default 3 — this test fires several maintenance actions per case.
        super().__init__(max_concurrent=50)

    def start(self, *, ticker, kind, argv, spawn=True):  # type: ignore[override]
        return super().start(ticker=ticker, kind=kind, argv=argv, spawn=False)


@pytest.fixture
def client(tmp_path: Path):
    (tmp_path / "data").mkdir()
    sqlite3.connect(str(tmp_path / "data" / "portfolio.db")).close()
    return comments_server.create_app(tmp_path, registry=_NonSpawningRegistry()).test_client()


def _argv(client, **body) -> list[str]:
    resp = client.post("/actions/maintenance", json=body)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    reg: Registry = client.application.config["DISPATCH_REGISTRY"]
    job = reg.get(resp.get_json()["job_id"])
    assert job is not None
    return job.argv


def test_repo_wide_actions_map_to_their_clis(client) -> None:
    # Call each action once — a second call to the same action 409s (single-flight).
    seed = _argv(client, action="seed_kpis")
    assert Path(seed[1]).name == "seed_kpi_definitions.py"
    assert "--all" in seed
    assert Path(_argv(client, action="process_inbox")[1]).name == "register_dropped_documents.py"
    assert Path(_argv(client, action="sweep_history")[1]).name == "sweep_output_history.py"
    assert Path(_argv(client, action="onboard_pending")[1]).name == "onboard_pending_tickers.py"


def test_onboard_requires_ticker(client) -> None:
    resp = client.post("/actions/maintenance", json={"action": "onboard"})
    assert resp.status_code == 400


def test_onboard_with_ticker(client) -> None:
    argv = _argv(client, action="onboard", ticker="nu")
    assert Path(argv[1]).name == "onboard_ticker.py"
    assert "--ticker" in argv
    assert "NU" in argv  # uppercased


def test_unknown_action_rejected(client) -> None:
    resp = client.post("/actions/maintenance", json={"action": "rm -rf /"})
    assert resp.status_code == 400
    assert "unknown action" in resp.get_json()["error"]


def test_dashboard_renders_maintenance_panel() -> None:
    html = render_dashboard_html({"portfolio": [], "evaluation": []})
    assert "Maintenance" in html
    assert 'data-action="seed_kpis"' in html
    assert "/actions/maintenance" in html
