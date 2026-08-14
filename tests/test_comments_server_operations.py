from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from operations.models import OperationsRegistry, OperationsSnapshot  # noqa: E402
from operations.paths import scheduler_receipt_path, service_receipt_path  # noqa: E402
from operations.registry import build_operations_registry  # noqa: E402
from operations.snapshot import collect_operations_snapshot  # noqa: E402
from pipeline.operations_panel import OperationsPanelView, render_operations_panel  # noqa: E402


def test_operations_panel_route_is_get_only_app_cached_and_one_connection_per_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    build_registry = Mock(return_value=registry)

    def collect(
        current: OperationsRegistry,
        *,
        repo_root: Path,
        conn: sqlite3.Connection,
        observed_at: datetime,
        scheduler_receipt_path: Path,
        service_receipt_path: Path,
    ) -> OperationsSnapshot:
        return collect_operations_snapshot(
            current,
            repo_root=repo_root,
            conn=conn,
            observed_at=observed_at,
            scheduler_receipt_path=scheduler_receipt_path,
            service_receipt_path=service_receipt_path,
        )

    def render(view: OperationsPanelView) -> str:
        return render_operations_panel(view)

    collect_snapshot = Mock(side_effect=collect)
    render_panel = Mock(side_effect=render)
    connections: list[sqlite3.Connection] = []

    def connect(_path: Path, **_kwargs: object) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        connections.append(conn)
        return conn

    monkeypatch.setattr(comments_server, "build_operations_registry", build_registry)
    monkeypatch.setattr(comments_server, "collect_operations_snapshot", collect_snapshot)
    monkeypatch.setattr(comments_server, "render_operations_panel", render_panel)
    monkeypatch.setattr(comments_server, "connect_sqlite", connect)

    client = comments_server.create_app(tmp_path).test_client()
    first = client.get("/api/panel/operations")
    second = client.get("/api/panel/operations")
    rejected = client.post("/api/panel/operations")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers["X-Panel-Cache"] == "hit"
    assert rejected.status_code == 405
    assert build_registry.call_count == 1
    build_registry.assert_called_once_with(comments_server.PROJECT_ROOT.resolve())
    assert collect_snapshot.call_count == 1
    assert render_panel.call_count == 1
    assert len(connections) == 1


def test_operations_registry_comes_from_code_root_not_minimal_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "runtime"
    code_root = tmp_path / "deployed-code"
    runtime_root.mkdir()
    code_root.mkdir()
    registry = build_operations_registry(PROJECT_ROOT)
    build_registry = Mock(return_value=registry)
    monkeypatch.setattr(comments_server, "build_operations_registry", build_registry)

    app = comments_server.create_app(runtime_root, code_root=code_root)

    assert app.config["OPERATIONS_REGISTRY"] is registry
    build_registry.assert_called_once_with(code_root.resolve())


def test_operations_route_uses_runtime_root_canonical_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    captured: dict[str, object] = {}

    def collect(
        current: OperationsRegistry,
        *,
        repo_root: Path,
        conn: sqlite3.Connection,
        observed_at: datetime,
        scheduler_receipt_path: Path,
        service_receipt_path: Path,
    ) -> OperationsSnapshot:
        captured.update(
            repo_root=repo_root,
            conn=conn,
            observed_at=observed_at,
            scheduler_receipt_path=scheduler_receipt_path,
            service_receipt_path=service_receipt_path,
        )
        return collect_operations_snapshot(
            current,
            repo_root=repo_root,
            conn=conn,
            observed_at=observed_at,
            scheduler_receipt_path=scheduler_receipt_path,
            service_receipt_path=service_receipt_path,
        )

    monkeypatch.setattr(comments_server, "collect_operations_snapshot", collect)

    def connect(_path: Path, **_kwargs: object) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(comments_server, "connect_sqlite", connect)
    client = comments_server.create_app(tmp_path, operations_registry=registry).test_client()

    response = client.get("/api/panel/operations")

    assert response.status_code == 200
    assert captured["repo_root"] == tmp_path
    assert captured["scheduler_receipt_path"] == (
        tmp_path / ".tmp" / "operations" / "runtime" / "scheduler.latest.json"
    )
    assert captured["service_receipt_path"] == (
        tmp_path / ".tmp" / "operations" / "runtime" / "services.latest.json"
    )
    assert isinstance(captured["observed_at"], datetime)
    assert captured["observed_at"].tzinfo is UTC


