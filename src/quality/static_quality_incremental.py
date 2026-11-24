"""Exact Pyright population counts from a trusted base receipt plus a re-checked closure.

A full Pyright scan of the retained population takes minutes. A pull request
usually changes a few files, and a file's diagnostics can change only when the
file itself or a module it imports, directly or transitively, changes. This
module selects that reverse-import closure, so the gate re-checks only those
files and takes every other per-file count from the receipt of a full scan of
the exact base commit.

The selection over-approximates on purpose. An import counts as a dependency
on every repository file that it could resolve to under any Pyright search root
or the importing file's directory. A path that cannot prove the receipt applies
(missing or foreign receipt, Pyright configuration change, gate change, a
receipt population that does not reconcile) selects a full scan instead.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from quality.git_env import clean_local_git_env

RECEIPT_SCHEMA = "static-quality-pyright-files.v1"
# Pyright resolves first-party imports from the project root and
# [tool.pyright].extraPaths; it also falls back to the importing file's folder.
SEARCH_ROOTS: tuple[str, ...] = ("", "src", "execution", "scratch", "cron")
# A change to any of these can move diagnostics in files that do not import it.
FULL_SCAN_PATHS = frozenset(
    {
        "pyproject.toml",
        "requirements.lock",
        "pyrightconfig.json",
        ".github/workflows/ci.yml",
        "src/quality/static_quality_gate.py",
        "src/quality/static_quality_incremental.py",
        "src/quality/changed_suppressions.py",
    }
)
FULL_SCAN_SUFFIXES = (".pyi",)
# Above this share of the population a closure saves little and a full scan is
# the simpler evidence.
MAX_CLOSURE_SHARE = 0.8


class IncrementalUnavailableError(RuntimeError):
    """The base receipt cannot prove an exact incremental count; scan everything."""


@dataclass(frozen=True)
class Plan:
    """Files to re-check and the base counts that stay valid."""

    recheck: tuple[str, ...]
    carried: Mapping[str, int]


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            env=clean_local_git_env(),
        )
    except (OSError, UnicodeError) as exc:
        raise IncrementalUnavailableError("git unavailable") from exc
    if result.returncode:
        raise IncrementalUnavailableError(f"git {args[0]} failed ({result.returncode})")
    return result.stdout


def changed_paths(root: Path, base_commit: str) -> list[str]:
    """Every path that differs between the base commit and the working tree."""
    committed = _git(root, "diff", "--name-only", "--no-renames", "-z", base_commit, "--")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted({item for item in (committed + untracked).split("\0") if item})


# Imports are statements, so a line-anchored scan finds every one the parser
# would; it may also match import-like text in strings, which only widens the
# closure. Semicolons and backslash continuations are normalized first.
_IMPORT = re.compile(r"^[ \t]*import[ \t]+([^\n#]+)", re.MULTILINE)
_FROM = re.compile(
    r"^[ \t]*from[ \t]+(\.*)[ \t]*([\w.]*)[ \t]+import[ \t]*(\([^)]*\)|[^\n#]*)", re.MULTILINE
)
_NAME = re.compile(r"[A-Za-z_][\w.]*")
_ALIAS = re.compile(r"\bas\s+\w+")


def imports(source: str) -> set[tuple[int, str, str | None]]:
    """(relative level, module, imported name) for every import-like statement."""
    text = source.replace("\\\n", " ").replace(";", "\n")
    found: set[tuple[int, str, str | None]] = set()
    for match in _IMPORT.finditer(text):
        for part in match.group(1).split(","):
            words = part.split()
            if words:
                found.add((0, words[0], None))
    for match in _FROM.finditer(text):
        level, module = len(match.group(1)), match.group(2)
        found.add((level, module, None))
        for name in _NAME.findall(_ALIAS.sub("", match.group(3))):
            found.add((level, module, name))
    return found


def _dotted(path: PurePosixPath) -> str:
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class ImportIndex:
    """Module names each Python path answers to, under every search root and folder."""

    def __init__(self, paths: Iterable[str]) -> None:
        self.paths = set(paths)
        self.names: defaultdict[str, set[str]] = defaultdict(set)
        self.local: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
        for raw in self.paths:
            path = PurePosixPath(raw)
            for root in SEARCH_ROOTS:
                if not root or path.is_relative_to(root):
                    name = _dotted(path.relative_to(root) if root else path)
                    if name:
                        self.names[name].add(raw)
            for folder in path.parents:
                name = _dotted(path.relative_to(folder))
                if name:
                    self.local[(folder.as_posix(), name)].add(raw)

    def dependencies(self, path: str, source: str) -> set[str]:
        importer = PurePosixPath(path)
        folder = importer.parent.as_posix()
        found: set[str] = set()
        for level, module, name in imports(source):
            if level:
                base = importer.parent
                for _ in range(level - 1):
                    base = base.parent
                target = base.joinpath(*module.split(".")) if module else base
                items = [target, target / name] if name else [target]
                candidates = [f"{base.as_posix()}/__init__.py"]
                for item in items:
                    candidates += [f"{item.as_posix()}.py", f"{item.as_posix()}/__init__.py"]
                found |= {item.removeprefix("./") for item in candidates} & self.paths
                continue
            dotted = f"{module}.{name}" if name else module
            parts = dotted.split(".")
            for depth in range(1, len(parts) + 1):
                prefix = ".".join(parts[:depth])
                found |= self.names.get(prefix, set())
                found |= self.local.get((folder, prefix), set())
        found.discard(path)
        return found


def reverse_closure(sources: Mapping[str, str], seeds: Iterable[str]) -> set[str]:
    """Seeds plus every file that imports one of them, directly or transitively."""
    seed_set = set(seeds)
    index = ImportIndex(set(sources) | seed_set)
    importers: defaultdict[str, set[str]] = defaultdict(set)
    for path, source in sources.items():
        for dependency in index.dependencies(path, source):
            importers[dependency].add(path)
    closed: set[str] = set()
    queue = deque(seed_set)
    while queue:
        current = queue.popleft()
        if current in closed:
            continue
        closed.add(current)
        queue.extend(importers.get(current, ()))
    return closed


def load_receipt(path: Path, base_commit: str) -> dict[str, int]:
    """Per-file counts of a full scan of exactly ``base_commit``; fail closed otherwise."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IncrementalUnavailableError("base receipt unreadable") from exc
    if not isinstance(payload, dict):
        raise IncrementalUnavailableError("base receipt malformed")
    receipt = cast(dict[str, object], payload)
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise IncrementalUnavailableError("base receipt schema unsupported")
    if receipt.get("commit") != base_commit:
        raise IncrementalUnavailableError("base receipt belongs to another commit")
    files = receipt.get("files")
    if not isinstance(files, dict):
        raise IncrementalUnavailableError("base receipt malformed")
    counts: dict[str, int] = {}
    for key, value in cast(dict[object, object], files).items():
        if not isinstance(key, str) or not isinstance(value, int) or isinstance(value, bool):
            raise IncrementalUnavailableError("base receipt malformed")
        if value < 0:
            raise IncrementalUnavailableError("base receipt malformed")
        counts[key] = value
    return counts


