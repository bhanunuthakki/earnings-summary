"""Record which repository functions each test file runs, for pull-request test selection.

A main-branch CI run loads ``quality.test_impact_plugin`` with
``TEST_IMPACT_RECORD`` set to an output folder. The recorder uses
``sys.monitoring`` (Python 3.12+) to see the first call of every repository
code object in each *bucket*:

- a test file, while pytest imports it or runs its tests;
- ``<fixture>:NAME``, while a session-, package-, module- or class-scoped
  fixture sets up, because later test files reuse its value without running it;
- ``<session>``, for conftest imports and anything else outside one test file.

Each call is reported once per bucket: the callback disables the code location
and the recorder re-enables every location when the bucket changes. A function
that runs while a repository module is importing is flagged ``import_time``,
because a module that is already imported never runs it again for later tests.
A subprocess started during a test inherits the bucket name through the
environment and records into its own file (``sitecustomize`` below), including
the ``<module>`` code of every module it imports.

This module imports only the standard library, so a subprocess can load it by
path without pytest.
"""

from __future__ import annotations

import argparse
import atexit
import gzip
import json
import os
import sys
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from types import CodeType
from typing import Any, cast

ENV_DIR = "TEST_IMPACT_RECORD"
ENV_ROOT = "TEST_IMPACT_ROOT"
ENV_BUCKET = "TEST_IMPACT_BUCKET"
ENV_MODULE = "TEST_IMPACT_MODULE"
SESSION = "<session>"
FIXTURE = "<fixture>:"
MODULE = "<module>"
PROCESS_SCHEMA = "test-impact-process.v1"
RECEIPT_SCHEMA = "test-impact.v1"
# The recorder's own code is not a dependency of any test.
SELF_PATHS = frozenset({"src/quality/test_impact_recorder.py", "src/quality/test_impact_plugin.py"})
TOOL_NAME = "test-impact"
SITECUSTOMIZE = """\
import importlib.util as _util
import os as _os

if _os.environ.get("TEST_IMPACT_RECORD") and _os.environ.get("TEST_IMPACT_BUCKET"):
    _spec = _util.spec_from_file_location("_test_impact_child", _os.environ["TEST_IMPACT_MODULE"])
    if _spec is not None and _spec.loader is not None:
        _module = _util.module_from_spec(_spec)
        _spec.loader.exec_module(_module)
        _module.start_child()
"""

_monitoring: Any = getattr(sys, "monitoring", None)


