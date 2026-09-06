"""Source-only static audit of test database-builder patterns."""

from __future__ import annotations

import ast
import hashlib
import os
import posixpath
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from quality.git_env import clean_local_git_env

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
Evidence = Literal[
    "call:downgrade",
    "call:upgrade",
    "call:stamp",
    "call:create_all",
    "call:executescript",
    "call:migrated_db",
    "sql:create table",
    "sql:alter table",
    "sql:create index",
    "sql:create trigger",
    "text:archived",
    "text:seed",
    "text:historical",
    "text:bootstrap",
    "text:volume",
    "text:cached-head",
]
FindingEvidence = Literal[
    "temporary-fixture", "checkout-default", "read-error", "invalid-utf8", "syntax-error"
]
HoldReason = Literal[
    "git-unavailable",
    "git-nonzero",
    "invalid-head",
    "invalid-git-utf8",
    "invalid-git-framing",
    "invalid-path",
    "duplicate-path",
    "empty-scope",
    "missing-path",
    "closure-untracked",
    "closure-unreadable",
    "dirty-tree",
    "invalid-porcelain",
    "scanner-closure-mismatch",
]
CollectionNote = Literal[
    "",
    "git-unavailable",
    "git-nonzero",
    "invalid-head",
    "invalid-git-utf8",
    "invalid-git-framing",
    "invalid-path",
    "duplicate-path",
    "empty-scope",
    "missing-path",
    "closure-untracked",
    "closure-unreadable",
    "dirty-tree",
    "invalid-porcelain",
    "scanner-closure-mismatch",
]
_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class PatternFinding(BaseModel):
    model_config = _STRICT
    path: str
    line: int
    kind: Literal["forbidden_checkout_default", "explicit_fixture", "parse_error"]
    evidence: FindingEvidence


class BuilderClassification(BaseModel):
    model_config = _STRICT
    path: str
    taxonomy: Taxonomy
    evidence: tuple[Evidence, ...]


class TestDbAudit(BaseModel):
    model_config = _STRICT
    schema_version: Literal["test-db-patterns/v1"] = "test-db-patterns/v1"
    scoped_commit: str
    scanner_sha256: str
    source_sha256: str
    collection_status: Literal["COMPLETE", "HOLD"]
    collection_note: CollectionNote = ""
    raw_audit_status: Literal["PASS", "HOLD"]
    admission_status: Literal["HOLD"] = "HOLD"
    admission_reason: Literal["disposition_and_ratchet_deferred"] = (
        "disposition_and_ratchet_deferred"
    )
    tracked_test_files: tuple[str, ...] = Field(default_factory=tuple)
    database_builders: tuple[BuilderClassification, ...] = Field(default_factory=tuple)
    counts_by_taxonomy: dict[str, int] = Field(default_factory=dict)
    findings: tuple[PatternFinding, ...] = Field(default_factory=tuple)
    violations: tuple[str, ...] = Field(default_factory=tuple)


_CLOSURE = (
    "execution/audit_test_db_patterns.py",
    "src/quality/git_env.py",
    "src/quality/test_db_patterns.py",
)
_ROOTS = ("tests", "instruction_tests")
_TIMEOUT = 30
_RISKY_LEAVES = frozenset({"connect", "connect_sqlite", "open_db", "get_connection", "open"})
_LEAF_EVIDENCE: dict[str, Evidence] = {
    "downgrade": "call:downgrade",
    "upgrade": "call:upgrade",
    "stamp": "call:stamp",
    "create_all": "call:create_all",
    "executescript": "call:executescript",
    "migrated_db": "call:migrated_db",
}
_SQL_EVIDENCE: tuple[tuple[str, Evidence], ...] = (
    ("create table", "sql:create table"),
    ("alter table", "sql:alter table"),
    ("create index", "sql:create index"),
    ("create trigger", "sql:create trigger"),
)
_EMPTY_SHA = hashlib.sha256(b"").hexdigest()

_ValueKind = Literal["forbidden", "fixture"]
_FORBIDDEN_DB_PATH = "data/portfolio.db"
_FIXTURE_MARKERS = ("tmp_path", "tmpdir", "temporarydirectory")


def _normalize_db_path(value: str) -> str:
    return posixpath.normpath(value.replace("\\", "/").casefold())


