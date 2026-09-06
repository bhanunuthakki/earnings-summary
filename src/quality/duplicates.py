"""Build a deterministic normalized-AST duplicate inventory for Python sources."""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import io
import json
import platform
import re
import subprocess
import sys
import tarfile
import zlib
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
MAX_SHINGLE_POSTINGS = 64
CANDIDATE_SHINGLES = 12


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
            ["git", "-C", str(repo_root), "ls-files", "-z", "--", "src", "execution"],
            check=True,
            capture_output=True,
            text=False,
        ).stdout
        candidates = [repo_root / item for item in raw.decode().split("\0") if item.endswith(".py")]
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot enumerate tracked Python files in {repo_root}") from exc
    missing = [path for path in candidates if not path.is_file()]
    if missing:
        relative = ", ".join(path.relative_to(repo_root).as_posix() for path in missing)
        raise ValueError(f"tracked Python files are missing from the worktree: {relative}")
    return sorted(candidates)


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


def resolve_git_commit(repo_root: Path, revision: str) -> str:
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
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"cannot resolve git commit for {revision}") from exc


def _source_items(repo_root: Path, revision: str) -> list[tuple[str, bytes]]:
    if revision == "WORKTREE":
        return [
            (path.relative_to(repo_root).as_posix(), path.read_bytes())
            for path in tracked_python_files(repo_root)
        ]
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "archive", "--format=tar", revision, "src", "execution"],
            check=True,
            capture_output=True,
        )
        items: list[tuple[str, bytes]] = []
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            for member in archive.getmembers():
                if not member.isfile() or not member.name.endswith(".py"):
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError(f"git archive member is unreadable: {member.name}")
                items.append((member.name, extracted.read()))
        return sorted(items)
    except (OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
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
    token_ids = [zlib.crc32(token.encode("utf-8")) for token in tokens]
    if len(token_ids) < 5:
        token_ids.extend([0] * (5 - len(token_ids)))
    counts: Counter[int] = Counter()
    mask = (1 << 64) - 1
    for index in range(len(token_ids) - 4):
        value = 0
        for token_id in token_ids[index : index + 5]:
            value = ((value * 1_000_003) ^ token_id) & mask
        counts[value] += 1
    return counts


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
    # group. A bounded rare-shingle inverted index keeps early edits visible
    # without the repository-wide quadratic pair explosion.
    unique: list[tuple[str, list[tuple[FunctionRecord, str]]]] = sorted(by_hash.items())
    shingle_counts_by_index: list[Counter[int]] = []
    postings: dict[int, list[int]] = {}
    for index, (_digest, items) in enumerate(unique):
        counts = _ast_shingle_counts(items[0][1])
        shingle_counts_by_index.append(counts)
        for shingle in counts:
            postings.setdefault(shingle, []).append(index)
    near: list[DuplicateGroup] = []
    for left_index, left_counts in enumerate(shingle_counts_by_index):
        rare = sorted(
            (
                shingle
                for shingle in left_counts
                if 1 < len(postings[shingle]) <= MAX_SHINGLE_POSTINGS
            ),
            key=lambda shingle: (len(postings[shingle]), shingle),
        )[:CANDIDATE_SHINGLES]
        right_indexes = sorted(
            {
                right_index
                for shingle in rare
                for right_index in postings[shingle]
                if right_index > left_index
            }
        )
        left_total = left_counts.total()
        for right_index in right_indexes:
            right_counts = shingle_counts_by_index[right_index]
            right_total = right_counts.total()
            if min(left_total, right_total) / max(left_total, right_total) < NEAR_MISS_RATIO:
                continue
            intersection_size = (left_counts & right_counts).total()
            union_size = left_total + right_total - intersection_size
            similarity = intersection_size / union_size if union_size else 1.0
            if similarity < NEAR_MISS_RATIO:
                continue
            near.append(
                _group(
                    "near",
                    [*unique[left_index][1], *unique[right_index][1]],
                    similarity,
                )
            )
    exact_groups.sort(key=lambda g: g.group_id)
    near.sort(key=lambda g: g.group_id)
    return DuplicateInventory(
        scoped_revision=revision,
        commit_hash=resolve_git_commit(repo_root, revision),
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
            "near_miss": "normalized AST token 5-shingle weighted-Jaccard similarity >=0.85 after bounded rare-shingle indexing",
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
    has_parse_errors = bool(current.parse_errors or baseline.parse_errors)
    status: Literal["PASS", "FAIL", "HOLD"] = (
        "HOLD" if scanner_mismatch or has_parse_errors else ("FAIL" if regressions else "PASS")
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
