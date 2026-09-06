"""Classify tracked test database builders without touching a database."""

from __future__ import annotations

import ast
import hashlib
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Taxonomy = Literal[
    "direct-downgrade",
    "archived-graph",
    "seeded-upgrade",
    "direct-historical",
    "custom-bootstrap",
    "performance-volume",
    "hand-DDL-unit-schema",
    "cached-current-head",
    "unclassified",
]


class PatternFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    line: int
    kind: Literal["forbidden_checkout_default", "explicit_fixture", "parse_error"]
    evidence: str


class BuilderClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    taxonomy: Taxonomy
    evidence: tuple[str, ...]


class TestDbAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["test-db-patterns/v1"] = "test-db-patterns/v1"
    scoped_commit: str
    scanner_sha256: str
    source_sha256: str
    status: Literal["PASS", "HOLD"]
    tracked_test_files: tuple[str, ...]
    database_builders: tuple[BuilderClassification, ...]
    counts_by_taxonomy: dict[str, int]
    findings: tuple[PatternFinding, ...]
    violations: tuple[str, ...] = Field(default_factory=tuple)


_DDL_MARKERS = ("create table", "alter table", "create index", "create trigger")
_BUILD_CALLS = {"downgrade", "upgrade", "stamp", "create_all", "migrated_db", "executescript"}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)


def _tracked_tests(root: Path) -> tuple[str, ...]:
    result = _git(root, "ls-files", "-z", "--", "tests/*.py", "instruction_tests/*.py")
    if result.returncode == 0:
        names = tuple(sorted(name for name in result.stdout.decode().split("\0") if name))
        if names:
            return names
    # Small unversioned fixtures remain useful for unit-testing this scanner.
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for directory in (root / "tests", root / "instruction_tests")
            if directory.is_dir()
            for path in directory.rglob("*.py")
            if path.is_file()
        )
    )


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _string_constants(tree: ast.AST) -> list[tuple[int, str]]:
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _builder_evidence(tree: ast.AST) -> tuple[str, ...]:
    evidence: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            leaf = name.rsplit(".", 1)[-1]
            if leaf in _BUILD_CALLS:
                evidence.add(f"call:{name}")
    for _line, value in _string_constants(tree):
        lowered = value.lower()
        for marker in _DDL_MARKERS:
            if marker in lowered:
                evidence.add(f"sql:{marker}")
    return tuple(sorted(evidence))


def _taxonomy(path: str, text: str, evidence: tuple[str, ...]) -> Taxonomy:
    lowered = text.lower()
    joined = " ".join(evidence)
    # Frozen precedence: the first matching class wins.
    if "call:command.downgrade" in joined or "call:downgrade" in joined:
        return "direct-downgrade"
    if "versions_archived" in lowered or ("archive" in lowered and "migration" in lowered):
        return "archived-graph"
    if "call:command.upgrade" in joined and ("seed" in lowered or "insert into" in lowered):
        return "seeded-upgrade"
    if (
        "call:command.stamp" in joined
        or "call:stamp" in joined
        or (
            "call:command.upgrade" in joined
            and any(
                marker in lowered
                for marker in ("prior_head", "historical", "revision=", "downgrade_revision")
            )
        )
    ):
        return "direct-historical"
    if (
        "call:create_all" in joined
        or "call:executescript" in joined
        or "call:command.upgrade" in joined
        or "call:upgrade" in joined
        or "bootstrap" in lowered
    ):
        return "custom-bootstrap"
    if any(
        marker in path.lower() or marker in lowered
        for marker in ("benchmark", "performance", "volume")
    ):
        return "performance-volume"
    if "sql:" in joined:
        return "hand-DDL-unit-schema"
    if "call:migrated_db" in joined or ("cached" in lowered and "head" in lowered):
        return "cached-current-head"
    return "unclassified"


def _db_path_findings(path: str, tree: ast.AST) -> list[PatternFinding]:
    findings: list[PatternFinding] = []
    forbidden_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None:
            continue
        expression = ast.unparse(value)
        lowered = expression.lower().replace("\\", "/")
        if "portfolio.db" not in lowered:
            continue
        if any(marker in lowered for marker in ("tmp_path", "tmpdir", "temporarydirectory")):
            findings.append(
                PatternFinding(
                    path=path,
                    line=node.lineno,
                    kind="explicit_fixture",
                    evidence="temporary fixture path expression",
                )
            )
        elif any(marker in expression for marker in ("PROJECT_ROOT", "Path.cwd()", "__file__")):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            forbidden_names.update(target.id for target in targets if isinstance(target, ast.Name))
    parents: dict[ast.AST, ast.AST] = {
        child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }

    def inside_expected_failure(node: ast.AST) -> bool:
        current = parents.get(node)
        while current is not None:
            if isinstance(current, ast.With) and any(
                isinstance(item.context_expr, ast.Call)
                and _call_name(item.context_expr.func) == "pytest.raises"
                for item in current.items
            ):
                return True
            current = parents.get(current)
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = _call_name(node.func)
        value = node.args[0]
        risky = name.endswith(("connect", "connect_sqlite", "open_db", "get_connection"))
        literal_default = (
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and value.value.replace("\\", "/") == "data/portfolio.db"
        )
        named_default = isinstance(value, ast.Name) and value.id in forbidden_names
        if risky and (literal_default or named_default) and not inside_expected_failure(node):
            findings.append(
                PatternFinding(
                    path=path,
                    line=node.lineno,
                    kind="forbidden_checkout_default",
                    evidence=f"checkout-default database passed to {name}",
                )
            )
    return findings


def audit_test_db_patterns(root: Path) -> TestDbAudit:
    repo_root = root.resolve()
    paths = _tracked_tests(repo_root)
    digest = hashlib.sha256()
    findings: list[PatternFinding] = []
    builders: list[BuilderClassification] = []
    for path in paths:
        raw = (repo_root / path).read_bytes()
        digest.update(path.encode() + b"\0" + raw)
        try:
            text = raw.decode("utf-8")
            tree = ast.parse(text, filename=path)
        except (UnicodeDecodeError, SyntaxError) as exc:
            findings.append(
                PatternFinding(path=path, line=0, kind="parse_error", evidence=str(exc))
            )
            continue
        evidence = _builder_evidence(tree)
        if evidence:
            builders.append(
                BuilderClassification(
                    path=path,
                    taxonomy=_taxonomy(path, text, evidence),
                    evidence=evidence,
                )
            )
        findings.extend(_db_path_findings(path, tree))
    counts: dict[str, int] = {}
    for builder in builders:
        counts[builder.taxonomy] = counts.get(builder.taxonomy, 0) + 1
    violations = [
        f"{finding.kind}: {finding.path}:{finding.line}"
        for finding in findings
        if finding.kind != "explicit_fixture"
    ]
    violations.extend(
        f"unclassified database-building test: {builder.path}"
        for builder in builders
        if builder.taxonomy == "unclassified"
    )
    commit_result = _git(repo_root, "rev-parse", "HEAD")
    scoped_commit = (
        commit_result.stdout.decode().strip() if commit_result.returncode == 0 else "UNCOMMITTED"
    )
    return TestDbAudit(
        scoped_commit=scoped_commit,
        scanner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        source_sha256=digest.hexdigest(),
        status="HOLD" if violations else "PASS",
        tracked_test_files=paths,
        database_builders=tuple(builders),
        counts_by_taxonomy=dict(sorted(counts.items())),
        findings=tuple(sorted(findings, key=lambda item: (item.path, item.line, item.kind))),
        violations=tuple(sorted(violations)),
    )