def write_receipt(path: Path, commit: str, counts: Mapping[str, int]) -> None:
    payload = {"schema": RECEIPT_SCHEMA, "commit": commit, "files": dict(sorted(counts.items()))}
    path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")


def plan(
    root: Path,
    retained: Sequence[str],
    base_counts: Mapping[str, int],
    changed: Sequence[str],
) -> Plan:
    """Choose the closure to re-check, or raise when only a full scan is exact."""
    for path in changed:
        if path in FULL_SCAN_PATHS or path.endswith(FULL_SCAN_SUFFIXES):
            raise IncrementalUnavailableError(f"{path} changes Pyright inputs for every file")
    retained_set = set(retained)
    changed_set = set(changed)
    # Every retained file the change did not touch must already be in the receipt,
    # and the receipt may name only retained files or files the change removed.
    if not (retained_set - changed_set) <= set(base_counts):
        raise IncrementalUnavailableError("base receipt does not cover the unchanged population")
    if not set(base_counts) <= retained_set | changed_set:
        raise IncrementalUnavailableError("base receipt names files outside this population")
    tracked = [item for item in _git(root, "ls-files", "-z", "--", "*.py").split("\0") if item]
    candidates = sorted(set(tracked) | retained_set)
    sources: dict[str, str] = {}
    for path in candidates:
        file = root.joinpath(*PurePosixPath(path).parts)
        if file.is_file():
            try:
                sources[path] = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise IncrementalUnavailableError(f"unreadable Python file: {path}") from exc
    seeds = {path for path in changed if path.endswith(".py")}
    recheck = tuple(sorted(reverse_closure(sources, seeds) & retained_set))
    if len(recheck) > MAX_CLOSURE_SHARE * max(len(retained), 1):
        raise IncrementalUnavailableError("closure covers most of the population")
    keep = retained_set - set(recheck)
    return Plan(recheck=recheck, carried={path: base_counts[path] for path in sorted(keep)})
