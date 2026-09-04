from __future__ import annotations

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


def test_reviewed_dynamic_external_process_is_not_unknown(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "execution/main.py",
        "import subprocess\nsubprocess.run(argv)  # reachability: external-process\n",
    )
    graph = build_graph(tmp_path)
    assert any(edge.kind == "external_process" for edge in graph.edges)
    assert not graph.unknown_edges


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
