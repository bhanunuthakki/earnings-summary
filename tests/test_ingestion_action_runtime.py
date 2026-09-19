"""Manual ingestion uses runtime code/dependencies and retains state write ownership."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from dispatch_registry import Job, Registry
from execution import comments_server
from operations.registry import build_operations_registry
from runtime import python_process


class RecordingRegistry(Registry):
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


@pytest.mark.parametrize("route", ["refresh", "refresh-ir"])
@pytest.mark.parametrize("environment", ["venv", ".venv", None])
def test_windows_ingestion_keeps_runtime_and_state_authorities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, environment: str | None
) -> None:
    state = tmp_path / "product-state"
    code = tmp_path / "runtime-code"
    state.mkdir()
    code.mkdir()
    database = state / "configured.db"
    database.touch()
    # A state-root environment is never a substitute for the runtime environment.
    state_python = state / "venv" / "Scripts" / "python.exe"
    state_python.parent.mkdir(parents=True)
    state_python.touch()
    expected_python = code / (environment or "venv") / "Scripts" / "python.exe"
    if environment:
        expected_python.parent.mkdir(parents=True)
        expected_python.touch()
    registry = RecordingRegistry(repo_root=state)
    app = comments_server.create_app(
        state,
        db_path=database,
        code_root=code,
        registry=registry,
        operations_registry=build_operations_registry(Path(__file__).resolve().parents[1]),
    )
    monkeypatch.setattr(
        python_process, "sys", SimpleNamespace(platform="win32", executable="unapproved-global.exe")
    )
    response = app.test_client().post(f"/actions/{route}", json={"ticker": "ACME"})
    if environment is None:
        assert response.status_code == 503
        assert response.get_json() == {"error": "managed_python_unavailable"}
        assert registry.list_jobs() == []
        return
    assert response.status_code == 201
    payload = cast("dict[str, object]", response.get_json())
    job_id = payload["job_id"]
    assert isinstance(job_id, str)
    job = registry.get(job_id)
    assert job is not None
    target = "refresh_dispatch.py" if route == "refresh" else "refresh_ir_kpis.py"
    assert job.argv[:3] == [
        str(expected_python),
        str(code / "execution" / "sqlite_bootstrap.py"),
        str(code / "execution" / target),
    ]
    state_flag = "--state-root" if route == "refresh" else "--repo-root"
    assert job.argv[job.argv.index(state_flag) + 1] == str(state)
    assert job.argv[job.argv.index("--db") + 1] == str(database)
    assert job.code_repo_root == str(code)
    assert job.cwd == str(code)
    assert job.lock_repo_root == str(state)
    assert job.write_sets == ("portfolio-db",)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_non_windows_keeps_current_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(
        python_process, "sys", SimpleNamespace(platform=platform, executable="isolated-test-python")
    )
    assert python_process.application_python_executable(tmp_path) == "isolated-test-python"


def test_windows_environment_precedence_matches_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for environment in ("venv", ".venv"):
        executable = tmp_path / environment / "Scripts" / "python.exe"
        executable.parent.mkdir(parents=True)
        executable.touch()
    monkeypatch.setattr(
        python_process, "sys", SimpleNamespace(platform="win32", executable="unapproved-global.exe")
    )
    assert python_process.application_python_executable(tmp_path) == str(
        tmp_path / "venv" / "Scripts" / "python.exe"
    )


def test_windows_environment_directory_is_not_an_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "venv" / "Scripts" / "python.exe").mkdir(parents=True)
    monkeypatch.setattr(
        python_process, "sys", SimpleNamespace(platform="win32", executable="unapproved-global.exe")
    )
    with pytest.raises(
        python_process.ManagedPythonUnavailableError, match="managed_python_unavailable"
    ):
        python_process.application_python_executable(tmp_path)
