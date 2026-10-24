"""PR D — /api/comments/process: synchronous dry-run inline vs apply-as-job."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import cast

import comments_server
import process_report_comments as prc
import pytest
from flask.testing import FlaskClient
from pydantic import TypeAdapter

import comments
from dispatch_registry import Job, Registry
from tests.comments_server_seams import patch_server_seam


class _NonSpawningRegistry(Registry):
    def start(
        self,
        *,
        ticker: str,
        kind: str,
        argv: list[str],
        spawn: bool = True,
        cwd: str | None = None,
        write_sets: list[str] | None = None,
        code_root: str | Path | None = None,
    ) -> Job:
        return super().start(
            ticker=ticker,
            kind=kind,
            argv=argv,
            spawn=False,
            cwd=cwd,
            write_sets=write_sets,
            code_root=code_root,
        )


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
def client(repo: Path) -> FlaskClient:
    return comments_server.create_app(repo, registry=_NonSpawningRegistry()).test_client()


def test_process_dry_run_returns_inline_results(
    client: FlaskClient,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_call_llm(*_args: object, **kwargs: object) -> object:
        schema = kwargs.get("schema")
        assert isinstance(schema, TypeAdapter)
        return cast(
            object,
            schema.validate_python({"revised_thesis": "x", "diff_summary": "y"}),
        )

    monkeypatch.setattr(
        prc,
        "call_llm_structured",
        _fake_call_llm,
    )
    resp = client.post(
        "/api/comments/process", json={"ticker": "NU", "report_date": "2026-05-18", "apply": False}
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["applied"] is False
    assert isinstance(body["results"], list)
    assert _on_disk_thesis(repo) == "Original."  # dry-run never writes


def test_process_apply_dispatches_job(client: FlaskClient) -> None:
    resp = client.post(
        "/api/comments/process", json={"ticker": "NU", "report_date": "2026-05-18", "apply": True}
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["kind"] == "comments-process"
    reg = cast(Registry, client.application.config["DISPATCH_REGISTRY"])
    job = reg.get(body["job_id"])
    assert job is not None
    assert "--apply" in job.argv
    assert "--report-date" in job.argv


def test_process_apply_threads_clear_and_no_rebuild(client: FlaskClient) -> None:
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
    reg = cast(Registry, client.application.config["DISPATCH_REGISTRY"])
    job = reg.get(resp.get_json()["job_id"])
    assert job is not None
    assert "--clear" in job.argv
    assert "--no-rebuild" in job.argv


def test_process_missing_ticker_400(client: FlaskClient) -> None:
    resp = client.post("/api/comments/process", json={})
    assert resp.status_code == 400


def test_process_preview_failure_is_generic_and_correlated(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("provider failed?api_key=secret-value")

    patch_server_seam(monkeypatch, "process_comments_for_ticker", _boom)
    resp = client.post(
        "/api/comments/process",
        json={"ticker": "NU", "report_date": "2026-05-18", "apply": False},
        headers={"X-Correlation-ID": "comment-degraded-test"},
    )

    assert resp.status_code == 200
    assert resp.get_json() == {
        "degraded": True,
        "reason": "comment preview unavailable; retry the request",
        "correlation_id": "comment-degraded-test",
    }
    assert "secret-value" not in resp.get_data(as_text=True)


def _on_disk_thesis(repo: Path) -> str:
    data = json.loads((repo / "micro_thesis" / "holdings" / "NU.json").read_text(encoding="utf-8"))
    return cast(str, data["thesis"])


@pytest.mark.parametrize("intent", ["edit_thesis", "edit_structured", "extract_kpi"])
@pytest.mark.parametrize(
    "failed_step", [None, "seed_kpi_definitions", "run_thesis_evaluator", "build_artifacts"]
)
def test_comment_rebuild_keeps_core_steps_without_automatic_numeric_extraction(
    repo: Path, monkeypatch: pytest.MonkeyPatch, intent: str, failed_step: str | None
) -> None:
    cache = repo / "data" / "bear_case" / "NU.json"
    cache.parent.mkdir()
    cache.write_text("{}")
    calls: list[tuple[str, list[str], int]] = []

    def spawn(root: Path, *, name: str, cmd: list[str], timeout: int) -> dict[str, object]:
        assert root == repo
        calls.append((name, cmd, timeout))
        return {"name": name, "exit_code": 1 if name == failed_step else 0}

    monkeypatch.setattr(prc, "_spawn_step", spawn)
    result = prc.maybe_auto_rebuild(repo, "NU", [{"intent": intent, "status": "ok"}])
    assert [name for name, _, _ in calls] == [
        "seed_kpi_definitions",
        "run_thesis_evaluator",
        "build_artifacts",
    ]
    assert [timeout for _, _, timeout in calls] == [60, 300, 900]
    assert not cache.exists()
    assert result["rebuilt"] is (failed_step is None)
    assert result["caches_invalidated"] == ["data/bear_case/NU.json"]
    assert result["mutating_intents_applied"] == 1
    assert "--enable-llm" in calls[-1][1]
    assert "--repo-root" in calls[0][1] and "--repo-root" in calls[-1][1]
