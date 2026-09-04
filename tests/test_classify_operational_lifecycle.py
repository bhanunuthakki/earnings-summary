"""Adversarial contract tests for the BHA-119 lifecycle inventory."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from src.quality.lifecycle import (
    REGISTRY_AUTHORITIES,
    LifecycleError,
    build_inventory,
    lifecycle_evidence_fields,
    validate_inventory,
)
from src.quality.reachability import build_graph


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fingerprint(path: str, line: int, source_line: str) -> str:
    return hashlib.sha256(f"{path}:{line}:{source_line.strip()}".encode()).hexdigest()


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
    _write(
        tmp_path / "docs/quality/lifecycle-dormant-policy.json",
        json.dumps(
            {
                "schema_version": "bha-119.dormant-policy/v1",
                "owner_evidence": "linear:BHA-119",
                "authorization_evidence": "Owner-authorized lifecycle review.",
                "activation_evidence": "explicit-owner-invocation-or-registration",
                "review_on": "2026-12-03",
                "path_prefixes": ["execution/", "src/", "cron/", "scripts/", ".github/"],
                "exact_paths": ["Makefile"],
            }
        ),
    )
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
        ("execution/entry.py", "dormant-until"),
        ("execution/helper.py", "dormant-until"),
    }
    assert report.coverage == {
        "candidates": 28,
        "inventoried": 28,
        "omissions": 0,
        "extras": 0,
        "duplicates": 0,
    }
    assert report.status == "PASS"


def test_src_python_candidates_require_a_main_guard(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo / "src/compute/ir_narrative.py", "if __name__ == '__main__':\n    main()\n")
    _write(
        repo / "src/library_with_parser.py",
        "# if __name__ == '__main__':\nimport argparse\nparser = argparse.ArgumentParser()\n",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    _refresh_graph(repo)

    report = build_inventory(repo)
    python_paths = {entry.path for entry in report.entries if entry.kind == "python_module"}
    assert "src/compute/ir_narrative.py" in python_paths
    assert "src/library_with_parser.py" not in python_paths
    assert report.status == "PASS"


def test_python_candidate_omission_is_not_masked_by_materialization(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(
        repo / "src/compute/ir_narrative.py",
        "# lifecycle: dormant\nif __name__ == '__main__':\n    main()\n",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    _refresh_graph(repo)
    report = build_inventory(repo)
    assert report.status == "HOLD"
    assert (
        "src/compute/ir_narrative.py:python_module:src/compute/ir_narrative.py" in report.omissions
    )


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
    assert task.disposition == "scheduled"
    service_units = [entry for entry in report.entries if entry.kind == "service"]
    assert all(entry.disposition == "service" for entry in service_units)
    by_path = {entry.path: entry for entry in report.entries if entry.kind == "python_module"}
    assert by_path["execution/comments_server.py"].disposition == "service"
    assert by_path["execution/capture_poller.py"].disposition == "service"


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


def test_manual_classification_requires_canonical_owner_and_invocation() -> None:
    with pytest.raises(LifecycleError, match="canonical/runbook owner and invocation"):
        lifecycle_evidence_fields(
            path="execution/manual.py",
            text="if __name__ == '__main__': pass\n",
            disposition="manual-supported",
        )
    with pytest.raises(LifecycleError, match="canonical/runbook owner and invocation"):
        lifecycle_evidence_fields(
            path="execution/manual.py",
            text="# lifecycle: owner=comment\n# lifecycle: invocation=python execution/manual.py\n",
            disposition="manual-supported",
        )


def test_internal_and_one_shot_classifications_fail_closed() -> None:
    with pytest.raises(LifecycleError, match="verified incoming typed edge"):
        lifecycle_evidence_fields(
            path="src/helper.py", text="VALUE = 1\n", disposition="internal-delegate"
        )
    with pytest.raises(LifecycleError, match="sealed completion"):
        lifecycle_evidence_fields(
            path="execution/migrate_data.py",
            text="if __name__ == '__main__': pass\n",
            disposition="one-shot-completed",
        )
    evidence = lifecycle_evidence_fields(
        path="execution/migrate_data.py",
        text="# lifecycle: completion=sealed:receipt.json\n",
        disposition="one-shot-completed",
    )
    assert evidence.get("sealed_completion_evidence") == "sealed:receipt.json"


def test_tombstone_and_dormant_require_distinct_lifecycle_receipts() -> None:
    with pytest.raises(LifecycleError, match="consumer and expiry"):
        lifecycle_evidence_fields(
            path="execution/old.py",
            text="# lifecycle: tombstone\n",
            disposition="compatibility-tombstone",
        )
    with pytest.raises(LifecycleError, match="owner, activation, and review"):
        lifecycle_evidence_fields(
            path="execution/parked.py",
            text="# lifecycle: dormant\n# lifecycle: owner=canonical:directives/old.md\n",
            disposition="dormant-until",
        )
    tombstone = lifecycle_evidence_fields(
        path="execution/old.py",
        text="# lifecycle: tombstone\n# lifecycle: consumer=legacy-adapter\n# lifecycle: expiry=2026-12-31\n",
        disposition="compatibility-tombstone",
    )
    dormant = lifecycle_evidence_fields(
        path="execution/parked.py",
        text="# lifecycle: dormant\n# lifecycle: owner=canonical:directives/old.md\n"
        "# lifecycle: activation=owner-approval\n# lifecycle: review=2026-12-31\n",
        disposition="dormant-until",
    )
    assert tombstone.get("tombstone_consumer") == "legacy-adapter"
    assert tombstone.get("dormant_owner") is None
    assert dormant.get("dormant_activation") == "owner-approval"
    assert dormant.get("tombstone_expiry") is None


def test_expired_lifecycle_deadlines_fail_closed() -> None:
    with pytest.raises(LifecycleError, match="expiry is expired"):
        lifecycle_evidence_fields(
            path="execution/old.py",
            text="# lifecycle: tombstone\n# lifecycle: consumer=legacy-adapter\n# lifecycle: expiry=2020-01-01\n",
            disposition="compatibility-tombstone",
        )
    with pytest.raises(LifecycleError, match="review is expired"):
        lifecycle_evidence_fields(
            path="execution/parked.py",
            text="# lifecycle: dormant\n# lifecycle: owner=linear:BHA-119\n"
            "# lifecycle: activation=owner-approval\n# lifecycle: review=2020-01-01\n",
            disposition="dormant-until",
        )


def test_missing_dormant_policy_and_unknown_operational_edge_fail_closed(
    tmp_path: Path,
) -> None:
    missing = _repo(tmp_path / "missing-policy")
    (missing / "docs/quality/lifecycle-dormant-policy.json").unlink()
    subprocess.run(["git", "add", "-u"], cwd=missing, check=True)
    _refresh_graph(missing)
    with pytest.raises(LifecycleError, match="policy is missing"):
        build_inventory(missing)

    unknown = _repo(tmp_path / "unknown-edge")
    _write(
        unknown / "scripts/probe.py",
        "import subprocess\n\ndef run(command):\n    return subprocess.run(command)\n",
    )
    subprocess.run(["git", "add", "."], cwd=unknown, check=True)
    _refresh_graph(unknown)
    report = build_inventory(unknown)
    assert report.status == "HOLD"
    assert any("operational reachability unknown" in item for item in report.violations)


def test_test_only_import_does_not_establish_runtime_reachability(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo / "tests/test_only.py", "from execution import entry\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    _refresh_graph(repo)
    report = build_inventory(repo)
    entry = next(item for item in report.entries if item.path == "execution/entry.py")
    assert entry.disposition == "dormant-until"
    assert entry.incoming_edge is None
    assert entry.dormant_policy_evidence is not None


def test_reviewed_multi_target_process_edges_retain_each_child(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source_line = "subprocess.run(command)"
    _write(
        repo / "execution/entry.py",
        "import subprocess\n" + source_line + "\nif __name__ == '__main__':\n    print('manual')\n",
    )
    _write(repo / "src/child_a.py", "if __name__ == '__main__':\n    print('a')\n")
    _write(repo / "src/child_b.py", "if __name__ == '__main__':\n    print('b')\n")
    _write(
        repo / "docs/quality/reachability-process-dispositions.json",
        json.dumps(
            {
                "schema_version": "reachability-process-dispositions/v1",
                "edges": [
                    {
                        "path": "execution/entry.py",
                        "line": 2,
                        "fingerprint": _fingerprint("execution/entry.py", 2, source_line),
                        "disposition": "internal_python_target",
                        "targets": ["src/child_b.py", "src/child_a.py"],
                        "evidence": "fixed child entrypoints",
                    }
                ],
            }
        ),
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    _refresh_graph(repo)

    report = build_inventory(repo)
    by_path = {entry.path: entry for entry in report.entries if entry.kind == "python_module"}
    assert by_path["src/child_a.py"].disposition == "internal-delegate"
    assert by_path["src/child_b.py"].disposition == "internal-delegate"
    assert by_path["src/child_a.py"].incoming_edge == "execution/entry.py:2:unknown"
    assert by_path["src/child_b.py"].incoming_edge == "execution/entry.py:2:unknown"


def test_wrapper_comments_do_not_create_scheduled_targets(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(
        repo / "cron/task_manifest.json",
        json.dumps(
            {
                "version": 1,
                "tasks": [{"task_name": "daily", "xml": "daily.task.xml", "wrapper": "daily.bat"}],
            }
        ),
    )
    _write(repo / "cron/daily.task.xml", "<Task><Command>daily.bat</Command></Task>\n")
    _write(
        repo / "cron/daily.bat",
        "REM python execution/comment_only.py\npython execution/entry.py\n",
    )
    _write(
        repo / "execution/comment_only.py",
        "if __name__ == '__main__':\n    print('dormant')\n",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    _refresh_graph(repo)
    report = build_inventory(repo)
    comment_only = next(item for item in report.entries if item.path == "execution/comment_only.py")
    assert comment_only.disposition == "dormant-until"


def test_duplicate_and_unauthoritative_dormant_evidence_fail_closed() -> None:
    with pytest.raises(LifecycleError, match="duplicate lifecycle evidence"):
        lifecycle_evidence_fields(
            path="execution/parked.py",
            text="# lifecycle: dormant\n# lifecycle: owner=linear:BHA-119\n"
            "# lifecycle: owner=linear:BHA-120\n# lifecycle: activation=owner-approval\n"
            "# lifecycle: review=2026-12-31\n",
            disposition="dormant-until",
        )
    with pytest.raises(LifecycleError, match="owner is not authoritative"):
        lifecycle_evidence_fields(
            path="execution/parked.py",
            text="# lifecycle: dormant\n# lifecycle: owner=garbage\n"
            "# lifecycle: activation=owner-approval\n# lifecycle: review=2026-12-31\n",
            disposition="dormant-until",
        )