def _is_path_constructor(expr: ast.expr) -> bool:
    return (isinstance(expr, ast.Name) and expr.id == "Path") or (
        isinstance(expr, ast.Attribute) and expr.attr == "Path"
    )


def _constant_path(expr: ast.expr, values: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.Name):
        return values.get(expr.id)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = _constant_path(expr.left, values)
        right = _constant_path(expr.right, values)
        return left + right if left is not None and right is not None else None
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
        left = _constant_path(expr.left, values)
        right = _constant_path(expr.right, values)
        if left is not None and right is not None:
            return left.rstrip("/\\") + "/" + right.lstrip("/\\")
        return None
    if isinstance(expr, ast.JoinedStr):
        parts: list[str] = []
        for part in expr.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                parts.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                value = _constant_path(part.value, values)
                if value is None:
                    return None
                parts.append(value)
            else:
                return None
        return "".join(parts)
    if (
        isinstance(expr, ast.Call)
        and len(expr.args) == 1
        and not expr.keywords
        and (
            _is_path_constructor(expr.func)
            or (isinstance(expr.func, ast.Name) and expr.func.id == "str")
        )
    ):
        return _constant_path(expr.args[0], values)
    if (
        isinstance(expr, ast.Call)
        and _is_path_constructor(expr.func)
        and not expr.keywords
        and len(expr.args) > 1
    ):
        parts: list[str] = []
        for argument in expr.args:
            part = _constant_path(argument, values)
            if part is None:
                return None
            parts.append(part)
        return posixpath.join(*parts)
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "join"
        and not expr.keywords
        and expr.args
    ):
        resolved: list[str] = []
        for arg in expr.args:
            part = _constant_path(arg, values)
            if part is None:
                return None
            resolved.append(part)
        joined = resolved[0]
        for tail in resolved[1:]:
            joined = joined.rstrip("/\\") + "/" + tail.lstrip("/\\")
        return joined
    return None


def _has_forbidden_suffix(expr: ast.expr, values: dict[str, str]) -> bool:
    concrete = _constant_path(expr, values)
    if concrete is not None:
        return _normalize_db_path(concrete) == _FORBIDDEN_DB_PATH
    if (
        isinstance(expr, ast.Call)
        and _is_path_constructor(expr.func)
        and not expr.keywords
        and expr.args
    ):
        tails: list[str] = []
        for argument in reversed(expr.args):
            part = _constant_path(argument, values)
            if part is None:
                break
            tails.append(part)
        if tails:
            normalized = _normalize_db_path("/".join(reversed(tails)))
            if normalized == _FORBIDDEN_DB_PATH or normalized.endswith("/" + _FORBIDDEN_DB_PATH):
                return True
        return False
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "join"
        and not expr.keywords
        and expr.args
    ):
        tails: list[str] = []
        for arg in reversed(expr.args):
            part = _constant_path(arg, values)
            if part is None:
                break
            tails.append(part)
        if tails:
            joined = tails[-1]
            for extra in reversed(tails[:-1]):
                joined = joined.rstrip("/\\") + "/" + extra.lstrip("/\\")
            normalized = _normalize_db_path(joined)
            if normalized == _FORBIDDEN_DB_PATH or normalized.endswith("/" + _FORBIDDEN_DB_PATH):
                return True
        return False
    segments: list[str] = []
    current = expr
    while isinstance(current, ast.BinOp) and isinstance(current.op, ast.Div):
        right = _constant_path(current.right, values)
        if right is None:
            return False
        normalized_right = _normalize_db_path(right)
        if normalized_right == _FORBIDDEN_DB_PATH or normalized_right.endswith(
            "/" + _FORBIDDEN_DB_PATH
        ):
            return True
        if "/" in normalized_right or "\\" in right:
            return False
        segments.append(right)
        current = current.left
    suffix = "/".join(reversed(segments))
    if not suffix:
        return False
    normalized = _normalize_db_path(suffix)
    return normalized == _FORBIDDEN_DB_PATH or normalized.endswith("/" + _FORBIDDEN_DB_PATH)


def _is_fixture_identifier(text: str) -> bool:
    lowered = text.casefold()
    return any(marker in lowered for marker in _FIXTURE_MARKERS)


