from __future__ import annotations

import json
from pathlib import Path

import pytest

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
