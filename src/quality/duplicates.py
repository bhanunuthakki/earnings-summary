"""Build a deterministic normalized-AST duplicate inventory for Python sources."""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import platform
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIN_AST_NODES = 20
MIN_BODY_LINES = 15
PARSER_VERSION = "python-ast-normalized-v1"
NEAR_MISS_RATIO = 0.85
MAX_STDOUT_BYTES = 100_000
_MINHASH_PRIME = (1 << 61) - 1
_MINHASH_COEFFICIENTS: tuple[tuple[int, int], ...] = tuple(
    (2 * index + 1, 104_729 * index + 17) for index in range(16)
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FunctionRecord(StrictModel):
    path: str
    qualified_name: str
    lineno: int
    end_lineno: int
    body_lines: int
    ast_nodes: int
    normalized_hash: str


class DuplicateGroup(StrictModel):
    group_id: str
    similarity: float = Field(ge=0, le=1)
    duplicated_loc: int
    functions: list[FunctionRecord]


class DuplicateTotals(StrictModel):
    groups: int = Field(ge=0)
    participating_functions: int = Field(ge=0)
    duplicated_loc: int = Field(ge=0)


class DuplicateInventory(StrictModel):
    schema_version: str = "1"
    scoped_revision: str
    commit_hash: str
    source_hash: str
    scanner_hash: str
    parser_version: str
    python_version: str
    thresholds: dict[str, int]
    files_scanned: int
    functions_scanned: int
    exact_groups: list[DuplicateGroup]
    near_miss_groups: list[DuplicateGroup]
    exact_totals: DuplicateTotals
    near_miss_totals: DuplicateTotals
    definitions: dict[str, str]
    parse_errors: list[str] = Field(default_factory=list)


class DuplicateRatchet(StrictModel):
    schema_version: str = "duplicate-ratchet-v1"
    status: Literal["PASS", "FAIL", "HOLD"]
    scoped_commit: str
    baseline_commit: str
    scanner_mismatch: bool
    regressions: tuple[str, ...]
    exact_totals: DuplicateTotals
    near_miss_totals: DuplicateTotals


def tracked_python_files(repo_root: Path) -> list[Path]:
    """Return tracked, active Python files in stable order."""
    try:
        raw = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", "--", "src/*.py", "execution/*.py"],
            check=True,
            capture_output=True,
            text=False,
        ).stdout
        candidates = [repo_root / item for item in raw.decode().split("\0") if item]
    except (OSError, subprocess.CalledProcessError):
        return []
    return sorted(p for p in candidates if p.is_file())


class _Normalizer(ast.NodeTransformer):
    def visit_Name(self, node: ast.Name) -> ast.AST:
        node.id = "IDENT"
        return node

    def visit_arg(self, node: ast.arg) -> ast.AST:
        node.arg = "ARG"
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        # Callable names and attribute chains are semantic targets; preserve
        # them while still normalizing argument-local identifiers.
        node.args = [self.visit(arg) for arg in node.args]
        node.keywords = [self.visit(keyword) for keyword in node.keywords]
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node.name = "FUNCTION"
        return self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        node.name = "FUNCTION"
        return self.generic_visit(node)


def _normalized_tree(node: ast.AST) -> ast.AST:
    cloned = copy.deepcopy(node)
    return _Normalizer().visit(ast.fix_missing_locations(cloned))


def _function_records(tree: ast.AST, relative_path: str) -> list[tuple[FunctionRecord, str]]:
    records: list[tuple[FunctionRecord, str]] = []

    def walk(node: ast.AST, parents: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = (*parents, child.name)
                count = sum(1 for _ in ast.walk(child))
                if child.body:
                    first_line = child.body[0].lineno
                    last_line = max(
                        (statement.end_lineno or statement.lineno) for statement in child.body
                    )
                    body_lines = last_line - first_line + 1
                else:
                    body_lines = 0
                normalized = ast.dump(
                    _normalized_tree(child), annotate_fields=True, include_attributes=False
                )
                digest = hashlib.sha256(normalized.encode()).hexdigest()
                records.append(
                    (
                        FunctionRecord(
                            path=relative_path,
                            qualified_name=".".join(name),
                            lineno=child.lineno,
                            end_lineno=child.end_lineno or child.lineno,
                            body_lines=body_lines,
                            ast_nodes=count,
                            normalized_hash=digest,
                        ),
                        normalized,
                    )
                )
                walk(child, name)
            elif isinstance(child, ast.ClassDef):
                walk(child, (*parents, child.name))
            else:
                walk(child, parents)

    walk(tree, ())
    return records


def _git_commit(repo_root: Path, revision: str) -> str:
    try:
        return subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "rev-parse",
                "HEAD" if revision == "WORKTREE" else revision,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNCOMMITTED"


def _source_items(repo_root: Path, revision: str) -> list[tuple[str, bytes]]:
    if revision == "WORKTREE":
        return [
            (path.relative_to(repo_root).as_posix(), path.read_bytes())
            for path in tracked_python_files(repo_root)
        ]
    try:
        listing = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "ls-tree",
                "-r",
                "--name-only",
                revision,
                "src",
                "execution",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        paths = sorted(
            path
            for path in listing.splitlines()
            if path.endswith(".py") and path.startswith(("src/", "execution/"))
        )
        items: list[tuple[str, bytes]] = []
        for path in paths:
            raw = subprocess.run(
                ["git", "-C", str(repo_root), "show", f"{revision}:{path}"],
                check=True,
                capture_output=True,
            ).stdout
            items.append((path, raw))
        return items
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"cannot read scoped revision {revision}") from exc


def _totals(groups: list[DuplicateGroup]) -> DuplicateTotals:
    participants = {
        (function.path, function.qualified_name, function.lineno)
        for group in groups
        for function in group.functions
    }
    return DuplicateTotals(
        groups=len(groups),
        participating_functions=len(participants),
        duplicated_loc=sum(group.duplicated_loc for group in groups),
    )


def _ast_shingle_counts(normalized: str) -> Counter[int]:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[^\s]", normalized)
    raw_shingles = ["|".join(tokens[index : index + 5]) for index in range(max(1, len(tokens) - 4))]
    return Counter(
        int.from_bytes(hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest(), "big")
        for value in raw_shingles
    )


def _minhash_signature(shingles: set[int]) -> tuple[int, ...]:
    if not shingles:
        return (0,) * len(_MINHASH_COEFFICIENTS)
    return tuple(
        min((coefficient * value + offset) % _MINHASH_PRIME for value in shingles)
        for coefficient, offset in _MINHASH_COEFFICIENTS
    )


def build_inventory(
    repo_root: Path = PROJECT_ROOT, revision: str = "WORKTREE"
) -> DuplicateInventory:
    sources = _source_items(repo_root, revision)
    records: list[tuple[FunctionRecord, str]] = []
    errors: list[str] = []
    source_digest = hashlib.sha256()
    for relative, raw in sources:
        source_digest.update(relative.encode() + b"\0" + raw)
        try:
            records.extend(
                _function_records(
                    ast.parse(raw.decode("utf-8"), filename=relative, type_comments=True), relative
                )
            )
        except (SyntaxError, UnicodeDecodeError) as exc:
            errors.append(f"{relative}: {exc}")
    eligible = [
        (r, n)
        for r, n in records
        if r.ast_nodes >= MIN_AST_NODES and r.body_lines >= MIN_BODY_LINES
    ]
    by_hash: dict[str, list[tuple[FunctionRecord, str]]] = {}
    for item in eligible:
        by_hash.setdefault(item[0].normalized_hash, []).append(item)
    exact_groups = [_group("exact", items, 1.0) for items in by_hash.values() if len(items) > 1]
    # Compare unique normalized bodies, not every member of an exact clone
    # group. Four deterministic MinHash bands over AST-type shingles keep early
    # edits discoverable without the repository-wide quadratic pair explosion.
    unique: list[tuple[str, list[tuple[FunctionRecord, str]]]] = sorted(by_hash.items())
    shingles_by_index: list[set[int]] = []
    shingle_counts_by_index: list[Counter[int]] = []
    band_buckets: dict[tuple[int, tuple[int, ...]], list[int]] = {}
    for index, (_digest, items) in enumerate(unique):
        counts = _ast_shingle_counts(items[0][1])
        shingles = set(counts)
        shingles_by_index.append(shingles)
        shingle_counts_by_index.append(counts)
        signature = _minhash_signature(shingles)
        for band in range(4):
            start = band * 4
            key = (band, signature[start : start + 4])
            band_buckets.setdefault(key, []).append(index)
    candidates: set[tuple[int, int]] = set()
    for indexes in band_buckets.values():
        for offset, left in enumerate(indexes):
            candidates.update((left, right) for right in indexes[offset + 1 :])
    near: list[DuplicateGroup] = []
    for left_index, right_index in sorted(candidates):
        left_items = unique[left_index][1]
        right_items = unique[right_index][1]
        left = left_items[0]
        right = right_items[0]
        size_ratio = min(left[0].ast_nodes, right[0].ast_nodes) / max(
            left[0].ast_nodes, right[0].ast_nodes
        )
        if size_ratio < 0.7:
            continue
        left_counts = shingle_counts_by_index[left_index]
        right_counts = shingle_counts_by_index[right_index]
        keys = left_counts.keys() | right_counts.keys()
        intersection_size = sum(min(left_counts[key], right_counts[key]) for key in keys)
        union_size = sum(max(left_counts[key], right_counts[key]) for key in keys)
        similarity = intersection_size / union_size if union_size else 1.0
        if similarity < NEAR_MISS_RATIO:
            continue
        near.append(_group("near", [*left_items, *right_items], similarity))
    exact_groups.sort(key=lambda g: g.group_id)
    near.sort(key=lambda g: g.group_id)
    return DuplicateInventory(
        scoped_revision=revision,
        commit_hash=_git_commit(repo_root, revision),
        source_hash=source_digest.hexdigest(),
        scanner_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        parser_version=PARSER_VERSION,
        python_version=platform.python_version(),
        thresholds={"min_ast_nodes": MIN_AST_NODES, "min_body_lines": MIN_BODY_LINES},
        files_scanned=len(sources),
        functions_scanned=len(records),
        exact_groups=exact_groups,
        near_miss_groups=near,
        exact_totals=_totals(exact_groups),
        near_miss_totals=_totals(near),
        definitions={
            "eligible_function": "at least 20 AST nodes and at least 15 physical body lines",
            "normalization": "identifiers, argument names, function names, and comments only",
            "semantic_preservation": "literals, operations, exception flow, SQL, and call targets remain exact",
            "duplicated_loc": "sum of participating function body-line spans per group",
            "near_miss": "normalized AST token 5-shingle Jaccard similarity >=0.85 after deterministic MinHash candidate indexing",
        },
        parse_errors=sorted(errors),
    )


def _group(
    kind: str, items: Iterable[tuple[FunctionRecord, str]], similarity: float
) -> DuplicateGroup:
    functions = sorted(
        (record for record, _ in items), key=lambda r: (r.path, r.lineno, r.qualified_name)
    )
    key = "|".join(f"{f.path}:{f.lineno}" for f in functions)
    return DuplicateGroup(
        group_id=f"{kind}-{hashlib.sha256(key.encode()).hexdigest()[:16]}",
        similarity=round(similarity, 6),
        duplicated_loc=sum(f.body_lines for f in functions),
        functions=functions,
    )


def compare_inventory(
    current: DuplicateInventory, baseline: DuplicateInventory
) -> DuplicateRatchet:
    comparisons = (
        ("exact groups", current.exact_totals.groups, baseline.exact_totals.groups),
        (
            "exact participating functions",
            current.exact_totals.participating_functions,
            baseline.exact_totals.participating_functions,
        ),
        (
            "exact duplicated LOC",
            current.exact_totals.duplicated_loc,
            baseline.exact_totals.duplicated_loc,
        ),
        ("near-miss groups", current.near_miss_totals.groups, baseline.near_miss_totals.groups),
        (
            "near-miss duplicated LOC",
            current.near_miss_totals.duplicated_loc,
            baseline.near_miss_totals.duplicated_loc,
        ),
    )
    regressions = tuple(
        f"{label} increased from {before} to {after}"
        for label, after, before in comparisons
        if after > before
    )
    scanner_mismatch = current.scanner_hash != baseline.scanner_hash
    status: Literal["PASS", "FAIL", "HOLD"] = (
        "HOLD" if scanner_mismatch or current.parse_errors else ("FAIL" if regressions else "PASS")
    )
    return DuplicateRatchet(
        status=status,
        scoped_commit=current.commit_hash,
        baseline_commit=baseline.commit_hash,
        scanner_mismatch=scanner_mismatch,
        regressions=regressions,
        exact_totals=current.exact_totals,
        near_miss_totals=current.near_miss_totals,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--revision", default="WORKTREE", help="git revision or WORKTREE")
    parser.add_argument("--out", type=Path, help="Optional JSON output path.")
    parser.add_argument("--baseline", type=Path, help="Compare with a frozen inventory receipt.")
    args = parser.parse_args(argv)
    try:
        inventory = build_inventory(args.repo_root.resolve(), args.revision)
        if args.baseline:
            baseline = DuplicateInventory.model_validate_json(
                args.baseline.read_text(encoding="utf-8")
            )
            ratchet = compare_inventory(inventory, baseline)
            sys.stdout.write(ratchet.model_dump_json(indent=2) + "\n")
            return 0 if ratchet.status == "PASS" else (2 if ratchet.status == "HOLD" else 1)
        payload = inventory.model_dump_json(indent=2) + "\n"
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(payload, encoding="utf-8")
        elif len(payload.encode("utf-8")) > MAX_STDOUT_BYTES:
            output = (
                args.repo_root.resolve()
                / ".tmp"
                / "quality"
                / f"duplicate-inventory-{inventory.source_hash[:16]}.json"
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(payload, encoding="utf-8")
            sys.stdout.write(
                json.dumps(
                    {
                        "output": str(output),
                        "exact_groups": inventory.exact_totals.groups,
                        "near_miss_groups": inventory.near_miss_totals.groups,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        else:
            sys.stdout.write(payload)
        if inventory.parse_errors:
            print(
                json.dumps(
                    {"event": "parse_errors", "count": len(inventory.parse_errors)}, sort_keys=True
                ),
                file=sys.stderr,
            )
        return 2 if inventory.parse_errors else 0
    except (OSError, ValueError) as exc:
        print(
            json.dumps({"event": "duplicate_inventory_failed", "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
