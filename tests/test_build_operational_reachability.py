from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import quality.reachability as reachability
from quality.reachability import build_graph, main


def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_collects_static_dynamic_and_operational_edges(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "execution/main.py",
        "from .worker import run\nfrom importlib import import_module\nimport_module('execution.worker')\ngetattr(mod, name)\n@app.route('/x')\ndef x(): return render_template('x.html')\n",
    )
    _write(tmp_path, "execution/worker.py", "def run(): pass\n")
    _write(tmp_path, "Makefile", "run:; python3 execution/main.py\n")
    _write(
        tmp_path,
        "cron/job.task.xml",
        "<Task><Actions><Exec><Command>python execution/main.py</Command></Exec></Actions></Task>",
    )
    _write(tmp_path, "directives/run.md", "Use execution/main.py")
    graph = build_graph(tmp_path)
    kinds = {edge.kind for edge in graph.edges}
    assert {
        "relative_import",
        "dynamic_import",
        "getattr",
        "route",
        "rendered_js",
        "wrapper",
        "schedule",
        "directive",
    } <= kinds
    assert graph.hold is False
    assert any(edge.unknown for edge in graph.edges)


def test_literal_getattr_and_non_python_process_are_not_unknown(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "execution/main.py",
        "import subprocess\ngetattr(mod, 'handler')\nsubprocess.run(['git', 'status'])\n",
    )
    graph = build_graph(tmp_path)
    assert any(edge.target == "<attribute:handler>" for edge in graph.edges)
    assert not graph.unknown_edges


