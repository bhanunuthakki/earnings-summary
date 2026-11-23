"""Choose the test files a pull request can affect, or say the full suite is required.

The main branch records, for every test file, which repository functions its
run executed (``quality.test_impact_recorder``). A pull request then runs only
the test files that executed code its diff alters (``quality.change_regions``):

- a changed function body selects the tests that ran that function;
- a changed class selects the tests that ran any of its code, and the code that
  reads the class name;
- a rebound module name (constant, import, type alias) selects the code that
  reads that name, in the module and in its importers, transitively;
- a function that ran during an import or caches its result also selects the
  code that reads its name, because later tests reuse its result unseen;
- a module-level statement that binds nothing, or a registration decorator,
  selects every test that imports the module, statically and transitively;
- a changed data file selects the code that names it.

Static rules add tests that the recording cannot see: tests that name a changed
file (subprocess entry points, fixtures), tests that name a ledger listing a
changed file, and tests that scan the whole repository. A test file missing
from the receipt always runs. A change to a shared test input (conftest,
migrations, dependency locks, pytest settings, CI, this selector) or a missing
receipt for the exact base commit needs the full suite. Selection only shortens
pull-request feedback: main and the merge queue always run the full suite.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import json
import re
import subprocess
import sys
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import cast

from quality import change_regions
from quality.git_env import clean_local_git_env
from quality.static_quality_incremental import ImportIndex
from quality.test_impact_recorder import MODULE, RECEIPT_SCHEMA

TEST_ROOTS = ("tests/",)
FULL_SUITE_PATHS = frozenset(
    {
        "pyproject.toml",
        "requirements.lock",
        "requirements-browser.lock",
        "requirements.txt",
        "alembic.ini",
        ".github/workflows/ci.yml",
        ".github/scripts/ci_gate.py",
        ".github/test-durations.json",
        "src/quality/test_selection.py",
        "src/quality/change_regions.py",
        "src/quality/test_impact_recorder.py",
        "src/quality/test_impact_plugin.py",
        "src/quality/static_quality_incremental.py",
    }
)
FULL_SUITE_PREFIXES = ("alembic/",)
FULL_SUITE_NAMES = frozenset({"conftest.py", "pytest.ini", "setup.cfg", "tox.ini"})
# Names too common to identify one file; references to them prove nothing.
GENERIC_NAMES = frozenset(
    {"__init__.py", "README.md", "readme.md", "__main__.py", "index.html", "index.ts"}
)
# Folder names too common to say which files code reads through them.
GENERIC_FOLDERS = frozenset({"tests", "fixtures", "data", "src", "docs", "execution", "scratch"})
# A quoted stem shorter than this, or quoted in more files than this, is noise.
MIN_STEM = 4
MAX_STEM_REFERENCES = 40
# Above this share of test seconds a selection saves little; run everything.
MAX_SELECTED_SHARE = 0.8
_TOKEN = re.compile(r"[\w.-]+")
_QUOTED = re.compile(r"""["']([^"'\n]{1,300})["']""")
_SPLIT = re.compile(r"[/\\:\s]+")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
# A test that globs from a repository constant, lists git files, walks trees or
# builds the repository graph depends on files it never names.
_REPO_SCAN = re.compile(
    r"\b[A-Z][A-Z0-9_]*(?:\s*/\s*[\"'][^\"']*[\"'])*\s*\)?\.(?:rglob|glob|iterdir)\("
    r"|ls-files|os\.walk\(|build_graph\(|_retained_python_files|Path\.cwd\(\)\.(?:rglob|glob)\("
)


@dataclass(frozen=True)
class Selection:
    """Either ``full`` with a reason, or the exact test files to run."""

    full: bool
    reason: str
    files: tuple[str, ...] = ()
    why: Mapping[str, str] = field(default_factory=dict[str, str])


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        env=clean_local_git_env(),
    )
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed ({result.returncode})")
    return result.stdout


def is_test_file(path: str) -> bool:
    return (
        path.startswith(TEST_ROOTS)
        and PurePosixPath(path).name.startswith("test_")
        and (path.endswith(".py"))
    )


class ReferenceIndex:
    """Which text files name a file, by basename or by quoted module stem."""

    def __init__(self, texts: Mapping[str, str]) -> None:
        self.tokens: defaultdict[str, set[str]] = defaultdict(set)
        self.quoted: defaultdict[str, set[str]] = defaultdict(set)
        for path, text in texts.items():
            for token in set(_TOKEN.findall(text)):
                self.tokens[token].add(path)
            pieces: set[str] = set()
            for literal in _QUOTED.findall(text):
                for piece in _SPLIT.split(literal):
                    if piece:
                        pieces.add(piece)
                        pieces.add(piece.removesuffix(".py"))
                        pieces.update(part for part in piece.split(".") if part)
            for piece in pieces:
                self.quoted[piece].add(path)

    def referrers(self, path: str) -> set[str]:
        """Files that name ``path``: its basename anywhere, or a Python stem in quotes."""
        name = PurePosixPath(path).name
        found: set[str] = set()
        if name not in GENERIC_NAMES and "." in name:
            found |= self.tokens.get(name, set())
        if path.endswith(".py"):
            stem = PurePosixPath(path).stem
            quoted = self.quoted.get(stem, set())
            if len(stem) >= MIN_STEM and len(quoted) <= MAX_STEM_REFERENCES:
                found |= quoted
        found.discard(path)
        return found


def _read_texts(root: Path, paths: Iterable[str]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for path in paths:
        file = root.joinpath(*PurePosixPath(path).parts)
        try:
            if file.stat().st_size > 2_000_000:
                continue
            raw = file.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue
        try:
            texts[path] = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return texts


def repository_scanners(texts: Mapping[str, str]) -> set[str]:
    """Test files that read the repository as a whole and so depend on any file."""
    return {path for path, text in texts.items() if is_test_file(path) and _REPO_SCAN.search(text)}


class FullSuiteRequiredError(Exception):
    """Selection cannot bound this change; run every test."""


@dataclass(frozen=True)
class Receipt:
    """The base commit's record of which test files ran which code."""

    tests: frozenset[str]
    by_key: Mapping[tuple[str, str], frozenset[str]]
    by_path: Mapping[str, Mapping[str, frozenset[str]]]
    import_time: frozenset[tuple[str, str]]
    everywhere: frozenset[tuple[str, str]]


