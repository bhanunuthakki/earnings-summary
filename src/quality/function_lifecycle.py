"""Conservative function-level lifecycle inventory.

This module is an evidence collector, not a deletion tool.  A symbol is only
reported as a static candidate when the scanner can prove that it has no
static inbound references and no known exposure or dynamic hazard. Ambiguous
symbols are retained as ``unknown``. ``HOLD`` is reserved for an incomplete
scan, not the expected presence of symbol-level uncertainty.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "bha-109.v1"
PARSER_VERSION = "python-ast-3.11-v2"

Classification = Literal[
    "referenced",
    "unreferenced-static-candidate",
    "protected",
    "unknown",
]
Visibility = Literal["public", "private", "nested"]
SymbolKind = Literal["function", "method"]
CommandRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


class FunctionLifecycleError(RuntimeError):
    """Raised when the inventory cannot establish its evidence contract."""


class FunctionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    qualified_name: str
    kind: SymbolKind
    line: int
    end_line: int
    loc: int
    source_hash: str
    fingerprint: str
    decorators: tuple[str, ...] = ()
    visibility: Visibility
    inbound_static_refs: tuple[str, ...] = ()
    inbound_static_ref_count: int = 0
    dynamic_hazards: tuple[str, ...] = ()
    classification: Classification


class FunctionLifecycleInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["bha-109.v1"] = SCHEMA_VERSION
    parser_version: str = PARSER_VERSION
    status: Literal["PASS", "HOLD"]
    revision: str
    worktree_dirty: bool
    tracked_tree_hash: str
    inventory_hash: str
    files_scanned: int
    files_failed: tuple[str, ...] = ()
    symbols: tuple[FunctionRecord, ...] = ()
    symbol_count: int = 0
    candidate_symbols: tuple[FunctionRecord, ...] = ()
    unknown_symbols: tuple[FunctionRecord, ...] = ()
    unknown_total: int = 0
    unknown_hazard_counts: dict[str, int] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    violations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def evidence_reconciles(self) -> FunctionLifecycleInventory:
        expected_keys = {
            "referenced",
            "unreferenced-static-candidate",
            "protected",
            "unknown",
        }
        if set(self.counts) != expected_keys or any(value < 0 for value in self.counts.values()):
            raise ValueError("classification counts are incomplete or negative")
        if sum(self.counts.values()) != self.symbol_count:
            raise ValueError("classification counts do not equal the symbol count")
        if len(self.candidate_symbols) != self.counts["unreferenced-static-candidate"]:
            raise ValueError("candidate inventory does not equal the candidate count")
        if self.unknown_total != self.counts["unknown"]:
            raise ValueError("unknown total does not equal the unknown count")
        if any(
            item.classification != "unreferenced-static-candidate"
            for item in self.candidate_symbols
        ):
            raise ValueError("candidate inventory contains a non-candidate")
        if any(item.classification != "unknown" for item in self.unknown_symbols):
            raise ValueError("unknown sample contains a non-unknown symbol")
        if self.status == "PASS" and (self.files_failed or self.violations):
            raise ValueError("PASS inventory cannot contain scan failures")
        if self.status == "HOLD" and not (self.files_failed or self.violations):
            raise ValueError("HOLD inventory must explain the incomplete scan")
        return self


def _run(args: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=root, text=True, capture_output=True, check=False)


def _tracked(root: Path, runner: CommandRunner = _run) -> list[str]:
    result = runner(["git", "ls-files", "--", "*.py"], root)
    if result.returncode:
        raise FunctionLifecycleError(f"git ls-files failed ({result.returncode})")
    paths = sorted(
        {line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()}
    )
    missing = [path for path in paths if not (root / path).is_file()]
    if missing:
        raise FunctionLifecycleError(f"tracked Python file is missing: {missing[0]}")
    # Immutable migration history is intentionally outside the active code
    # denominator; current migrations remain active and are scanned.
    return [
        path
        for path in paths
        if not path.startswith(("alembic/versions/", "alembic/versions_archived/"))
    ]


def _revision(root: Path, runner: CommandRunner) -> tuple[str, bool]:
    result = runner(["git", "rev-parse", "HEAD"], root)
    if result.returncode or not result.stdout.strip():
        raise FunctionLifecycleError("cannot resolve git revision")
    dirty = runner(["git", "status", "--porcelain"], root)
    if dirty.returncode:
        raise FunctionLifecycleError("cannot resolve worktree state")
    return result.stdout.strip(), bool(dirty.stdout.strip())


def _tree_hash(root: Path, paths: Iterable[str]) -> str:
    digest = hashlib.sha256()
    digest.update(PARSER_VERSION.encode())
    for path in paths:
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256((root / path).read_bytes()).digest())
    return digest.hexdigest()


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _decorator_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ast.unparse(node) if hasattr(ast, "unparse") else "<dynamic>"


def _qualified(parent: tuple[str, ...], name: str) -> str:
    return ".".join((*parent, name))


def _source_fingerprint(
    source: str, node: ast.FunctionDef | ast.AsyncFunctionDef
) -> tuple[str, str, int]:
    segment = ast.get_source_segment(source, node) or ""
    source_hash = hashlib.sha256(segment.encode()).hexdigest()
    fingerprint = hashlib.sha256(
        f"{node.name}:{node.lineno}:{node.end_lineno}:{segment}".encode()
    ).hexdigest()
    return source_hash, fingerprint, (node.end_lineno or node.lineno) - node.lineno + 1


_HAZARD_WORDS = (
    "route",
    "command",
    "callback",
    "register",
    "fixture",
    "task",
    "signal",
    "event",
    "click",
    "before_request",
    "after_request",
    "teardown_request",
    "errorhandler",
    "override",
    "abstractmethod",
    "property",
    "cached_property",
)
_DYNAMIC_NAMES = {"getattr", "setattr", "globals", "locals", "eval", "exec", "__import__"}


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _string_values(node: ast.AST) -> set[str]:
    return {
        value.value
        for value in ast.walk(node)
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    }


def _dynamic_symbol_names(tree: ast.Module) -> set[str]:
    """Return exact symbol strings consumed by known reflection/export sites."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call = _call_name(node.func)
            if call in {"getattr", "setattr", "hasattr", "delattr"} and len(node.args) >= 2:
                candidate = node.args[1]
                if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
                    names.add(candidate.value)
            elif "register" in call.lower():
                names.update(_string_values(node))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is not None and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in targets
            ):
                names.update(_string_values(value))
    return names


