from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

import quality.reachability as reachability
from quality.git_env import clean_local_git_env
from quality.reachability import (
    GraphEdge,
    GraphNode,
    ReachabilityGraph,
    build_graph,
    main,
    production_reachable_nodes,
    production_unknown_edges,
)


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
        env=clean_local_git_env(),
    )


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _track(root: Path, *paths: str) -> None:
    _git(root, "add", *paths)
    _git(
        root,
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "user.name=Test",
        "commit",
        "-qm",
        "fixture",
    )


def _repo(tmp_path: Path, *, authority: bool = True) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    _write(tmp_path, "execution/main.py", "VALUE = 1\n")
    _write(tmp_path, "src/__init__.py", "\n")
    if authority:
        _write(
            tmp_path,
            "directives/directive_manifest.json",
            json.dumps({"directives": {"live.md": {"class": "canonical"}}}) + "\n",
        )
        _write(tmp_path, "directives/live.md", "Use execution/main.py\n")
    _track(tmp_path, ".")
    return tmp_path


def test_collects_static_dynamic_and_operational_edges(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/worker.py", "def run() -> None:\n    return None\n")
    _write(
        root,
        "execution/main.py",
        "from .worker import run\nfrom importlib import import_module\nimport_module('execution.worker')\ngetattr(mod, name)\n@app.route('/x')\ndef x(): return render_template('x.html')\n",
    )
    _write(root, "Makefile", "run:; python3 execution/main.py\n")
    _write(
        root,
        "cron/job.task.xml",
        "<Task><Actions><Exec><Command>python execution/main.py</Command></Exec></Actions></Task>",
    )
    _track(root, "execution", "Makefile", "cron")
    graph = build_graph(root)
    kinds = {edge.kind for edge in graph.edges}
    assert {
        "relative_import",
        "dynamic_import",
        "getattr",
        "route",
        "rendered_js",
        "wrapper",
        "schedule",
    } <= kinds
    assert any(edge.unknown for edge in graph.edges)


def test_tracked_inputs_exclude_untracked_decoy(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/decoy.py", "getattr(mod, name)\n")
    graph = build_graph(root)
    assert "execution/decoy.py" not in {node.id for node in graph.nodes}
    assert not any(edge.source == "execution/decoy.py" for edge in graph.edges)
    assert "execution/decoy.py" not in {entry.path for entry in graph.attempted_input_manifest}


def test_runtime_data_exclusions_do_not_hide_same_named_source_packages(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "transcripts/runtime.json", "{}\n")
    _write(root, "src/transcripts/__init__.py", "\n")
    _write(root, "src/transcripts/longitudinal.py", "VALUE = 1\n")
    _track(root, "transcripts", "src/transcripts")
    graph = build_graph(root)
    assert "transcripts/runtime.json" not in graph.population
    assert "src/transcripts/longitudinal.py" in graph.population
    assert any(node.id == "src/transcripts/longitudinal.py" for node in graph.nodes)


def test_git_failure_is_nonzero_and_never_falls_back_to_rglob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    called = False

    def fail_rglob(_pattern: str) -> list[Path]:
        nonlocal called
        called = True
        raise AssertionError("rglob fallback must not run")

    monkeypatch.setattr(Path, "rglob", fail_rglob)

    def fail_git(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(reachability.subprocess, "run", fail_git)
    assert main(["--repo-root", str(root)]) != 0
    assert called is False


@pytest.mark.parametrize(
    "stdout", [b"../../escape\0", b"/absolute\0", b"bad\xff\0", b"missing-terminator"]
)
def test_malformed_git_path_output_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes
) -> None:
    root = _repo(tmp_path)

    class Result:
        returncode = 0
        stderr = b""

        def __init__(self) -> None:
            self.stdout = stdout

    def fake_run(*_args: Any, **_kwargs: Any) -> Result:
        return Result()

    monkeypatch.setattr(reachability.subprocess, "run", fake_run)
    with pytest.raises(reachability.ReachabilityCollectionError):
        build_graph(root)


def test_actual_head_is_supported() -> None:
    root = Path(__file__).resolve().parents[1]
    graph = build_graph(root)
    expected = _git(root, "rev-parse", "HEAD").stdout.strip()
    assert graph.subject_commit == expected
    assert graph.parser["source_sha256"]
    assert any(node.id == "execution/comments_server.py" for node in graph.nodes)
    assert graph.collection_status == "COMPLETE"
    assert graph.closure_status == "PASS"
    assert graph.stats["production_unknown"] == 0
    assert graph.stats["production_unresolved"] == 0
    residual_source_edges = [
        edge
        for edge in graph.unknown_edges
        if not edge.source.startswith(("tests/", "instruction_tests/"))
    ]
    assert len(graph.unknown_edges) == 88
    assert [(edge.source, edge.line, edge.kind, edge.target) for edge in residual_source_edges] == [
        ("src/search/fact_projection.py", 1945, "getattr", "<dynamic attribute>")
    ]


def test_discovery_and_manifest_hash_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _write(root, "src/b.py", "VALUE = 2\n")
    _write(root, "src/a.py", "VALUE = 1\n")
    _track(root, "src")
    first = build_graph(root).model_dump()
    original: Any = reachability.subprocess.run

    def reversed_git(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        result = cast(subprocess.CompletedProcess[bytes], original(args, **kwargs))
        if args[:4] == ["git", "-C", str(root), "ls-files"]:
            paths = [path for path in result.stdout.split(b"\0") if path]
            result.stdout = b"\0".join(reversed(paths)) + b"\0"
        return result

    monkeypatch.setattr(reachability.subprocess, "run", reversed_git)
    second = build_graph(root).model_dump()
    assert first == second


def test_manifest_hash_binds_tracked_content(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    before = build_graph(root)
    entry = next(
        item for item in before.attempted_input_manifest if item.path == "execution/main.py"
    )
    assert entry.content_sha256 and entry.error is None
    _write(root, "execution/main.py", "VALUE = 2\n")
    _track(root, "execution/main.py")
    after = build_graph(root)
    assert after.source_manifest_sha256 != before.source_manifest_sha256
    assert after.scanner_sha256 and len(after.scanner_sha256) == 64


def test_unreadable_input_is_diagnostic_or_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    path = _write(root, "execution/unreadable.py", "VALUE = 1\n")
    _track(root, "execution/unreadable.py")
    original = Path.read_bytes

    def unreadable(self: Path) -> bytes:
        if self == path:
            raise OSError("permission denied")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    graph = build_graph(root)
    assert any(item.path == "execution/unreadable.py" for item in graph.diagnostics)
    entry = next(
        item for item in graph.attempted_input_manifest if item.path == "execution/unreadable.py"
    )
    assert entry.content_sha256 is None and entry.error
    assert graph.collection_status == "INCOMPLETE"


def test_invalid_encoding_python_and_xml_are_diagnostics(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "execution/bad.py").write_bytes(b"\xff\xfe\xff\xff")
    (root / "cron/bad.task.xml").parent.mkdir(parents=True)
    (root / "cron/bad.task.xml").write_bytes(b"\xff\xfe\x00\x00")
    _track(root, "execution/bad.py", "cron/bad.task.xml")
    graph = build_graph(root)
    assert {item.path for item in graph.diagnostics} >= {"execution/bad.py", "cron/bad.task.xml"}
    assert graph.collection_status == "INCOMPLETE"


def test_invalid_python_and_xml_parse_diagnostics_are_deterministic(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/bad.py", "def broken(:\n")
    _write(root, "cron/bad.task.xml", "<Task>")
    _track(root, "execution/bad.py", "cron/bad.task.xml")
    first = build_graph(root).model_dump()
    assert first == build_graph(root).model_dump()
    assert {item["kind"] for item in first["diagnostics"]} >= {"parse_error"}


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_utf16_bom_windows_schedule_is_parsed(tmp_path: Path, encoding: str) -> None:
    root = _repo(tmp_path)
    path = root / "cron/job.task.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "<Task><Actions><Exec><Command>python</Command><Arguments>execution/main.py</Arguments></Exec></Actions></Task>"
    bom = b"\xff\xfe" if encoding == "utf-16-le" else b"\xfe\xff"
    path.write_bytes(bom + body.encode(encoding))
    _track(root, "cron/job.task.xml")
    graph = build_graph(root)
    assert any(
        edge.kind == "schedule" and edge.target == "execution/main.py" for edge in graph.edges
    )
    assert not any(item.path == "cron/job.task.xml" for item in graph.diagnostics)


def test_missing_directive_authority_creates_no_edges_and_holds(tmp_path: Path) -> None:
    root = _repo(tmp_path, authority=False)
    _write(root, "directives/live.md", "Use execution/main.py\n")
    _track(root, "directives/live.md")
    graph = build_graph(root)
    assert not any(edge.kind == "directive" for edge in graph.edges)
    assert graph.collection_status == "INCOMPLETE"


@pytest.mark.parametrize(
    "payload", ["{", '{"directives": []}', '{"directives": {"live.md": {"class": "unknown"}}}']
)
def test_malformed_or_incomplete_directive_authority_creates_no_edges(
    tmp_path: Path, payload: str
) -> None:
    root = _repo(tmp_path, authority=False)
    _write(root, "directives/live.md", "Use execution/main.py\n")
    _write(root, "directives/directive_manifest.json", payload)
    _track(root, "directives")
    graph = build_graph(root)
    assert not any(edge.kind == "directive" for edge in graph.edges)
    assert graph.collection_status == "INCOMPLETE"


def test_only_canonical_and_runbook_directives_create_authority_edges(tmp_path: Path) -> None:
    root = _repo(tmp_path, authority=False)
    for name in ("live", "runbook", "draft", "history"):
        _write(root, f"directives/{name}.md", "Use execution/main.py\n")
    _write(
        root,
        "directives/directive_manifest.json",
        json.dumps(
            {
                "directives": {
                    "live.md": {"class": "canonical"},
                    "runbook.md": {"class": "runbook"},
                    "draft.md": {"class": "draft"},
                    "history.md": {"class": "history"},
                }
            }
        ),
    )
    _track(root, "directives")
    graph = build_graph(root)
    sources = {edge.source for edge in graph.edges if edge.kind == "directive"}
    assert sources == {"directives/live.md", "directives/runbook.md"}


def test_untracked_directive_manifest_cannot_authorize_edges(tmp_path: Path) -> None:
    root = _repo(tmp_path, authority=False)
    _write(root, "directives/live.md", "Use execution/main.py\n")
    _track(root, "directives/live.md")
    _write(
        root,
        "directives/directive_manifest.json",
        json.dumps({"directives": {"live.md": {"class": "canonical"}}}),
    )
    graph = build_graph(root)
    assert not any(edge.kind == "directive" for edge in graph.edges)
    assert graph.collection_status == "INCOMPLETE"


def test_literal_getattr_and_unrelated_run_calls_are_not_unknown(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(
        root,
        "execution/main.py",
        "getattr(mod, 'handler')\napp.run(options)\nprovider.call(payload)\nrun(job)\n",
    )
    _track(root, "execution/main.py")
    graph = build_graph(root)
    assert any(edge.target == "<attribute:handler>" for edge in graph.edges)
    assert not graph.unknown_edges
    assert not any(
        edge.kind in {"python_entrypoint", "external_process"}
        for edge in graph.edges
        if edge.source == "execution/main.py"
    )


def test_aliased_process_calls_and_external_annotation_are_scanned(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/worker.py", "VALUE = 1\n")
    _write(
        root,
        "execution/main.py",
        "import subprocess as sp\n"
        "from runpy import run_path as execute\n"
        "sp.run(['python', 'execution/worker.py'])\n"
        "execute('execution/worker.py')\n"
        "sp.run(argv)  # reachability: external-process\n",
    )
    _track(root, "execution")
    graph = build_graph(root)
    local = [edge for edge in graph.edges if edge.source == "execution/main.py"]
    assert sum(edge.kind == "python_entrypoint" for edge in local) == 2
    assert any(edge.kind == "external_process" for edge in local)
    assert not any(edge.unknown for edge in local)


def test_untracked_python_target_cannot_become_a_known_edge(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(
        root,
        "execution/main.py",
        "import subprocess\nsubprocess.run(['python', 'src/untracked.py'])\n",
    )
    _track(root, "execution/main.py")
    _write(root, "src/untracked.py", "from src.successor import run\n")
    _write(root, "src/successor.py", "VALUE = 1\n")
    graph = build_graph(root)
    assert "src/untracked.py" not in graph.population
    assert not any(edge.target == "src/untracked.py" for edge in graph.edges)
    assert any("unresolved operational target" in item.message for item in graph.diagnostics)


def test_import_package_namespace_wrapper_and_reconstruction_edges(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "src/pkg/__init__.py", "from .worker import run\n")
    _write(root, "src/pkg/worker.py", "def run() -> None:\n    return None\n")
    _write(root, "src/namespace/worker.py", "VALUE = 1\n")
    _write(root, "execution/main.py", "import pkg\nimport namespace.worker\n")
    _write(root, "scripts/run.sh", "python execution/main.py\n")
    _write(root, "reconstruction_manifest.json", json.dumps({"entry": "execution/main.py"}))
    _track(root, ".")
    graph = build_graph(root)
    assert any(
        edge.source == "execution/main.py" and edge.target == "src/pkg/__init__.py"
        for edge in graph.edges
    )
    assert any(
        edge.source == "execution/main.py" and edge.target == "src/namespace/worker.py"
        for edge in graph.edges
    )
    assert any(
        edge.kind == "wrapper" and edge.target == "execution/main.py" for edge in graph.edges
    )
    assert any(
        edge.kind == "reconstruction" and edge.target == "execution/main.py" for edge in graph.edges
    )


def test_literal_targets_cannot_escape_repository(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    outside = root.parent / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    literal_target = getattr(reachability, "_literal_target")
    safe_repo_path = getattr(reachability, "_safe_repo_path")
    assert literal_target(root, "../outside.py", {}) is None
    assert literal_target(root, str(outside), {}) is None
    with pytest.raises(reachability.ReachabilityCollectionError):
        safe_repo_path(root, "../outside.py")


def test_unknown_edge_is_preserved_and_does_not_traverse_target_or_successors(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "import subprocess\nsubprocess.run(command)\n")
    _write(root, "src/unknown_target.py", "from src.successor import run\n")
    _write(root, "src/successor.py", "VALUE = 1\n")
    _track(root, "execution/main.py", "src")
    graph = build_graph(root)
    assert graph.unknown_edges
    reachable = production_reachable_nodes(graph)
    assert "src/unknown_target.py" not in reachable
    assert "src/successor.py" not in reachable
    assert production_unknown_edges(graph)
    assert graph.collection_status == "COMPLETE"
    assert graph.closure_status == "HOLD"


def test_complete_graph_with_unknown_closure_is_always_hold(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "getattr(mod, name)\n")
    _track(root, "execution/main.py")
    graph = build_graph(root)
    assert graph.unknown_edges
    assert graph.collection_status == "COMPLETE"
    assert graph.closure_status == "HOLD"
    assert graph.hold is True
    assert any("production unknown" in reason for reason in graph.closure_reasons)


def test_cli_zero_for_complete_collection_even_when_closure_holds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    assert main(["--repo-root", str(root)]) == 0
    capsys.readouterr()
    _write(root, "execution/main.py", "getattr(mod, name)\n")
    _track(root, "execution/main.py")
    assert main(["--repo-root", str(root)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["collection_status"] == "COMPLETE"
    assert payload["closure_status"] == "HOLD"


def test_cli_returns_two_for_incomplete_collection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "def broken(:\n")
    _track(root, "execution/main.py")
    assert main(["--repo-root", str(root)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["collection_status"] == "INCOMPLETE"
    assert payload["closure_status"] == "HOLD"


def test_cli_large_output_is_written_under_tmp(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(reachability, "_MAX_STDOUT_BYTES", 1)
    assert main(["--repo-root", str(root)]) == 0
    summary = json.loads(capsys.readouterr().out)
    output = summary["output"]
    assert output.startswith(".tmp/")
    assert (root / output).is_file()


def test_unknown_edge_does_not_unlock_downstream_closure_model() -> None:
    unknown = GraphEdge(
        source="root",
        target="unresolved",
        kind="unknown",
        evidence="dynamic",
        confidence="low",
        unknown=True,
    )
    successor = GraphEdge(
        source="unresolved", target="leaf", kind="import", evidence="successor", confidence="high"
    )
    graph = ReachabilityGraph(
        subject_commit="0" * 40,
        source_manifest_sha256="0" * 64,
        scanner_sha256="0" * 64,
        scanner_version="test",
        python_version=sys.version,
        population=(),
        exclusions=(),
        attempted_input_manifest=(),
        collection_status="COMPLETE",
        closure_status="HOLD",
        closure_reasons=("reviewed reachability closure deferred",),
        nodes=[
            GraphNode(id="root", kind="python"),
            GraphNode(id="unresolved", kind="python"),
            GraphNode(id="leaf", kind="python"),
        ],
        edges=[unknown, successor],
        roots=["root"],
        unresolved=[],
        diagnostics=[],
        unknown_edges=[unknown],
        hold=True,
        stats={"files": 3, "edges": 2, "unknown": 1, "diagnostics": 0},
        parser={"name": "test", "version": "1", "python": ">=3.11"},
    )
    assert production_reachable_nodes(graph) == frozenset({"root"})
    assert production_unknown_edges(graph) == (unknown,)


def _fingerprint(path: str, line: int, source_line: str) -> str:
    return hashlib.sha256(f"{path}:{line}:{source_line.strip()}".encode()).hexdigest()


def _provenance(root: Path) -> dict[str, object]:
    graph = build_graph(root)
    return {
        "path": ".tmp/quality/reachability-check.json",
        "schema_version": "operational-reachability-raw/v1",
        "parser": {
            "name": graph.parser["name"],
            "version": graph.parser["version"],
            "python": graph.parser["python"],
            "source_sha256": graph.parser["source_sha256"],
        },
        "source_manifest_sha256": graph.source_manifest_sha256,
        "scanner_sha256": graph.scanner_sha256,
    }


def _write_manifest(
    root: Path,
    name: str,
    schema: str,
    entries: list[dict[str, object]],
    provenance: dict[str, object] | None = None,
) -> None:
    prov = provenance if provenance is not None else _provenance(root)
    manifests: dict[str, tuple[str, list[dict[str, object]]]] = {
        "reachability-dynamic-import-dispositions.json": (
            "reachability-dynamic-import-dispositions/v1",
            [],
        ),
        "reachability-getattr-dispositions.json": (
            "reachability-getattr-dispositions/v1",
            [],
        ),
        "reachability-process-dispositions.json": (
            "reachability-process-dispositions/v1",
            [],
        ),
    }
    manifests[name] = (schema, entries)
    for manifest_name, (manifest_schema, manifest_entries) in manifests.items():
        payload: dict[str, object] = {
            "schema_version": manifest_schema,
            "graph_provenance": prov,
            "edges": manifest_entries,
        }
        _write(
            root,
            f"docs/quality/{manifest_name}",
            json.dumps(payload, sort_keys=True) + "\n",
        )
    _track(root, "docs/quality")


@pytest.mark.parametrize(
    ("manifest", "schema", "source", "line", "disposition"),
    [
        (
            "reachability-dynamic-import-dispositions.json",
            "reachability-dynamic-import-dispositions/v1",
            "import importlib\nimportlib.import_module(name)\n",
            2,
            "external_optional_dependency",
        ),
        (
            "reachability-getattr-dispositions.json",
            "reachability-getattr-dispositions/v1",
            "getattr(mod, name)\n",
            1,
            "closed_literal_set",
        ),
        (
            "reachability-process-dispositions.json",
            "reachability-process-dispositions/v1",
            "import subprocess\nsubprocess.run(command)\n",
            2,
            "external_process",
        ),
    ],
)
def test_valid_manifest_per_family(
    tmp_path: Path, manifest: str, schema: str, source: str, line: int, disposition: str
) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", source)
    _track(root, "execution/main.py")
    raw = build_graph(root)
    edge = next(e for e in raw.unknown_edges if e.source == "execution/main.py")
    assert edge.line == line
    entries: list[dict[str, object]] = [
        {
            "path": "execution/main.py",
            "line": line,
            "fingerprint": _fingerprint("execution/main.py", line, source.splitlines()[line - 1]),
            "disposition": disposition,
            "evidence": "reviewed",
        }
    ]
    _write_manifest(root, manifest, schema, entries)
    graph = build_graph(root)
    assert graph.collection_status == "COMPLETE"
    assert graph.closure_status == "PASS"
    assert graph.hold is False
    assert not graph.unknown_edges
    assert not production_unknown_edges(graph)
    reviewed = [e for e in graph.edges if e.source == "execution/main.py"]
    assert all(e.reviewed_disposition == disposition for e in reviewed if e.line == line)
    assert all(not e.unknown for e in reviewed if e.line == line)


def test_exact_provenance_required(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "getattr(mod, name)\n")
    _track(root, "execution/main.py")
    prov = _provenance(root)
    entry: dict[str, object] = {
        "path": "execution/main.py",
        "line": 1,
        "fingerprint": _fingerprint("execution/main.py", 1, "getattr(mod, name)"),
        "disposition": "closed_literal_set",
        "evidence": "reviewed",
    }
    bad_provenance = [
        {**prov, "path": "wrong.json"},
        {**prov, "schema_version": "operational-reachability/v1"},
        {**prov, "source_manifest_sha256": "0" * 64},
        {**prov, "scanner_sha256": "0" * 64},
    ]
    wrong_parser = dict(cast(dict[str, str], prov["parser"]))
    wrong_parser["version"] = "0.0.0"
    bad_provenance.append({**prov, "parser": wrong_parser})
    for bad in bad_provenance:
        _write_manifest(
            root,
            "reachability-getattr-dispositions.json",
            "reachability-getattr-dispositions/v1",
            [entry],
            bad,
        )
        graph = build_graph(root)
        assert graph.unknown_edges
        assert graph.closure_status == "HOLD"
        assert any(
            marker in diagnostic.message
            for diagnostic in graph.diagnostics
            for marker in ("provenance mismatch", "invalid reachability disposition manifest")
        )


def test_parser_source_scanner_mismatch(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "getattr(mod, name)\n")
    _track(root, "execution/main.py")
    prov = _provenance(root)
    parser = dict(cast(dict[str, str], prov["parser"]))
    parser["source_sha256"] = "0" * 64
    _write_manifest(
        root,
        "reachability-getattr-dispositions.json",
        "reachability-getattr-dispositions/v1",
        [
            {
                "path": "execution/main.py",
                "line": 1,
                "fingerprint": _fingerprint("execution/main.py", 1, "getattr(mod, name)"),
                "disposition": "closed_literal_set",
                "evidence": "x",
            }
        ],
        {**prov, "parser": parser},
    )
    graph = build_graph(root)
    assert graph.unknown_edges
    assert graph.closure_status == "HOLD"


def test_wrong_manifest_path_and_schema(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "getattr(mod, name)\n")
    _track(root, "execution/main.py")
    prov = _provenance(root)
    _write_manifest(
        root,
        "reachability-getattr-dispositions.json",
        "reachability-dynamic-import-dispositions/v1",
        [
            {
                "path": "execution/main.py",
                "line": 1,
                "fingerprint": _fingerprint("execution/main.py", 1, "getattr(mod, name)"),
                "disposition": "closed_literal_set",
                "evidence": "x",
            }
        ],
        prov,
    )
    graph = build_graph(root)
    assert graph.unknown_edges
    assert graph.closure_status == "HOLD"
    assert any("invalid reachability disposition manifest" in d.message for d in graph.diagnostics)


def test_missing_manifest_is_hold_not_failure(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "getattr(mod, name)\n")
    _track(root, "execution/main.py")
    graph = build_graph(root)
    assert graph.collection_status == "COMPLETE"
    assert graph.closure_status == "HOLD"
    assert graph.unknown_edges


def test_missing_manifest_set_holds_even_without_raw_unknowns(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    graph = build_graph(root)
    assert graph.collection_status == "COMPLETE"
    assert graph.closure_status == "HOLD"
    assert not graph.unknown_edges
    assert (
        sum(
            diagnostic.message == "missing reachability disposition manifest"
            for diagnostic in graph.diagnostics
        )
        == 3
    )


def test_malformed_and_extra_fields(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "getattr(mod, name)\n")
    _track(root, "execution/main.py")
    _write(root, "docs/quality/reachability-getattr-dispositions.json", "{bad json\n")
    _track(root, "docs/quality/reachability-getattr-dispositions.json")
    graph = build_graph(root)
    assert graph.unknown_edges
    assert any("invalid reachability disposition manifest" in d.message for d in graph.diagnostics)
    assert graph.closure_status == "HOLD"
    root2 = _repo(tmp_path / "second")
    _write(root2, "execution/main.py", "getattr(mod, name)\n")
    _track(root2, "execution/main.py")
    prov = _provenance(root2)
    _write(
        root2,
        "docs/quality/reachability-getattr-dispositions.json",
        json.dumps(
            {
                "schema_version": "reachability-getattr-dispositions/v1",
                "graph_provenance": prov,
                "edges": [
                    {
                        "path": "execution/main.py",
                        "line": 1,
                        "fingerprint": _fingerprint("execution/main.py", 1, "getattr(mod, name)"),
                        "disposition": "closed_literal_set",
                        "evidence": "x",
                        "unexpected": 1,
                    }
                ],
            }
        )
        + "\n",
    )
    _git(root2, "add", "docs/quality/reachability-getattr-dispositions.json")
    _git(
        root2,
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "user.name=Test",
        "commit",
        "-qm",
        "bad",
    )
    graph2 = build_graph(root2)
    assert graph2.unknown_edges
    assert any("invalid reachability disposition manifest" in d.message for d in graph2.diagnostics)


def test_tracked_unreadable_disposition_manifest_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    manifest = _write(
        root,
        "docs/quality/reachability-getattr-dispositions.json",
        "{}\n",
    )
    _track(root, "docs/quality/reachability-getattr-dispositions.json")
    original = Path.read_bytes

    def guarded_read(path: Path) -> bytes:
        if path == manifest:
            raise OSError("unreadable disposition fixture")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    graph = build_graph(root)
    assert graph.collection_status == "COMPLETE"
    assert graph.closure_status == "HOLD"
    assert any(
        diagnostic.path == "docs/quality/reachability-getattr-dispositions.json"
        and diagnostic.message == "invalid reachability disposition manifest"
        for diagnostic in graph.diagnostics
    )


def test_tracked_symlink_disposition_manifest_holds(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    target = _write(root, "manifest-target.json", "{}\n")
    manifest = root / "docs/quality/reachability-getattr-dispositions.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    try:
        manifest.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    _track(root, "manifest-target.json", "docs/quality/reachability-getattr-dispositions.json")
    graph = build_graph(root)
    assert graph.collection_status == "COMPLETE"
    assert graph.closure_status == "HOLD"
    assert any(
        diagnostic.path == "docs/quality/reachability-getattr-dispositions.json"
        and diagnostic.message == "invalid reachability disposition manifest"
        for diagnostic in graph.diagnostics
    )


def test_stale_fingerprint(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "getattr(mod, name)\n")
    _track(root, "execution/main.py")
    _write_manifest(
        root,
        "reachability-getattr-dispositions.json",
        "reachability-getattr-dispositions/v1",
        [
            {
                "path": "execution/main.py",
                "line": 1,
                "fingerprint": "0" * 64,
                "disposition": "closed_literal_set",
                "evidence": "stale",
            }
        ],
    )
    graph = build_graph(root)
    assert len(graph.unknown_edges) == 1
    assert any("stale reachability disposition" in d.message for d in graph.diagnostics)
    assert graph.closure_status == "HOLD"


def test_duplicate_key_wrong_kind_unmatched(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "getattr(mod, name)\n")
    _track(root, "execution/main.py")
    fp = _fingerprint("execution/main.py", 1, "getattr(mod, name)")
    _write_manifest(
        root,
        "reachability-getattr-dispositions.json",
        "reachability-getattr-dispositions/v1",
        [
            {
                "path": "execution/main.py",
                "line": 1,
                "fingerprint": fp,
                "disposition": "closed_literal_set",
                "evidence": "a",
            },
            {
                "path": "execution/main.py",
                "line": 1,
                "fingerprint": fp,
                "disposition": "closed_literal_set",
                "evidence": "b",
            },
        ],
    )
    graph = build_graph(root)
    assert any("duplicate reachability disposition" in d.message for d in graph.diagnostics)
    assert graph.closure_status == "HOLD"
    assert graph.unknown_edges
    root2 = _repo(tmp_path / "orphan")
    _write(root2, "execution/main.py", "getattr(mod, name)\n")
    _track(root2, "execution/main.py")
    _write_manifest(
        root2,
        "reachability-dynamic-import-dispositions.json",
        "reachability-dynamic-import-dispositions/v1",
        [
            {
                "path": "execution/main.py",
                "line": 1,
                "fingerprint": _fingerprint("execution/main.py", 1, "getattr(mod, name)"),
                "disposition": "external_optional_dependency",
                "evidence": "wrong kind",
            }
        ],
    )
    graph2 = build_graph(root2)
    assert graph2.unknown_edges
    assert any("no longer matches" in d.message for d in graph2.diagnostics)


def test_unresolved_remains_unknown(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "getattr(mod, name)\n")
    _track(root, "execution/main.py")
    _write_manifest(
        root,
        "reachability-getattr-dispositions.json",
        "reachability-getattr-dispositions/v1",
        [
            {
                "path": "execution/main.py",
                "line": 1,
                "fingerprint": _fingerprint("execution/main.py", 1, "getattr(mod, name)"),
                "disposition": "unresolved",
                "evidence": "still arbitrary",
            }
        ],
    )
    graph = build_graph(root)
    assert len(graph.unknown_edges) == 1
    assert graph.unknown_edges[0].reviewed_disposition is None
    assert graph.closure_status == "HOLD"


@pytest.mark.parametrize(
    "target",
    [
        "execution/first.py, execution/second.py",
        "../outside.py",
        "/absolute.py",
        "execution/./first.py",
        "execution/missing.py",
    ],
)
def test_internal_target_rejects_bad_targets(tmp_path: Path, target: str) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "import subprocess\nsubprocess.run(command)\n")
    _write(root, "execution/first.py", "VALUE = 1\n")
    _write(root, "execution/second.py", "VALUE = 2\n")
    _track(root, "execution", "src")
    _write_manifest(
        root,
        "reachability-process-dispositions.json",
        "reachability-process-dispositions/v1",
        [
            {
                "path": "execution/main.py",
                "line": 2,
                "fingerprint": _fingerprint("execution/main.py", 2, "subprocess.run(command)"),
                "disposition": "internal_python_target",
                "target": target,
                "evidence": "bad",
            }
        ],
    )
    graph = build_graph(root)
    assert len(graph.unknown_edges) == 1
    assert any(
        "requires exact existing repository file targets" in d.message for d in graph.diagnostics
    )
    assert graph.closure_status == "HOLD"


def test_internal_symlink_target_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "import subprocess\nsubprocess.run(command)\n")
    _write(root, "execution/real.py", "VALUE = 1\n")
    _track(root, "execution", "src")
    link = root / "execution" / "link.py"
    try:
        link.symlink_to(root / "execution" / "real.py")
    except OSError:
        pytest.skip("symlinks unavailable")
    _git(root, "add", "execution/link.py")
    _git(
        root,
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "user.name=Test",
        "commit",
        "-qm",
        "link",
    )
    _write_manifest(
        root,
        "reachability-process-dispositions.json",
        "reachability-process-dispositions/v1",
        [
            {
                "path": "execution/main.py",
                "line": 2,
                "fingerprint": _fingerprint("execution/main.py", 2, "subprocess.run(command)"),
                "disposition": "internal_python_target",
                "target": "execution/link.py",
                "evidence": "symlink",
            }
        ],
    )
    graph = build_graph(root)
    assert graph.unknown_edges
    assert graph.closure_status == "HOLD"


def test_multi_target_expands_exactly(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "import subprocess\nsubprocess.run(command)\n")
    _write(root, "execution/first.py", "VALUE = 1\n")
    _write(root, "execution/second.py", "VALUE = 2\n")
    _track(root, "execution", "src")
    _write_manifest(
        root,
        "reachability-process-dispositions.json",
        "reachability-process-dispositions/v1",
        [
            {
                "path": "execution/main.py",
                "line": 2,
                "fingerprint": _fingerprint("execution/main.py", 2, "subprocess.run(command)"),
                "disposition": "internal_python_target",
                "targets": ["execution/second.py", "execution/first.py", "execution/first.py"],
                "evidence": "fixed",
            }
        ],
    )
    graph = build_graph(root)
    reviewed = [e for e in graph.edges if e.kind == "unknown" and e.line == 2]
    assert [(e.target, e.reviewed_disposition) for e in reviewed] == [
        ("execution/first.py", "internal_python_target"),
        ("execution/second.py", "internal_python_target"),
    ]
    assert not graph.unknown_edges
    assert graph.closure_status == "PASS"


def test_internal_dynamic_target_is_traversed_into_production_closure(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "import importlib\nimportlib.import_module(name)\n")
    _write(root, "src/child.py", "getattr(registry, name)\n")
    _track(root, "execution", "src")
    _write_manifest(
        root,
        "reachability-dynamic-import-dispositions.json",
        "reachability-dynamic-import-dispositions/v1",
        [
            {
                "path": "execution/main.py",
                "line": 2,
                "fingerprint": _fingerprint(
                    "execution/main.py", 2, "importlib.import_module(name)"
                ),
                "disposition": "internal_target",
                "target": "src/child.py",
                "evidence": "fixed internal registry target",
            }
        ],
    )
    graph = build_graph(root)
    reviewed = [
        edge for edge in graph.edges if edge.source == "execution/main.py" and edge.line == 2
    ]
    assert [(edge.target, edge.reviewed_disposition) for edge in reviewed] == [
        ("src/child.py", "internal_target")
    ]
    assert any(edge.source == "src/child.py" for edge in production_unknown_edges(graph))
    assert graph.closure_status == "HOLD"


def test_production_unresolved_diagnostic_forces_hold(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "import src.missing_runtime_module\n")
    _track(root, "execution/main.py")
    _write_manifest(
        root,
        "reachability-getattr-dispositions.json",
        "reachability-getattr-dispositions/v1",
        [],
    )
    graph = build_graph(root)
    assert graph.collection_status == "COMPLETE"
    assert graph.stats["production_unknown"] == 0
    assert graph.stats["production_unresolved"] > 0
    assert graph.closure_status == "HOLD"
    assert graph.hold is True
    assert any("production unresolved diagnostics" in reason for reason in graph.closure_reasons)


@pytest.mark.parametrize("duplicate_scope", ["top", "nested"])
def test_duplicate_json_object_keys_are_rejected(tmp_path: Path, duplicate_scope: str) -> None:
    root = _repo(tmp_path)
    _write_manifest(
        root,
        "reachability-getattr-dispositions.json",
        "reachability-getattr-dispositions/v1",
        [],
    )
    path = root / "docs/quality/reachability-getattr-dispositions.json"
    raw = path.read_text(encoding="utf-8").strip()
    if duplicate_scope == "top":
        raw = raw[:-1] + ', "schema_version": "reachability-getattr-dispositions/v1"}'
    else:
        marker = '"path": ".tmp/quality/reachability-check.json"'
        raw = raw.replace(marker, f"{marker}, {marker}", 1)
    _write(root, "docs/quality/reachability-getattr-dispositions.json", raw + "\n")
    _track(root, "docs/quality/reachability-getattr-dispositions.json")
    graph = build_graph(root)
    assert graph.closure_status == "HOLD"
    assert graph.hold is True
    assert any(
        diagnostic.path == "docs/quality/reachability-getattr-dispositions.json"
        and diagnostic.message == "invalid reachability disposition manifest"
        for diagnostic in graph.diagnostics
    )


def test_production_unknown_forces_hold_orphan_passes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "from src.service import run\n")
    _write(root, "src/service.py", "getattr(mod, name)\n")
    _write(root, "src/orphan.py", "getattr(mod, name)\n")
    _write(root, "src/__init__.py", "\n")
    _track(root, "execution", "src")
    graph = build_graph(root)
    assert production_unknown_edges(graph)
    assert graph.closure_status == "HOLD"
    assert graph.stats["production_unknown"] == len(production_unknown_edges(graph))
    fp = _fingerprint("src/service.py", 1, "getattr(mod, name)")
    _write_manifest(
        root,
        "reachability-getattr-dispositions.json",
        "reachability-getattr-dispositions/v1",
        [
            {
                "path": "src/service.py",
                "line": 1,
                "fingerprint": fp,
                "disposition": "closed_literal_set",
                "evidence": "fixed",
            }
        ],
    )
    graph2 = build_graph(root)
    assert graph2.closure_status == "PASS"
    assert graph2.hold is False
    assert len(graph2.unknown_edges) == 1
    assert graph2.unknown_edges[0].source == "src/orphan.py"


def test_disposition_hash_changes_with_bytes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "getattr(mod, name)\n")
    _track(root, "execution/main.py")
    _write_manifest(
        root,
        "reachability-getattr-dispositions.json",
        "reachability-getattr-dispositions/v1",
        [
            {
                "path": "execution/main.py",
                "line": 1,
                "fingerprint": _fingerprint("execution/main.py", 1, "getattr(mod, name)"),
                "disposition": "closed_literal_set",
                "evidence": "a",
            }
        ],
    )
    first = build_graph(root).parser["dispositions_sha256"]
    _write_manifest(
        root,
        "reachability-getattr-dispositions.json",
        "reachability-getattr-dispositions/v1",
        [
            {
                "path": "execution/main.py",
                "line": 1,
                "fingerprint": _fingerprint("execution/main.py", 1, "getattr(mod, name)"),
                "disposition": "closed_literal_set",
                "evidence": "b",
            }
        ],
    )
    second = build_graph(root).parser["dispositions_sha256"]
    assert first != second
    assert len(first) == 64 and len(second) == 64


def test_disposition_files_excluded_from_population(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "getattr(mod, name)\n")
    _track(root, "execution/main.py")
    _write_manifest(
        root,
        "reachability-getattr-dispositions.json",
        "reachability-getattr-dispositions/v1",
        [
            {
                "path": "execution/main.py",
                "line": 1,
                "fingerprint": _fingerprint("execution/main.py", 1, "getattr(mod, name)"),
                "disposition": "closed_literal_set",
                "evidence": "x",
            }
        ],
    )
    graph = build_graph(root)
    assert "docs/quality/reachability-getattr-dispositions.json" not in graph.population
    assert not any(n.id.startswith("docs/quality/") for n in graph.nodes)
    assert any(
        e.path == "docs/quality/reachability-getattr-dispositions.json" for e in graph.exclusions
    )