class Recorder:
    """Per-process recording state; ``child`` records one inherited bucket."""

    def __init__(self, root: Path, out_dir: Path, *, child: bool, bucket: str) -> None:
        if _monitoring is None:
            raise RuntimeError("test impact recording needs Python 3.12 or later")
        self.root = os.path.join(os.path.abspath(root), "")
        self.out_dir = out_dir
        self.child = child
        self.keys: list[tuple[str, str]] = []
        self.ids: dict[tuple[str, str], int] = {}
        self.codes: dict[CodeType, int] = {}
        self.buckets: dict[str, set[int]] = {}
        self.import_time: set[int] = set()
        self.fixture_users: defaultdict[str, set[str]] = defaultdict(set)
        self.depth = 0
        self.stack: list[str] = []
        self.name = bucket
        self.current = self.buckets.setdefault(bucket, set())
        self.tool = self._claim_tool()
        events = _monitoring.events
        _monitoring.register_callback(self.tool, events.PY_START, self._on_start)
        mask = events.PY_START
        if not child:
            _monitoring.register_callback(self.tool, events.PY_RETURN, self._on_return)
            _monitoring.register_callback(self.tool, events.PY_UNWIND, self._on_unwind)
            mask |= events.PY_RETURN | events.PY_UNWIND
        _monitoring.set_events(self.tool, mask)
        os.environ[ENV_BUCKET] = bucket

    @staticmethod
    def _claim_tool() -> int:
        for tool in (4, 3, 5, 2, 1, 0):
            if _monitoring.get_tool(tool) is None:
                _monitoring.use_tool_id(tool, TOOL_NAME)
                return tool
        raise RuntimeError("no free sys.monitoring tool id")

    def stop(self) -> None:
        _monitoring.set_events(self.tool, 0)
        _monitoring.free_tool_id(self.tool)

    def _resolve(self, code: CodeType) -> int:
        filename = code.co_filename
        if not filename or filename.startswith("<"):
            return -1
        path = os.path.abspath(filename)
        if not path.startswith(self.root):
            return -1
        rel = path[len(self.root) :].replace(os.sep, "/")
        if rel in SELF_PATHS or rel.startswith((".venv/", "venv/")) or "/site-packages/" in rel:
            return -1
        key = (rel, code.co_qualname)
        found = self.ids.get(key)
        if found is None:
            found = self.ids[key] = len(self.keys)
            self.keys.append(key)
        return found

    def _on_start(self, code: CodeType, offset: int) -> object:
        del offset
        key = self.codes.get(code)
        if key is None:
            key = self.codes[code] = self._resolve(code)
        if key < 0:
            return _monitoring.DISABLE
        self.current.add(key)
        if code.co_name == MODULE:
            if not self.child:
                self.depth += 1
                # Functions already reported in this bucket may run again
                # inside this import; re-enable them so the import is seen.
                _monitoring.restart_events()
            return None
        if self.depth:
            self.import_time.add(key)
        return _monitoring.DISABLE

    def _module_exit(self, code: CodeType) -> bool:
        if code.co_name != MODULE or self.codes.get(code, -1) < 0:
            return False
        self.depth = max(0, self.depth - 1)
        return True

    def _on_return(self, code: CodeType, offset: int, value: object) -> object:
        del offset, value
        return None if self._module_exit(code) else _monitoring.DISABLE

    def _on_unwind(self, code: CodeType, offset: int, exc: BaseException) -> None:
        del offset, exc
        self._module_exit(code)

    def switch(self, bucket: str) -> None:
        if bucket == self.name:
            return
        self.name = bucket
        self.current = self.buckets.setdefault(bucket, set())
        os.environ[ENV_BUCKET] = bucket
        _monitoring.restart_events()

    def push(self, bucket: str) -> None:
        self.stack.append(self.name)
        self.switch(bucket)

    def pop(self) -> None:
        self.switch(self.stack.pop() if self.stack else SESSION)

    def payload(self) -> dict[str, object]:
        return {
            "schema": PROCESS_SCHEMA,
            "child": self.child,
            "keys": [list(key) for key in self.keys],
            "buckets": {name: sorted(ids) for name, ids in self.buckets.items() if ids},
            "import_time": sorted(self.import_time),
            "fixture_users": {name: sorted(users) for name, users in self.fixture_users.items()},
        }

    def dump(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        target = self.out_dir / f"process-{os.getpid()}-{uuid.uuid4().hex}.json"
        target.write_text(json.dumps(self.payload(), separators=(",", ":")), encoding="utf-8")


_active: Recorder | None = None


def start_child() -> None:
    """Record a subprocess into the bucket of the test that started it."""
    global _active
    if _active is not None or _monitoring is None:
        return
    out_dir, root = os.environ.get(ENV_DIR), os.environ.get(ENV_ROOT)
    bucket = os.environ.get(ENV_BUCKET)
    if not out_dir or not root or not bucket:
        return
    _active = Recorder(Path(root), Path(out_dir), child=True, bucket=bucket)
    atexit.register(_active.dump)


def start_parent(root: Path, out_dir: Path) -> Recorder:
    """Take over this process for a pytest run, discarding a child recorder."""
    global _active
    if _active is not None:
        _active.stop()
    os.environ[ENV_ROOT] = os.path.abspath(root)
    os.environ[ENV_MODULE] = os.path.abspath(__file__)
    _active = Recorder(root, out_dir, child=False, bucket=SESSION)
    return _active


def is_test_file(path: str) -> bool:
    pure = PurePosixPath(path)
    return path.startswith("tests/") and pure.name.startswith("test_") and pure.suffix == ".py"


def merge(payloads: Iterable[Mapping[str, Any]], commit: str) -> dict[str, object]:
    """One receipt: for each key, the test files whose run executed it."""
    keys: list[tuple[str, str]] = []
    ids: dict[tuple[str, str], int] = {}
    users: defaultdict[str, set[str]] = defaultdict(set)
    loaded: list[tuple[list[int], Mapping[str, Any]]] = []
    for payload in payloads:
        if payload.get("schema") != PROCESS_SCHEMA:
            raise ValueError("unsupported process record")
        local: list[int] = []
        for raw in cast(list[list[str]], payload["keys"]):
            key = (raw[0], raw[1])
            if key not in ids:
                ids[key] = len(keys)
                keys.append(key)
            local.append(ids[key])
        for name, files in cast(dict[str, list[str]], payload["fixture_users"]).items():
            users[name].update(files)
        loaded.append((local, payload))
    tests = sorted(
        {
            name
            for _, payload in loaded
            for name in cast(dict[str, list[int]], payload["buckets"])
            if is_test_file(name)
        }
    )
    test_ids = {path: index for index, path in enumerate(tests)}
    runs: defaultdict[int, set[int]] = defaultdict(set)
    everywhere: set[int] = set()
    import_time: set[int] = set()
    for local, payload in loaded:
        import_time.update(local[item] for item in cast(list[int], payload["import_time"]))
        for name, items in cast(dict[str, list[int]], payload["buckets"]).items():
            if name.startswith(FIXTURE):
                owners = {
                    test_ids[path] for path in users[name[len(FIXTURE) :]] if path in test_ids
                }
            elif name in test_ids:
                owners = {test_ids[name]}
            else:
                everywhere.update(local[item] for item in items)
                continue
            for item in items:
                runs[local[item]].update(owners)
    return {
        "schema": RECEIPT_SCHEMA,
        "commit": commit,
        "tests": tests,
        "keys": [list(key) for key in keys],
        "runs": {str(key): sorted(owners) for key, owners in sorted(runs.items())},
        "import_time": sorted(import_time),
        "everywhere": sorted(everywhere),
    }


def _merge_command(args: argparse.Namespace) -> int:
    files = sorted(path for folder in args.inputs for path in Path(folder).rglob("process-*.json"))
    if not files:
        print("no process records found", file=sys.stderr)
        return 1
    payloads = [
        cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8"))) for path in files
    ]
    receipt = merge(payloads, args.commit)
    with gzip.open(args.out, "wt", encoding="utf-8") as handle:
        json.dump(receipt, handle, separators=(",", ":"))
    print(
        f"test impact receipt: {len(cast(list[str], receipt['tests']))} test files, "
        f"{len(cast(list[object], receipt['keys']))} keys, {len(files)} processes"
    )
    return 0


def _sitecustomize_command(args: argparse.Namespace) -> int:
    folder = Path(args.folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    merge_parser = commands.add_parser("merge", help="merge process records into a receipt")
    merge_parser.add_argument("--commit", required=True)
    merge_parser.add_argument("--out", type=Path, required=True)
    merge_parser.add_argument("inputs", nargs="+")
    merge_parser.set_defaults(handler=_merge_command)
    site_parser = commands.add_parser("sitecustomize", help="write the subprocess hook")
    site_parser.add_argument("folder")
    site_parser.set_defaults(handler=_sitecustomize_command)
    args = parser.parse_args(argv)
    return cast(int, args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
