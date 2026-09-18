"""Build a review-only ownership inventory for the current SQLite schema."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import BaseModel, ConfigDict, Field

from alembic import command
from quality.atomic_write import write_text_atomic
from quality.git_env import clean_local_git_env
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

SCHEMA_VERSION = "schema-ownership-inventory/v1"
PRODUCT_ROOTS = ("src/", "execution/", "cron/", "scripts/", ".github/scripts/")
MIGRATION_ROOT = "alembic/versions/"
CREATE_RE = re.compile(
    r"\ACREATE\s+(?:VIRTUAL\s+)?(?P<kind>TABLE|VIEW)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?:"
    r'"(?P<double_name>[^"]+)"|'
    r"`(?P<backtick_name>[^`]+)`|"
    r"\[(?P<bracket_name>[^\]]+)\]|"
    r"'(?P<single_name>[^']+)'|"
    r"(?P<bare_name>[A-Za-z_][A-Za-z0-9_]*))(?=\s|\(|$)",
    re.IGNORECASE,
)
MAX_STDOUT_BYTES = 100_000

ObjectKind = Literal["table", "view", "virtual"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SchemaObjectEntry(StrictModel):
    name: str
    kind: ObjectKind
    schema_revision: str
    readers: tuple[str, ...]
    writers: tuple[str, ...]
    historical_evidence: tuple[str, ...]
    recovery_owner: str | None
    ownership: Literal["owned", "unowned"]
    ddl_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SchemaOwnershipInventory(StrictModel):
    schema_version: Literal["schema-ownership-inventory/v1"] = SCHEMA_VERSION
    subject_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scanner_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    python_version: str
    sqlite_version: str
    schema_revision: str
    deletion_authority: Literal[False] = False
    files_scanned: int = Field(ge=0)
    objects_scanned: int = Field(ge=0)
    counts: dict[str, int]
    definitions: dict[str, str]
    entries: tuple[SchemaObjectEntry, ...]
    status: Literal["PASS", "HOLD"]
    violations: tuple[str, ...]


class SchemaOwnershipError(RuntimeError):
    """The schema inventory could not be collected safely."""


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            env=clean_local_git_env(),
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SchemaOwnershipError(f"git {' '.join(args)} failed") from exc


def _tracked_sources(root: Path) -> list[tuple[str, bytes]]:
    raw = _git(root, "ls-files", "-z", "--", "*.py")
    selected = sorted(
        path
        for path in raw.decode().split("\0")
        if path
        and (path.startswith(PRODUCT_ROOTS) or path.startswith(MIGRATION_ROOT))
        and not path.startswith("alembic/versions_archived/")
    )
    sources: list[tuple[str, bytes]] = []
    resolved_root = root.resolve()
    for relative in selected:
        pure = PurePosixPath(relative)
        path = (root / relative).resolve()
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or not path.is_relative_to(resolved_root)
            or not path.is_file()
            or path.is_symlink()
        ):
            raise SchemaOwnershipError(f"tracked Python file is missing or unsafe: {relative}")
        try:
            sources.append((relative, path.read_bytes()))
        except OSError as exc:
            raise SchemaOwnershipError(f"tracked Python file is unreadable: {relative}") from exc
    return sources


def _source_manifest_hash(sources: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for path, raw in sources:
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(raw).digest())
    return digest.hexdigest()


def _assignment(tree: ast.Module, name: str) -> str | tuple[str, ...] | None:
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            if node.value is None:
                return None
            try:
                evaluated: object = ast.literal_eval(node.value)
            except (TypeError, ValueError):
                return None
            if isinstance(evaluated, str):
                return evaluated
            if isinstance(evaluated, tuple):
                items = cast(tuple[object, ...], evaluated)
                if all(isinstance(item, str) for item in items):
                    return tuple(item for item in items if isinstance(item, str))
            return None
    return None


def _migration_ranks(
    sources: list[tuple[str, bytes]],
) -> tuple[dict[str, tuple[str, int]], dict[str, int], list[str]]:
    metadata: dict[str, tuple[tuple[str, ...], str]] = {}
    errors: list[str] = []
    for path, raw in sources:
        if not path.startswith(MIGRATION_ROOT):
            continue
        try:
            tree = ast.parse(raw.decode("utf-8-sig"), filename=path)
        except (SyntaxError, UnicodeDecodeError):
            errors.append(f"{path}: migration metadata is unreadable")
            continue
        revision = _assignment(tree, "revision")
        down = _assignment(tree, "down_revision")
        if not isinstance(revision, str):
            errors.append(f"{path}: migration revision is missing")
            continue
        if down is None:
            parents: tuple[str, ...] = ()
        elif isinstance(down, str):
            parents = (down,)
        else:
            parents = down
        metadata[revision] = (parents, path)

    ranks: dict[str, int] = {}
    while len(ranks) < len(metadata):
        progressed = False
        for revision, (parents, _) in metadata.items():
            if revision in ranks:
                continue
            if all(parent in ranks for parent in parents):
                ranks[revision] = max((ranks[parent] for parent in parents), default=-1) + 1
                progressed = True
        if not progressed:
            unresolved = sorted(set(metadata) - set(ranks))
            errors.append("migration graph is incomplete or cyclic: " + ", ".join(unresolved))
            break
    locations = {
        revision: (path, ranks.get(revision, -1)) for revision, (_, path) in metadata.items()
    }
    return locations, ranks, errors


def _static_migration_string(node: ast.expr, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "text":
            return _static_migration_string(node.args[0], constants)
    return None


class _BindingVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.names.add(node.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _bound_names(statements: list[ast.stmt] | tuple[ast.stmt, ...]) -> set[str]:
    visitor = _BindingVisitor()
    for statement in statements:
        visitor.visit(statement)
    return visitor.names


def _created_schema_names(sql: str) -> tuple[str, ...]:
    statements: list[str] = []
    current: list[str] = []
    index = 0
    state = "normal"
    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if state == "line-comment":
            if char == "\n":
                state = "normal"
                current.append(char)
            else:
                current.append(" ")
        elif state == "block-comment":
            if char == "*" and following == "/":
                current.extend((" ", " "))
                index += 1
                state = "normal"
            else:
                current.append("\n" if char == "\n" else " ")
        elif state == "single-quote":
            current.append(char)
            if char == "'" and following == "'":
                current.append(following)
                index += 1
            elif char == "'":
                state = "normal"
        elif state in {"double-quote", "backtick", "bracket"}:
            current.append(char)
            closing = {"double-quote": '"', "backtick": "`", "bracket": "]"}[state]
            if char == closing:
                if state != "bracket" and following == closing:
                    current.append(following)
                    index += 1
                else:
                    state = "normal"
        elif char == "-" and following == "-":
            current.extend((" ", " "))
            index += 1
            state = "line-comment"
        elif char == "/" and following == "*":
            current.extend((" ", " "))
            index += 1
            state = "block-comment"
        elif char == "'":
            current.append(char)
            state = "single-quote"
        elif char == '"':
            current.append(char)
            state = "double-quote"
        elif char == "`":
            current.append(char)
            state = "backtick"
        elif char == "[":
            current.append(char)
            state = "bracket"
        elif char == ";":
            statements.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    statements.append("".join(current))
    names: list[str] = []
    for statement in statements:
        match = CREATE_RE.match(statement.lstrip())
        if match is not None:
            name = next(
                group
                for group in (
                    match.group("double_name"),
                    match.group("backtick_name"),
                    match.group("bracket_name"),
                    match.group("single_name"),
                    match.group("bare_name"),
                )
                if group is not None
            )
            names.append(name)
    return tuple(names)


def _record_upgrade_evidence(
    tree: ast.Module,
    *,
    path: str,
    rank: int,
    revision: str,
    evidence: dict[str, list[tuple[int, str, str]]],
) -> None:
    constants: dict[str, str] = {}
    functions: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            is_generator = any(
                isinstance(child, (ast.Yield, ast.YieldFrom)) for child in ast.walk(node)
            )
            if node.decorator_list or is_generator:
                functions.pop(node.name, None)
            else:
                functions[node.name] = node
            continue
        for bound_name in _bound_names([node]):
            functions.pop(bound_name, None)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            try:
                value: object = ast.literal_eval(node.value)
            except (TypeError, ValueError):
                continue
            if isinstance(value, str):
                for target in targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = value

    visited: set[str] = set()

    def visit_function(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        function = functions.get(name)
        if function is None:
            return
        lexical_bindings = _bound_names(function.body)
        function_constants = {
            key: value for key, value in constants.items() if key not in lexical_bindings
        }
        callable_functions = set(functions) - lexical_bindings

        def record_call(node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id in callable_functions:
                visit_function(node.func.id)
            if not node.args or not isinstance(node.func, ast.Attribute):
                return
            if node.func.attr == "create_table":
                table_name = _static_migration_string(node.args[0], function_constants)
                if table_name is not None:
                    evidence[table_name].append((rank, revision, f"{path}:{node.lineno}"))
                return
            if node.func.attr != "execute":
                return
            sql = _static_migration_string(node.args[0], function_constants)
            if sql is None:
                return
            for schema_name in _created_schema_names(sql):
                evidence[schema_name].append((rank, revision, f"{path}:{node.lineno}"))

        for statement in function.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                if statement.value is None:
                    continue
                targets = (
                    statement.targets if isinstance(statement, ast.Assign) else (statement.target,)
                )
                target_names = _bound_names([statement])
                for target_name in target_names:
                    function_constants.pop(target_name, None)
                try:
                    value: object = ast.literal_eval(statement.value)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, str):
                    simple_targets = tuple(
                        target.id for target in targets if isinstance(target, ast.Name)
                    )
                    for target_name in simple_targets:
                        function_constants[target_name] = value
                continue
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                record_call(statement.value)
                continue
            if isinstance(statement, ast.Return):
                if isinstance(statement.value, ast.Call):
                    record_call(statement.value)
                break
            if isinstance(statement, ast.Raise):
                break

    visit_function("upgrade")


def _migration_evidence(
    sources: list[tuple[str, bytes]],
) -> tuple[dict[str, list[tuple[int, str, str]]], list[str]]:
    locations, _, errors = _migration_ranks(sources)
    by_path = {path: (revision, rank) for revision, (path, rank) in locations.items()}
    evidence: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for path, raw in sources:
        if path not in by_path:
            continue
        revision, rank = by_path[path]
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError:
            continue
        _record_upgrade_evidence(
            tree,
            path=path,
            rank=rank,
            revision=revision,
            evidence=evidence,
        )
    for records in evidence.values():
        records.sort(key=lambda item: (item[0], item[2], item[1]))
    return evidence, errors


def _sql_references(
    sources: list[tuple[str, bytes]], names: tuple[str, ...]
) -> tuple[dict[str, set[str]], dict[str, set[str]], list[str]]:
    readers: dict[str, set[str]] = defaultdict(set)
    writers: dict[str, set[str]] = defaultdict(set)
    errors: list[str] = []
    canonical_names = {name.casefold(): name for name in names}
    identifier = r'["`\[]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)["`\]]?'
    read_pattern = re.compile(rf"\b(?:FROM|JOIN)\s+(?:main\.)?{identifier}", re.I)
    write_pattern = re.compile(
        rf"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+"
        rf"(?:main\.)?{identifier}",
        re.I,
    )
    for path, raw in sources:
        if not path.startswith(PRODUCT_ROOTS):
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            errors.append(f"{path}: product source is not UTF-8")
            continue
        for match in read_pattern.finditer(text):
            name = canonical_names.get(match.group("name").casefold())
            if name is not None:
                readers[name].add(f"{path}:{text.count(chr(10), 0, match.start()) + 1}")
        for match in write_pattern.finditer(text):
            name = canonical_names.get(match.group("name").casefold())
            if name is not None:
                writers[name].add(f"{path}:{text.count(chr(10), 0, match.start()) + 1}")
    return readers, writers, errors


def _schema_objects(database: Path) -> list[tuple[str, ObjectKind, str]]:
    try:
        connection = connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error as exc:
        raise SchemaOwnershipError(
            "disposable schema database could not be opened read-only"
        ) from exc
    try:
        table_types = {
            str(row[1]): str(row[2])
            for row in connection.execute("PRAGMA main.table_list")
            if str(row[1]).lower() not in {"sqlite_schema", "sqlite_temp_schema"}
        }
        rows = connection.execute(
            "SELECT name, type, COALESCE(sql, '') FROM sqlite_master "
            "WHERE type IN ('table', 'view') ORDER BY name"
        ).fetchall()
    except sqlite3.Error as exc:
        raise SchemaOwnershipError("disposable schema database could not be inspected") from exc
    finally:
        connection.close()
    objects: list[tuple[str, ObjectKind, str]] = []
    for raw_name, raw_kind, raw_ddl in rows:
        name = str(raw_name)
        if name.casefold().startswith("sqlite_"):
            continue
        table_type = table_types.get(name, str(raw_kind))
        if table_type == "shadow":
            continue
        kind: ObjectKind
        if table_type == "virtual":
            kind = "virtual"
        elif raw_kind == "view":
            kind = "view"
        else:
            kind = "table"
        objects.append((name, kind, str(raw_ddl)))
    return objects


def inventory_database(
    root: Path, database: Path, *, schema_revision: str
) -> SchemaOwnershipInventory:
    root = root.resolve()
    sources = _tracked_sources(root)
    objects = _schema_objects(database)
    names = tuple(name for name, _, _ in objects)
    historical, migration_errors = _migration_evidence(sources)
    readers, writers, source_errors = _sql_references(sources, names)
    entries: list[SchemaObjectEntry] = []
    violations = [*migration_errors, *source_errors]
    schema_digest = hashlib.sha256()
    for name, kind, ddl in objects:
        schema_digest.update(name.encode())
        schema_digest.update(b"\0")
        schema_digest.update(kind.encode())
        schema_digest.update(b"\0")
        schema_digest.update(ddl.encode())
        records = historical.get(name, [])
        if name == "alembic_version":
            object_revision = schema_revision
            evidence = ("alembic:version-table",)
            recovery_owner = "alembic:version-table"
        elif records:
            _, object_revision, latest = records[-1]
            evidence = tuple(sorted({record[2] for record in records}))
            recovery_owner = latest
        else:
            object_revision = schema_revision
            evidence = ()
            recovery_owner = None
            violations.append(f"{kind} {name}: no active migration recovery owner")
        ownership = "owned" if recovery_owner is not None else "unowned"
        entries.append(
            SchemaObjectEntry(
                name=name,
                kind=kind,
                schema_revision=object_revision,
                readers=tuple(sorted(readers.get(name, set()))),
                writers=tuple(sorted(writers.get(name, set()))),
                historical_evidence=evidence,
                recovery_owner=recovery_owner,
                ownership=ownership,
                ddl_sha256=hashlib.sha256(ddl.encode()).hexdigest(),
            )
        )
    counts = Counter(entry.kind for entry in entries)
    ownership_counts = Counter(entry.ownership for entry in entries)
    commit = _git(root, "rev-parse", "HEAD").decode().strip()
    return SchemaOwnershipInventory(
        subject_commit=commit,
        source_manifest_sha256=_source_manifest_hash(sources),
        scanner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        database_schema_sha256=schema_digest.hexdigest(),
        python_version=platform.python_version(),
        sqlite_version=sqlite3.sqlite_version,
        schema_revision=schema_revision,
        files_scanned=len(sources),
        objects_scanned=len(entries),
        counts={
            "table": counts.get("table", 0),
            "view": counts.get("view", 0),
            "virtual": counts.get("virtual", 0),
            "owned": ownership_counts.get("owned", 0),
            "unowned": ownership_counts.get("unowned", 0),
            "with_readers": sum(bool(entry.readers) for entry in entries),
            "with_writers": sum(bool(entry.writers) for entry in entries),
        },
        definitions={
            "owned": "An active migration or Alembic itself provides a recovery owner.",
            "unowned": "No active migration recovery owner was found; status is HOLD.",
            "reader": "A tracked product source contains a static SQL FROM or JOIN reference.",
            "writer": "A tracked product source contains a static SQL mutation reference.",
            "scope": "Review evidence only; this inventory never authorizes deletion or schema mutation.",
        },
        entries=tuple(entries),
        status="HOLD" if violations else "PASS",
        violations=tuple(sorted(set(violations))),
    )


def _alembic_config(root: Path, database: Path) -> Config:
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


def build_inventory(root: Path) -> SchemaOwnershipInventory:
    root = root.resolve()
    with tempfile.TemporaryDirectory(prefix="schema-ownership-") as temporary:
        database = Path(temporary) / "inventory.db"
        config = _alembic_config(root, database)
        try:
            revision = ScriptDirectory.from_config(config).get_current_head()
            if revision is None:
                raise SchemaOwnershipError("Alembic has no current head")
            command.upgrade(config, "head")
        except SchemaOwnershipError:
            raise
        except Exception as exc:
            raise SchemaOwnershipError("disposable Alembic migration failed") from exc
        return inventory_database(root, database, schema_revision=revision)


def _safe_output_path(root: Path, requested: Path) -> Path:
    resolved_root = root.resolve()
    lexical = requested if requested.is_absolute() else resolved_root / requested
    declared_tmp = resolved_root / ".tmp"
    if declared_tmp.is_symlink():
        raise SchemaOwnershipError("repository .tmp directory cannot be a symlink")
    try:
        resolved_tmp = declared_tmp.resolve()
        resolved_output = lexical.resolve()
    except OSError as exc:
        raise SchemaOwnershipError("output path cannot be resolved safely") from exc
    if not resolved_tmp.is_relative_to(resolved_root) or not resolved_output.is_relative_to(
        resolved_tmp
    ):
        raise SchemaOwnershipError("output must remain under the repository .tmp directory")
    try:
        if lexical.exists() and lexical.stat().st_nlink > 1:
            raise SchemaOwnershipError("output aliases another file")
    except OSError as exc:
        raise SchemaOwnershipError("output path cannot be inspected safely") from exc
    return resolved_output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        repo_root = args.repo_root.resolve()
        inventory = build_inventory(repo_root)
        payload = inventory.model_dump_json(indent=2) + "\n"
        if args.output is not None:
            output = _safe_output_path(repo_root, args.output)
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
                repo_root, Path(".tmp/quality/schema-ownership-inventory.json")
            )
            write_text_atomic(output, payload)
            print(
                json.dumps(
                    {
                        "output": str(output.relative_to(repo_root)),
                        "status": inventory.status,
                        "counts": inventory.counts,
                    },
                    sort_keys=True,
                )
            )
        else:
            sys.stdout.write(payload)
        return 0 if inventory.status == "PASS" else 2
    except (SchemaOwnershipError, OSError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
