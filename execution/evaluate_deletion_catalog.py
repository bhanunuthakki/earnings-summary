"""Fail-closed evaluation of approved code/test deletion candidates."""

from __future__ import annotations

import argparse
import ast
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Candidate(_ClosedModel):
    id: str = Field(min_length=1)
    authorization: str = Field(min_length=1)
    disposition: Literal["delete"]
    rollback_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    code_targets: list[str]
    test_targets: list[str]
    schema_targets: list[str]
    data_restore_exemptions: dict[str, str] = Field(default_factory=dict[str, str])
    code_restore_verified: bool
    data_restore_verified: bool
    data_restore_note: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_targets(self) -> Candidate:
        if not (self.code_targets or self.test_targets or self.schema_targets):
            raise ValueError("a deletion candidate must name at least one target")
        for targets in (self.code_targets, self.test_targets, self.schema_targets):
            if targets != sorted(targets) or len(targets) != len(set(targets)):
                raise ValueError("target lists must be sorted and unique")
        unknown_exemptions = sorted(set(self.data_restore_exemptions) - set(self.schema_targets))
        if unknown_exemptions:
            raise ValueError(
                "data restore exemptions must be schema targets: " + ",".join(unknown_exemptions)
            )
        if any(not reason.strip() for reason in self.data_restore_exemptions.values()):
            raise ValueError("data restore exemption reasons must be non-empty")
        for target in (*self.code_targets, *self.test_targets):
            parsed = PurePosixPath(target)
            if parsed.is_absolute() or ".." in parsed.parts or "\\" in target:
                raise ValueError(f"unsafe target path: {target}")
        if (self.code_targets or self.test_targets) and not self.code_restore_verified:
            raise ValueError("delete disposition requires verified Git code restore")
        unrestored_schema = sorted(set(self.schema_targets) - set(self.data_restore_exemptions))
        if unrestored_schema and not self.data_restore_verified:
            raise ValueError(
                "schema deletion requires verified data restore or an explicit "
                "per-target exemption: " + ",".join(unrestored_schema)
            )
        return self


class Catalog(_ClosedModel):
    schema_version: Literal[1]
    catalog_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    audited_head: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidates: list[Candidate] = Field(min_length=1)


class CandidateResult(_ClosedModel):
    id: str
    eligible: bool
    issues: list[str]
    data_restore_verified: bool


class Evaluation(_ClosedModel):
    valid: bool
    candidates: list[CandidateResult]


def _module_names(targets: Iterable[str]) -> set[str]:
    names: set[str] = set()
    for target in targets:
        path = PurePosixPath(target)
        if path.suffix != ".py":
            continue
        names.add(".".join(path.with_suffix("").parts))
        if path.parts[0] == "src":
            names.add(".".join(path.with_suffix("").parts[1:]))
    return names


def _normalized_module_names(parts: list[str]) -> set[str]:
    if not parts:
        return set()
    name = ".".join(parts)
    names = {name}
    if parts[0] == "src" and len(parts) > 1:
        names.add(".".join(parts[1:]))
    return names


def _imports(path: Path, repo_root: Path) -> tuple[set[str], str | None]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return set(), type(exc).__name__
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    imported.add(node.module)
                continue

            try:
                package_parts = list(path.relative_to(repo_root).with_suffix("").parts[:-1])
            except ValueError:
                continue
            parent_hops = node.level - 1
            if parent_hops > len(package_parts):
                continue
            base_parts = package_parts[: len(package_parts) - parent_hops]
            if node.module:
                module_parts = [*base_parts, *node.module.split(".")]
                imported.update(_normalized_module_names(module_parts))
            else:
                for alias in node.names:
                    imported.update(_normalized_module_names([*base_parts, *alias.name.split(".")]))
    return imported, None