def test_literal_targets_cannot_escape_repository(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    literal_target = getattr(reachability, "_literal_target")
    assert literal_target(tmp_path, "../outside.py", {}) is None
    assert literal_target(tmp_path, str(outside), {}) is None


def test_unrelated_run_and_call_methods_are_not_process_edges(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "execution/main.py",
        "app.run(options)\nprovider.call(payload)\nrun(job)\n",
    )
    graph = build_graph(tmp_path)
    assert not graph.unknown_edges
    assert not any(edge.kind in {"python_entrypoint", "external_process"} for edge in graph.edges)


def test_aliased_subprocess_and_runpy_calls_are_scanned(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "execution/main.py",
        "import subprocess as sp\nfrom runpy import run_path as execute\n"
        "sp.run(command)\nexecute(target)\n",
    )
    graph = build_graph(tmp_path)
    assert len(graph.unknown_edges) == 2


def test_reviewed_dynamic_external_process_is_not_unknown(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "execution/main.py",
        "import subprocess\nsubprocess.run(argv)  # reachability: external-process\n",
    )
    graph = build_graph(tmp_path)
    assert any(edge.kind == "external_process" for edge in graph.edges)
    assert not graph.unknown_edges


def _fingerprint(path: str, line: int, source_line: str) -> str:
    return hashlib.sha256(f"{path}:{line}:{source_line.strip()}".encode()).hexdigest()


def test_valid_reviewed_disposition_resolves_unknown_edge(tmp_path: Path) -> None:
    source_line = "getattr(mod, name)"
    _write(tmp_path, "execution/main.py", source_line + "\n")
    _write(
        tmp_path,
        "docs/quality/reachability-getattr-dispositions.json",
        json.dumps(
            {
                "schema_version": "reachability-getattr-dispositions/v1",
                "edges": [
                    {
                        "path": "execution/main.py",
                        "line": 1,
                        "fingerprint": _fingerprint("execution/main.py", 1, source_line),
                        "disposition": "closed_literal_set",
                        "evidence": "caller registry fixes the names",
                    }
                ],
            }
        ),
    )
    graph = build_graph(tmp_path)
    reviewed = next(edge for edge in graph.edges if edge.kind == "getattr")
    assert reviewed.unknown is False
    assert reviewed.reviewed_disposition == "closed_literal_set"
    assert not graph.unknown_edges
    assert graph.hold is False
    assert graph.parser["dispositions_sha256"]


def test_stale_reviewed_disposition_holds_and_does_not_resolve(tmp_path: Path) -> None:
    _write(tmp_path, "execution/main.py", "getattr(mod, name)\n")
    _write(
        tmp_path,
        "docs/quality/reachability-getattr-dispositions.json",
        json.dumps(
            {
                "schema_version": "reachability-getattr-dispositions/v1",
                "edges": [
                    {
                        "path": "execution/main.py",
                        "line": 1,
                        "fingerprint": "0" * 64,
                        "disposition": "closed_literal_set",
                        "evidence": "stale",
                    }
                ],
            }
        ),
    )
    graph = build_graph(tmp_path)
    assert len(graph.unknown_edges) == 1
    assert graph.hold is True
    assert any("stale reachability disposition" in item.message for item in graph.diagnostics)


def test_unresolved_reviewed_disposition_remains_unknown(tmp_path: Path) -> None:
    source_line = "getattr(mod, name)"
    _write(tmp_path, "execution/main.py", source_line + "\n")
    _write(
        tmp_path,
        "docs/quality/reachability-getattr-dispositions.json",
        json.dumps(
            {
                "schema_version": "reachability-getattr-dispositions/v1",
                "edges": [
                    {
                        "path": "execution/main.py",
                        "line": 1,
                        "fingerprint": _fingerprint("execution/main.py", 1, source_line),
                        "disposition": "unresolved",
                        "evidence": "target is still arbitrary",
                    }
                ],
            }
        ),
    )
    graph = build_graph(tmp_path)
    assert len(graph.unknown_edges) == 1
    assert graph.unknown_edges[0].reviewed_disposition is None


def test_internal_process_review_requires_existing_repo_file_and_keeps_target(
    tmp_path: Path,
) -> None:
    source_line = "subprocess.run(command)"
    _write(tmp_path, "execution/main.py", "import subprocess\n" + source_line + "\n")
    _write(tmp_path, "execution/child.py", "VALUE = 1\n")
    _write(
        tmp_path,
        "docs/quality/reachability-process-dispositions.json",
        json.dumps(
            {
                "schema_version": "reachability-process-dispositions/v1",
                "edges": [
                    {
                        "path": "execution/main.py",
                        "line": 2,
                        "fingerprint": _fingerprint("execution/main.py", 2, source_line),
                        "disposition": "internal_python_target",
                        "target": "execution/child.py",
                        "evidence": "fixed child entrypoint",
                    }
                ],
            }
        ),
    )

    graph = build_graph(tmp_path)
    reviewed = next(edge for edge in graph.edges if edge.kind == "unknown")
    assert reviewed.target == "execution/child.py"
    assert reviewed.reviewed_disposition == "internal_python_target"
    assert not graph.unknown_edges
    assert graph.hold is False


def test_internal_process_review_emits_one_edge_per_target(tmp_path: Path) -> None:
    source_line = "subprocess.run(command)"
    _write(tmp_path, "execution/main.py", "import subprocess\n" + source_line + "\n")
    _write(tmp_path, "execution/first.py", "VALUE = 1\n")
    _write(tmp_path, "execution/second.py", "VALUE = 2\n")
    _write(
        tmp_path,
        "docs/quality/reachability-process-dispositions.json",
        json.dumps(
            {
                "schema_version": "reachability-process-dispositions/v1",
                "edges": [
                    {
                        "path": "execution/main.py",
                        "line": 2,
                        "fingerprint": _fingerprint("execution/main.py", 2, source_line),
                        "disposition": "internal_python_target",
                        "targets": [
                            "execution/second.py",
                            "execution/first.py",
                            "execution/first.py",
                        ],
                        "evidence": "fixed child entrypoints",
                    }
                ],
            }
        ),
    )

    graph = build_graph(tmp_path)
    reviewed = [edge for edge in graph.edges if edge.kind == "unknown"]
    assert [(edge.target, edge.reviewed_disposition) for edge in reviewed] == [
        ("execution/first.py", "internal_python_target"),
        ("execution/second.py", "internal_python_target"),
    ]
    assert all(not edge.unknown for edge in reviewed)
    assert not any("," in edge.target for edge in reviewed)
    assert not graph.unknown_edges
    assert graph.hold is False


def test_internal_process_review_rejects_prose_target(tmp_path: Path) -> None:
    source_line = "subprocess.run(command)"
    _write(tmp_path, "execution/main.py", "import subprocess\n" + source_line + "\n")
    _write(
        tmp_path,
        "docs/quality/reachability-process-dispositions.json",
        json.dumps(
            {
                "schema_version": "reachability-process-dispositions/v1",
                "edges": [
                    {
                        "path": "execution/main.py",
                        "line": 2,
                        "fingerprint": _fingerprint("execution/main.py", 2, source_line),
                        "disposition": "internal_python_target",
                        "target": "IR discovery child commands",
                        "evidence": "not an exact target",
                    }
                ],
            }
        ),
    )

    graph = build_graph(tmp_path)
    assert graph.hold is True
    assert len(graph.unknown_edges) == 1
    assert any(
        "requires exact existing repository file targets" in item.message
        for item in graph.diagnostics
    )


def test_only_current_directives_create_authority_edges(tmp_path: Path) -> None:
    _write(tmp_path, "execution/live.py", "VALUE = 1\n")
    _write(tmp_path, "execution/old.py", "VALUE = 1\n")
    _write(tmp_path, "directives/live.md", "Use execution/live.py\n")
    _write(tmp_path, "directives/old.md", "Use execution/old.py\n")
    _write(
        tmp_path,
        "directives/directive_manifest.json",
        json.dumps(
            {
                "directives": {
                    "live.md": {"class": "runbook"},
                    "old.md": {"class": "history"},
                }
            }
        ),
    )
    graph = build_graph(tmp_path)
    directive_edges = {
        (edge.source, edge.target) for edge in graph.edges if edge.kind == "directive"
    }
    assert ("directives/live.md", "execution/live.py") in directive_edges
    assert ("directives/old.md", "execution/old.py") not in directive_edges


def test_malformed_sources_are_diagnostics_and_deterministic(tmp_path: Path) -> None:
    _write(tmp_path, "execution/bad.py", "def broken(:\n")
    _write(tmp_path, "cron/bad.xml", "<Task>")
    first = build_graph(tmp_path).model_dump()
    second = build_graph(tmp_path).model_dump()
    assert first == second
    assert {item["kind"] for item in first["unresolved"]} == set()
    assert first["stats"]["diagnostics"] == 2


def test_parser_provenance_is_stable_across_supported_python_runtimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "execution/main.py", "VALUE = 1\n")

    monkeypatch.setattr(reachability.sys, "version", "3.11.9 (main, clean clone)")
    first = build_graph(tmp_path)
    monkeypatch.setattr(reachability.sys, "version", "3.14.7 (main, clean clone)")
    second = build_graph(tmp_path)

    assert first.parser == second.parser
    assert first.parser["python"] == ">=3.11"


