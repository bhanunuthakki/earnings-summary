"""Hermetic tests for the operational lifecycle producer."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

import quality.lifecycle_inventory as lifecycle_inventory_module

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from execution.classify_operational_lifecycle import main as cli_main
from quality.git_env import clean_local_git_env
from quality.lifecycle import (
    REGISTRY_AUTHORITIES,
    LifecycleEntry,
    LifecycleError,
    build_inventory,
    lifecycle_evidence_fields,
    validate_inventory,
)
from quality.lifecycle_discovery import route_entries
from quality.reachability import ReachabilityGraph, build_graph
from scheduler_manifest import TaskManifest


def _w(p: Path, t: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(t, encoding="utf-8")


def _fp(p: str, ln: int, ev: str) -> str:
    return hashlib.sha256(f"{p}:{ln}:{ev.strip()}".encode()).hexdigest()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, env=clean_local_git_env())


def _load_object(path: Path) -> dict[str, object]:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    out: dict[str, object] = {}
    for key, value in cast(dict[object, object], loaded).items():
        assert isinstance(key, str)
        out[key] = value
    return out


def _task_manifest(*tasks: tuple[str, str, str], version: int = 1) -> str:
    schedule: dict[str, object] = {
        "trigger": "CalendarTrigger",
        "start_boundary": "2099-01-01T00:00:00",
        "repetition_interval": None,
        "days_interval": 1,
        "weeks_interval": None,
        "days_of_week": [],
        "days_of_month": [],
        "months": [],
    }
    return json.dumps(
        {
            "version": version,
            "namespace": "\\earnings-summary",
            "tasks": [
                {"task_name": name, "xml": xml, "wrapper": wrapper, "schedule": schedule}
                for name, xml, wrapper in tasks
            ],
        }
    )


def _refresh(repo: Path) -> None:
    first = build_graph(repo)
    prov = dict(first.parser)
    for rel, schema in (
        (
            "docs/quality/reachability-dynamic-import-dispositions.json",
            "reachability-dynamic-import-dispositions/v1",
        ),
        (
            "docs/quality/reachability-getattr-dispositions.json",
            "reachability-getattr-dispositions/v1",
        ),
        (
            "docs/quality/reachability-process-dispositions.json",
            "reachability-process-dispositions/v1",
        ),
    ):
        target = repo / rel
        payload = _load_object(target) if target.is_file() else {}
        edges_val = payload.get("edges")
        assert edges_val is None or isinstance(edges_val, list)
        payload.setdefault("edges", [])
        payload["schema_version"] = schema
        payload["graph_provenance"] = {
            "path": ".tmp/quality/reachability-check.json",
            "schema_version": "operational-reachability-raw/v1",
            "parser": {
                "name": prov["name"],
                "version": prov["version"],
                "python": prov["python"],
                "source_sha256": prov["source_sha256"],
            },
            "source_manifest_sha256": first.source_manifest_sha256,
            "scanner_sha256": first.scanner_sha256,
        }
        _w(
            target,
            json.dumps(payload),
        )
    _git(repo, "add", "docs/quality")
    _git(
        repo,
        "-c",
        "user.name=F",
        "-c",
        "user.email=f@x.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "refresh",
    )
    final = build_graph(repo)
    assert not final.hold
    _w(repo / ".tmp/quality/reachability-check.json", final.model_dump_json(indent=2))


def _repo(tmp: Path, *, graph: bool = True) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True, env=clean_local_git_env())
    _w(tmp / ".gitignore", ".tmp/\n")
    _w(tmp / "execution/entry.py", "if __name__ == '__main__':\n    print('x')\n")
    _w(tmp / "execution/helper.py", "VALUE = 1\n")
    _w(tmp / "cron/task_manifest.json", _task_manifest() + "\n")
    _w(tmp / "directives/directive_manifest.json", '{"directives": {}}\n')
    _w(
        tmp / "docs/quality/lifecycle-dormant-policy.json",
        json.dumps(
            {
                "schema_version": "operational-lifecycle-dormant-policy/v1",
                "owner_evidence": "linear:BHA-142",
                "authorization_evidence": "auth",
                "activation_evidence": "activate",
                "review_on": "2099-12-31",
                "path_prefixes": ["execution/", "src/", "cron/", "scripts/", ".github/"],
                "exact_paths": ["Makefile"],
            }
        ),
    )
    for r in REGISTRY_AUTHORITIES:
        _w(tmp / r, "PUBLIC = 1\n")
    _git(tmp, "add", ".")
    _git(
        tmp,
        "-c",
        "user.name=F",
        "-c",
        "user.email=f@x.invalid",
        "commit",
        "-qm",
        "f",
    )
    if graph:
        _refresh(tmp)
    return tmp


def test_candidate_coverage(tmp_path: Path) -> None:
    r = build_inventory(_repo(tmp_path))
    assert r.status == "PASS"
    assert (
        r.coverage["omissions"] == 0 and r.coverage["extras"] == 0 and r.coverage["duplicates"] == 0
    )
    assert r.coverage["candidates"] == r.coverage["inventoried"]
    assert [e.path for e in r.entries] == sorted(e.path for e in r.entries)
    assert set(r.counts) == {
        "scheduled",
        "service",
        "ui-reachable",
        "manual-supported",
        "internal-delegate",
        "one-shot-completed",
        "compatibility-tombstone",
        "dormant-until",
        "retire",
    }


def test_canonical_task_manifest_loads() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = lifecycle_inventory_module.load_task_manifest(root)
    assert manifest.namespace == "\\earnings-summary"
    assert len(manifest.tasks) >= 40
    assert all(task.schedule.trigger for task in manifest.tasks)
    assert all(task.wrapper.endswith(".bat") for task in manifest.tasks)


def test_literal_route_and_test_exclusion(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(repo / "execution/server.py", "@app.get('/health')\ndef health(): ...\n")
    _w(repo / "tests/test_r.py", "@app.route('/fake')\ndef fake(): ...\n")
    _git(repo, "add", ".")
    _refresh(repo)
    r = build_inventory(repo)
    routes = [e for e in r.entries if e.kind == "flask_route"]
    assert [(e.targets, e.methods, e.endpoint) for e in routes] == [
        (("/health",), ("GET",), "health")
    ]
    _w(
        repo / "execution/bad.py",
        "@app.route(RULE)\ndef bad(): ...\nif __name__ == '__main__':\n    pass\n",
    )
    _git(repo, "add", ".")
    _refresh(repo)
    r2 = build_inventory(repo)
    assert r2.status == "HOLD" and any(
        "non-literal" in v or "lacks literal" in v for v in r2.violations
    )


def test_route_methods_fail_closed_hold(tmp_path: Path) -> None:
    cases: dict[str, str] = {
        "dynamic": "METHODS = ['POST']\n@app.route('/submit', methods=METHODS)\ndef submit(): ...\n",
        "partial": "M = 'POST'\n@app.route('/submit', methods=['GET', M])\ndef submit(): ...\n",
        "empty_seq": "@app.route('/submit', methods=[])\ndef submit(): ...\n",
        "empty_str": "@app.route('/submit', methods=[''])\ndef submit(): ...\n",
        "dynamic_endpoint": "NAME = 'x'\n@app.route('/submit', methods=['POST'], endpoint=NAME)\ndef submit(): ...\n",
        "empty_endpoint": "@app.route('/submit', methods=['POST'], endpoint='')\ndef submit(): ...\n",
        "keyword_unpacking": "@app.route('/submit', **OPTS)\ndef submit(): ...\n",
    }
    for name, body in cases.items():
        repo = _repo(tmp_path / name)
        _w(repo / "execution/server.py", body)
        _git(repo, "add", ".")
        _refresh(repo)
        inv = build_inventory(repo)
        assert inv.status == "HOLD", name
        assert any(
            "methods" in v or "endpoint" in v or "non-literal" in v for v in inv.violations
        ), name
        routes = [e for e in inv.entries if e.kind == "flask_route"]
        assert routes == [], name


def test_route_literal_identities_pass(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(
        repo / "execution/server.py",
        "@app.route('/submit', methods=['POST'], endpoint='custom')\n"
        "def submit(): ...\n"
        "@app.route('/multi', methods=['POST', 'GET'])\n"
        "def multi(): ...\n"
        "@app.get('/health', endpoint='healthy')\n"
        "def health(): ...\n",
    )
    _git(repo, "add", ".")
    _refresh(repo)
    inv = build_inventory(repo)
    assert inv.status == "PASS"
    by_ident = {e.identifier: e for e in inv.entries if e.kind == "flask_route"}
    assert by_ident["POST /submit custom"].methods == ("POST",)
    assert by_ident["POST /submit custom"].endpoint == "custom"
    assert by_ident["POST /submit custom"].targets == ("/submit",)
    assert by_ident["GET,POST /multi multi"].methods == ("GET", "POST")
    assert by_ident["GET,POST /multi multi"].endpoint == "multi"
    assert by_ident["GET /health healthy"].methods == ("GET",)
    assert by_ident["GET /health healthy"].endpoint == "healthy"


def test_task_wrapper_service_reconstruction_registry(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(
        repo / "cron/task_manifest.json",
        _task_manifest(("daily", "daily.task.xml", "daily.bat")),
    )
    _w(repo / "cron/daily.task.xml", "<Task><Command>daily.bat</Command></Task>\n")
    _w(repo / "cron/daily.bat", "python execution/entry.py\n")
    _w(repo / "src/runtime/service_registry.py", "x = ManagedService(name='svc-a')\n")
    _w(repo / "reconstruction_manifest.json", '{"a": "execution/entry.py"}\n')
    _git(repo, "add", ".")
    _refresh(repo)
    r = build_inventory(repo)
    assert r.status == "PASS"
    kinds = {e.kind for e in r.entries}
    assert {"scheduled_task", "wrapper", "service", "reconstruction", "registry"} <= kinds
    assert next(e for e in r.entries if e.kind == "scheduled_task").disposition == "scheduled"


def test_managed_service_spacing_is_inventoried(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(
        repo / "src/runtime/service_registry.py",
        "x = ManagedService (name='svc-a')\ny = ManagedService(\n    name='svc-b',\n)\n",
    )
    _git(repo, "add", ".")
    _refresh(repo)
    inventory = build_inventory(repo)
    assert inventory.status == "PASS"
    services = {e.identifier for e in inventory.entries if e.kind == "service"}
    assert services == {"svc-a", "svc-b"}
    assert inventory.coverage["omissions"] == 0


def test_missing_malformed_stale_graph(tmp_path: Path) -> None:
    m = _repo(tmp_path / "a", graph=False)
    with pytest.raises(LifecycleError, match="graph is missing"):
        build_inventory(m)
    b = _repo(tmp_path / "b", graph=False)
    _w(b / ".tmp/quality/reachability-check.json", "[bad]")
    with pytest.raises(LifecycleError, match="invalid typed"):
        build_inventory(b)
    c = _repo(tmp_path / "c")
    _w(c / "execution/entry.py", "if __name__ == '__main__':\n    print('stale')\n")
    with pytest.raises(LifecycleError, match="stale"):
        build_inventory(c)


def test_orphan_missing_duplicate_task(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(repo / "cron/orphan.task.xml", "<Task/>\n")
    _git(repo, "add", ".")
    _refresh(repo)
    assert "absent from manifest" in " ".join(build_inventory(repo).violations)
    (repo / "cron/orphan.task.xml").unlink()
    _w(
        repo / "cron/task_manifest.json",
        _task_manifest(("t", "gone.task.xml", "daily.bat")),
    )
    _w(repo / "cron/daily.bat", "python execution/entry.py\n")
    _git(repo, "add", ".")
    _refresh(repo)
    assert "manifest XML missing" in " ".join(build_inventory(repo).violations)
    _w(
        repo / "cron/task_manifest.json",
        _task_manifest(
            ("t", "gone.task.xml", "gone.bat"),
            ("t", "gone.task.xml", "gone.bat"),
        ),
    )
    _git(repo, "add", ".")
    _refresh(repo)
    assert "duplicate" in " ".join(build_inventory(repo).violations)


def test_deterministic_and_drift(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    a = build_inventory(repo).model_dump(mode="json", exclude={"worktree_dirty"})
    b = build_inventory(repo).model_dump(mode="json", exclude={"worktree_dirty"})
    assert a == b
    cur = build_inventory(repo)
    assert validate_inventory(repo, cur) == ()
    bad = cur.model_copy(deep=True)
    object.__setattr__(bad.entries[0], "rationale", "x")
    assert "persisted lifecycle semantics differ" in " ".join(validate_inventory(repo, bad))


def test_disposition_proofs_and_expiry() -> None:
    with pytest.raises(LifecycleError, match="canonical/runbook"):
        lifecycle_evidence_fields(path="a.py", text="x\n", disposition="manual-supported")
    with pytest.raises(LifecycleError, match="typed edge"):
        lifecycle_evidence_fields(path="a.py", text="x\n", disposition="internal-delegate")
    with pytest.raises(LifecycleError, match="sealed"):
        lifecycle_evidence_fields(path="a.py", text="x\n", disposition="one-shot-completed")
    with pytest.raises(LifecycleError, match="consumer"):
        lifecycle_evidence_fields(path="a.py", text="x\n", disposition="compatibility-tombstone")
    with pytest.raises(LifecycleError, match="expired"):
        lifecycle_evidence_fields(
            path="a.py",
            text="# lifecycle: consumer=c\n# lifecycle: expiry=2020-01-01\n",
            disposition="compatibility-tombstone",
        )
    with pytest.raises(ValueError, match="expired"):
        LifecycleEntry(
            path="a",
            line=1,
            kind="python_module",
            identifier="a",
            evidence="e",
            fingerprint="f",
            disposition="dormant-until",
            classification_basis="b",
            rationale="r",
            dormant_owner="linear:BHA-142",
            dormant_activation="a",
            dormant_review="2020-01-01",
            dormant_policy_evidence="policy:x#hash",
        )
    with pytest.raises(ValueError, match="named consumer"):
        LifecycleEntry(
            path="a",
            line=1,
            kind="python_module",
            identifier="a",
            evidence="e",
            fingerprint="f",
            disposition="compatibility-tombstone",
            classification_basis="b",
            rationale="r",
            tombstone_consumer="none",
            tombstone_expiry="2099-01-01",
        )
    with pytest.raises(ValueError, match="five deletion-proof"):
        LifecycleEntry(
            path="a",
            line=1,
            kind="python_module",
            identifier="a",
            evidence="e",
            fingerprint="f",
            disposition="retire",
            classification_basis="b",
            rationale="r",
            retirement_evidence=("no-incoming-runtime-edges:x",),
        )
    with pytest.raises(LifecycleError, match="duplicate"):
        lifecycle_evidence_fields(
            path="a.py",
            text="# lifecycle: owner=linear:BHA-142\n# lifecycle: owner=linear:BHA-142\n",
            disposition="dormant-until",
        )
    with pytest.raises(LifecycleError, match="not authoritative"):
        lifecycle_evidence_fields(
            path="a.py",
            text="# lifecycle: owner=zzz\n# lifecycle: activation=a\n# lifecycle: review=2099-01-01\n",
            disposition="dormant-until",
        )
    e1 = lifecycle_evidence_fields(
        path="a.py",
        text="# lifecycle: consumer=c\n# lifecycle: expiry=2099-01-01\n",
        disposition="compatibility-tombstone",
    )
    assert e1["tombstone_consumer"] == "c"
    assert e1["dormant_policy_evidence"] is None


def test_duplicate_identities_and_uncatalogued(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(repo / "src/extra_registry.py", "THING_REGISTRY = {}\n")
    _git(repo, "add", ".")
    _refresh(repo)
    assert "uncatalogued" in " ".join(build_inventory(repo).violations)


def test_test_imports_and_wrapper_comments(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(repo / "tests/test_only.py", "from execution import entry\n")
    _w(
        repo / "cron/task_manifest.json",
        _task_manifest(("d", "d.task.xml", "d.bat")),
    )
    _w(repo / "cron/d.task.xml", "<Task/>\n")
    _w(repo / "cron/d.bat", "REM python execution/helper.py\npython execution/entry.py\n")
    _w(repo / "execution/helper.py", "VALUE = 1\n")
    _git(repo, "add", ".")
    _refresh(repo)
    r = build_inventory(repo)
    by = {e.path: e for e in r.entries if e.kind == "python_module"}
    assert by["execution/entry.py"].disposition == "scheduled"
    assert by["execution/helper.py"].disposition == "dormant-until"


def test_multi_target_process_edges(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    line = "subprocess.run(command)"
    _w(
        repo / "execution/entry.py",
        "import subprocess\n" + line + "\nif __name__ == '__main__':\n    pass\n",
    )
    _w(repo / "src/child_a.py", "if __name__ == '__main__':\n    pass\n")
    _w(repo / "src/child_b.py", "if __name__ == '__main__':\n    pass\n")
    g = build_graph(repo)
    unk = [e for e in g.unknown_edges if e.source == "execution/entry.py"]
    assert unk, "need unknown edge fixture"
    assert unk[0].line is not None
    ln: int = unk[0].line
    cur = (repo / "execution/entry.py").read_text()
    rows = cur.splitlines()
    while len(rows) < ln:
        rows.append("x = 1")
    rows[ln - 1] = line
    _w(repo / "execution/entry.py", "\n".join(rows) + "\n")
    g2 = build_graph(repo)
    unk2 = [e for e in g2.unknown_edges if e.source == "execution/entry.py"]
    assert unk2 and unk2[0].line is not None
    _line: object = unk2[0].line
    assert isinstance(_line, int)
    ln2: int = _line
    evline = (repo / "execution/entry.py").read_text().splitlines()[ln2 - 1].strip()
    prov = dict(g2.parser)
    _w(
        repo / "docs/quality/reachability-process-dispositions.json",
        json.dumps(
            {
                "schema_version": "reachability-process-dispositions/v1",
                "graph_provenance": {
                    "path": ".tmp/quality/reachability-check.json",
                    "schema_version": "operational-reachability-raw/v1",
                    "parser": {
                        "name": prov["name"],
                        "version": prov["version"],
                        "python": prov["python"],
                        "source_sha256": prov["source_sha256"],
                    },
                    "source_manifest_sha256": g2.source_manifest_sha256,
                    "scanner_sha256": g2.scanner_sha256,
                },
                "edges": [
                    {
                        "path": "execution/entry.py",
                        "line": ln2,
                        "fingerprint": _fp("execution/entry.py", ln2, evline),
                        "disposition": "internal_python_target",
                        "targets": ["src/child_a.py", "src/child_b.py"],
                        "evidence": "e",
                    }
                ],
            }
        ),
    )
    _git(repo, "add", ".")
    _refresh(repo)
    r = build_inventory(repo)
    by = {e.path: e for e in r.entries if e.kind == "python_module"}
    assert by["src/child_a.py"].disposition == "internal-delegate"
    assert by["src/child_b.py"].disposition == "internal-delegate"


def test_dormant_policy_invalid(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs/quality/lifecycle-dormant-policy.json").unlink()
    _git(repo, "add", "-u")
    _refresh(repo)
    with pytest.raises(LifecycleError, match="policy is missing"):
        build_inventory(repo)


def test_cli_exits(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path)
    out = repo / ".tmp/o.json"
    code = cli_main(["--repo-root", str(repo), "--output", str(out)])
    assert code == 0
    assert out.is_file()
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "PASS"
    persisted = json.loads(out.read_text(encoding="utf-8"))
    assert persisted["status"] == "PASS"
    assert persisted["coverage"]["omissions"] == 0
    assert persisted["coverage"]["extras"] == 0
    code = cli_main(["--repo-root", str(repo), "--validate", str(out)])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"
    valid = out.read_text(encoding="utf-8")
    duplicate = valid.replace('"status": "PASS"', '"status": "HOLD", "status": "PASS"', 1)
    assert duplicate != valid
    out.write_text(duplicate, encoding="utf-8")
    assert cli_main(["--repo-root", str(repo), "--validate", str(out)]) == 1
    assert "duplicate object key" in capsys.readouterr().err
    bad = json.loads(out.read_text())
    bad["status"] = "HOLD"
    _w(out, json.dumps(bad))
    code = cli_main(["--repo-root", str(repo), "--validate", str(out)])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "HOLD"
    code = cli_main(["--repo-root", str(repo / "nope")])
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"] in {"LifecycleError", "OSError", "ValueError"}
    repo2 = _repo(tmp_path / "h")
    _w(repo2 / "cron/orphan.task.xml", "<Task/>\n")
    _git(repo2, "add", ".")
    _refresh(repo2)
    hold_out = tmp_path / "h.json"
    code = cli_main(["--repo-root", str(repo2), "--output", str(hold_out)])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "HOLD"
    assert json.loads(hold_out.read_text(encoding="utf-8"))["status"] == "HOLD"


def test_policy_owner_contract() -> None:
    p = Path(__file__).resolve().parents[1] / "docs/quality/lifecycle-dormant-policy.json"
    if p.is_file():
        assert json.loads(p.read_text())["owner_evidence"] == "linear:BHA-142"


def test_dirty_worktree_hold(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    pol = repo / "docs/quality/lifecycle-dormant-policy.json"
    payload = _load_object(pol)
    payload["activation_evidence"] = "activate-dirty"
    pol.write_text(json.dumps(payload), encoding="utf-8")
    r = build_inventory(repo)
    assert r.worktree_dirty is True
    assert r.status == "HOLD"
    assert any("dirty" in v for v in r.violations)


def test_service_comment_no_phantom_and_nonliteral_hold(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(repo / "src/runtime/service_registry.py", "# ManagedService(name='ghost')\nPUBLIC = 1\n")
    _git(repo, "add", ".")
    _refresh(repo)
    r = build_inventory(repo)
    assert r.status == "PASS"
    assert not [e for e in r.entries if e.identifier == "ghost"]
    _w(repo / "src/runtime/service_registry.py", "x = ManagedService(name=NAME)\n")
    _git(repo, "add", ".")
    _refresh(repo)
    r2 = build_inventory(repo)
    assert r2.status == "HOLD"
    assert any("non-literal" in v or "could not be parsed" in v for v in r2.violations)


def test_workflow_wrapper_dormant_not_scheduled(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(repo / ".github/workflows/ci.yml", "jobs:\n  x:\n    runs-on: ubuntu\n")
    _git(repo, "add", ".")
    _refresh(repo)
    r = build_inventory(repo)
    assert r.status == "PASS"
    w = next(e for e in r.entries if e.path == ".github/workflows/ci.yml")
    assert w.disposition == "dormant-until"
    assert w.dormant_policy_evidence is not None


def test_sealed_non_object_hold(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(repo / "execution/backfill_job.py", "if __name__ == '__main__':\n    pass\n")
    _w(repo / "docs/receipt.json", "[1, 2]\n")
    _git(repo, "add", ".")
    _refresh(repo)
    with pytest.raises(LifecycleError, match="not an object"):
        lifecycle_evidence_fields(
            path="execution/backfill_job.py",
            text="# lifecycle: completion=sealed:docs/receipt.json\n",
            disposition="one-shot-completed",
            root=repo,
        )


def test_disposition_integration(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(
        repo / "directives/directive_manifest.json",
        json.dumps(
            {
                "directives": {
                    "manual_tool.md": {"class": "canonical"},
                    "manual_runbook.md": {"class": "runbook"},
                }
            }
        ),
    )
    _w(repo / "directives/manual_tool.md", "# Manual tool authority\n")
    _w(repo / "directives/manual_runbook.md", "# Manual runbook\n")
    _w(
        repo / "execution/manual_tool.py",
        "# lifecycle: owner=canonical:directives/manual_tool.md\n"
        "# lifecycle: invocation=cli:manual_tool\n"
        "if __name__ == '__main__':\n    pass\n",
    )
    _w(
        repo / "execution/manual_runbook_tool.py",
        "# lifecycle: owner=runbook:directives/manual_runbook.md\n"
        "# lifecycle: invocation=cli:manual_runbook_tool\n"
        "if __name__ == '__main__':\n    pass\n",
    )
    _w(repo / "docs/seed-receipt.json", json.dumps({"status": "PASS"}))
    _w(
        repo / "execution/backfill_job.py",
        "# lifecycle: completion=sealed:docs/seed-receipt.json\n"
        "if __name__ == '__main__':\n    pass\n",
    )
    _w(
        repo / "execution/legacy.py",
        "# lifecycle: tombstone\n"
        "# lifecycle: consumer=team-x\n"
        "# lifecycle: expiry=2099-01-01\n"
        "if __name__ == '__main__':\n    pass\n",
    )
    _w(
        repo / "execution/quiet.py",
        "# lifecycle: owner=linear:BHA-142\n"
        "# lifecycle: activation=on-demand\n"
        "# lifecycle: review=2099-01-01\n"
        "if __name__ == '__main__':\n    pass\n",
    )
    _git(repo, "add", ".")
    _refresh(repo)
    r = build_inventory(repo)
    assert r.status == "PASS"
    by = {e.path: e for e in r.entries if e.kind == "python_module"}
    assert by["execution/manual_tool.py"].disposition == "manual-supported"
    assert by["execution/manual_tool.py"].owner_evidence == ("canonical:directives/manual_tool.md")
    assert by["execution/manual_runbook_tool.py"].owner_evidence == (
        "runbook:directives/manual_runbook.md"
    )
    assert by["execution/backfill_job.py"].disposition == "one-shot-completed"
    assert by["execution/legacy.py"].disposition == "compatibility-tombstone"
    assert by["execution/quiet.py"].disposition == "dormant-until"
    assert by["execution/quiet.py"].dormant_policy_evidence is not None
    assert "source:" not in (by["execution/quiet.py"].dormant_policy_evidence or "")


def test_directive_owner_wrong_class_and_outside_directives(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(repo / "directives/history.md", "# Historical only\n")
    _w(
        repo / "directives/directive_manifest.json",
        json.dumps({"directives": {"history.md": {"class": "history"}}}),
    )
    with pytest.raises(LifecycleError, match="class mismatch"):
        lifecycle_evidence_fields(
            path="execution/manual.py",
            text=(
                "# lifecycle: owner=canonical:directives/history.md\n"
                "# lifecycle: invocation=cli:manual\n"
            ),
            disposition="manual-supported",
            root=repo,
        )
    with pytest.raises(LifecycleError, match="must reference directives"):
        lifecycle_evidence_fields(
            path="execution/manual.py",
            text=(
                "# lifecycle: owner=canonical:execution/entry.py\n"
                "# lifecycle: invocation=cli:manual\n"
            ),
            disposition="manual-supported",
            root=repo,
        )


def test_explicit_dormant_requires_policy(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(LifecycleError, match="policy coverage"):
        lifecycle_evidence_fields(
            path="other/out.py",
            text="# lifecycle: owner=linear:BHA-142\n# lifecycle: activation=a\n# lifecycle: review=2099-01-01\n",
            disposition="dormant-until",
            root=repo,
            dormant_policy=None,
            dormant_policy_evidence=None,
        )


def test_explicit_dormant_whitespace_requires_direct_evidence() -> None:
    with pytest.raises(LifecycleError, match="owner, activation, and review"):
        lifecycle_evidence_fields(
            path="execution/quiet.py",
            text="# lifecycle:    dormant\n",
            disposition="dormant-until",
        )


def test_duplicate_candidate_identity_hold(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(
        repo / "src/runtime/service_registry.py",
        "x = ManagedService(name='svc-a')\ny = ManagedService(name='svc-a')\n",
    )
    _git(repo, "add", ".")
    _refresh(repo)
    inv = build_inventory(repo)
    assert inv.status == "HOLD"
    assert inv.coverage["duplicates"] > 0


def test_duplicate_json_keys_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manifest = repo / "cron/task_manifest.json"
    manifest.write_text('{"version": 2, "version": 1, "tasks": []}\n', encoding="utf-8")
    _git(repo, "add", ".")
    _refresh(repo)
    with pytest.raises(LifecycleError, match="invalid scheduled-task manifest"):
        build_inventory(repo)

    sealed_repo = _repo(tmp_path / "sealed")
    _w(sealed_repo / "docs/receipt.json", '{"status": "HOLD", "status": "PASS"}\n')
    with pytest.raises(LifecycleError, match=r"typed JSON|duplicate"):
        lifecycle_evidence_fields(
            path="execution/backfill_job.py",
            text="# lifecycle: completion=sealed:docs/receipt.json\n",
            disposition="one-shot-completed",
            root=sealed_repo,
        )

    graph_repo = _repo(tmp_path / "graph")
    graph_path = graph_repo / ".tmp/quality/reachability-check.json"
    graph_raw = graph_path.read_text(encoding="utf-8")
    graph_path.write_text('{"hold": true, "hold": false, "payload": ' + graph_raw + "}")
    with pytest.raises(LifecycleError, match="invalid typed reachability graph"):
        build_inventory(graph_repo)


def test_manifest_version_and_extras_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manifest = repo / "cron/task_manifest.json"
    manifest.write_text(_task_manifest(version=2) + "\n", encoding="utf-8")
    _git(repo, "add", ".")
    _refresh(repo)
    with pytest.raises(LifecycleError, match="invalid scheduled-task manifest"):
        build_inventory(repo)
    manifest.write_text(
        json.dumps({"version": 1, "namespace": "\\earnings-summary", "tasks": [{}]}) + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _refresh(repo)
    with pytest.raises(LifecycleError, match="invalid scheduled-task manifest"):
        build_inventory(repo)


def test_sealed_receipt_participates_in_inventory_hash(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(repo / "docs/seed-receipt.json", json.dumps({"status": "PASS"}))
    _w(
        repo / "execution/backfill_job.py",
        "# lifecycle: completion=sealed:docs/seed-receipt.json\n"
        "if __name__ == '__main__':\n    pass\n",
    )
    _git(repo, "add", ".")
    _refresh(repo)
    persisted = build_inventory(repo)
    assert persisted.status == "PASS"

    _w(repo / "docs/seed-receipt.json", json.dumps({"status": "PASS", "note": "v2"}))
    _git(repo, "add", ".")
    _refresh(repo)
    drift = validate_inventory(repo, persisted)
    assert "tracked content fingerprint is stale" in drift


def test_sealed_receipt_snapshot_change_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    receipt = repo / ".tmp/seed-receipt.json"
    _w(receipt, json.dumps({"status": "PASS"}))
    _w(
        repo / "execution/backfill_job.py",
        "# lifecycle: completion=sealed:.tmp/seed-receipt.json\n"
        "if __name__ == '__main__':\n    pass\n",
    )
    _git(repo, "add", ".")
    _refresh(repo)
    baseline = build_inventory(repo)
    assert baseline.status == "PASS"
    canonical_read = Path.read_bytes
    replaced = False

    def read_then_replace(path: Path) -> bytes:
        nonlocal replaced
        raw = canonical_read(path)
        if path.resolve() == receipt.resolve() and not replaced:
            replaced = True
            _w(receipt, json.dumps({"status": "HOLD", "violations": ["revoked"]}))
        return raw

    monkeypatch.setattr(Path, "read_bytes", read_then_replace)

    raced = build_inventory(repo)

    assert raced.status == "HOLD"
    assert raced.tracked_tree_hash == baseline.tracked_tree_hash
    assert any("sealed completion receipt changed" in value for value in raced.violations)


def test_expired_dormant_policy_hold(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    pol = repo / "docs/quality/lifecycle-dormant-policy.json"
    payload = json.loads(pol.read_text(encoding="utf-8"))
    payload["review_on"] = "2020-01-01"
    pol.write_text(json.dumps(payload), encoding="utf-8")
    _git(repo, "add", ".")
    _refresh(repo)
    inv = build_inventory(repo)
    assert inv.status == "HOLD"
    assert any("policy is expired" in v for v in inv.violations)
    out = tmp_path / ".tmp/expired.json"
    code = cli_main(["--repo-root", str(repo), "--output", str(out)])
    assert code == 2
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "HOLD"


def test_broken_python_syntax_records_violation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    text = "VALUE = {\nif __name__ == '__main__':\n    pass\n"
    _w(repo / "execution/broken.py", text)
    violations: list[str] = []
    assert route_entries(repo, "execution/broken.py", text, violations) == []
    assert any("route syntax could not be parsed" in v for v in violations)


def test_uncatalogued_registry_inventoried_with_basis(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(repo / "src/extra_registry.py", "THING_REGISTRY = {}\n")
    _git(repo, "add", ".")
    _refresh(repo)
    inv = build_inventory(repo)
    assert inv.status == "HOLD"
    assert any("uncatalogued" in v for v in inv.violations)
    entry = next(
        e for e in inv.entries if e.path == "src/extra_registry.py" and e.kind == "registry"
    )
    assert entry.classification_basis == "uncatalogued_registry"
    assert entry.disposition in {"internal-delegate", "dormant-until"}
    assert inv.coverage["omissions"] == 0


def test_scheduled_module_flag_normalization(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(repo / "cron/job.py", "if __name__ == '__main__':\n    print('job')\n")
    _w(
        repo / "cron/task_manifest.json",
        _task_manifest(("d", "d.task.xml", "d.bat")),
    )
    _w(repo / "cron/d.task.xml", "<Task/>\n")
    _w(repo / "cron/d.bat", "python -m execution.entry\npython -m cron.job\n")
    _git(repo, "add", ".")
    _refresh(repo)
    inv = build_inventory(repo)
    assert inv.status == "PASS"
    by = {e.path: e for e in inv.entries if e.kind == "python_module"}
    assert by["execution/entry.py"].disposition == "scheduled"
    assert by["cron/job.py"].disposition == "scheduled"


def test_scheduled_targets_require_command_context(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(
        repo / "cron/task_manifest.json",
        _task_manifest(("daily", "daily.task.xml", "daily.bat")),
    )
    _w(repo / "cron/daily.task.xml", "<Task><Command>daily.bat</Command></Task>\n")
    _w(
        repo / "cron/daily.bat",
        "echo execution/helper.py\n"
        "set TARGET=execution/helper.py\n"
        "echo cron/hidden.bat\n"
        "python execution/direct.py --input execution/data.py\n"
        "python -c \"print('execution/literal.py')\"\n"
        "python execution/entry.py # execution/comment.py\n"
        'call "%PROJECT_ROOT%\\cron\\run_python.bat" "job" "lane" execution\\entry.py\n',
    )
    _w(repo / "cron/hidden.bat", "python execution/helper.py\n")
    _w(
        repo / "cron/run_python.bat",
        '"%PYTHON_EXE%" -u "%PROJECT_ROOT%\\execution\\sqlite_bootstrap.py" '
        '"%PROJECT_ROOT%\\cron\\job_runtime.py" -- %*\n',
    )
    _w(repo / "execution/sqlite_bootstrap.py", "if __name__ == '__main__':\n    pass\n")
    _w(repo / "cron/job_runtime.py", "if __name__ == '__main__':\n    pass\n")
    for name in ("direct", "data", "literal", "comment"):
        _w(repo / f"execution/{name}.py", "if __name__ == '__main__':\n    pass\n")
    _git(repo, "add", ".")
    _refresh(repo)
    inventory = build_inventory(repo)
    assert inventory.status == "PASS"
    modules = {e.path: e for e in inventory.entries if e.kind == "python_module"}
    assert modules["execution/entry.py"].disposition == "scheduled"
    assert modules["execution/sqlite_bootstrap.py"].disposition == "scheduled"
    assert modules["cron/job_runtime.py"].disposition == "scheduled"
    assert modules["execution/direct.py"].disposition == "scheduled"
    assert modules["execution/helper.py"].disposition == "dormant-until"
    assert modules["execution/data.py"].disposition == "dormant-until"
    assert modules["execution/literal.py"].disposition == "dormant-until"
    assert modules["execution/comment.py"].disposition == "dormant-until"
    wrappers = {e.path: e for e in inventory.entries if e.kind == "wrapper"}
    assert wrappers["cron/run_python.bat"].disposition == "scheduled"
    assert wrappers["cron/hidden.bat"].disposition == "dormant-until"


def test_sealed_last_status_with_issues_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(repo / "docs/receipt.json", json.dumps({"status": "sealed", "violations": ["v"]}))
    with pytest.raises(LifecycleError, match="unresolved issues"):
        lifecycle_evidence_fields(
            path="execution/backfill_job.py",
            text="# lifecycle: completion=sealed:docs/receipt.json\n",
            disposition="one-shot-completed",
            root=repo,
        )
    _w(repo / "docs/bad.json", json.dumps({"status": "PASS", "parse_errors": "oops"}))
    with pytest.raises(LifecycleError, match="unresolved issues"):
        lifecycle_evidence_fields(
            path="execution/backfill_job.py",
            text="# lifecycle: completion=sealed:docs/bad.json\n",
            disposition="one-shot-completed",
            root=repo,
        )
    _w(
        repo / "docs/ok.json",
        json.dumps({"status": "sealed", "violations": [], "parse_errors": []}),
    )
    fields = lifecycle_evidence_fields(
        path="execution/backfill_job.py",
        text="# lifecycle: completion=sealed:docs/ok.json\n",
        disposition="one-shot-completed",
        root=repo,
    )
    assert fields["sealed_completion_evidence"] == "sealed:docs/ok.json"


def test_registry_parse_error_bounded_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    original = lifecycle_inventory_module.registry_symbols

    def fail_one_registry(text: str, path: str) -> tuple[str, ...]:
        if path == "src/ask/engine.py":
            raise LifecycleError("registry authority could not be parsed: fixture")
        return original(text, path)

    monkeypatch.setattr(lifecycle_inventory_module, "registry_symbols", fail_one_registry)
    inv = build_inventory(repo)
    assert inv.status == "HOLD"
    assert any("registry authority could not be parsed" in v for v in inv.violations)


def test_dormant_registry_policy_scope_hold(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    pol = repo / "docs/quality/lifecycle-dormant-policy.json"
    payload = _load_object(pol)
    payload["path_prefixes"] = ["execution/"]
    _w(pol, json.dumps(payload))
    _git(repo, "add", ".")
    _refresh(repo)
    inv = build_inventory(repo)
    assert inv.status == "HOLD"
    assert any("does not cover registry" in v for v in inv.violations)
    assert inv.coverage["omissions"] == 0
    assert inv.coverage["extras"] == 0
    dormant = [e for e in inv.entries if e.kind == "registry" and e.disposition == "dormant-until"]
    assert dormant
    assert any(e.path.startswith("src/") for e in dormant)


def test_uncatalogued_registry_policy_scope_hold(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _w(repo / "src/extra_registry.py", "THING_REGISTRY = {}\n")
    pol = repo / "docs/quality/lifecycle-dormant-policy.json"
    payload = _load_object(pol)
    payload["path_prefixes"] = ["execution/"]
    _w(pol, json.dumps(payload))
    _git(repo, "add", ".")
    _refresh(repo)
    inv = build_inventory(repo)
    assert inv.status == "HOLD"
    assert any("does not cover registry" in v and "extra_registry" in v for v in inv.violations)
    assert inv.coverage["omissions"] == 0
    entry = next(
        e for e in inv.entries if e.path == "src/extra_registry.py" and e.kind == "registry"
    )
    assert entry.disposition == "dormant-until"
    assert entry.classification_basis == "uncatalogued_registry"


def test_sealed_completion_truth_table(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    def check(payload: dict[str, object], *, ok: bool) -> None:
        _w(repo / "docs/receipt.json", json.dumps(payload))
        text = "# lifecycle: completion=sealed:docs/receipt.json\n"
        if ok:
            fields = lifecycle_evidence_fields(
                path="execution/backfill_job.py",
                text=text,
                disposition="one-shot-completed",
                root=repo,
            )
            assert fields["sealed_completion_evidence"] == "sealed:docs/receipt.json"
        else:
            with pytest.raises(LifecycleError, match="not terminal"):
                lifecycle_evidence_fields(
                    path="execution/backfill_job.py",
                    text=text,
                    disposition="one-shot-completed",
                    root=repo,
                )

    check({"status": "PASS"}, ok=True)
    check({"status": "complete"}, ok=True)
    check({"status": "completed", "sealed": False}, ok=True)
    check({"status": "sealed"}, ok=True)
    check({"sealed": True}, ok=True)
    check({"sealed": True, "violations": [], "parse_errors": []}, ok=True)
    check({}, ok=False)
    check({"sealed": False}, ok=False)
    check({"status": "HOLD", "sealed": True}, ok=False)
    check({"status": "failed", "sealed": True}, ok=False)
    check({"status": "error", "sealed": True}, ok=False)
    check({"status": "partial", "sealed": True}, ok=False)
    check({"status": "weird-status", "sealed": True}, ok=False)
    check({"status": "HOLD"}, ok=False)
    check({"status": []}, ok=False)
    check({"status": {}}, ok=False)
    check({"status": [], "sealed": True}, ok=False)
    check({"status": {}, "sealed": True}, ok=False)
    _w(repo / "docs/receipt.json", json.dumps({"status": "PASS", "violations": ["v"]}))
    with pytest.raises(LifecycleError, match="unresolved issues"):
        lifecycle_evidence_fields(
            path="execution/backfill_job.py",
            text="# lifecycle: completion=sealed:docs/receipt.json\n",
            disposition="one-shot-completed",
            root=repo,
        )


def test_sealed_non_string_status_cli_hold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    _w(repo / "docs/receipt.json", json.dumps({"status": []}))
    _w(
        repo / "execution/backfill_job.py",
        "# lifecycle: completion=sealed:docs/receipt.json\nif __name__ == '__main__':\n    pass\n",
    )
    _git(repo, "add", ".")
    _refresh(repo)
    code = cli_main(["--repo-root", str(repo)])
    assert code == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out + captured.err
    result = json.loads(captured.out)
    assert result["status"] == "HOLD"
    assert any("not terminal" in value for value in result["violations"])


def test_validate_inventory_rejects_forged_revision_and_dirty(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    current = build_inventory(repo)
    assert current.status == "PASS"
    assert validate_inventory(repo, current) == ()
    forged = current.model_copy(update={"revision": "not-a-commit", "worktree_dirty": True})

    drift = validate_inventory(repo, forged)

    assert any("revision identity mismatch" in value for value in drift)
    assert any("worktree identity is not clean" in value for value in drift)


def test_inventory_holds_when_revision_changes_during_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    canonical_loader = lifecycle_inventory_module.load_task_manifest

    def load_after_commit(root: Path) -> TaskManifest:
        manifest = canonical_loader(root)
        _w(repo / "execution/late.py", "if __name__ == '__main__':\n    pass\n")
        _git(repo, "add", "execution/late.py")
        _git(
            repo,
            "-c",
            "user.name=F",
            "-c",
            "user.email=f@x.invalid",
            "commit",
            "-m",
            "inject collection race",
        )
        return manifest

    monkeypatch.setattr(lifecycle_inventory_module, "load_task_manifest", load_after_commit)

    inventory = build_inventory(repo)

    assert inventory.status == "HOLD"
    assert any("revision changed during build" in value for value in inventory.violations)
    assert any("graph subject does not match" in value for value in inventory.violations)


def test_inventory_hashes_the_validated_graph_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    graph_path = repo / ".tmp/quality/reachability-check.json"
    validated_hash = hashlib.sha256(graph_path.read_bytes()).hexdigest()
    canonical_builder = lifecycle_inventory_module.build_graph

    def replace_after_fresh(root: Path) -> ReachabilityGraph:
        graph = canonical_builder(root)
        _w(graph_path, "{}\n")
        return graph

    monkeypatch.setattr(lifecycle_inventory_module, "build_graph", replace_after_fresh)

    inventory = build_inventory(repo)

    assert inventory.status == "HOLD"
    assert inventory.reachability_graph_hash == validated_hash
    assert any("graph changed during" in value for value in inventory.violations)


def test_graph_parent_symlink_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo-graph-link")
    graph_path = repo / ".tmp/quality/reachability-check.json"
    raw = graph_path.read_bytes()
    outside = tmp_path / "outside-graph"
    outside.mkdir(exist_ok=True)
    (outside / "reachability-check.json").write_bytes(raw)
    shutil.rmtree(repo / ".tmp/quality")
    (repo / ".tmp/quality").symlink_to(outside, target_is_directory=True)

    with pytest.raises(LifecycleError, match=r"repository file|escapes root"):
        build_inventory(repo)


def test_canonical_manifest_symlink_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo-manifest-link")
    target = repo / "cron/task_manifest.json"
    raw = target.read_bytes()
    outside = tmp_path / "outside-manifest.json"
    outside.write_bytes(raw)
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(LifecycleError, match=r"repository file|escapes root"):
        build_inventory(repo)


def test_resolver_accepts_canonical_tracked_input(tmp_path: Path) -> None:
    from quality.lifecycle_models import resolve_repo_file

    repo = _repo(tmp_path / "repo-resolver-ok")
    resolved = resolve_repo_file(repo, "cron/task_manifest.json", label="scheduled-task manifest")

    assert resolved.is_file()
    assert not resolved.is_symlink()


def test_cli_refuses_primary_input_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path / "repo-guard-direct")
    protected = repo / "cron/task_manifest.json"
    before = protected.read_bytes()

    code = cli_main(["--repo-root", str(repo), "--output", str(protected)])

    assert code == 1
    assert protected.read_bytes() == before
    assert json.loads(capsys.readouterr().err) == {
        "error": "LifecycleError",
        "message": "protected lifecycle output",
    }


def test_cli_refuses_hardlink_alias_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path / "repo-guard-hardlink")
    protected = repo / "cron/task_manifest.json"
    before = protected.read_bytes()
    alias = tmp_path / "alias-outside.json"
    os.link(protected, alias)

    code = cli_main(["--repo-root", str(repo), "--output", str(alias)])

    assert code == 1
    assert protected.read_bytes() == before
    assert alias.read_bytes() == before
    assert json.loads(capsys.readouterr().err) == {
        "error": "LifecycleError",
        "message": "protected lifecycle output",
    }


def test_cli_late_hardlink_swap_is_replaced_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import quality.atomic_write as atomic_write

    repo = _repo(tmp_path / "repo-race-atomic")
    protected = repo / "cron/task_manifest.json"
    before = protected.read_bytes()
    output = repo / ".tmp/race-output.json"
    real_replace = os.replace

    def raced_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst) == output:
            Path(dst).unlink(missing_ok=True)
            os.link(protected, dst)
        real_replace(src, dst)

    monkeypatch.setattr(atomic_write.os, "replace", raced_replace)

    code = cli_main(["--repo-root", str(repo), "--output", str(output)])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"
    assert protected.read_bytes() == before
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "PASS"
    assert not output.is_symlink()
    assert not os.path.samefile(protected, output)
