"""Reject new architecture drift in the two clearest growth seams.

The check is baseline-based:

- existing ``execution`` sys.path mutation remains tolerated until it is
  explicitly removed from the allowlist in ``config/architecture_boundaries.json``;
- existing loose ``src/*.py`` root modules remain tolerated for the same reason;
- sanctioned execution helpers are explicit config-owned exceptions, and stale
  helper entries fail so the allowlist never drifts away from the tree.

The goal is not to police line counts. It is to keep new work flowing into the
right packages and to stop new ad-hoc entrypoint scaffolding from spreading.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "architecture_boundaries.json"

_MUTATING_METHODS = frozenset(
    {"append", "clear", "extend", "insert", "pop", "remove", "reverse", "sort"}
)


def _repo_relative(text: str) -> str:
    path = Path(text)
    if path.is_absolute():
        raise ValueError(f"expected repo-relative path, got absolute path: {text!r}")
    if any(part == ".." for part in path.parts):
        raise ValueError(f"expected repo-relative path, got parent traversal: {text!r}")
    if not text:
        raise ValueError("expected non-empty path")
    return text.replace("\\", "/")


def _load_config(path: Path) -> dict[str, list[str]]:
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {path.relative_to(PROJECT_ROOT)}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"cannot parse {path.relative_to(PROJECT_ROOT)}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("architecture boundary config must be a JSON object")

    typed = cast(dict[object, object], payload)
    expected_keys = {
        "sanctioned_execution_helpers",
        "execution_sys_path_mutations",
        "root_src_modules",
    }
    missing = sorted(expected_keys - typed.keys())
    if missing:
        raise ValueError(f"architecture boundary config missing keys: {missing}")

    config: dict[str, list[str]] = {}
    for key in sorted(expected_keys):
        value = typed[key]
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list of repo-relative paths")
        normalized: list[str] = []
        items = cast(list[object], value)
        for item in items:
            if not isinstance(item, str):
                raise ValueError(f"{key} entries must be strings")
            normalized.append(_repo_relative(item))
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{key} contains duplicate paths")
        config[key] = sorted(normalized)
    return config


def _is_sys_path_attribute(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
        and node.attr == "path"
    )


def _is_sys_path_mutating_call(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and _is_sys_path_attribute(node.func.value)
        and node.func.attr in _MUTATING_METHODS
    )


def _is_sys_path_assignment_target(target: ast.AST) -> bool:
    if isinstance(target, ast.Attribute):
        return _is_sys_path_attribute(target)
    return isinstance(target, ast.Subscript) and _is_sys_path_attribute(target.value)


def _has_sys_path_mutation(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_sys_path_mutating_call(node):
            return True
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if _is_sys_path_assignment_target(target):
                    return True
        if isinstance(node, ast.AugAssign) and _is_sys_path_assignment_target(node.target):
            return True
    return False


def collect_current_inventory(
    root: Path | None = None, sanctioned_helpers: list[str] | None = None
) -> dict[str, list[str]]:
    """Return the current architecture-seam inventory for ``root``."""
    repo_root = PROJECT_ROOT if root is None else root
    execution_root = repo_root / "execution"
    src_root = repo_root / "src"

    helper_allowlist = (
        [
            "execution/_lib.py",
            "execution/sqlite_bootstrap.py",
        ]
        if sanctioned_helpers is None
        else sanctioned_helpers
    )
    helper_set = set(helper_allowlist)

    current_helpers: list[str] = []
    sys_path_mutations: list[str] = []
    if execution_root.exists():
        for path in sorted(execution_root.rglob("*.py")):
            rel = path.relative_to(repo_root).as_posix()
            if rel in helper_set:
                current_helpers.append(rel)
                continue
            if _has_sys_path_mutation(path):
                sys_path_mutations.append(rel)

    root_modules = []
    if src_root.exists():
        root_modules = [
            path.relative_to(repo_root).as_posix() for path in sorted(src_root.glob("*.py"))
        ]

    return {
        "sanctioned_execution_helpers": sorted(current_helpers),
        "execution_sys_path_mutations": sys_path_mutations,
        "root_src_modules": root_modules,
    }


def validate(root: Path | None = None, config_path: Path | None = None) -> list[str]:
    """Return human-readable failures for new architecture drift."""
    repo_root = PROJECT_ROOT if root is None else root
    boundary_path = CONFIG_PATH if config_path is None else config_path

    try:
        config = _load_config(boundary_path)
    except ValueError as exc:
        return [str(exc)]

    current = collect_current_inventory(repo_root, config["sanctioned_execution_helpers"])
    failures: list[str] = []

    stale_helpers = sorted(
        set(config["sanctioned_execution_helpers"]) - set(current["sanctioned_execution_helpers"])
    )
    if stale_helpers:
        failures.append("stale sanctioned execution helpers in config: " + ", ".join(stale_helpers))

    new_mutations = sorted(
        set(current["execution_sys_path_mutations"]) - set(config["execution_sys_path_mutations"])
    )
    if new_mutations:
        failures.append(
            "new execution sys.path mutations outside the allowlist: " + ", ".join(new_mutations)
        )

    stale_mutations = sorted(
        set(config["execution_sys_path_mutations"]) - set(current["execution_sys_path_mutations"])
    )
    if stale_mutations:
        failures.append(
            "stale execution sys.path mutations in config: " + ", ".join(stale_mutations)
        )

    new_root_modules = sorted(set(current["root_src_modules"]) - set(config["root_src_modules"]))
    if new_root_modules:
        failures.append(
            "new loose src root modules outside the allowlist: " + ", ".join(new_root_modules)
        )

    stale_root_modules = sorted(set(config["root_src_modules"]) - set(current["root_src_modules"]))
    if stale_root_modules:
        failures.append("stale loose src root modules in config: " + ", ".join(stale_root_modules))

    return failures


def main() -> int:
    failures = validate()
    if failures:
        print("architecture-boundaries: FAILED", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("architecture-boundaries: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
