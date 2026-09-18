"""Build a conservative, review-only inventory of active Python functions."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import platform
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from quality.atomic_write import write_text_atomic
from quality.git_env import clean_local_git_env

SCHEMA_VERSION = "function-candidate-inventory/v1"
MAX_STDOUT_BYTES = 100_000
ACTIVE_ROOTS = ("src/", "execution/", "cron/", "scripts/", ".github/scripts/")
TEST_ROOTS = ("tests/", "instruction_tests/")
EXCLUDED_ROOTS = ("alembic/versions/", "alembic/versions_archived/", "scratch/")
REGISTRY_NAMES = ("REGISTRY", "HOOKS", "CALLBACKS", "HANDLERS", "DISPATCH")
DYNAMIC_CALLS = {"eval", "exec", "getattr", "globals", "locals", "vars"}

Disposition = Literal["referenced", "protected", "unknown", "candidate"]
Surface = Literal["runtime", "tooling", "test"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FunctionEntry(StrictModel):
    path: str
    qualified_name: str
    line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    surface: Surface
    disposition: Disposition
    reasons: tuple[str, ...]
    static_reference_count: int = Field(ge=0)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class FunctionInventory(StrictModel):
    schema_version: Literal["function-candidate-inventory/v1"] = SCHEMA_VERSION
    subject_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scanner_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    python_version: str
    deletion_authority: Literal[False] = False
    files_scanned: int = Field(ge=0)
    functions_scanned: int = Field(ge=0)
    counts: dict[str, int]
    definitions: dict[str, str]
    entries: tuple[FunctionEntry, ...]
    parse_errors: tuple[str, ...]
    status: Literal["PASS", "HOLD"]
    violations: tuple[str, ...]


class FunctionInventoryError(RuntimeError):
    """The inventory population could not be collected safely."""


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            env=clean_local_git_env(),
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FunctionInventoryError(f"git {' '.join(args)} failed") from exc


def _tracked_sources(root: Path) -> list[tuple[str, bytes]]:
    raw = _git(root, "ls-files", "-z", "--", "*.py")
    paths = sorted(path for path in raw.decode().split("\0") if path)
    selected = [
        path
        for path in paths
        if path.startswith((*ACTIVE_ROOTS, *TEST_ROOTS)) and not path.startswith(EXCLUDED_ROOTS)
    ]
    sources: list[tuple[str, bytes]] = []
    resolved_root = root.resolve()
    for relative in selected:
        if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
            raise FunctionInventoryError(f"tracked Python path escapes repository: {relative}")
        path = (root / relative).resolve()
        if not path.is_relative_to(resolved_root) or not path.is_file() or path.is_symlink():
            raise FunctionInventoryError(f"tracked Python file is missing or unsafe: {relative}")
        try:
            sources.append((relative, path.read_bytes()))
        except OSError as exc:
            raise FunctionInventoryError(f"tracked Python file is unreadable: {relative}") from exc
    return sources


def _surface(path: str) -> Surface:
    if path.startswith(TEST_ROOTS):
        return "test"
    if path.startswith("src/"):
        return "runtime"
    return "tooling"


def _module_name(path: str) -> str:
    value = path[:-3].replace("/", ".")
    if value.startswith("src."):
        value = value[4:]
    if value.endswith(".__init__"):
        value = value[: -len(".__init__")]
    return value


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _decorator_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
    targets: Iterable[ast.expr]
    targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
    return tuple(target.id for target in targets if isinstance(target, ast.Name))


class _ModuleFacts(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.parents: list[str] = []
        self.class_bases: list[bool] = []
        self.functions: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str, bool]] = []
        self.references: Counter[str] = Counter()
        self.callback_names: set[str] = set()
        self.registry_names: set[str] = set()
        self.reflection_names: set[str] = set()
        self.unresolved_reflection_modules: set[str] = set()
        self.unresolved_external_reflection = False
        self.unresolved_dynamic = False
        self.exports: set[str] = set()
        self.imported_names: set[tuple[str, str]] = set()
        self.imported_modules: dict[str, str] = {}
        self.star_imports: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualified = ".".join((*self.parents, node.name))
        self.functions.append((node, qualified, bool(self.class_bases and self.class_bases[-1])))
        self.parents.append(node.name)
        self.generic_visit(node)
        self.parents.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.parents.append(node.name)
        self.class_bases.append(bool(node.bases))
        self.generic_visit(node)
        self.class_bases.pop()
        self.parents.pop()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.references[node.id] += 1

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self.references[node.attr] += 1
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _decorator_name(node.func)
        dynamic_name = name.rsplit(".", 1)[-1]
        if dynamic_name == "getattr":
            attribute = node.args[1] if len(node.args) > 1 else None
            if (
                isinstance(attribute, ast.Constant)
                and isinstance(attribute.value, str)
                and attribute.value.isidentifier()
            ):
                self.reflection_names.add(attribute.value)
            else:
                target = node.args[0] if node.args else None
                target_module = self._imported_module_for_expr(target)
                if target_module is None:
                    self.unresolved_dynamic = True
                    self.unresolved_external_reflection = True
                else:
                    self.unresolved_reflection_modules.add(target_module)
        elif dynamic_name in DYNAMIC_CALLS or name.endswith("import_module"):
            literal_names = {
                child.value
                for child in (*node.args, *(keyword.value for keyword in node.keywords))
                if isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and child.value.isidentifier()
            }
            self.reflection_names.update(literal_names)
            if not literal_names:
                self.unresolved_dynamic = True
        for arg in node.args:
            if isinstance(arg, ast.Name):
                self.callback_names.add(arg.id)
        for keyword in node.keywords:
            if isinstance(keyword.value, ast.Name):
                self.callback_names.add(keyword.value.id)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._visit_assignment(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._visit_assignment(node)

    def _visit_assignment(self, node: ast.Assign | ast.AnnAssign) -> None:
        names = _assigned_names(node)
        value = node.value
        imported_module = self._imported_module_for_expr(value)
        if imported_module is not None:
            for name in names:
                self.imported_modules[name] = imported_module
        if "__all__" in names and isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            self.exports.update(
                item.value
                for item in value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
        if any(any(marker in name for marker in REGISTRY_NAMES) for name in names) and value:
            self.registry_names.update(
                child.id
                for child in ast.walk(value)
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = self._resolved_import_from_module(node)
        for alias in node.names:
            if alias.name == "*":
                self.star_imports.add(module)
            else:
                self.imported_names.add((module, alias.name))
                if module:
                    self.imported_modules[alias.asname or alias.name] = f"{module}.{alias.name}"

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local_name = alias.asname or alias.name.split(".", 1)[0]
            self.imported_modules[local_name] = alias.name

    def _resolved_import_from_module(self, node: ast.ImportFrom) -> str:
        if node.level == 0:
            return node.module or ""
        current = _module_name(self.path).split(".")
        package = current if self.path.endswith("/__init__.py") else current[:-1]
        keep = max(0, len(package) - (node.level - 1))
        prefix = package[:keep]
        if node.module:
            prefix.extend(node.module.split("."))
        return ".".join(prefix)

    def _imported_module_for_expr(self, node: ast.expr | None) -> str | None:
        if node is None:
            return None
        dotted = _decorator_name(node)
        if not dotted:
            return None
        root, _, suffix = dotted.partition(".")
        imported = self.imported_modules.get(root)
        if imported is None:
            return None
        if not suffix or dotted == imported:
            return imported
        if imported == root:
            return f"{imported}.{suffix}"
        return None


def _hazards(
    facts: _ModuleFacts,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    qualified_name: str,
    subclass_method: bool,
    imported_names: set[tuple[str, str]],
    star_imports: set[str],
) -> set[str]:
    name = node.name
    reasons: set[str] = set()
    decorators = {_decorator_name(item) for item in node.decorator_list}
    module = _module_name(facts.path)
    top_level = "." not in qualified_name
    if name in {"main", "_main"} or name.startswith(("test_", "pytest_")):
        reasons.add("entrypoint")
    if any(
        value.endswith((".route", ".command", ".get", ".post", ".put", ".patch"))
        for value in decorators
    ):
        reasons.add("entrypoint")
    if any(value.endswith("fixture") or value.endswith("parametrize") for value in decorators):
        reasons.add("fixture")
    if decorators:
        reasons.add("decorator")
    if subclass_method or (name.startswith("__") and name.endswith("__")):
        reasons.add("override")
    if name in facts.callback_names:
        reasons.add("callback")
    if name in facts.registry_names:
        reasons.add("registry")
    if name in facts.exports or (
        facts.path.endswith("/__init__.py") and top_level and not name.startswith("_")
    ):
        reasons.add("export")
    if "." in qualified_name and not name.startswith("_"):
        reasons.add("public-method")
    if (module, name) in imported_names or any(imported == name for _, imported in imported_names):
        reasons.add("import")
    if module in star_imports and top_level and not name.startswith("_"):
        reasons.add("import-star")
    return reasons


def _module_matches(module: str, target: str) -> bool:
    return (
        module == target
        or module.startswith(f"{target}.")
        or target.startswith(f"{module}.")
        or module.endswith(f".{target}")
        or target.endswith(f".{module}")
    )


def _safe_output_path(root: Path, requested: Path) -> Path:
    resolved_root = root.resolve()
    lexical = requested if requested.is_absolute() else resolved_root / requested
    declared_tmp = resolved_root / ".tmp"
    if declared_tmp.is_symlink():
        raise FunctionInventoryError("repository .tmp directory cannot be a symlink")
    try:
        resolved_tmp = declared_tmp.resolve()
        resolved_output = lexical.resolve()
    except OSError as exc:
        raise FunctionInventoryError("output path cannot be resolved safely") from exc
    if not resolved_tmp.is_relative_to(resolved_root) or not resolved_output.is_relative_to(
        resolved_tmp
    ):
        raise FunctionInventoryError("output must remain under the repository .tmp directory")
    try:
        if lexical.exists() and lexical.stat().st_nlink > 1:
            raise FunctionInventoryError("output aliases another file")
    except OSError as exc:
        raise FunctionInventoryError("output path cannot be inspected safely") from exc
    return resolved_output


def _source_manifest_hash(sources: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for path, raw in sources:
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(raw).digest())
    return digest.hexdigest()


def build_inventory(root: Path) -> FunctionInventory:
    root = root.resolve()
    sources = _tracked_sources(root)
    facts_by_path: dict[str, _ModuleFacts] = {}
    parse_errors: list[str] = []
    imported_names: set[tuple[str, str]] = set()
    star_imports: set[str] = set()
    global_reflection_names: set[str] = set()
    unresolved_reflection_modules: set[str] = set()
    unresolved_external_reflection = False
    global_references: Counter[str] = Counter()
    for path, raw in sources:
        try:
            text = raw.decode("utf-8-sig")
            tree = ast.parse(text, filename=path)
        except (SyntaxError, UnicodeDecodeError) as exc:
            line = exc.lineno if isinstance(exc, SyntaxError) else 1
            parse_errors.append(f"{path}:{line}: parse failed")
            continue
        facts = _ModuleFacts(path)
        facts.visit(tree)
        facts_by_path[path] = facts
        imported_names.update(facts.imported_names)
        star_imports.update(facts.star_imports)
        global_reflection_names.update(facts.reflection_names)
        unresolved_reflection_modules.update(facts.unresolved_reflection_modules)
        unresolved_external_reflection = (
            unresolved_external_reflection or facts.unresolved_external_reflection
        )
        global_references.update(facts.references)

    entries: list[FunctionEntry] = []
    for path, facts in sorted(facts_by_path.items()):
        for node, qualified_name, subclass_method in facts.functions:
            reasons = _hazards(
                facts,
                node,
                qualified_name,
                subclass_method,
                imported_names,
                star_imports,
            )
            references = global_references[node.name]
            if node.name in global_reflection_names:
                reasons.add("reflection")
            if reasons:
                disposition: Disposition = "protected"
            elif references:
                disposition = "referenced"
                reasons.add("static-reference")
            elif (
                facts.unresolved_dynamic
                or unresolved_external_reflection
                or any(
                    _module_matches(_module_name(path), target)
                    for target in unresolved_reflection_modules
                )
            ):
                disposition = "unknown"
                reasons.add("unresolved-dynamic-reflection")
            else:
                disposition = "candidate"
                reasons.add("no-static-reference")
            fingerprint = hashlib.sha256(
                f"{path}:{node.lineno}:{qualified_name}".encode()
            ).hexdigest()
            entries.append(
                FunctionEntry(
                    path=path,
                    qualified_name=qualified_name,
                    line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    surface=_surface(path),
                    disposition=disposition,
                    reasons=tuple(sorted(reasons)),
                    static_reference_count=references,
                    fingerprint=fingerprint,
                )
            )
    entries.sort(key=lambda item: (item.path, item.line, item.qualified_name))
    counts = Counter(entry.disposition for entry in entries)
    violations = tuple(parse_errors)
    scanner_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    commit = _git(root, "rev-parse", "HEAD").decode().strip()
    return FunctionInventory(
        subject_commit=commit,
        source_manifest_sha256=_source_manifest_hash(sources),
        scanner_sha256=scanner_sha,
        python_version=platform.python_version(),
        files_scanned=len(sources),
        functions_scanned=len(entries),
        counts={
            key: counts.get(key, 0) for key in ("referenced", "protected", "unknown", "candidate")
        },
        definitions={
            "referenced": "A same-name static load exists in the tracked active population.",
            "protected": "A framework, registry, callback, override, export, fixture, reflection, or import hazard exists.",
            "unknown": "Dynamic behavior prevents a conservative static disposition.",
            "candidate": "No static reference or protected hazard was found; review is required and deletion is not authorized.",
        },
        entries=tuple(entries),
        parse_errors=tuple(parse_errors),
        status="HOLD" if violations else "PASS",
        violations=violations,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        inventory = build_inventory(args.repo_root)
        payload = inventory.model_dump_json(indent=2) + "\n"
        if args.output is not None:
            output = _safe_output_path(args.repo_root, args.output)
            write_text_atomic(output, payload)
            print(
                json.dumps(
                    {
                        "output": str(args.output),
                        "status": inventory.status,
                        "counts": inventory.counts,
                    },
                    sort_keys=True,
                )
            )
        elif len(payload.encode()) > MAX_STDOUT_BYTES:
            output = _safe_output_path(
                args.repo_root, Path(".tmp/quality/function-candidate-inventory.json")
            )
            write_text_atomic(output, payload)
            print(
                json.dumps(
                    {
                        "output": str(output.relative_to(args.repo_root.resolve())),
                        "status": inventory.status,
                        "counts": inventory.counts,
                    },
                    sort_keys=True,
                )
            )
        else:
            sys.stdout.write(payload)
        return 0 if inventory.status == "PASS" else 2
    except (FunctionInventoryError, OSError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