def load_receipt(path: Path, base_commit: str) -> Receipt:
    """Read a merged receipt; fail closed unless it records exactly ``base_commit``."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FullSuiteRequiredError("test impact receipt unreadable") from exc
    if not isinstance(payload, dict):
        raise FullSuiteRequiredError("test impact receipt malformed")
    data = cast(dict[str, object], payload)
    if data.get("schema") != RECEIPT_SCHEMA:
        raise FullSuiteRequiredError("test impact receipt schema unsupported")
    if data.get("commit") != base_commit:
        raise FullSuiteRequiredError("test impact receipt belongs to another commit")
    try:
        tests = [str(item) for item in cast(list[object], data["tests"])]
        keys = [(str(item[0]), str(item[1])) for item in cast(list[list[object]], data["keys"])]
        runs = cast(dict[str, list[int]], data["runs"])
        by_key = {
            keys[int(key)]: frozenset(tests[index] for index in owners)
            for key, owners in runs.items()
        }
        import_time = frozenset(keys[index] for index in cast(list[int], data["import_time"]))
        everywhere = frozenset(keys[index] for index in cast(list[int], data["everywhere"]))
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise FullSuiteRequiredError("test impact receipt malformed") from exc
    by_path: defaultdict[str, dict[str, frozenset[str]]] = defaultdict(dict)
    for (file, qualname), owners in by_key.items():
        by_path[file][qualname] = owners
    return Receipt(frozenset(tests), by_key, dict(by_path), import_time, everywhere)


def diff_hunks(root: Path, base: str, path: str) -> list[tuple[range, range]]:
    """Changed line ranges, (base side, head side), for each hunk of ``path``."""
    output = _git(root, "diff", "-U0", "--no-renames", base, "HEAD", "--", path)
    hunks: list[tuple[range, range]] = []
    for match in _HUNK.finditer(output):
        old_start, old_count = int(match.group(1)), int(match.group(2) or "1")
        new_start, new_count = int(match.group(3)), int(match.group(4) or "1")
        hunks.append(
            (range(old_start, old_start + old_count), range(new_start, new_start + new_count))
        )
    return hunks


def _base_source(root: Path, base: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{base}:{path}"],
        cwd=root,
        capture_output=True,
        check=False,
        env=clean_local_git_env(),
    )
    if result.returncode:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def _module_targets(index: ImportIndex, importer: str, level: int, module: str) -> set[str]:
    if level:
        base = PurePosixPath(importer).parent
        for _ in range(level - 1):
            base = base.parent
        target = base.joinpath(*module.split(".")) if module else base
        names = {f"{target.as_posix()}.py", f"{target.as_posix()}/__init__.py"}
        return {item.removeprefix("./") for item in names} & index.paths
    folder = PurePosixPath(importer).parent.as_posix()
    return set(index.names.get(module, set())) | set(index.local.get((folder, module), set()))


class _Impact:
    """Work list from altered regions to the test files that observe them."""

    def __init__(self, texts: Mapping[str, str], receipt: Receipt, seeds: Iterable[str]) -> None:
        self.texts = texts
        self.receipt = receipt
        sources = {path: text for path, text in texts.items() if path.endswith(".py")}
        self.index = ImportIndex(set(sources) | set(seeds))
        self.importers: defaultdict[str, set[str]] = defaultdict(set)
        for path, source in sources.items():
            for dependency in self.index.dependencies(path, source):
                self.importers[dependency].add(path)
        self.trees: dict[str, ast.Module | None] = {}
        self.tests: set[str] = set()
        self.reasons: dict[str, str] = {}
        self.functions: set[tuple[str, str]] = set()
        self.classes: set[tuple[str, str]] = set()
        self.modules: set[str] = set()
        self.names: defaultdict[str, set[str]] = defaultdict(set)

    def tree(self, path: str) -> ast.Module | None:
        if path not in self.trees:
            try:
                self.trees[path] = ast.parse(self.texts[path]) if path in self.texts else None
            except (SyntaxError, ValueError):
                self.trees[path] = None
        return self.trees[path]

    def run(self, path: str, owners: Iterable[str], why: str) -> None:
        for owner in owners:
            if owner not in self.tests:
                self.tests.add(owner)
                self.reasons[owner] = why

    def apply(self, path: str, regions: change_regions.Regions) -> None:
        if regions.module:
            self.module(path)
        for qualname in regions.functions:
            self.function(path, qualname, cached=qualname in regions.cached)
        for qualname in regions.classes:
            self.klass(path, qualname)
        if regions.names:
            self.rebound(path, regions.names)

    def function(self, path: str, qualname: str, *, cached: bool = False) -> None:
        key = (path, qualname)
        if key in self.functions:
            return
        self.functions.add(key)
        if key in self.receipt.everywhere:
            raise FullSuiteRequiredError(f"{path}:{qualname} runs for every test")
        self.run(path, self.receipt.by_key.get(key, ()), f"runs {path}:{qualname}")
        if cached or key in self.receipt.import_time:
            self.rebound(path, {qualname.rsplit(".", 1)[-1]})

    def klass(self, path: str, qualname: str) -> None:
        if (path, qualname) in self.classes:
            return
        self.classes.add((path, qualname))
        for member, owners in self.receipt.by_path.get(path, {}).items():
            if member == qualname or member.startswith(qualname + "."):
                if (path, member) in self.receipt.everywhere:
                    raise FullSuiteRequiredError(f"{path}:{member} runs for every test")
                self.run(path, owners, f"runs {path}:{member}")
        self.rebound(path, {qualname.split(".", 1)[0]})

    def module(self, path: str) -> None:
        if path in self.modules:
            return
        self.modules.add(path)
        if (path, MODULE) in self.receipt.everywhere:
            raise FullSuiteRequiredError(f"{path} is imported for every test")
        for owners in self.receipt.by_path.get(path, {}).values():
            self.run(path, owners, f"runs code in {path}")
        closed: set[str] = set()
        queue = deque([path])
        while queue:
            current = queue.popleft()
            if current in closed:
                continue
            closed.add(current)
            queue.extend(self.importers.get(current, ()))
        self.run(path, (item for item in closed if is_test_file(item)), f"imports {path}")

    def rebound(self, path: str, names: set[str]) -> None:
        fresh = names - self.names[path]
        if not fresh:
            return
        self.names[path] |= fresh
        tree = self.tree(path)
        if tree is None:
            if path in self.texts:
                self.module(path)
            return
        self.apply(path, change_regions.analyze(tree, _reader_lines(tree, fresh)))
        for importer in sorted(self.importers.get(path, ())):
            importer_tree = self.tree(importer)
            if importer_tree is None:
                self.module(importer)
                continue
            aliases: set[str] = set()
            for node in ast.walk(importer_tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if path not in _module_targets(self.index, importer, node.level, node.module or ""):
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        aliases |= fresh
                    elif alias.name in fresh:
                        aliases.add(alias.asname or alias.name)
            if aliases:
                self.rebound(importer, aliases)
            lines = {
                node.lineno
                for node in ast.walk(importer_tree)
                if isinstance(node, ast.Attribute) and node.attr in fresh
            }
            if lines:
                self.apply(importer, change_regions.analyze(importer_tree, lines))

    def named_by(self, path: str, tree: ast.Module, needle: str) -> None:
        """Code in ``path`` that mentions ``needle`` (a data file's name)."""
        lines = {
            number
            for number, line in enumerate(self.texts[path].splitlines(), start=1)
            if needle in line
        }
        self.apply(path, change_regions.analyze(tree, lines))


def _reader_lines(tree: ast.Module, names: set[str]) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in names and not isinstance(node.ctx, ast.Store):
            lines.add(node.lineno)
        if isinstance(node, ast.Attribute) and node.attr in names:
            lines.add(node.lineno)
    return lines


def select(
    root: Path,
    changed: Sequence[str],
    *,
    base: str,
    receipt_path: Path | None,
    test_seconds: Mapping[str, float] | None = None,
    default_seconds: float = 1.0,
) -> Selection:
    for path in changed:
        name = PurePosixPath(path).name
        if path in FULL_SUITE_PATHS or path.startswith(FULL_SUITE_PREFIXES):
            return Selection(True, f"{path} is a shared test input")
        if name in FULL_SUITE_NAMES:
            return Selection(True, f"{path} configures every test")
    if receipt_path is None or not receipt_path.is_file():
        return Selection(True, "no test impact receipt for the base commit")
    try:
        receipt = load_receipt(receipt_path, base)
    except FullSuiteRequiredError as exc:
        return Selection(True, str(exc))
    tracked = [item for item in _git(root, "ls-files", "-z").split("\0") if item]
    texts = _read_texts(root, tracked)
    tests = sorted(path for path in texts if is_test_file(path))
    references = ReferenceIndex(texts)
    for path in changed:
        data = not path.endswith(".py")
        if data and path.startswith(TEST_ROOTS) and data_referrers(references, path)[1] is None:
            return Selection(True, f"{path} is test data that no file or folder name reaches")
    impact = _Impact(texts, receipt, changed)
    try:
        for path in changed:
            _changed_file(root, base, path, impact, references)
    except FullSuiteRequiredError as exc:
        return Selection(True, str(exc))
    why = dict(impact.reasons)
    for path in changed:
        for referrer in references.referrers(path):
            if is_test_file(referrer):
                why.setdefault(referrer, f"names {path}")
            elif not referrer.endswith(".py"):
                # A test that checks a ledger against the tree sees the change.
                for item in references.referrers(referrer):
                    if is_test_file(item):
                        why.setdefault(item, f"names {referrer}, which names {path}")
        if is_test_file(path) and path in texts:
            why.setdefault(path, "changed")
    for path in repository_scanners(texts):
        why.setdefault(path, "scans the repository")
    for path in tests:
        if path not in receipt.tests:
            why.setdefault(path, "not in the receipt")
    chosen = set(why) & set(tests)
    seconds = test_seconds or {}
    total = sum(seconds.get(path, default_seconds) for path in tests)
    picked = sum(seconds.get(path, default_seconds) for path in chosen)
    if total and picked > MAX_SELECTED_SHARE * total:
        return Selection(True, f"selection covers {picked / total:.0%} of test time")
    return Selection(
        False,
        f"{len(chosen)} of {len(tests)} test files",
        tuple(sorted(chosen)),
        {path: why[path] for path in sorted(chosen)},
    )


def _changed_file(
    root: Path, base: str, path: str, impact: _Impact, references: ReferenceIndex
) -> None:
    if not path.endswith(".py"):
        referrers, needle = data_referrers(references, path)
        for referrer in sorted(referrers):
            tree = impact.tree(referrer) if referrer.endswith(".py") else None
            if tree is not None and needle is not None:
                impact.named_by(referrer, tree, needle)
        return
    head = impact.texts.get(path)
    before = _base_source(root, base, path)
    if before is None:
        return  # A new module: only the tests that name or import it, all changed.
    if head is None:
        impact.module(path)
        return
    try:
        base_tree = ast.parse(before)
        head_tree = ast.parse(head)
    except (SyntaxError, ValueError):
        impact.module(path)
        return
    before_tokens = change_regions.code_tokens(before)
    head_tokens = change_regions.code_tokens(head)
    base_lines: set[int] = set()
    head_lines: set[int] = set()
    for old, new in diff_hunks(root, base, path):
        if not change_regions.same_code(before_tokens, head_tokens, old, new):
            base_lines.update(old)
            head_lines.update(new)
    regions = change_regions.changed_regions(
        base_tree,
        head_tree,
        base_lines & change_regions.code_lines(before),
        head_lines & change_regions.code_lines(head),
    )
    impact.apply(path, regions)


def data_referrers(references: ReferenceIndex, path: str) -> tuple[set[str], str | None]:
    """Files that name a data file, or else its nearest named folder, and that name.

    Code often reads fixtures through their folder (``FIXTURES / "tracker_v1"``),
    so a file nobody names is reached through the closest folder somebody does.
    """
    found = references.referrers(path)
    if found:
        return found, PurePosixPath(path).name
    for folder in PurePosixPath(path).parents:
        name = folder.name
        if not name or name in GENERIC_FOLDERS:
            continue
        found = {item for item in references.tokens.get(name, set()) if item != path}
        if found:
            return found, name
    return set(), None


def changed_paths(root: Path, base: str) -> list[str]:
    output = _git(root, "diff", "--name-only", "--no-renames", "-z", base, "HEAD", "--")
    return sorted({item for item in output.split("\0") if item})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--base", required=True, help="commit the change is measured from")
    parser.add_argument("--receipt", type=Path, help="test impact receipt of the base commit")
    parser.add_argument("--durations-file", type=Path)
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    seconds: dict[str, float] = {}
    default = 1.0
    try:
        base = _git(root, "rev-parse", "--verify", f"{args.base}^{{commit}}").strip()
        if args.durations_file:
            payload = json.loads(args.durations_file.read_text(encoding="utf-8"))
            default = float(payload["default_seconds"])
            seconds = {path: float(row["seconds"]) for path, row in payload["files"].items()}
        selection = select(
            root,
            changed_paths(root, base),
            base=base,
            receipt_path=args.receipt,
            test_seconds=seconds,
            default_seconds=default,
        )
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"FULL\tselection unavailable: {exc}")
        return 0
    if selection.full:
        print(f"FULL\t{selection.reason}")
        return 0
    print(f"SELECTED\t{selection.reason}")
    for path in selection.files:
        print(path)
        print(f"  {path}: {selection.why.get(path, '')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