@pytest.mark.parametrize(
    ("case", "expected"),
    (("current", "Current"), ("stale", "Stale"), ("missing", "Missing"), ("invalid", "Invalid")),
)
def test_operations_route_projects_cached_scheduler_receipt_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected: str,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    scheduler = scheduler_receipt_path(tmp_path)
    services = service_receipt_path(tmp_path)
    if case != "missing":
        scheduler.parent.mkdir(parents=True)
        if case == "invalid":
            scheduler.write_text("not json", encoding="utf-8")
            services.write_text("not json", encoding="utf-8")
        else:
            observed = (
                "2020-01-01T00:00:00+00:00" if case == "stale" else datetime.now(UTC).isoformat()
            )
            scheduler.write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "observed_at": observed,
                        "tasks": [
                            {"task_name": task.task_name, "state": "Ready"}
                            for task in registry.scheduled_tasks
                        ],
                    }
                ),
                encoding="utf-8",
            )
            services.write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "observed_at": observed,
                        "services": [
                            {"name": service.name, "state": "Running"}
                            for service in registry.services
                        ],
                    }
                ),
                encoding="utf-8",
            )

    def connect(_path: Path, **_kwargs: object) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(comments_server, "connect_sqlite", connect)
    client = comments_server.create_app(tmp_path, operations_registry=registry).test_client()

    response = client.get("/api/panel/operations")

    assert response.status_code == 200
    assert expected in response.get_data(as_text=True)


def test_readme_update_action_dispatches_preview_and_exact_approved_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code_root = tmp_path / "code"
    runtime_root = tmp_path / "runtime"
    code_root.mkdir()
    runtime_root.mkdir()
    (code_root / "README.md").write_text("# Current\n", encoding="utf-8")
    registry = build_operations_registry(PROJECT_ROOT)
    jobs = Mock()
    jobs.start.return_value = Mock(
        job_id="job_readme",
        ticker="_REPO",
        kind="readme-preview",
        started_at=datetime(2026, 8, 13, tzinfo=UTC),
    )

    def fake_managed_python_argv(*args: object) -> list[str]:
        return [str(arg) for arg in args]

    monkeypatch.setattr(comments_server, "managed_python_argv", fake_managed_python_argv)

    app = comments_server.create_app(
        runtime_root,
        code_root=code_root,
        db_path=runtime_root / "portfolio.db",
        registry=jobs,
        operations_registry=registry,
    )
    client = app.test_client()

    preview = client.post("/actions/readme-update", json={"action": "preview"})

    assert preview.status_code == 201
    preview_call = jobs.start.call_args
    assert preview_call.kwargs["ticker"] == "_REPO"
    assert preview_call.kwargs["kind"] == "readme-preview"
    assert preview_call.kwargs["write_sets"] == ["portfolio-db", "readme-updater"]
    assert "--apply-run" not in preview_call.kwargs["argv"]

    run_id = "d" * 32
    monkeypatch.setattr(
        comments_server,
        "collect_readme_governance_status",
        Mock(return_value=Mock(state="approved_preview", run_id=run_id, can_apply=True)),
    )
    jobs.start.return_value.kind = "readme-apply"
    applied = client.post("/actions/readme-update", json={"action": "apply", "run_id": run_id})

    assert applied.status_code == 201
    apply_call = jobs.start.call_args
    assert apply_call.kwargs["kind"] == "readme-apply"
    assert apply_call.kwargs["argv"][-2:] == ["--apply-run", run_id]
    assert apply_call.kwargs["write_sets"] == ["portfolio-db", "readme-updater"]


def test_readme_apply_route_rejects_unknown_or_stale_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    jobs = Mock()
    monkeypatch.setattr(
        comments_server,
        "collect_readme_governance_status",
        Mock(return_value=Mock(state="stale", run_id="e" * 32, can_apply=False)),
    )
    client = comments_server.create_app(
        tmp_path, registry=jobs, operations_registry=registry
    ).test_client()

    invalid = client.post(
        "/actions/readme-update", json={"action": "apply", "run_id": "../receipt"}
    )
    stale = client.post("/actions/readme-update", json={"action": "apply", "run_id": "e" * 32})

    assert invalid.status_code == 400
    assert stale.status_code == 409
    jobs.start.assert_not_called()


def test_readme_update_action_rejects_non_object_json(tmp_path: Path) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    jobs = Mock()
    client = comments_server.create_app(
        tmp_path, registry=jobs, operations_registry=registry
    ).test_client()

    response = client.post("/actions/readme-update", json=["preview"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON request body must be an object"
    jobs.start.assert_not_called()