def _has_fixture_identifier(expr: ast.expr) -> bool:
    for node in ast.walk(expr):
        if isinstance(node, ast.Name) and _is_fixture_identifier(node.id):
            return True
        if isinstance(node, ast.Attribute) and _is_fixture_identifier(node.attr):
            return True
    return False


def _mentions_fixture(expr: ast.expr, kinds: dict[str, _ValueKind]) -> bool:
    if _has_fixture_identifier(expr):
        return True
    return any(
        isinstance(node, ast.Name) and kinds.get(node.id) == "fixture" for node in ast.walk(expr)
    )


def _is_forbidden_expr(
    expr: ast.expr, values: dict[str, str], kinds: dict[str, _ValueKind]
) -> bool:
    concrete = _constant_path(expr, values)
    if concrete is not None:
        return _normalize_db_path(concrete) == _FORBIDDEN_DB_PATH
    if isinstance(expr, ast.Name) and kinds.get(expr.id) == "forbidden":
        return True
    if _mentions_fixture(expr, kinds):
        return False
    if _has_forbidden_suffix(expr, values):
        return True
    if (
        isinstance(expr, ast.Call)
        and len(expr.args) == 1
        and not expr.keywords
        and isinstance(expr.func, ast.Name)
        and expr.func.id == "str"
    ):
        return _is_forbidden_expr(expr.args[0], values, kinds)
    return False


class _HoldError(Exception):
    def __init__(self, note: HoldReason) -> None:
        super().__init__(note)
        self.note: HoldReason = note


def _run_git(root: Path, args: tuple[str, ...]) -> bytes:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=False,
            shell=False,
            env=clean_local_git_env(),
            timeout=_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise _HoldError("git-unavailable") from exc
    if proc.returncode != 0:
        raise _HoldError("git-nonzero")
    return proc.stdout


def _valid_commit(text: str) -> bool:
    return len(text) in (40, 64) and all(c in "0123456789abcdef" for c in text)


def _scoped_commit(root: Path) -> str:
    raw = _run_git(root, ("rev-parse", "HEAD"))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _HoldError("invalid-head") from exc
    if "\x00" in text or "\r" in text:
        raise _HoldError("invalid-head")
    if text.endswith("\n"):
        if text.count("\n") != 1:
            raise _HoldError("invalid-head")
        value = text[:-1]
    else:
        if "\n" in text:
            raise _HoldError("invalid-head")
        value = text
    if not _valid_commit(value):
        raise _HoldError("invalid-head")
    return value


