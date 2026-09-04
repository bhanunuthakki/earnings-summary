"""Measure the frozen 9+ architecture contract and score evidence without discretion.

The scanner operates only on git-tracked Python modules under ``src/`` and
``execution/``.  It can read the working tree or an exact git revision, making
baseline receipts reproducible even after the tree changes.  Architecture is
computed here; the remaining score blocks consume explicit evidence states.
Missing evidence is ``HOLD`` and never silently becomes partial credit.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import subprocess
import sys
import tokenize
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCHEMA_VERSION = "quality-score-v1"
SOURCE_ROOTS = ("src/", "execution/")
COMPOSITION_ROOTS = {
    "execution/comments_server.py": 600,
    "src/pipeline/portfolio_panel.py": 200,
}
DECLARATIVE_EXCEPTION_CAP = 3


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LineCounts(StrictModel):
    physical: int = Field(ge=0)
    nonblank: int = Field(ge=0)
    noncomment: int = Field(ge=0)


class ModuleMetric(StrictModel):
    path: str
    module: str
    lines: LineCounts
    internal_fan_out: int = Field(ge=0)
    responsibilities: tuple[str, ...]
    public_functions: int = Field(ge=0)
    fully_annotated_public_functions: int = Field(ge=0)


class ArchitectureMetrics(StrictModel):
    executable_modules: int = Field(ge=0)
    total_noncomment_loc: int = Field(ge=0)
    modules_over_1000_loc: int = Field(ge=0)
    modules_over_2000_loc: int = Field(ge=0)
    modules_at_least_3000_loc: int = Field(ge=0)
    max_internal_fan_out: int = Field(ge=0)
    scc_count: int = Field(ge=0)
    scc_module_count: int = Field(ge=0)
    largest_scc: int = Field(ge=0)
    composition_root_loc: dict[str, int]
    composition_root_fan_out: dict[str, int]
    facade_violations: tuple[str, ...]
    modules: tuple[ModuleMetric, ...]
    strongly_connected_components: tuple[tuple[str, ...], ...]


class ArchitectureReceipt(StrictModel):
    schema_version: Literal["quality-score-v1"] = SCHEMA_VERSION
    scoped_revision: str
    scoped_commit: str
    scanner_sha256: str
    source_sha256: str
    python_version: str
    ast_version: str
    source_roots: tuple[str, ...] = SOURCE_ROOTS
    definitions: dict[str, str]
    metrics: ArchitectureMetrics


EvidenceState = Literal["pass", "fail", "missing"]


class EvidenceEntry(StrictModel):
    state: EvidenceState
    receipt: str | None = None
    note: str | None = None


class ScoreEvidence(StrictModel):
    schema_version: Literal["quality-score-evidence-v1"]
    scoped_commit: str
    blocks: dict[str, EvidenceEntry]
    hard_gates: dict[str, EvidenceEntry]


class ScoreBlock(StrictModel):
    key: str
    label: str
    points: int = Field(gt=0)
    state: EvidenceState
    awarded: int = Field(ge=0)
    reason: str


class QualityScoreReceipt(StrictModel):
    schema_version: Literal["quality-score-result-v1"]
    scoped_commit: str
    score_points: int = Field(ge=0, le=100)
    score_out_of_ten: str
    verdict: Literal["PASS", "FAIL", "HOLD"]
    blocks: tuple[ScoreBlock, ...]
    hard_gate_failures: tuple[str, ...]
    hard_gate_missing: tuple[str, ...]
    architecture_regressions: tuple[str, ...]
    architecture: ArchitectureReceipt

    @model_validator(mode="after")
    def points_match_blocks(self) -> QualityScoreReceipt:
        if sum(block.points for block in self.blocks) != 100:
            raise ValueError("score block weights must total exactly 100")
        if sum(block.awarded for block in self.blocks) != self.score_points:
            raise ValueError("awarded block points do not match score_points")
        return self


class ArchitectureRatchetReceipt(StrictModel):
    schema_version: Literal["architecture-ratchet-v1"]
    status: Literal["PASS", "FAIL", "HOLD"]
    scoped_commit: str
    baseline_commit: str
    current_source_sha256: str
    baseline_source_sha256: str
    regressions: tuple[str, ...]
    scanner_mismatch: bool


SCORE_BLOCKS: tuple[tuple[str, str, int], ...] = (
    ("elegance.cycles", "Elegance: cycles", 8),
    ("elegance.composition_roots", "Elegance: composition roots", 6),
    ("elegance.module_shape", "Elegance: module shape", 6),
    ("elegance.cohesive_typed_facades", "Elegance: cohesive typed facades", 5),
    ("maintainability.static_quality", "Maintainability: static quality", 8),
    ("maintainability.duplication", "Maintainability: duplication", 6),
    ("maintainability.authorities", "Maintainability: authorities", 5),
    ("maintainability.sustainable_tests", "Maintainability: sustainable tests", 3),
    ("maintainability.enforced_ratchets", "Maintainability: enforced ratchets", 3),
    ("efficiency.integrity_audit", "Efficiency: integrity audit", 10),
    ("efficiency.request_path", "Efficiency: request path", 6),
    ("efficiency.test_ci", "Efficiency: test/CI", 6),
    ("efficiency.dcf_disposition", "Efficiency: DCF disposition", 3),
    ("cleanup.lifecycle_inventory", "Cleanup: lifecycle inventory", 8),
    ("cleanup.reachability_oracle", "Cleanup: reachability oracle", 6),
    ("cleanup.deletion_proof", "Cleanup: deletion proof", 5),
    ("cleanup.schema_ownership", "Cleanup: schema ownership", 3),
    ("cleanup.reconstructability", "Cleanup: reconstructability", 3),
)
HARD_GATES: tuple[str, ...] = (
    "repository_gates",
    "active_static_zero",
    "compatibility_parity",
    "benchmark_contract",
    "database_authority",
    "deletion_evidence",
    "network_consolidation_safety",
    "owner_acceptance",
    "architecture_duplication_ratchets",
    "touched_reachability_closure",
)


def _run(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        args,
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({args[0]} exit {result.returncode})")
    return result.stdout


def _tracked_python_paths(repo_root: Path, revision: str) -> tuple[str, ...]:
    if revision == "WORKTREE":
        raw = _run(repo_root, "git", "ls-files", "--", "src", "execution")
    else:
        raw = _run(repo_root, "git", "ls-tree", "-r", "--name-only", revision, "src", "execution")
    paths = {
        line.strip()
        for line in raw.splitlines()
        if line.strip().endswith(".py") and line.strip().startswith(SOURCE_ROOTS)
    }
    return tuple(sorted(paths))


def _read_source(repo_root: Path, revision: str, path: str) -> str:
    if revision == "WORKTREE":
        return (repo_root / path).read_text(encoding="utf-8")
    return _run(repo_root, "git", "show", f"{revision}:{path}")


def _module_name(path: str) -> str:
    posix = PurePosixPath(path)
    parts = list(posix.parts[1:]) if posix.parts[0] == "src" else ["execution", *posix.parts[1:]]
    parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _line_counts(source: str) -> LineCounts:
    lines = source.splitlines()
    comment_lines: set[int] = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if (
                token.type == tokenize.COMMENT
                and not lines[token.start[0] - 1][: token.start[1]].strip()
            ):
                comment_lines.add(token.start[0])
    except (IndentationError, tokenize.TokenError) as exc:
        raise ValueError(f"tokenization failed: {exc}") from exc
    nonblank = sum(bool(line.strip()) for line in lines)
    noncomment = sum(
        bool(line.strip()) and index not in comment_lines
        for index, line in enumerate(lines, start=1)
    )
    return LineCounts(physical=len(lines), nonblank=nonblank, noncomment=noncomment)


def _resolve_imports(
    tree: ast.Module, source_module: str, is_package: bool, known_modules: set[str]
) -> set[str]:
    targets: set[str] = set()
    package_parts = source_module.split(".") if is_package else source_module.split(".")[:-1]

    def add_candidate(candidate: str) -> None:
        parts = candidate.split(".")
        for end in range(len(parts), 0, -1):
            possible = ".".join(parts[:end])
            if possible in known_modules:
                targets.add(possible)
                return

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add_candidate(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = max(0, len(package_parts) - node.level + 1)
                prefix = package_parts[:keep]
            else:
                prefix = []
            base = ".".join([*prefix, *(node.module or "").split(".")]).strip(".")
            for alias in node.names:
                if alias.name != "*":
                    add_candidate(".".join(part for part in (base, alias.name) if part))
            if base:
                add_candidate(base)
    targets.discard(source_module)
    return targets


def _responsibilities(tree: ast.Module) -> tuple[str, ...]:
    flags: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            if any(name.startswith(("requests", "httpx", "urllib", "aiohttp")) for name in names):
                flags.add("network")
            if any(name.startswith(("sqlite3", "sqlalchemy")) for name in names):
                flags.add("database")
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name.split(".")[-1] in {"execute", "executemany", "commit", "connect"}:
                flags.add("database")
            if name.split(".")[-1] in {"get", "post", "put", "delete", "urlopen", "request"}:
                flags.add("network")
            if name.split(".")[-1] in {
                "open",
                "read_text",
                "write_text",
                "read_bytes",
                "write_bytes",
            }:
                flags.add("filesystem")
            if name.split(".")[-1] in {"run", "Popen", "check_call", "check_output"}:
                flags.add("process")
    return tuple(sorted(flags))


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _public_annotation_counts(tree: ast.Module) -> tuple[int, int]:
    public = 0
    annotated = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith(
            "_"
        ):
            public += 1
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            if (
                node.returns is not None
                and all(argument.annotation is not None for argument in arguments)
                and (node.args.vararg is None or node.args.vararg.annotation is not None)
                and (node.args.kwarg is None or node.args.kwarg.annotation is not None)
            ):
                annotated += 1
    return public, annotated


def _strong_components(edges: dict[str, set[str]]) -> tuple[tuple[str, ...], ...]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(edges[node]):
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])
        if lowlinks[node] == indexes[node]:
            component: list[str] = []
            while stack:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                components.append(tuple(sorted(component)))

    for node in sorted(edges):
        if node not in indexes:
            visit(node)
    return tuple(sorted(components, key=lambda value: (-len(value), value)))


def analyze_sources(sources: dict[str, str]) -> ArchitectureMetrics:
    module_by_path = {path: _module_name(path) for path in sources}
    path_by_module = {module: path for path, module in module_by_path.items()}
    known_modules = set(path_by_module)
    trees: dict[str, ast.Module] = {}
    lines: dict[str, LineCounts] = {}
    edges: dict[str, set[str]] = {module: set() for module in known_modules}
    module_metrics: list[ModuleMetric] = []
    for path in sorted(sources):
        try:
            tree = ast.parse(sources[path], filename=path)
        except SyntaxError as exc:
            raise ValueError(f"cannot parse {path}: {exc.msg} at line {exc.lineno}") from exc
        trees[path] = tree
        lines[path] = _line_counts(sources[path])
        module = module_by_path[path]
        edges[module] = _resolve_imports(tree, module, path.endswith("/__init__.py"), known_modules)

    facade_violations: list[str] = []
    for path in sorted(sources):
        module = module_by_path[path]
        public, annotated = _public_annotation_counts(trees[path])
        responsibilities = _responsibilities(trees[path])
        is_facade = path.endswith("_facade.py") or path in COMPOSITION_ROOTS
        if is_facade and responsibilities:
            facade_violations.append(f"{path}: side-effect responsibilities {responsibilities}")
        if is_facade and public != annotated:
            facade_violations.append(f"{path}: {public - annotated} unannotated public functions")
        module_metrics.append(
            ModuleMetric(
                path=path,
                module=module,
                lines=lines[path],
                internal_fan_out=len(edges[module]),
                responsibilities=responsibilities,
                public_functions=public,
                fully_annotated_public_functions=annotated,
            )
        )

    components = _strong_components(edges)
    large_1000 = sum(metric.lines.noncomment > 1000 for metric in module_metrics)
    large_2000 = sum(metric.lines.noncomment > 2000 for metric in module_metrics)
    at_least_3000 = sum(metric.lines.noncomment >= 3000 for metric in module_metrics)
    root_loc = {path: lines[path].noncomment if path in lines else -1 for path in COMPOSITION_ROOTS}
    root_fanout = {
        path: len(edges[module_by_path[path]]) if path in module_by_path else -1
        for path in COMPOSITION_ROOTS
    }
    return ArchitectureMetrics(
        executable_modules=len(module_metrics),
        total_noncomment_loc=sum(metric.lines.noncomment for metric in module_metrics),
        modules_over_1000_loc=large_1000,
        modules_over_2000_loc=large_2000,
        modules_at_least_3000_loc=at_least_3000,
        max_internal_fan_out=max((len(targets) for targets in edges.values()), default=0),
        scc_count=len(components),
        scc_module_count=sum(len(component) for component in components),
        largest_scc=max((len(component) for component in components), default=0),
        composition_root_loc=root_loc,
        composition_root_fan_out=root_fanout,
        facade_violations=tuple(facade_violations),
        modules=tuple(module_metrics),
        strongly_connected_components=components,
    )


def build_architecture_receipt(repo_root: Path, revision: str) -> ArchitectureReceipt:
    paths = _tracked_python_paths(repo_root, revision)
    sources = {path: _read_source(repo_root, revision, path) for path in paths}
    source_hasher = hashlib.sha256()
    for path, source in sources.items():
        source_hasher.update(path.encode("utf-8"))
        source_hasher.update(b"\0")
        source_hasher.update(source.encode("utf-8"))
    scoped_commit = _run(
        repo_root, "git", "rev-parse", "HEAD" if revision == "WORKTREE" else revision
    ).strip()
    scanner_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return ArchitectureReceipt(
        scoped_revision=revision,
        scoped_commit=scoped_commit,
        scanner_sha256=scanner_hash,
        source_sha256=source_hasher.hexdigest(),
        python_version=sys.version.split()[0],
        ast_version=f"python-{sys.version_info.major}.{sys.version_info.minor}",
        definitions={
            "executable_module": "git-tracked .py file under src/ or execution/",
            "physical_loc": "splitlines count; terminal newline adds no line",
            "nonblank_loc": "physical line containing at least one non-whitespace character",
            "noncomment_loc": "nonblank physical line that does not contain a COMMENT token",
            "internal_edge": "resolved AST Import/ImportFrom target in the executable-module set",
            "scc": "Tarjan component with at least two internal modules",
            "fan_out": "unique resolved internal module targets per source module",
            "facade": "*_facade.py plus frozen composition-root facade paths",
            "cohesion": "facade has no database/network/filesystem/process responsibility and all public functions are fully annotated",
        },
        metrics=analyze_sources(sources),
    )


def _architecture_states(metrics: ArchitectureMetrics) -> dict[str, tuple[EvidenceState, str]]:
    cycle_ok = metrics.scc_count <= 3 and metrics.largest_scc <= 4
    composition_ok = metrics.max_internal_fan_out <= 25 and all(
        metrics.composition_root_loc.get(path, -1) <= limit
        and metrics.composition_root_loc.get(path, -1) >= 0
        for path, limit in COMPOSITION_ROOTS.items()
    )
    module_shape_ok = (
        metrics.modules_over_1000_loc <= 35
        and metrics.modules_at_least_3000_loc <= DECLARATIVE_EXCEPTION_CAP
    )
    facade_ok = not metrics.facade_violations
    return {
        "elegance.cycles": ("pass" if cycle_ok else "fail", "frozen SCC caps"),
        "elegance.composition_roots": (
            "pass" if composition_ok else "fail",
            "fan-out and composition-root LOC caps",
        ),
        "elegance.module_shape": ("pass" if module_shape_ok else "fail", "large-module caps"),
        "elegance.cohesive_typed_facades": (
            "pass" if facade_ok else "fail",
            "facade responsibility and annotation checks",
        ),
    }


def architecture_regressions(
    current: ArchitectureMetrics, baseline: ArchitectureMetrics
) -> tuple[str, ...]:
    comparisons = {
        "total_noncomment_loc": (current.total_noncomment_loc, baseline.total_noncomment_loc),
        "modules_over_1000_loc": (current.modules_over_1000_loc, baseline.modules_over_1000_loc),
        "modules_over_2000_loc": (current.modules_over_2000_loc, baseline.modules_over_2000_loc),
        "modules_at_least_3000_loc": (
            current.modules_at_least_3000_loc,
            baseline.modules_at_least_3000_loc,
        ),
        "max_internal_fan_out": (current.max_internal_fan_out, baseline.max_internal_fan_out),
        "scc_count": (current.scc_count, baseline.scc_count),
        "scc_module_count": (current.scc_module_count, baseline.scc_module_count),
        "largest_scc": (current.largest_scc, baseline.largest_scc),
        "facade_violations": (len(current.facade_violations), len(baseline.facade_violations)),
    }
    return tuple(
        f"{key} increased from {before} to {after}"
        for key, (after, before) in comparisons.items()
        if after > before
    )


def score_quality(
    architecture: ArchitectureReceipt,
    evidence: ScoreEvidence | None,
    baseline: ArchitectureReceipt | None,
) -> QualityScoreReceipt:
    architecture_states = _architecture_states(architecture.metrics)
    blocks: list[ScoreBlock] = []
    for key, label, points in SCORE_BLOCKS:
        if key in architecture_states:
            state, reason = architecture_states[key]
        elif evidence is None or key not in evidence.blocks:
            state, reason = "missing", "required evidence entry is absent"
        else:
            entry = evidence.blocks[key]
            state = entry.state
            reason = entry.note or entry.receipt or "explicit evidence state"
        blocks.append(
            ScoreBlock(
                key=key,
                label=label,
                points=points,
                state=state,
                awarded=points if state == "pass" else 0,
                reason=reason,
            )
        )

    hard_failures: list[str] = []
    hard_missing: list[str] = []
    if evidence is None:
        hard_missing.append("all hard-gate evidence")
    else:
        if evidence.scoped_commit != architecture.scoped_commit:
            hard_failures.append("evidence commit differs from architecture commit")
        known_block_keys = {key for key, _label, _points in SCORE_BLOCKS}
        unknown_blocks = sorted(set(evidence.blocks) - known_block_keys)
        if unknown_blocks:
            hard_failures.append("unknown score blocks: " + ", ".join(unknown_blocks))
        unknown_gates = sorted(set(evidence.hard_gates) - set(HARD_GATES))
        if unknown_gates:
            hard_failures.append("unknown hard gates: " + ", ".join(unknown_gates))
        for key in HARD_GATES:
            entry = evidence.hard_gates.get(key)
            if entry is None:
                hard_missing.append(key)
                continue
            if entry.state == "fail":
                hard_failures.append(key)
            elif entry.state == "missing":
                hard_missing.append(key)
    regressions = (
        architecture_regressions(architecture.metrics, baseline.metrics) if baseline else ()
    )
    points = sum(block.awarded for block in blocks)
    missing = any(block.state == "missing" for block in blocks) or bool(hard_missing)
    failed = (
        any(block.state == "fail" for block in blocks) or bool(hard_failures) or bool(regressions)
    )
    verdict: Literal["PASS", "FAIL", "HOLD"]
    if missing:
        verdict = "HOLD"
    elif failed or points < 90:
        verdict = "FAIL"
    else:
        verdict = "PASS"
    return QualityScoreReceipt(
        schema_version="quality-score-result-v1",
        scoped_commit=architecture.scoped_commit,
        score_points=points,
        score_out_of_ten=f"{points // 10}.{points % 10}",
        verdict=verdict,
        blocks=tuple(blocks),
        hard_gate_failures=tuple(hard_failures),
        hard_gate_missing=tuple(hard_missing),
        architecture_regressions=regressions,
        architecture=architecture,
    )


def _load_architecture(path: Path) -> ArchitectureReceipt:
    return ArchitectureReceipt.model_validate_json(path.read_text(encoding="utf-8"))


def _load_evidence(path: Path) -> ScoreEvidence:
    return ScoreEvidence.model_validate_json(path.read_text(encoding="utf-8"))


def _write_json(payload: BaseModel, output: Path | None) -> None:
    rendered = payload.model_dump_json(indent=2) + "\n"
    if output is None:
        sys.stdout.write(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(json.dumps({"output": str(output), "bytes": len(rendered)}) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--revision", default="WORKTREE", help="git revision or WORKTREE")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--architecture-only", action="store_true")
    parser.add_argument("--ratchet-only", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    try:
        architecture = build_architecture_receipt(repo_root, args.revision)
        if args.architecture_only:
            _write_json(architecture, args.output)
            return 0
        baseline = _load_architecture(args.baseline) if args.baseline else None
        if args.ratchet_only:
            if baseline is None:
                raise ValueError("--ratchet-only requires --baseline")
            regressions = architecture_regressions(architecture.metrics, baseline.metrics)
            scanner_mismatch = architecture.scanner_sha256 != baseline.scanner_sha256
            status: Literal["PASS", "FAIL", "HOLD"] = (
                "HOLD" if scanner_mismatch else ("FAIL" if regressions else "PASS")
            )
            ratchet = ArchitectureRatchetReceipt(
                schema_version="architecture-ratchet-v1",
                status=status,
                scoped_commit=architecture.scoped_commit,
                baseline_commit=baseline.scoped_commit,
                current_source_sha256=architecture.source_sha256,
                baseline_source_sha256=baseline.source_sha256,
                regressions=regressions,
                scanner_mismatch=scanner_mismatch,
            )
            _write_json(ratchet, args.output)
            return 0 if status == "PASS" else (2 if status == "HOLD" else 1)
        evidence = _load_evidence(args.evidence) if args.evidence else None
        receipt = score_quality(architecture, evidence, baseline)
        _write_json(receipt, args.output)
        return 0 if receipt.verdict == "PASS" else 1
    except (OSError, RuntimeError, ValueError, ValidationError) as exc:
        sys.stderr.write(
            json.dumps({"event": "quality_score_failed", "error_type": type(exc).__name__}) + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
