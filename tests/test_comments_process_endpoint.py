"""PR D — /api/comments/process: synchronous dry-run inline vs apply-as-job."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402
import process_report_comments as prc  # noqa: E402

import comments  # noqa: E402
from dispatch_registry import Registry  # noqa: E402


class _NonSpawningRegistry(Registry):
    def start(self, *, ticker, kind, argv, spawn=True):  # type: ignore[override]
        return super().start(ticker=ticker, kind=kind, argv=argv, spawn=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    sqlite3.connect(str(tmp_path / "data" / "portfolio.db")).close()
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(
        json.dumps({"ticker": "NU", "name": "Nu", "thesis": "Original.", "verdict": "Pending"}),
        encoding="utf-8",
    )
    comments.append_comment(
        tmp_path,
        "NU",
        date(2026, 5, 18),
        anchor=comments.Anchor(type="thesis_lede", key="thesis_lede"),
        text="Tighten ROE.",
        selected_text=None,
        intent="edit_thesis",
    )
    return tmp_path


@pytest.fixture
def client(repo: Path):
    return comments_server.create_app(repo, registry=_NonSpawningRegistry()).test_client()


def test_process_dry_run_returns_inline_results(client, repo, monkeypatch) -> None:
    monkeypatch.setattr(
        prc,
        "call_llm",
        lambda *a, **k: '{"revised_thesis": "x", "diff_summary": "y"}',
    )
    resp = client.post(
        "/api/comments/process", json={"ticker": "NU", "report_date": "2026-05-18", "apply": False}
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["applied"] is False
    assert isinstance(body["results"], list)
    assert _on_disk_thesis(repo) == "Original."  # dry-run never writes


def test_process_apply_dispatches_job(client) -> None:
    resp = client.post(
        "/api/comments/process", json={"ticker": "NU", "report_date": "2026-05-18", "apply": True}
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["kind"] == "comments-process"
    reg = client.application.config["DISPATCH_REGISTRY"]
    job = reg.get(body["job_id"])
    assert job is not None
    assert "--apply" in job.argv
    assert "--report-date" in job.argv


def test_process_apply_threads_clear_and_no_rebuild(client) -> None:
    resp = client.post(
        "/api/comments/process",
        json={
            "ticker": "NU",
            "report_date": "2026-05-18",
            "apply": True,
            "clear": True,
            "no_rebuild": True,
        },
    )
    reg = client.application.config["DISPATCH_REGISTRY"]
    job = reg.get(resp.get_json()["job_id"])
    assert "--clear" in job.argv
    assert "--no-rebuild" in job.argv


def test_process_missing_ticker_400(client) -> None:
    resp = client.post("/api/comments/process", json={})
    assert resp.status_code == 400


def _on_disk_thesis(repo: Path) -> str:
    data = json.loads((repo / "micro_thesis" / "holdings" / "NU.json").read_text(encoding="utf-8"))
    return data["thesis"]