def _has_unbounded_reflection(tree: ast.Module) -> bool:
    """Detect reflection whose target name cannot be resolved statically."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = _call_name(node.func)
        if call in {"getattr", "setattr", "hasattr", "delattr"}:
            if len(node.args) < 2 or not (
                isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
            ):
                return True
        elif call in {"eval", "exec", "globals", "locals"}:
            return True
    return False


class _Symbol:
    def __init__(
        self,
        path: str,
        module: str,
        parents: tuple[str, ...],
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        source: str,
        class_depth: int,
    ) -> None:
        self.path = path
        self.module = module
        self.parents = parents
        self.node = node
        self.source = source
        self.class_depth = class_depth
        self.qualified_name = f"{module}.{_qualified(parents, node.name)}"
        self.inbound: set[str] = set()
        self.hazards: set[str] = set()


def _collect(path: str, source: str) -> tuple[list[_Symbol], ast.Module]:
    tree = ast.parse(source, filename=path)
    module = path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[:-9]
    symbols: list[_Symbol] = []

    class Collector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.parents: tuple[str, ...] = ()
            self.class_depth = 0

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            symbols.append(_Symbol(path, module, self.parents, node, source, self.class_depth))
            previous = self.parents
            self.parents = (*self.parents, node.name)
            self.generic_visit(node)
            self.parents = previous

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            previous = self.parents
            self.parents = (*self.parents, node.name)
            self.class_depth += 1
            self.generic_visit(node)
            self.class_depth -= 1
            self.parents = previous

    Collector().visit(tree)
    return symbols, tree


def _aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = item.name
        elif isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = item.name
    return aliases


def _reference_sites(path: str, tree: ast.Module) -> dict[str, set[str]]:
    """Collect all name/attribute references before classifying any symbol."""
    aliases = _aliases(tree)
    sites: dict[str, set[str]] = defaultdict(set)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            sites[aliases.get(node.id, node.id)].add(f"{path}:{node.lineno}")
        elif isinstance(node, ast.Attribute):
            sites[node.attr].add(f"{path}:{node.lineno}")
    return sites


def _hazards(
    symbol: _Symbol,
    dynamic_symbol_names: set[str],
    *,
    module_has_unbounded_reflection: bool,
) -> set[str]:
    node = symbol.node
    hazards: set[str] = set()
    decorators = [_decorator_name(item).lower() for item in node.decorator_list]
    for decorator in decorators:
        if any(word in decorator for word in _HAZARD_WORDS):
            hazards.add(f"decorator:{decorator}")
    if symbol.class_depth:
        # Methods can be invoked by a framework, subclass, or reflection even
        # when no direct call is visible in this checkout.
        hazards.add("class-method-dispatch")
    if symbol.node.name.startswith("__") and symbol.node.name.endswith("__"):
        hazards.add("dunder-protocol")
    if node.name == "main" or (
        symbol.path.startswith(("execution/", "cron/", "scripts/")) and node.name.startswith("cli")
    ):
        hazards.add("cli-entrypoint")
    if not node.name.startswith("_") and not symbol.parents:
        hazards.add("public-module-symbol")
    if symbol.path.startswith("tests/") or symbol.path.endswith("/conftest.py"):
        hazards.add("test-fixture-or-helper")
    calls = {_call_name(call.func) for call in ast.walk(node) if isinstance(call, ast.Call)}
    for name in sorted(_DYNAMIC_NAMES.intersection(calls)):
        hazards.add(f"dynamic:{name}")
    if symbol.node.name in dynamic_symbol_names:
        hazards.add("string-or-reflection-reference")
    if module_has_unbounded_reflection and symbol.node.name.startswith("_"):
        hazards.add("dynamic:unbounded-reflection-in-module")
    return hazards


def _build_records(
    path: str,
    source: str,
    symbols: list[_Symbol],
    reference_sites: dict[str, set[str]],
    dynamic_symbol_names: set[str],
    *,
    module_has_unbounded_reflection: bool,
) -> list[FunctionRecord]:
    records: list[FunctionRecord] = []
    for symbol in symbols:
        symbol.inbound = set(reference_sites.get(symbol.node.name, ()))
        # Remove the defining name itself from inbound evidence.
        symbol.inbound = {
            item
            for item in symbol.inbound
            if not (item == f"{path}:{symbol.node.lineno}" and symbol.node.col_offset == 0)
        }
        hazards = _hazards(
            symbol,
            dynamic_symbol_names,
            module_has_unbounded_reflection=module_has_unbounded_reflection,
        )
        source_hash, fingerprint, loc = _source_fingerprint(source, symbol.node)
        inbound_count = len(symbol.inbound)
        inbound_refs = tuple(sorted(symbol.inbound)[:8])
        visibility: Visibility = (
            "nested"
            if symbol.class_depth == 0 and symbol.parents
            else ("public" if not symbol.node.name.startswith("_") else "private")
        )
        classification: Classification
        uncertain = any(hazard.startswith(("dynamic:", "string-or")) for hazard in hazards)
        if uncertain:
            classification = "unknown"
        elif hazards:
            classification = "protected" if not symbol.inbound else "referenced"
        elif symbol.inbound:
            classification = "referenced"
        else:
            classification = "unreferenced-static-candidate"
        records.append(
            FunctionRecord(
                path=path,
                qualified_name=symbol.qualified_name,
                kind="method" if symbol.class_depth else "function",
                line=symbol.node.lineno,
                end_line=symbol.node.end_lineno or symbol.node.lineno,
                loc=loc,
                source_hash=source_hash,
                fingerprint=fingerprint,
                decorators=tuple(_decorator_name(item) for item in symbol.node.decorator_list),
                visibility=visibility,
                inbound_static_refs=inbound_refs,
                inbound_static_ref_count=inbound_count,
                dynamic_hazards=tuple(sorted(hazards)),
                classification=classification,
            )
        )
    return records


def build_inventory(root: Path, runner: CommandRunner = _run) -> FunctionLifecycleInventory:
    root = root.resolve()
    paths = _tracked(root, runner)
    revision, dirty = _revision(root, runner)
    tree_hash = _tree_hash(root, paths)
    parsed: list[tuple[str, str, list[_Symbol], ast.Module]] = []
    failed: list[str] = []
    violations: list[str] = []
    for path in paths:
        source = (root / path).read_text(encoding="utf-8")
        try:
            symbols, tree = _collect(path, source)
        except (SyntaxError, UnicodeDecodeError) as exc:
            failed.append(path)
            violations.append(f"malformed AST: {path}: {exc}")
            continue
        parsed.append((path, source, symbols, tree))
    reference_sites: dict[str, set[str]] = defaultdict(set)
    for path, _, _, tree in parsed:
        for name, sites in _reference_sites(path, tree).items():
            reference_sites[name].update(sites)
    dynamic_symbol_names = {
        name for _, _, _, tree in parsed for name in _dynamic_symbol_names(tree)
    }
    records = [
        record
        for path, source, symbols, tree in parsed
        for record in _build_records(
            path,
            source,
            symbols,
            reference_sites,
            dynamic_symbol_names,
            module_has_unbounded_reflection=_has_unbounded_reflection(tree),
        )
    ]
    records = sorted(records, key=lambda item: (item.path, item.line, item.qualified_name))
    counts = {
        classification: sum(item.classification == classification for item in records)
        for classification in (
            "referenced",
            "unreferenced-static-candidate",
            "protected",
            "unknown",
        )
    }
    symbol_payload = [item.model_dump(mode="json") for item in records]
    inventory_hash = hashlib.sha256(
        json.dumps(symbol_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    status: Literal["PASS", "HOLD"] = "HOLD" if violations or failed else "PASS"
    unknown_symbols = tuple(item for item in records if item.classification == "unknown")
    candidate_symbols = tuple(
        item for item in records if item.classification == "unreferenced-static-candidate"
    )
    unknown_hazard_counts: dict[str, int] = defaultdict(int)
    for item in unknown_symbols:
        for hazard in item.dynamic_hazards:
            unknown_hazard_counts[hazard] += 1
    return FunctionLifecycleInventory(
        revision=revision,
        worktree_dirty=dirty,
        tracked_tree_hash=tree_hash,
        inventory_hash=inventory_hash,
        status=status,
        files_scanned=len(paths),
        symbol_count=len(records),
        candidate_symbols=candidate_symbols,
        unknown_symbols=unknown_symbols[:64],
        unknown_total=len(unknown_symbols),
        unknown_hazard_counts=dict(sorted(unknown_hazard_counts.items())),
        files_failed=tuple(failed),
        symbols=tuple(records),
        counts=counts,
        violations=tuple(violations),
    )


def validate_inventory(
    root: Path,
    persisted: FunctionLifecycleInventory,
    runner: CommandRunner = _run,
    *,
    current: FunctionLifecycleInventory | None = None,
) -> tuple[str, ...]:
    current = current or build_inventory(root, runner)
    violations = list(persisted.violations)
    if persisted.schema_version != SCHEMA_VERSION:
        violations.append("schema version is stale")
    if persisted.parser_version != PARSER_VERSION:
        violations.append("parser version is stale")
    # ``revision`` is generation provenance, not a freshness oracle: committing
    # this tracked JSON necessarily creates a newer commit. The tracked Python
    # tree and complete inventory hashes below are the non-cyclic authority.
    if persisted.tracked_tree_hash != current.tracked_tree_hash:
        violations.append("tracked tree hash changed")
    if persisted.files_scanned != current.files_scanned:
        violations.append("tracked file count changed")
    if persisted.inventory_hash != current.inventory_hash:
        violations.append("function inventory hash changed")
    if persisted.counts != current.counts:
        violations.append("function classification counts changed")
    if persisted.files_failed != current.files_failed:
        violations.append("failed-file set changed")
    if persisted.status != current.status:
        violations.append("scan status changed")
    if persisted.symbol_count != current.symbol_count:
        violations.append("symbol count changed")
    if persisted.candidate_symbols != current.candidate_symbols:
        violations.append("candidate inventory changed")
    if persisted.unknown_symbols != current.unknown_symbols:
        violations.append("unknown sample changed")
    if persisted.unknown_total != current.unknown_total:
        violations.append("unknown total changed")
    if persisted.unknown_hazard_counts != current.unknown_hazard_counts:
        violations.append("unknown hazard counts changed")
    if persisted.violations != current.violations:
        violations.append("scan violations changed")
    return tuple(sorted(set(violations)))


def load_inventory(path: Path) -> FunctionLifecycleInventory:
    return FunctionLifecycleInventory.model_validate_json(path.read_text(encoding="utf-8"))


def _receipt_payload(inventory: FunctionLifecycleInventory) -> dict[str, object]:
    """Return a compact tracked receipt while retaining exact rebuild identity."""
    return inventory.model_dump(mode="json", exclude={"symbols"})


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate", type=Path)
    args = parser.parse_args(argv)
    if args.output is None and args.validate is None:
        parser.error("at least one of --output or --validate is required")
    try:
        inventory = build_inventory(args.root)
        if args.validate:
            violations = validate_inventory(
                args.root,
                load_inventory(args.validate),
                current=inventory,
            )
            if violations:
                raise FunctionLifecycleError("; ".join(violations))
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(_receipt_payload(inventory), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    except (FunctionLifecycleError, OSError, ValueError) as exc:
        print(f"function lifecycle inventory failed: {exc}", file=sys.stderr)
        return 2
    return 0 if inventory.status == "PASS" else 1


__all__ = [
    "FunctionLifecycleError",
    "FunctionLifecycleInventory",
    "FunctionRecord",
    "build_inventory",
    "load_inventory",
    "main",
    "validate_inventory",
]