def test_import_index_distinguishes_project_external_and_unresolved(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/feature.py",
        "import json\nimport execution.missing_internal\nfrom execution.worker import run\n",
    )
    _write(tmp_path, "execution/worker.py", "def run(): pass\n")
    graph = build_graph(tmp_path)
    imports = {(edge.target, edge.kind) for edge in graph.edges if edge.source == "src/feature.py"}
    assert ("<external:json>", "import") in imports
    assert any(item.message.endswith("execution.missing_internal") for item in graph.unresolved)
    assert ("execution/worker.py", "import") in imports
    assert not any(edge.kind == "reexport" for edge in graph.edges)


def test_package_root_import_resolves_to_init(tmp_path: Path) -> None:
    _write(tmp_path, "execution/__init__.py", "VALUE = 1\n")
    _write(tmp_path, "src/feature.py", "import execution\n")
    graph = build_graph(tmp_path)
    assert any(
        edge.source == "src/feature.py" and edge.target == "execution/__init__.py"
        for edge in graph.edges
    )
    assert not graph.unresolved


def test_namespace_package_root_import_resolves(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/tool.py", "VALUE = 1\n")
    _write(tmp_path, "tests/test_tool.py", "import scripts\n")
    graph = build_graph(tmp_path)
    assert any(
        edge.source == "tests/test_tool.py" and edge.target == "scripts/" for edge in graph.edges
    )
    assert any(node.id == "scripts/" and node.kind == "package" for node in graph.nodes)
    assert not graph.unresolved


def test_from_package_module_and_operational_missing_hold(tmp_path: Path) -> None:
    _write(tmp_path, "src/pkg/__init__.py", "from . import module\n")
    _write(tmp_path, "src/pkg/module.py", "VALUE = 1\n")
    _write(tmp_path, "execution/run.py", "from src.pkg import module\n")
    _write(
        tmp_path,
        "cron/job.task.xml",
        "<Task><Actions><Exec><Command>python execution/missing.py</Command></Exec></Actions></Task>",
    )
    graph = build_graph(tmp_path, {"cron/job.task.xml"})
    assert any(edge.target == "src/pkg/module.py" for edge in graph.edges)
    assert graph.hold is True
    assert any(
        d.kind == "unresolved" and "execution/missing.py" in d.message for d in graph.diagnostics
    )


def test_utf16_windows_schedule_is_parsed(tmp_path: Path) -> None:
    _write(tmp_path, "execution/main.py", "VALUE = 1\n")
    path = tmp_path / "cron/job.task.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        "<Task><Actions><Exec><Command>python</Command><Arguments>execution/main.py</Arguments>"
        "</Exec></Actions></Task>".encode("utf-16")
    )
    graph = build_graph(tmp_path)
    assert any(
        edge.kind == "schedule" and edge.target == "execution/main.py" for edge in graph.edges
    )
    assert not any(d.path == "cron/job.task.xml" for d in graph.diagnostics)


def test_parse_diagnostics_are_preserved(tmp_path: Path) -> None:
    _write(tmp_path, "execution/bad.py", "def broken(:\n")
    graph = build_graph(tmp_path)
    assert any(d.kind == "parse_error" and d.path == "execution/bad.py" for d in graph.diagnostics)


def test_touched_unknowns_return_hold_exit_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "execution/main.py", "getattr(x, name)\n")
    assert main(["--repo-root", str(tmp_path), "--touched", "execution/main.py"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["hold"] is True
    assert payload["unknown_edges"][0]["confidence"] == "low"


def test_worktree_inside_dot_tmp_does_not_hide_every_file(tmp_path: Path) -> None:
    root = tmp_path / ".tmp" / "linked-worktree"
    _write(root, "execution/main.py", "VALUE = 1\n")
    graph = build_graph(root)
    assert any(node.id == "execution/main.py" for node in graph.nodes)


def test_large_cli_result_is_redirected_to_tmp(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "execution/main.py", "VALUE = 1\n")
    monkeypatch.setattr(reachability, "_MAX_STDOUT_BYTES", 1)
    assert main(["--repo-root", str(tmp_path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["output"] == ".tmp/quality/operational-reachability.json"
    assert (tmp_path / summary["output"]).is_file()
