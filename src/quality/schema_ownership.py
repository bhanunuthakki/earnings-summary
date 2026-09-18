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

SCHEMA_VERSION = "schema-ownership-inventory/v1"
PRODUCT_ROOTS = ("src/", "execution/", "cron/", "scripts/", ".github/scripts/")
MIGRATION_ROOT = "alembic/versions/"
CREATE_RE = re.compile(
    r"\bCREATE\s+(?:VIRTUAL\s+)?(?P<kind>TABLE|VIEW)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?[\"`\[]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
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
        for match in CREATE_RE.finditer(text):
            name = match.group("name")
            line = text.count("\n", 0, match.start()) + 1
            evidence[name].append((rank, revision, f"{path}:{line}"))
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "create_table"
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                continue
            name = node.args[0].value
            evidence[name].append((rank, revision, f"{path}:{node.lineno}"))
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
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
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
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    except sqlite3.Error as exc:
        raise SchemaOwnershipError("disposable schema database could not be inspected") from exc
    finally:
        connection.close()
    objects: list[tuple[str, ObjectKind, str]] = []
    for raw_name, raw_kind, raw_ddl in rows:
        name = str(raw_name)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        inventory = build_inventory(args.repo_root)
        payload = inventory.model_dump_json(indent=2) + "\n"
        if args.output is not None:
            output = args.output if args.output.is_absolute() else args.repo_root / args.output
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
            output = args.repo_root / ".tmp/quality/schema-ownership-inventory.json"
            write_text_atomic(output, payload)
            print(
                json.dumps(
                    {
                        "output": str(output.relative_to(args.repo_root)),
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
