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
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

PARSER_VERSION = "1.0.0"
EdgeKind = Literal[
    "import",
    "relative_import",
    "reexport",
    "dynamic_import",
    "registry",
    "getattr",
    "python_entrypoint",
    "route",
    "rendered_js",
    "schedule",
    "wrapper",
    "directive",
    "reconstruction",
    "unknown",
]
NodeKind = Literal[
    "python", "javascript", "xml", "make", "ci", "json", "directive", "wrapper", "service"
]
Confidence = Literal["high", "medium", "low"]


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
    path: str
    message: str
    kind: Literal["parse_error", "unresolved", "unknown"]


class ReachabilityGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "operational-reachability/v1"
    parser: dict[str, str]
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    roots: list[str]
    unresolved: list[Diagnostic]
    diagnostics: list[Diagnostic]
    unknown_edges: list[GraphEdge]
    hold: bool
    stats: dict[str, int]


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


def _files(root: Path) -> list[Path]:
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
        )
        paths = [root / p for p in listed.stdout.decode().split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        paths = [p for p in root.rglob("*") if p.is_file() and not any(x in _SKIP for x in p.parts)]
    return sorted(p for p in paths if p.is_file() and not any(x in _SKIP for x in p.parts))


def _module_index(root: Path, files: list[Path]) -> dict[str, str]:
    """Index only project Python modules; third-party names are not unresolved."""
    index: dict[str, str] = {}
    for path in files:
        rel_parts = path.relative_to(root).parts
        if path.suffix != ".py" or not rel_parts or rel_parts[0] not in {"src", "execution"}:
            continue
        rel = path.relative_to(root).as_posix()
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts and parts[0] in {"src", "execution"}:
            parts.pop(0)
        if not parts:
            continue
        module = ".".join(parts)
        index[module] = rel
        index["src." + module] = rel
        if rel.startswith("execution/"):
            index["execution." + module] = rel
    return index


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
    if candidate.suffix == ".py" and (root / candidate).is_file():
        return candidate.as_posix()
    return None


def build_graph(repo_root: str | Path, touched: set[str] | None = None) -> ReachabilityGraph:
    root = Path(repo_root).resolve()
    files = _files(root)
    module_index = _module_index(root, files)
    project_roots = {key.split(".", 1)[0] for key in module_index}
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    diagnostics: list[Diagnostic] = []
    source_hashes: dict[str, str] = {}

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
        if unknown:
            diagnostics.append(Diagnostic(path=source, message=evidence, kind="unknown"))

    def operational_target(value: str, source: str) -> str:
        target = _literal_target(root, value, module_index)
        if target:
            return target
        normalized = value.replace("\\", "/")
        if normalized.startswith(("src/", "execution/")):
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
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            diagnostics.append(
                Diagnostic(path=rel, message=f"read failed: {exc}", kind="parse_error")
            )
            continue
        source_hashes[rel] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if suffix == ".py":
            try:
                tree = ast.parse(text, filename=rel)
            except SyntaxError as exc:
                diagnostics.append(
                    Diagnostic(path=rel, message=f"syntax error: {exc.msg}", kind="parse_error")
                )
                continue
            for item in ast.walk(tree):
                if isinstance(item, ast.Import):
                    for alias in item.names:
                        target, internal = _resolve_module(rel, alias.name, 0, module_index)
                        if target:
                            node(target, "python")
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
                        node(target, "python")
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
                    if fn in {"import_module", "__import__"}:
                        target = _literal_target(root, val, module_index) if val else None
                        if target:
                            node(target, "python")
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
                    elif fn in {"getattr"}:
                        add(
                            rel,
                            val or "<dynamic attribute>",
                            "getattr",
                            "getattr expression",
                            "low",
                            item.lineno,
                            True,
                        )
                    elif fn in {"run", "call", "check_call", "check_output", "Popen", "run_path"}:
                        arg = val or (
                            item.args[0].value
                            if item.args and isinstance(item.args[0], ast.Constant)
                            else None
                        )
                        normalized = (
                            _literal_target(root, arg, module_index)
                            if isinstance(arg, str)
                            else None
                        )
                        if normalized:
                            add(
                                rel,
                                normalized,
                                "python_entrypoint",
                                "subprocess/runpy literal",
                                "medium",
                                item.lineno,
                            )
                        else:
                            add(
                                rel,
                                "<dynamic process entrypoint>",
                                "unknown",
                                "subprocess/runpy expression",
                                "low",
                                item.lineno,
                                True,
                            )
                            if isinstance(arg, str) and arg.replace("\\", "/").startswith(
                                ("src/", "execution/")
                            ):
                                diagnostics.append(
                                    Diagnostic(
                                        path=rel,
                                        message=f"unresolved operational target {arg}",
                                        kind="unresolved",
                                    )
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
                )
        elif suffix == ".xml":
            try:
                xml = ET.fromstring(text.lstrip("\ufeff"))
            except ET.ParseError as exc:
                diagnostics.append(
                    Diagnostic(path=rel, message=f"XML parse error: {exc}", kind="parse_error")
                )
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
                        )
            if kind == "directive":
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
    unknown = [e for e in edges if e.unknown]
    roots = sorted(
        n
        for n in nodes
        if n.startswith(("execution/", "cron/", ".github/"))
        or nodes[n].kind in {"wrapper", "service"}
        or n in {"Makefile", "reconstruction_manifest.json"}
    )
    touched = touched or set()
    hold = bool(touched and any(d.path in touched for d in diagnostics))
    return ReachabilityGraph(
        parser={
            "name": "ast+xml+literal-scanner",
            "version": PARSER_VERSION,
            "python": sys.version.split()[0],
            "source_sha256": hashlib.sha256(
                "".join(f"{key}:{source_hashes[key]}\n" for key in sorted(source_hashes)).encode()
            ).hexdigest(),
        },
        nodes=sorted(nodes.values(), key=lambda n: n.id),
        edges=edges,
        roots=roots,
        unresolved=[d for d in diagnostics if d.kind == "unresolved"],
        diagnostics=diagnostics,
        unknown_edges=unknown,
        hold=hold,
        stats={
            "files": len(nodes),
            "edges": len(edges),
            "unknown": len(unknown),
            "diagnostics": len(diagnostics),
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, help="Write JSON here instead of stdout")
    parser.add_argument(
        "--touched",
        action="append",
        default=[],
        help="Touched path; repeatable, enables conservative HOLD",
    )
    args = parser.parse_args(argv)
    try:
        result = build_graph(args.repo_root, set(args.touched))
        payload = result.model_dump_json(indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
        else:
            sys.stdout.write(payload)
        return 2 if result.hold else 0
    except (OSError, TypeError, ValueError) as exc:
        sys.stderr.write(json.dumps({"error": type(exc).__name__, "message": str(exc)}) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