def _active_imports(repo_root: Path, modules: set[str]) -> list[str]:
    if not modules:
        return []
    findings: list[str] = []
    for root_name in ("src", "execution", "cron", "tests"):
        root = repo_root / root_name
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            imports, error = _imports(path, repo_root)
            if error is not None:
                findings.append(f"scan_error:{path.relative_to(repo_root).as_posix()}:{error}")
                continue
            for imported in imports:
                if any(
                    imported == module or imported.startswith(module + ".") for module in modules
                ):
                    findings.append(f"{path.relative_to(repo_root).as_posix()} -> {imported}")
    return sorted(findings)


def _git_head_issues(repo_root: Path, audited_head: str) -> list[str]:
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{audited_head}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if exists.returncode != 0:
        return ["audited_head_missing"]
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", audited_head, "HEAD"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    return [] if ancestor.returncode == 0 else ["audited_head_not_ancestor"]


def migration_schema_targets(repo_root: Path) -> tuple[set[str], str | None]:
    path = repo_root / "alembic" / "versions" / "0002_drop_dead_tables.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return set(), type(exc).__name__
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "DEAD_TABLES" for target in node.targets
        ):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            return set(), "invalid_DEAD_TABLES"
        if isinstance(value, (list, tuple)):
            items = cast(list[object] | tuple[object, ...], value)
            if all(isinstance(item, str) for item in items):
                return {item for item in items if isinstance(item, str)}, None
        return set(), "invalid_DEAD_TABLES"
    return set(), "missing_DEAD_TABLES"


def _missing_git_blobs(repo_root: Path, commit: str, targets: Sequence[str]) -> list[str]:
    if not targets:
        return []
    specs = [f"{commit}:{target}" for target in targets]
    result = subprocess.run(
        ["git", "cat-file", "--batch-check"],
        cwd=repo_root,
        input="\n".join(specs) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return list(targets)
    lines = result.stdout.splitlines()
    return [
        target for target, line in zip(targets, lines, strict=True) if line.endswith(" missing")
    ]


def evaluate(repo_root: Path, catalog: Catalog) -> Evaluation:
    results: list[CandidateResult] = []
    global_issues = _git_head_issues(repo_root, catalog.audited_head)
    deleted_schema, schema_error = migration_schema_targets(repo_root)
    cataloged_schema = {
        target for candidate in catalog.candidates for target in candidate.schema_targets
    }
    if schema_error is not None:
        global_issues.append("schema_scan_error:" + schema_error)
    else:
        missing_schema = sorted(cataloged_schema - deleted_schema)
        if missing_schema:
            global_issues.append("schema_targets_not_deleted:" + ",".join(missing_schema))
        uncataloged_schema = sorted(deleted_schema - cataloged_schema)
        if uncataloged_schema:
            global_issues.append("deleted_schema_not_cataloged:" + ",".join(uncataloged_schema))
    for candidate in catalog.candidates:
        issues = list(global_issues)
        targets = (*candidate.code_targets, *candidate.test_targets)
        present = [target for target in targets if (repo_root / target).exists()]
        if present:
            issues.append("targets_still_present:" + ",".join(present))
        missing_restore = _missing_git_blobs(repo_root, candidate.rollback_commit, list(targets))
        if missing_restore:
            issues.append("rollback_blob_missing:" + ",".join(missing_restore))
        unrestored_schema = sorted(
            set(candidate.schema_targets) - set(candidate.data_restore_exemptions)
        )
        if unrestored_schema and not candidate.data_restore_verified:
            issues.append("data_restore_unverified:" + ",".join(unrestored_schema))
        live_imports = _active_imports(repo_root, _module_names(candidate.code_targets))
        if live_imports:
            issues.append("active_imports:" + ",".join(live_imports))
        results.append(
            CandidateResult(
                id=candidate.id,
                eligible=not issues,
                issues=issues,
                data_restore_verified=candidate.data_restore_verified,
            )
        )
    return Evaluation(valid=all(result.eligible for result in results), candidates=results)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    catalog = Catalog.model_validate_json(args.catalog.read_text(encoding="utf-8"))
    report = evaluate(args.repo_root.resolve(), catalog)
    print(report.model_dump_json(indent=2))
    return 0 if report.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
