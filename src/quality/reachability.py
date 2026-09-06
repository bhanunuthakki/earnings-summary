"""Build a conservative, deterministic operational reachability graph.

This is an evidence oracle, not a Python runtime import graph: edges which
cannot be proved statically are retained as ``unknown`` or ``unresolved``.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Literal
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from quality.git_env import clean_local_git_env

PARSER_VERSION = "1.0.0"
# Parser provenance is compared as part of the semantic lifecycle receipt.
# Record the supported interpreter contract rather than the runtime's patch or
# minor version, which would make equivalent receipts stale across clean clones.
PARSER_PYTHON = ">=3.11"
_MAX_STDOUT_BYTES = 100_000
EdgeKind = Literal[
    "import",
    "relative_import",
    "reexport",
    "dynamic_import",
    "registry",
    "getattr",
    "python_entrypoint",
    "external_process",
    "route",
    "rendered_js",
    "schedule",
    "wrapper",
    "directive",
    "reconstruction",
    "unknown",
]
NodeKind = Literal[
    "python",
    "package",
    "javascript",
    "xml",
    "make",
    "ci",
    "json",
    "directive",
    "wrapper",
    "service",
]
Confidence = Literal["high", "medium", "low"]
CollectionStatus = Literal["COMPLETE", "INCOMPLETE"]


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: NodeKind


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    target: str
    kind: EdgeKind
    evidence: str
    confidence: Literal["high", "medium", "low"]
    line: int | None = None
    unknown: bool = False


class Diagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    message: str
    kind: Literal["parse_error", "unresolved", "unknown"]


class InputManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    content_sha256: str | None = None
    error: str | None = None


class ExcludedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    reason: str


class ReachabilityGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["operational-reachability-raw/v1"] = "operational-reachability-raw/v1"
    subject_commit: str
    source_manifest_sha256: str
    scanner_sha256: str
    scanner_version: str
    python_version: str
    population: tuple[str, ...]
    exclusions: tuple[ExcludedInput, ...]
    attempted_input_manifest: tuple[InputManifestEntry, ...]
    collection_status: CollectionStatus
    collection_reasons: tuple[str, ...] = ()
    closure_status: Literal["HOLD"] = "HOLD"
    closure_reasons: tuple[str, ...] = ("reviewed reachability closure deferred",)
    parser: dict[str, str]
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    roots: list[str]
    unresolved: list[Diagnostic]
    diagnostics: list[Diagnostic]
    unknown_edges: list[GraphEdge]
    hold: Literal[True] = True
    stats: dict[str, int]


class DirectiveEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    classification: Literal["canonical", "runbook", "draft", "history"] = Field(alias="class")


class DirectiveManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    directives: dict[str, DirectiveEntry]


class ReachabilityCollectionError(RuntimeError):
    """The repository identity or tracked population cannot be established."""


_SKIP = {
    ".git",
    ".venv",
    ".tmp",
    ".cache",
    "data",
    "transcripts",
    "ir_documents",
    "scratch",
    "__pycache__",
}
_PY_RE = re.compile(
    r"(?:[\w./\\:-]*python(?:3)?(?:\.exe)?|py(?:\.exe)?)\s+"
    r"(?:-u\s+)?(?:-m\s+)?([\w./\\-]+(?:\.py)?)",
    re.IGNORECASE,
)
_STR = re.compile(r"['\"]([^'\"]+)['\"]")


def _safe_repo_path(root: Path, relative: str) -> Path:
    candidate = PurePosixPath(relative)
    if (
        not relative
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ReachabilityCollectionError(f"unsafe repository path: {relative!r}")
    path = root.joinpath(*candidate.parts)
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ReachabilityCollectionError(f"unable to resolve repository path: {relative}") from exc
    if not resolved.is_relative_to(root):
        raise ReachabilityCollectionError(f"repository path escapes root: {relative}")
    return path


def _tracked_manifest(root: Path) -> tuple[str, ...]:
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=True,
            env=clean_local_git_env(),
        )
        decoded = listed.stdout.decode("utf-8")
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError) as exc:
        raise ReachabilityCollectionError("unable to enumerate tracked repository files") from exc
    if decoded and not decoded.endswith("\0"):
        raise ReachabilityCollectionError("tracked path output is missing its NUL terminator")
    paths = tuple(sorted(path for path in decoded.split("\0") if path))
    if len(paths) != len(set(paths)):
        raise ReachabilityCollectionError("tracked path output contains duplicate paths")
    for relative in paths:
        _safe_repo_path(root, relative)
    return paths


def _files(
    root: Path,
) -> tuple[tuple[str, ...], tuple[ExcludedInput, ...]]:
    attempted = _tracked_manifest(root)
    exclusions = tuple(
        ExcludedInput(
            path=relative,
            reason=(
                "quality evidence artifact"
                if relative.startswith("docs/quality/")
                else "excluded runtime-data or tool directory"
            ),
        )
        for relative in attempted
        if relative.startswith("docs/quality/")
        or any(part in _SKIP for part in PurePosixPath(relative).parts)
    )
    return attempted, exclusions


def _module_index(root: Path, files: list[Path]) -> dict[str, str]:
    """Index only project Python modules; third-party names are not unresolved."""
    index: dict[str, str] = {}
    namespace_roots: set[str] = set()
    for path in files:
        rel_parts = path.relative_to(root).parts
        if (
            path.suffix != ".py"
            or not rel_parts
            or rel_parts[0]
            not in {
                "src",
                "execution",
                "tests",
                "instruction_tests",
                "scripts",
            }
        ):
            continue
        namespace_roots.add(rel_parts[0])
        rel = path.relative_to(root).as_posix()
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
            if parts == ["execution"]:
                index["execution"] = rel
            elif parts == ["src"]:
                index["src"] = rel
        if parts and parts[0] in {"src", "execution"}:
            parts.pop(0)
        if not parts:
            continue
        module = ".".join(parts)
        index[module] = rel
        index["src." + module] = rel
        if rel.startswith("execution/"):
            index["execution." + module] = rel
    for name in namespace_roots:
        index.setdefault(name, f"{name}/")
    return index


def _active_directives(manifest_raw: bytes | None) -> tuple[set[str], str | None]:
    """Return current directive paths and an authority failure, if any."""
    if manifest_raw is None:
        return set(), "directive manifest is missing"
    try:
        manifest = DirectiveManifest.model_validate_json(manifest_raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValidationError, ValueError) as exc:
        return set(), f"directive manifest is invalid: {exc}"
    return {
        f"directives/{name}"
        for name, metadata in manifest.directives.items()
        if metadata.classification in {"canonical", "runbook"}
    }, None


def _literal_string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _process_target(call: ast.Call, fn: str) -> tuple[str | None, bool]:
    """Return a literal Python target and whether the launch is dynamic."""
    if not call.args:
        return None, True
    first = call.args[0]
    if fn == "run_path":
        literal = _literal_string(first)
        return literal, literal is None
    if isinstance(first, (ast.List, ast.Tuple)):
        first_literal = _literal_string(first.elts[0]) if first.elts else None
        if first_literal is not None and Path(
            first_literal.replace("\\", "/")
        ).name.lower() not in {
            "python",
            "python3",
            "python.exe",
            "python3.exe",
            "py",
            "py.exe",
        }:
            return None, False
        values = [_literal_string(item) for item in first.elts]
        if any(value is None for value in values):
            return None, True
        argv = [value for value in values if value is not None]
        python_at = next(
            (
                index
                for index, value in enumerate(argv)
                if Path(value.replace("\\", "/")).name.lower()
                in {"python", "python3", "python.exe", "python3.exe", "py", "py.exe"}
            ),
            None,
        )
        if python_at is None:
            return None, False
        tail = argv[python_at + 1 :]
        while tail and tail[0] in {"-u", "-B", "-I"}:
            tail.pop(0)
        if len(tail) >= 2 and tail[0] == "-m":
            return tail[1], False
        return (tail[0], False) if tail else (None, True)
    literal = _literal_string(first)
    if literal is None:
        return None, True
    match = _PY_RE.search(literal)
    if match:
        return match.group(1), False
    if literal.replace("\\", "/").endswith(".py"):
        return literal, False
    return None, False


def _import_base(source_rel: str, module: str, level: int) -> str:
    if not level:
        return module
    package = source_rel.removesuffix(".py").split("/")
    if package[-1] == "__init__":
        package.pop()
    else:
        package.pop()
    package = package[: max(0, len(package) - (level - 1))]
    return ".".join([*package, *([module] if module else [])])


def _resolve_module(
    source_rel: str, module: str, level: int, index: dict[str, str]
) -> tuple[str | None, bool]:
    """Return (path, is_project_name), distinguishing external imports."""
    candidate = _import_base(source_rel, module, level)
    if level:
        return index.get(candidate), True
    return index.get(candidate), candidate.split(".", 1)[0] in {
        key.split(".", 1)[0] for key in index
    }


def _literal_target(root: Path, value: str, index: dict[str, str]) -> str | None:
    if value in index:
        return index[value]
    candidate = Path(value.replace("\\", "/"))
    normalized = candidate.as_posix()
    if (
        candidate.suffix == ".py"
        and not candidate.is_absolute()
        and ".." not in candidate.parts
        and normalized in set(index.values())
    ):
        return normalized
    return None


def _head_commit(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True,
            check=True,
            env=clean_local_git_env(),
        )
        commit = result.stdout.decode("ascii").strip()
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError) as exc:
        raise ReachabilityCollectionError("unable to resolve repository HEAD") from exc
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise ReachabilityCollectionError("repository HEAD is not a commit hash")
    return commit


def build_graph(repo_root: str | Path) -> ReachabilityGraph:
    root = Path(repo_root).resolve()
    attempted_paths, exclusions = _files(root)
    excluded_paths = {entry.path for entry in exclusions}
    attempted_manifest: list[InputManifestEntry] = []
    input_bytes: dict[str, bytes] = {}
    collection_reasons: list[str] = []
    diagnostics: list[Diagnostic] = []
    for relative in attempted_paths:
        if relative in excluded_paths:
            continue
        path = _safe_repo_path(root, relative)
        try:
            if not path.is_file():
                raise OSError("tracked input is not a regular file")
            raw = path.read_bytes()
        except OSError as exc:
            message = f"unable to read tracked input {relative}: {exc}"
            attempted_manifest.append(InputManifestEntry(path=relative, error=message))
            collection_reasons.append(message)
            diagnostics.append(Diagnostic(path=relative, message=message, kind="parse_error"))
            continue
        input_bytes[relative] = raw
        attempted_manifest.append(
            InputManifestEntry(path=relative, content_sha256=hashlib.sha256(raw).hexdigest())
        )
    population = tuple(
        relative
        for relative in attempted_paths
        if relative not in excluded_paths and relative in input_bytes
    )
    files = [root / PurePosixPath(relative) for relative in population]
    module_index = _module_index(root, files)
    project_roots = {key.split(".", 1)[0] for key in module_index}
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    source_hashes: dict[str, str] = {}
    active_directives, directive_failure = _active_directives(
        input_bytes.get("directives/directive_manifest.json")
    )
    if directive_failure:
        collection_reasons.append(directive_failure)

    def node(path: str, kind: NodeKind) -> None:
        nodes.setdefault(path, GraphNode(id=path, kind=kind))

    def add(
        source: str,
        target: str,
        kind: EdgeKind,
        evidence: str,
        confidence: Confidence,
        line: int | None = None,
        unknown: bool = False,
    ) -> None:
        edge = GraphEdge(
            source=source,
            target=target,
            kind=kind,
            evidence=evidence,
            confidence=confidence,
            line=line,
            unknown=unknown,
        )
        edges.append(edge)

    def operational_target(value: str, source: str) -> str:
        target = _literal_target(root, value, module_index)
        if target:
            return target
        normalized = value.replace("\\", "/")
        if normalized.startswith(("src/", "execution/")) and normalized.endswith(".py"):
            diagnostics.append(
                Diagnostic(
                    path=source,
                    message=f"unresolved operational target {value}",
                    kind="unresolved",
                )
            )
        return value

    for path in files:
        rel = path.relative_to(root).as_posix()
        suffix = path.suffix.lower()
        kind: NodeKind
        if suffix == ".py":
            kind = "python"
        elif suffix in {".js", ".ts", ".tsx"}:
            kind = "javascript"
        elif suffix == ".xml":
            kind = "xml"
        elif path.name == "Makefile" or suffix in {".mk"}:
            kind = "make"
        elif ".github" in path.parts:
            kind = "ci"
        elif suffix == ".json":
            kind = "json"
        elif rel.startswith("directives/"):
            kind = "directive"
        elif suffix in {".cmd", ".bat", ".ps1", ".sh"}:
            kind = "wrapper"
        elif suffix == ".service":
            kind = "service"
        else:
            continue
        node(rel, kind)
        raw = input_bytes[rel]
        try:
            if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                text = raw.decode("utf-16")
            else:
                text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            diagnostics.append(
                Diagnostic(path=rel, message=f"read failed: {exc}", kind="parse_error")
            )
            collection_reasons.append(f"invalid encoding for {rel}")
            continue
        source_hashes[rel] = hashlib.sha256(raw).hexdigest()
        if suffix == ".py":
            try:
                tree = ast.parse(text, filename=rel)
            except SyntaxError as exc:
                diagnostics.append(
                    Diagnostic(path=rel, message=f"syntax error: {exc.msg}", kind="parse_error")
                )
                collection_reasons.append(f"Python parse failed for {rel}")
                continue
            subprocess_modules = {
                alias.asname or alias.name
                for item in ast.walk(tree)
                if isinstance(item, ast.Import)
                for alias in item.names
                if alias.name == "subprocess"
            }
            runpy_modules = {
                alias.asname or alias.name
                for item in ast.walk(tree)
                if isinstance(item, ast.Import)
                for alias in item.names
                if alias.name == "runpy"
            }
            subprocess_functions = {
                alias.asname or alias.name: alias.name
                for item in ast.walk(tree)
                if isinstance(item, ast.ImportFrom) and item.module == "subprocess"
                for alias in item.names
                if alias.name in {"run", "call", "check_call", "check_output", "Popen"}
            }
            runpy_functions = {
                alias.asname or alias.name: alias.name
                for item in ast.walk(tree)
                if isinstance(item, ast.ImportFrom) and item.module == "runpy"
                for alias in item.names
                if alias.name == "run_path"
            }
            for item in ast.walk(tree):
                if isinstance(item, ast.Import):
                    for alias in item.names:
                        target, internal = _resolve_module(rel, alias.name, 0, module_index)
                        if target:
                            node(target, "package" if target.endswith("/") else "python")
                            add(rel, target, "import", f"import {alias.name}", "high", item.lineno)
                        elif internal:
                            diagnostics.append(
                                Diagnostic(
                                    path=rel,
                                    message=f"unresolved import {alias.name}",
                                    kind="unresolved",
                                )
                            )
                        else:
                            add(
                                rel,
                                f"<external:{alias.name}>",
                                "import",
                                f"external import {alias.name}",
                                "high",
                            )
                elif isinstance(item, ast.ImportFrom):
                    module = "." * item.level + (item.module or "")
                    target, internal = _resolve_module(
                        rel, item.module or "", item.level, module_index
                    )
                    if target:
                        node(target, "package" if target.endswith("/") else "python")
                        add(
                            rel,
                            target,
                            "relative_import" if item.level else "import",
                            f"from {module}",
                            "high",
                            item.lineno,
                        )
                        explicit_exports = "__all__" in text or path.name == "__init__.py"
                        if explicit_exports:
                            add(
                                rel,
                                target,
                                "reexport",
                                "from-import binding",
                                "medium",
                                item.lineno,
                            )
                        base_module = _import_base(rel, item.module or "", item.level)
                        for alias in item.names:
                            member = module_index.get(f"{base_module}.{alias.name}")
                            if member:
                                node(member, "python")
                                add(
                                    rel,
                                    member,
                                    "reexport" if explicit_exports else "import",
                                    f"from {module} import {alias.name}",
                                    "high",
                                    item.lineno,
                                )
                    elif internal:
                        diagnostics.append(
                            Diagnostic(
                                path=rel, message=f"unresolved import {module}", kind="unresolved"
                            )
                        )
                    else:
                        add(
                            rel,
                            f"<external:{module}>",
                            "import",
                            f"external import {module}",
                            "high",
                            item.lineno,
                        )
                elif isinstance(item, ast.Call):
                    fn = (
                        item.func.attr
                        if isinstance(item.func, ast.Attribute)
                        else (item.func.id if isinstance(item.func, ast.Name) else "")
                    )
                    val = (
                        item.args[0].value
                        if item.args
                        and isinstance(item.args[0], ast.Constant)
                        and isinstance(item.args[0].value, str)
                        else None
                    )
                    process_fn: str | None = None
                    if (
                        isinstance(item.func, ast.Attribute)
                        and isinstance(item.func.value, ast.Name)
                        and (
                            (
                                item.func.value.id in subprocess_modules
                                and item.func.attr
                                in {"run", "call", "check_call", "check_output", "Popen"}
                            )
                            or (
                                item.func.value.id in runpy_modules and item.func.attr == "run_path"
                            )
                        )
                    ):
                        process_fn = item.func.attr
                    elif isinstance(item.func, ast.Name):
                        process_fn = subprocess_functions.get(item.func.id) or runpy_functions.get(
                            item.func.id
                        )
                    if fn in {"import_module", "__import__"}:
                        target = _literal_target(root, val, module_index) if val else None
                        if target:
                            node(target, "package" if target.endswith("/") else "python")
                            add(
                                rel,
                                target,
                                "dynamic_import",
                                f"{fn}({val!r})",
                                "medium",
                                item.lineno,
                            )
                        else:
                            add(
                                rel,
                                val or "<dynamic module>",
                                "dynamic_import",
                                "dynamic import expression (target unresolved)"
                                if val
                                else "dynamic import expression",
                                "low",
                                item.lineno,
                                True,
                            )
                            if val and val.split(".", 1)[0] in project_roots:
                                diagnostics.append(
                                    Diagnostic(
                                        path=rel,
                                        message=f"unresolved dynamic import {val}",
                                        kind="unresolved",
                                    )
                                )
                    elif fn == "getattr":
                        attribute = _literal_string(item.args[1]) if len(item.args) >= 2 else None
                        add(
                            rel,
                            f"<attribute:{attribute}>" if attribute else "<dynamic attribute>",
                            "getattr",
                            "literal getattr attribute"
                            if attribute
                            else "dynamic getattr expression",
                            "medium" if attribute else "low",
                            item.lineno,
                            not bool(attribute),
                        )
                    elif process_fn is not None:
                        source_lines = text.splitlines()
                        source_line = (
                            source_lines[item.lineno - 1]
                            if item.lineno <= len(source_lines)
                            else ""
                        )
                        if "reachability: external-process" in source_line:
                            add(
                                rel,
                                "<declared external process>",
                                "external_process",
                                "reviewed external-process annotation",
                                "medium",
                                item.lineno,
                            )
                            continue
                        process_target, dynamic = _process_target(item, process_fn)
                        normalized = None
                        if process_target:
                            normalized = _literal_target(root, process_target, module_index)
                        if normalized is not None:
                            add(
                                rel,
                                normalized,
                                "python_entrypoint",
                                "subprocess/runpy literal",
                                "medium",
                                item.lineno,
                            )
                        elif process_target:
                            normalized_target = process_target.replace("\\", "/")
                            if normalized_target.startswith(("src/", "execution/")):
                                diagnostics.append(
                                    Diagnostic(
                                        path=rel,
                                        message=f"unresolved operational target {process_target}",
                                        kind="unresolved",
                                    )
                                )
                        elif dynamic:
                            add(
                                rel,
                                "<dynamic process entrypoint>",
                                "unknown",
                                "subprocess/runpy expression",
                                "low",
                                item.lineno,
                                True,
                            )
            for match in re.finditer(r"@[^\n]*\.route\s*\(", text):
                add(
                    rel,
                    "<Flask route>",
                    "route",
                    "route decorator",
                    "medium",
                    text.count("\n", 0, match.start()) + 1,
                )
            for match in re.finditer(
                r"(?:render_template|fetch|axios\.(?:get|post))\s*\(\s*['\"]([^'\"]+)", text
            ):
                add(
                    rel,
                    operational_target(match.group(1), rel),
                    "rendered_js",
                    "render/template reference",
                    "medium",
                    text.count("\n", 0, match.start()) + 1,
                )
            for match in re.finditer(
                r"(?i)(?:registry|plugins?|handlers?|adapters?)\s*(?:\[|\()\s*['\"]([^'\"]+)", text
            ):
                add(
                    rel,
                    operational_target(match.group(1), rel),
                    "registry",
                    "explicit registry module/string",
                    "medium",
                    text.count("\n", 0, match.start()) + 1,
                )
        elif suffix == ".xml":
            try:
                xml = ElementTree.fromstring(text.lstrip("\ufeff"))
            except ParseError as exc:
                diagnostics.append(
                    Diagnostic(path=rel, message=f"XML parse error: {exc}", kind="parse_error")
                )
                collection_reasons.append(f"XML parse failed for {rel}")
                continue
            command_text = " ".join(
                element.text or ""
                for element in xml.iter()
                if element.tag.split("}")[-1].lower() in {"command", "exec", "arguments"}
            )
            for match in _PY_RE.finditer(command_text):
                target = operational_target(match.group(1), rel)
                add(rel, target, "schedule", "scheduled task command", "high")
        else:
            for match in _PY_RE.finditer(text):
                add(
                    rel,
                    operational_target(match.group(1), rel),
                    "wrapper" if kind in {"make", "wrapper", "service"} else "python_entrypoint",
                    "textual command entrypoint",
                    "medium",
                )
            if rel == "reconstruction_manifest.json":
                for match in _STR.finditer(text):
                    if ".py" in match.group(1):
                        add(
                            rel,
                            operational_target(match.group(1), rel),
                            "reconstruction",
                            "manifest path",
                            "high",
                            text.count("\n", 0, match.start()) + 1,
                        )
            if kind == "directive" and rel in active_directives:
                for match in re.finditer(r"(?:execution/|src/)[\w./-]+", text):
                    add(
                        rel,
                        operational_target(match.group(0), rel),
                        "directive",
                        "directive path reference",
                        "medium",
                    )
    edges = sorted(
        {e.model_dump_json(): e for e in edges}.values(),
        key=lambda e: (e.source, e.target, e.kind, e.line or 0, e.evidence),
    )
    source_sha256 = hashlib.sha256(
        "".join(f"{key}:{source_hashes[key]}\n" for key in sorted(source_hashes)).encode()
    ).hexdigest()
    expected_parser = dict(
        name="ast+xml+literal-scanner",
        version=PARSER_VERSION,
        python=PARSER_PYTHON,
        source_sha256=source_sha256,
    )
    unknown = [e for e in edges if e.unknown]
    roots = sorted(
        n
        for n in nodes
        if n.startswith(("execution/", "cron/", ".github/"))
        or nodes[n].kind in {"wrapper", "service"}
        or n in {"Makefile", "reconstruction_manifest.json"}
    )
    source_manifest_sha256 = hashlib.sha256(
        json.dumps(
            [entry.model_dump(mode="json") for entry in attempted_manifest],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    try:
        scanner_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError as exc:
        scanner_sha256 = ""
        collection_reasons.append(f"unable to hash reachability scanner: {exc}")
    collection_status: CollectionStatus = "COMPLETE" if not collection_reasons else "INCOMPLETE"
    return ReachabilityGraph(
        subject_commit=_head_commit(root),
        source_manifest_sha256=source_manifest_sha256,
        scanner_sha256=scanner_sha256,
        scanner_version=PARSER_VERSION,
        python_version=sys.version,
        population=population,
        exclusions=exclusions,
        attempted_input_manifest=tuple(attempted_manifest),
        collection_status=collection_status,
        collection_reasons=tuple(collection_reasons),
        parser={
            **expected_parser,
        },
        nodes=sorted(nodes.values(), key=lambda n: n.id),
        edges=edges,
        roots=roots,
        unresolved=[d for d in diagnostics if d.kind == "unresolved"],
        diagnostics=diagnostics,
        unknown_edges=unknown,
        stats={
            "files": len(nodes),
            "edges": len(edges),
            "unknown": len(unknown),
            "diagnostics": len(diagnostics),
        },
    )


def production_reachable_nodes(graph: ReachabilityGraph) -> frozenset[str]:
    """Return nodes reachable from runtime roots through known edges only."""
    known_targets: dict[str, set[str]] = {}
    for edge in graph.edges:
        if not edge.unknown:
            known_targets.setdefault(edge.source, set()).add(edge.target)

    reachable = set(graph.roots)
    pending = list(reachable)
    while pending:
        source = pending.pop()
        for target in known_targets.get(source, ()):
            if target not in reachable:
                reachable.add(target)
                pending.append(target)
    return frozenset(reachable)


def production_unknown_edges(graph: ReachabilityGraph) -> tuple[GraphEdge, ...]:
    """Return unknown edges whose source is in the conservative production closure."""
    reachable = production_reachable_nodes(graph)
    return tuple(edge for edge in graph.unknown_edges if edge.source in reachable)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, help="Write JSON here instead of stdout")
    args = parser.parse_args(argv)
    try:
        result = build_graph(args.repo_root)
        payload = result.model_dump_json(indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
        elif len(payload.encode("utf-8")) > _MAX_STDOUT_BYTES:
            output = args.repo_root / ".tmp" / "quality" / "operational-reachability-raw.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(payload, encoding="utf-8")
            sys.stdout.write(
                json.dumps(
                    {
                        "output": output.relative_to(args.repo_root).as_posix(),
                        "collection_status": result.collection_status,
                        "closure_status": result.closure_status,
                        "stats": result.stats,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        else:
            sys.stdout.write(payload)
        return 0 if result.collection_status == "COMPLETE" else 2
    except (ReachabilityCollectionError, OSError, TypeError, ValueError) as exc:
        sys.stderr.write(json.dumps({"error": type(exc).__name__, "message": str(exc)}) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
