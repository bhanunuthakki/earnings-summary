from __future__ import annotations

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


def _disposition_payload() -> dict[str, object]:
    return {
        "schema_version": "reachability-getattr-dispositions/v1",
        "graph_provenance": {
            "path": ".tmp/quality/reachability-check.json",
            "schema_version": "operational-reachability/v1",
            "parser": {
                "name": "wrong",
                "version": "wrong",
                "python": ">=3.11",
                "source_sha256": "0" * 64,
            },
        },
        "edges": [],
    }


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


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("reachability-dynamic-import-dispositions.json", "{}"),
        ("reachability-getattr-dispositions.json", "{"),
        ("reachability-process-dispositions.json", json.dumps(_disposition_payload())),
    ],
)
def test_tracked_disposition_files_have_zero_effect(
    tmp_path: Path, name: str, payload: str
) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "getattr(mod, name)\n")
    before = build_graph(root)
    _write(root, f"docs/quality/{name}", payload + "\n")
    _track(root, f"docs/quality/{name}")
    after = build_graph(root)
    assert after.edges == before.edges
    assert after.unknown_edges == before.unknown_edges
    assert after.hold == before.hold
    assert after.parser == before.parser
    assert after.source_manifest_sha256 == before.source_manifest_sha256
    assert after.collection_status == before.collection_status == "COMPLETE"


def test_excluded_disposition_contents_are_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    disposition = _write(root, "docs/quality/reachability-getattr-dispositions.json", "{}\n")
    _track(root, "docs/quality/reachability-getattr-dispositions.json")
    original = Path.read_bytes

    def guarded_read(self: Path) -> bytes:
        if self == disposition:
            raise AssertionError("excluded evidence must not be read")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    graph = build_graph(root)
    assert graph.collection_status == "COMPLETE"
    assert any(item.path == disposition.relative_to(root).as_posix() for item in graph.exclusions)


def test_complete_graph_with_unknown_closure_is_always_hold(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "execution/main.py", "getattr(mod, name)\n")
    _track(root, "execution/main.py")
    graph = build_graph(root)
    assert graph.unknown_edges
    assert graph.collection_status == "COMPLETE"
    assert graph.closure_status == "HOLD"
    assert graph.closure_reasons == ("reviewed reachability closure deferred",)


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
        stats={"files": 3, "edges": 2, "unknown": 1, "diagnostics": 0},
        parser={"name": "test", "version": "1", "python": ">=3.11"},
    )
    assert production_reachable_nodes(graph) == frozenset({"root"})
    assert production_unknown_edges(graph) == (unknown,)