def _split_nul(raw: bytes) -> list[str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _HoldError("invalid-git-utf8") from exc
    if text == "":
        return []
    if "\x00" not in text:
        raise _HoldError("invalid-git-framing")
    if not text.endswith("\x00"):
        raise _HoldError("invalid-git-framing")
    parts = text.split("\x00")
    if parts and parts[-1] == "":
        parts.pop()
    if any(p == "" for p in parts):
        raise _HoldError("invalid-git-framing")
    if "\n" in text:
        raise _HoldError("invalid-git-framing")
    return parts


def _validate_canonical(name: str) -> None:
    if not name or name.startswith("/") or "\\" in name:
        raise _HoldError("invalid-path")
    if "\x00" in name or "\n" in name or "\r" in name:
        raise _HoldError("invalid-path")
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts:
        raise _HoldError("invalid-path")
    if posixpath.normpath(name) != name:
        raise _HoldError("invalid-path")
    if name.startswith("./") or "/./" in name or "//" in name:
        raise _HoldError("invalid-path")
    if name.endswith("/"):
        raise _HoldError("invalid-path")


def _tracked_tests(root: Path) -> tuple[str, ...]:
    raw = _run_git(root, ("ls-files", "-z", "--", *_ROOTS))
    names = _split_nul(raw)
    cleaned: list[str] = []
    seen: set[str] = set()
    for name in names:
        _validate_canonical(name)
        if not any(name == r or name.startswith(r + "/") for r in _ROOTS):
            raise _HoldError("invalid-path")
        if not name.endswith(".py"):
            continue
        if name in seen:
            raise _HoldError("duplicate-path")
        seen.add(name)
        cleaned.append(name)
    if not cleaned:
        raise _HoldError("empty-scope")
    for item in cleaned:
        target = root / item
        try:
            if target.is_symlink():
                raise _HoldError("missing-path")
            if not target.is_file():
                raise _HoldError("missing-path")
        except OSError as exc:
            raise _HoldError("missing-path") from exc
    return tuple(sorted(cleaned))


def _closure_tracked(root: Path) -> None:
    raw = _run_git(root, ("ls-files", "-z", "--", *_CLOSURE))
    names = _split_nul(raw)
    if len(names) != len(set(names)):
        raise _HoldError("duplicate-path")
    for name in names:
        _validate_canonical(name)
    if set(names) != set(_CLOSURE):
        raise _HoldError("closure-untracked")


def _secure_read_bytes(repo: Path, target: Path) -> bytes:
    try:
        before = os.lstat(target)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise OSError("unreadable")
        resolved_before = target.resolve()
        resolved_before.relative_to(repo)
        data = target.read_bytes()
        after = os.lstat(target)
        if stat.S_ISLNK(after.st_mode) or not stat.S_ISREG(after.st_mode):
            raise OSError("unreadable")
        resolved_after = target.resolve()
        resolved_after.relative_to(repo)
    except (OSError, ValueError) as exc:
        raise OSError("unreadable") from exc
    if resolved_before != resolved_after or (before.st_dev, before.st_ino) != (
        after.st_dev,
        after.st_ino,
    ):
        raise OSError("unreadable")
    return data


def _closure_bytes(root: Path) -> list[tuple[str, bytes]]:
    items: list[tuple[str, bytes]] = []
    for name in sorted(_CLOSURE):
        _validate_canonical(name)
        target = root / name
        try:
            data = _secure_read_bytes(root, target)
        except OSError as exc:
            raise _HoldError("closure-unreadable") from exc
        items.append((name, data))
    return items


def _running_closure_bytes() -> list[tuple[str, bytes]]:
    return _closure_bytes(Path(__file__).resolve().parents[2])


def _assert_clean(root: Path) -> None:
    raw = _run_git(
        root, ("status", "--porcelain=v1", "-z", "--untracked-files=no", "--", *_ROOTS, *_CLOSURE)
    )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _HoldError("invalid-git-utf8") from exc
    if text == "":
        return
    if "\x00" not in text:
        raise _HoldError("invalid-git-framing")
    if not text.endswith("\x00"):
        raise _HoldError("invalid-git-framing")
    parts = text.split("\x00")
    if parts and parts[-1] == "":
        parts.pop()
    if any(p == "" for p in parts):
        raise _HoldError("invalid-porcelain")
    raise _HoldError("dirty-tree")


def _call_leaf(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _builder_evidence(tree: ast.AST) -> tuple[Evidence, ...]:
    found: set[Evidence] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            leaf = _call_leaf(node.func)
            mapped = _LEAF_EVIDENCE.get(leaf)
            if mapped is not None:
                found.add(mapped)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            for marker, ev in _SQL_EVIDENCE:
                if marker in lowered:
                    found.add(ev)
    return tuple(sorted(found))


def _taxonomy(path: str, lowered: str, evidence: tuple[Evidence, ...]) -> Taxonomy:
    joined = set(evidence)
    if "call:downgrade" in joined:
        return "direct-downgrade"
    if "versions_archived" in lowered or ("archive" in lowered and "migration" in lowered):
        return "archived-graph"
    if "call:upgrade" in joined and ("seed" in lowered or "insert into" in lowered):
        return "seeded-upgrade"
    if "call:stamp" in joined or (
        "call:upgrade" in joined
        and any(
            m in lowered for m in ("prior_head", "historical", "revision=", "downgrade_revision")
        )
    ):
        return "direct-historical"
    if (
        "call:create_all" in joined
        or "call:executescript" in joined
        or "call:upgrade" in joined
        or "bootstrap" in lowered
    ):
        return "custom-bootstrap"
    if any(m in path.lower() or m in lowered for m in ("benchmark", "performance", "volume")):
        return "performance-volume"
    if any(e.startswith("sql:") for e in joined):
        return "hand-DDL-unit-schema"
    if "call:migrated_db" in joined or ("cached" in lowered and "head" in lowered):
        return "cached-current-head"
    return "unclassified"


def _enrich_evidence(
    lowered: str, path: str, evidence: tuple[Evidence, ...]
) -> tuple[Evidence, ...]:
    extra: set[Evidence] = set(evidence)
    if "versions_archived" in lowered or ("archive" in lowered and "migration" in lowered):
        extra.add("text:archived")
    if "seed" in lowered or "insert into" in lowered:
        extra.add("text:seed")
    if any(m in lowered for m in ("prior_head", "historical", "revision=", "downgrade_revision")):
        extra.add("text:historical")
    if "bootstrap" in lowered:
        extra.add("text:bootstrap")
    if any(m in path.lower() or m in lowered for m in ("benchmark", "performance", "volume")):
        extra.add("text:volume")
    if "cached" in lowered and "head" in lowered:
        extra.add("text:cached-head")
    return tuple(sorted(extra))


def _db_findings(path: str, tree: ast.AST) -> list[PatternFinding]:
    findings: list[PatternFinding] = []
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    scope_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

    def nearest_scope(node: ast.AST) -> ast.AST:
        current: ast.AST | None = node
        while current is not None:
            if isinstance(current, scope_types):
                return current
            current = parents.get(id(current))
        return tree

    assignments: list[tuple[int, int, ast.AST, tuple[str, ...], ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = tuple(target.id for target in node.targets if isinstance(target, ast.Name))
            if names:
                assignments.append(
                    (node.lineno, node.col_offset, nearest_scope(node), names, node.value)
                )
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                assignments.append(
                    (
                        node.lineno,
                        node.col_offset,
                        nearest_scope(node),
                        (node.target.id,),
                        node.value,
                    )
                )
    assignments.sort(key=lambda item: (item[0], item[1]))

    def state_before(call: ast.Call) -> tuple[dict[str, str], dict[str, _ValueKind]]:
        call_scope = nearest_scope(call)
        values: dict[str, str] = {}
        kinds: dict[str, _ValueKind] = {}
        for line, column, scope, names, expression in assignments:
            if (line, column) >= (call.lineno, call.col_offset):
                continue
            if scope is not tree and scope is not call_scope:
                continue
            if _mentions_fixture(expression, kinds):
                for name in names:
                    kinds[name] = "fixture"
                    values.pop(name, None)
                continue
            if _is_forbidden_expr(expression, values, kinds):
                for name in names:
                    kinds[name] = "forbidden"
                concrete = _constant_path(expression, values)
                if concrete is not None:
                    for name in names:
                        values[name] = concrete
                continue
            concrete = _constant_path(expression, values)
            for name in names:
                kinds.pop(name, None)
                if concrete is None:
                    values.pop(name, None)
                else:
                    values[name] = concrete
        return values, kinds

    for line, _column, _scope, _names, expression in assignments:
        mentions_db = any(
            isinstance(constant.value, str) and "portfolio.db" in constant.value.casefold()
            for constant in ast.walk(expression)
            if isinstance(constant, ast.Constant)
        )
        if mentions_db and _has_fixture_identifier(expression):
            findings.append(
                PatternFinding(
                    path=path,
                    line=line,
                    kind="explicit_fixture",
                    evidence="temporary-fixture",
                )
            )

    def inside_raises(node: ast.AST) -> bool:
        current: ast.AST | None = parents.get(id(node))
        while current is not None:
            if isinstance(current, ast.With):
                for item in current.items:
                    expr = item.context_expr
                    if isinstance(expr, ast.Call):
                        func = expr.func
                        if (
                            isinstance(func, ast.Attribute)
                            and func.attr == "raises"
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "pytest"
                        ):
                            return True
            current = parents.get(id(current))
        return False

    def call_candidates(node: ast.Call) -> tuple[ast.expr, ...]:
        leaf = _call_leaf(node.func)
        if leaf not in _RISKY_LEAVES:
            return ()
        candidates = list(node.args)
        candidates.extend(keyword.value for keyword in node.keywords)
        if isinstance(node.func, ast.Attribute) and leaf == "open":
            candidates.append(node.func.value)
        return tuple(candidates)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        candidates = call_candidates(node)
        if not candidates or inside_raises(node):
            continue
        values, kinds = state_before(node)
        if any(_is_forbidden_expr(candidate, values, kinds) for candidate in candidates):
            findings.append(
                PatternFinding(
                    path=path,
                    line=node.lineno,
                    kind="forbidden_checkout_default",
                    evidence="checkout-default",
                )
            )
    return findings


def _hold_receipt(note: HoldReason) -> TestDbAudit:
    return TestDbAudit(
        scoped_commit="UNKNOWN",
        scanner_sha256=_EMPTY_SHA,
        source_sha256=_EMPTY_SHA,
        collection_status="HOLD",
        collection_note=note,
        raw_audit_status="HOLD",
        tracked_test_files=tuple(),
        database_builders=tuple(),
        counts_by_taxonomy={},
        findings=tuple(),
        violations=(note,),
    )


def audit_test_db_patterns(root: Path) -> TestDbAudit:
    try:
        repo = root.resolve()
    except OSError:
        return _hold_receipt("invalid-path")
    try:
        commit = _scoped_commit(repo)
        paths = _tracked_tests(repo)
        _closure_tracked(repo)
        closure_items = _closure_bytes(repo)
        _assert_clean(repo)
        running_items = _running_closure_bytes()
        if closure_items != running_items:
            raise _HoldError("scanner-closure-mismatch")
    except _HoldError as hold:
        return _hold_receipt(hold.note)
    scanner_digest = hashlib.sha256()
    for name, data in closure_items:
        scanner_digest.update(name.encode("utf-8") + b"\x00" + data + b"\x00")
    source_digest = hashlib.sha256()
    findings: list[PatternFinding] = []
    builders: list[BuilderClassification] = []
    for path in paths:
        source_digest.update(path.encode("utf-8") + b"\x00")
        try:
            raw = _secure_read_bytes(repo, repo / path)
        except OSError:
            findings.append(
                PatternFinding(path=path, line=0, kind="parse_error", evidence="read-error")
            )
            source_digest.update(b"read-error\x00")
            continue
        source_digest.update(raw + b"\x00")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(
                PatternFinding(path=path, line=0, kind="parse_error", evidence="invalid-utf8")
            )
            continue
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError:
            findings.append(
                PatternFinding(path=path, line=0, kind="parse_error", evidence="syntax-error")
            )
            continue
        lowered = text.lower()
        base = _builder_evidence(tree)
        if base:
            enriched = _enrich_evidence(lowered, path, base)
            builders.append(
                BuilderClassification(
                    path=path, taxonomy=_taxonomy(path, lowered, enriched), evidence=enriched
                )
            )
        findings.extend(_db_findings(path, tree))
    try:
        _assert_clean(repo)
        if _scoped_commit(repo) != commit:
            raise _HoldError("dirty-tree")
        target_after = _closure_bytes(repo)
        running_after = _running_closure_bytes()
        if target_after != closure_items:
            raise _HoldError("dirty-tree")
        if running_after != running_items or target_after != running_after:
            raise _HoldError("scanner-closure-mismatch")
    except _HoldError as hold:
        return _hold_receipt(hold.note)
    builders_sorted = sorted(builders, key=lambda b: b.path)
    counts: dict[str, int] = {}
    for item in builders_sorted:
        counts[item.taxonomy] = counts.get(item.taxonomy, 0) + 1
    violations: list[str] = []
    for item in sorted(findings, key=lambda f: (f.path, f.line, f.kind)):
        if item.kind != "explicit_fixture":
            violations.append(f"{item.kind}:{item.path}:{item.line}")
    for item in builders_sorted:
        if item.taxonomy == "unclassified":
            violations.append(f"unclassified-builder:{item.path}")
    status: Literal["PASS", "HOLD"] = "HOLD" if violations else "PASS"
    return TestDbAudit(
        scoped_commit=commit,
        scanner_sha256=scanner_digest.hexdigest(),
        source_sha256=source_digest.hexdigest(),
        collection_status="COMPLETE",
        collection_note="",
        raw_audit_status=status,
        tracked_test_files=paths,
        database_builders=tuple(builders_sorted),
        counts_by_taxonomy=dict(sorted(counts.items())),
        findings=tuple(sorted(findings, key=lambda f: (f.path, f.line, f.kind))),
        violations=tuple(sorted(violations)),
    )
