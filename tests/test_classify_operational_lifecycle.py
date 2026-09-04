"""Adversarial contract tests for the BHA-119 lifecycle inventory."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.quality.lifecycle import (
    REGISTRY_AUTHORITIES,
    LifecycleError,
    build_inventory,
    validate_inventory,
)
from src.quality.reachability import build_graph


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _refresh_graph(repo: Path) -> None:
    graph = build_graph(repo)
    _write(
        repo / ".tmp/quality/reachability-check.json",
        graph.model_dump_json(indent=2),
    )


def _repo(tmp_path: Path, *, with_graph: bool = True) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _write(tmp_path / "execution/entry.py", "if __name__ == '__main__':\n    print('manual')\n")
    _write(tmp_path / "execution/helper.py", "VALUE = 1\n")
    _write(tmp_path / "cron/task_manifest.json", '{"version": 1, "tasks": []}\n')
    for registry in REGISTRY_AUTHORITIES:
        _write(tmp_path / registry, "PUBLIC_REGISTRY_SURFACE = ()\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=tmp_path,
        check=True,
    )
    if with_graph:
        _refresh_graph(tmp_path)
    return tmp_path


def test_fixture_relative_python_candidate_coverage(tmp_path: Path) -> None:
    report = build_inventory(_repo(tmp_path))
    python = [entry for entry in report.entries if entry.kind == "python_module"]
    assert {(entry.path, entry.disposition) for entry in python} == {
        ("execution/entry.py", "interactive/manual"),
        ("execution/helper.py", "internal/library"),
    }
    assert report.coverage == {
        "candidates": 28,
        "inventoried": 28,
        "omissions": 0,
        "extras": 0,
        "duplicates": 0,
    }
    assert report.status == "PASS"


def test_routes_exclude_tests_and_capture_rule_method_endpoint(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo / "execution/server.py", "@app.get(\n    '/health'\n)\ndef health(): ...\n")
    _write(repo / "tests/test_routes.py", "@app.route('/fake')\ndef fake(): ...\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    _refresh_graph(repo)
    report = build_inventory(repo)
    routes = [entry for entry in report.entries if entry.kind == "flask_route"]
    assert [(entry.targets, entry.methods, entry.endpoint) for entry in routes] == [
        (("/health",), ("GET",), "health")
    ]
    assert report.status == "PASS"


def test_task_wrapper_service_reconstruction_registry_surfaces(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(
        repo / "cron/task_manifest.json",
        json.dumps(
            {
                "version": 1,
                "tasks": [
                    {
                        "task_name": "\\earnings-summary\\daily",
                        "xml": "daily.task.xml",
                        "wrapper": "run_daily.bat",
                    }
                ],
            }
        ),
    )
    _write(repo / "cron/daily.task.xml", "<Task><Command>run_daily.bat</Command></Task>\n")
    _write(repo / "cron/run_daily.bat", "python execution/entry.py\n")
    _write(
        repo / "src/runtime/service_registry.py",
        "SERVICES = (\n"
        "    ManagedService(name='es-dashboard'),\n"
        "    ManagedService(name='es-poller'),\n"
        ")\n",
    )
    _write(
        repo / "start_comments_server.bat",
        "python execution/comments_server.py\n",
    )
    _write(
        repo / "cron/run_capture_poller.bat",
        "python execution/capture_poller.py\n",
    )
    _write(
        repo / "execution/comments_server.py",
        "if __name__ == '__main__':\n    print('dashboard')\n",
    )
    _write(
        repo / "execution/capture_poller.py",
        "if __name__ == '__main__':\n    print('poller')\n",
    )
    _write(repo / "reconstruction_manifest.json", '{"entrypoint":"execution/entry.py"}\n')
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    _refresh_graph(repo)
    report = build_inventory(repo)
    assert report.status == "PASS"
    assert report.surface_counts == {
        "python_module": 4,
        "flask_route": 0,
        "scheduled_task": 1,
        "wrapper": 3,
        "service": 2,
        "reconstruction": 1,
        "registry": 26,
    }
    task = next(entry for entry in report.entries if entry.kind == "scheduled_task")
    assert task.identifier == "\\earnings-summary\\daily"
    assert task.targets == ("cron/run_daily.bat",)
    by_path = {entry.path: entry for entry in report.entries if entry.kind == "python_module"}
    assert by_path["execution/comments_server.py"].disposition == "scheduled/service"
    assert by_path["execution/capture_poller.py"].disposition == "scheduled/service"


def test_missing_or_malformed_graph_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(LifecycleError, match="graph is missing"):
        build_inventory(_repo(tmp_path / "missing", with_graph=False))
    bad = _repo(tmp_path / "bad", with_graph=False)
    _write(bad / ".tmp/quality/reachability-check.json", json.dumps(["bad"]))
    with pytest.raises(LifecycleError, match="invalid typed"):
        build_inventory(bad)


def test_orphan_task_and_stale_fingerprint_hold(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo / "cron/orphan.task.xml", "<Task/>\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    _refresh_graph(repo)
    report = build_inventory(repo)
    assert report.status == "HOLD"
    assert any("absent from manifest" in item for item in report.violations)

    (repo / "cron/orphan.task.xml").unlink()
    subprocess.run(["git", "add", "-u"], cwd=repo, check=True)
    _refresh_graph(repo)
    clean = build_inventory(repo)
    tampered = clean.model_copy(deep=True)
    tampered.counts = {}
    tampered.entries[0].rationale = "fabricated"
    assert "persisted lifecycle semantics differ from the current inventory" in validate_inventory(
        repo, tampered
    )
    _write(repo / "execution/entry.py", "if '__main__' == __name__:\n    print('changed')\n")
    with pytest.raises(LifecycleError, match="graph is stale"):
        validate_inventory(repo, clean)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    _refresh_graph(repo)
    assert "tracked content fingerprint is stale" in validate_inventory(repo, clean)


def test_lifecycle_output_is_deterministic(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = build_inventory(repo).model_dump(mode="json", exclude={"worktree_dirty"})
    second = build_inventory(repo).model_dump(mode="json", exclude={"worktree_dirty"})
    assert first == second
