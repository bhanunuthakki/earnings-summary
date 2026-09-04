"""Capture a declared full-suite or CI-shard pytest evidence receipt."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import resource
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import cast

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.log_redact import redact  # noqa: E402
from src.quality.test_ci_performance import (  # noqa: E402
    ArtifactIdentity,
    FrozenTestCohort,
    WorkerEvidence,
    config_identity,
    receipt_from_fragments,
    write_receipt,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=PROJECT_ROOT)
    parser.add_argument("--cohort", choices=("full-suite", "ci-shard"), required=True)
    parser.add_argument("--source-shard", type=int)
    parser.add_argument("--source-shards", type=int, default=8)
    parser.add_argument("--split-count", type=int, default=1)
    parser.add_argument("--split-part", type=int, default=0)
    parser.add_argument("--cache-state", choices=("cold", "warm", "unknown"), required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--fragments-dir", default=".tmp/quality/test-ci-performance")
    parser.add_argument("--expected-population", type=Path)
    parser.add_argument("--expected-population-sha256")
    parser.add_argument("--trusted-harness-sha256")
    parser.add_argument("--trusted-selector-sha256")
    parser.add_argument("--trusted-plugin-sha256")
    parser.add_argument("--capture-python-sha256")
    return parser


def _process_peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    return value * 1024 if platform.system() != "Darwin" else value


_SAFE_PARENT_ENVIRONMENT = frozenset(
    {
        # Process/runtime discovery and temporary-file behavior.
        "PATH",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "TEMP",
        "TMP",
        "TMPDIR",
        "SYSTEMROOT",
        "SystemRoot",
        "WINDIR",
        "windir",
        "COMSPEC",
        "PATHEXT",
        # Locale and deterministic interpreter behavior.
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PYTHONHASHSEED",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "TZ",
        # Non-secret CI mode markers used by test configuration.
        "CI",
        "GITHUB_ACTIONS",
    }
)


def safe_test_environment(attempt_dir: Path, cache_state: str) -> dict[str, str]:
    """Build a least-privilege environment for untrusted revision tests.

    A revision's tests are code execution.  In particular, inheriting the
    runner's environment would make provider credentials and GitHub tokens
    available to arbitrary test code.  Keep only portable process/runtime
    variables, then set the evidence protocol variables ourselves.
    """
    environment = {
        name: value for name, value in os.environ.items() if name in _SAFE_PARENT_ENVIRONMENT
    }
    environment.update(
        {
            "TEST_CI_PERFORMANCE_FRAGMENT_DIR": str(attempt_dir),
            "TEST_CI_PERFORMANCE_CACHE_STATE": cache_state,
            "NO_NETWORK": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            # The private trusted plugin loader is the only import path added
            # by the harness; the revision remains the pytest working root.
            "PYTHONPATH": str(attempt_dir),
        }
    )
    return environment


def redacted_output(raw: bytes) -> bytes:
    """Return UTF-8 subprocess output safe for echoing and retention."""
    return redact(raw.decode("utf-8", errors="replace")).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trusted_plugin_loader(attempt_dir: Path) -> tuple[str, Path]:
    """Create a private pytest loader for collector-owned plugins.

    ``pytest -p`` imports by module name. A module rooted in the revision under
    test is forgeable, so the randomized loader is kept outside that checkout,
    imports the pinned collector plugin by absolute path, and then drops the
    collector ``src`` modules so tests still import the revision's source.
    """
    try:
        xdist_root = Path(str(importlib.metadata.distribution("pytest-xdist").locate_file("xdist")))
    except importlib.metadata.PackageNotFoundError as exc:
        raise SystemExit("pytest-xdist is required for the trusted production harness") from exc
    if not xdist_root.is_dir():
        raise SystemExit("trusted pytest-xdist package is unavailable")
    module_name = f"_bha115_trusted_plugins_{uuid.uuid4().hex}"
    loader = attempt_dir / f"{module_name}.py"
    collector_root = PROJECT_ROOT.resolve()
    collector_src = collector_root / "src"
    xdist_init = (xdist_root / "__init__.py").resolve()
    source = f"""from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path


def _load(name: str, path: Path, *, package: bool = False) -> object:
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(path.parent)] if package else None
    )
    if spec is None or spec.loader is None:
        raise ImportError(f\"cannot load trusted plugin {{name}}\")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load(\"xdist\", Path({str(xdist_init)!r}), package=True)
_xdist_plugin = importlib.import_module(\"xdist.plugin\")
_collector_prefix = \"_bha115_collector_src\"
_collector_src = Path({str(collector_src)!r})
_collector_quality = _collector_src / \"quality\"
for _name, _path in ((_collector_prefix, _collector_src),
                     (_collector_prefix + \".quality\", _collector_quality)):
    _package = types.ModuleType(_name)
    _package.__path__ = [str(_path)]
    sys.modules[_name] = _package
_load(_collector_prefix + \".quality.test_ci_performance\",
      _collector_quality / \"test_ci_performance.py\")
_performance_plugin = _load(
    _collector_prefix + \".quality.pytest_performance_plugin\",
    _collector_quality / \"pytest_performance_plugin.py\",
)
for _plugin in (_xdist_plugin, _performance_plugin):
    for _name in dir(_plugin):
        if _name.startswith(\"pytest_\") or _name in {{\"worker_id\", \"testrun_uid\"}}:
            globals()[_name] = getattr(_plugin, _name)

# Hook functions retain their collector-owned globals in the private package
# while test code continues to import ``src`` from the revision.
"""
    loader.write_text(source, encoding="utf-8")
    return module_name, loader


def _selected_files(root: Path, args: argparse.Namespace) -> tuple[str, ...]:
    if args.expected_population is None:
        raise SystemExit("trusted expected test population is required")
    try:
        payload = json.loads(args.expected_population.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("trusted expected test population is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "ci-population/v1":
        raise SystemExit("trusted expected test population schema is invalid")
    payload_map = cast(dict[str, object], payload)
    raw_files_obj = payload_map.get("test_files")
    if not isinstance(raw_files_obj, list):
        raise SystemExit("trusted expected test-file population is invalid")
    raw_files_values = cast(list[object], raw_files_obj)
    if not all(isinstance(value, str) for value in raw_files_values):
        raise SystemExit("trusted expected test-file population is invalid")
    raw_files = cast(list[str], raw_files_values)
    files: tuple[str, ...] = tuple(raw_files)
    if files != tuple(sorted(set(files))):
        raise SystemExit("trusted expected test-file population is not canonical")
    if any(not (root / name).is_file() for name in files):
        raise SystemExit("trusted expected test-file population is absent from revision")
    if args.cohort == "full-suite":
        return files
    selector = PROJECT_ROOT / ".github" / "scripts" / "ci_gate.py"
    selected = subprocess.run(
        [
            sys.executable,
            str(selector),
            "select-tests",
            "--source-shard",
            str(args.source_shard),
            "--source-shards",
            str(args.source_shards),
            "--split-count",
            str(args.split_count),
            "--split-part",
            str(args.split_part),
        ],
        cwd=PROJECT_ROOT,
        input=("\n".join(files) + "\n").encode(),
        capture_output=True,
        check=False,
    )
    if selected.returncode != 0:
        raise SystemExit("canonical CI shard selection failed")
    selected_files = tuple(line for line in selected.stdout.decode().splitlines() if line)
    if selected_files != files:
        raise SystemExit("trusted CI selector disagrees with expected population")
    return selected_files


def main() -> int:
    args = _parser().parse_args()
    root = Path(args.repo_root).resolve()
    harness_sha256 = _sha256(Path(__file__).resolve())
    selector_path = PROJECT_ROOT / ".github" / "scripts" / "ci_gate.py"
    plugin_path = PROJECT_ROOT / "src" / "quality" / "pytest_performance_plugin.py"
    selector_sha256 = _sha256(selector_path)
    plugin_sha256 = _sha256(plugin_path)
    if args.trusted_harness_sha256 != harness_sha256:
        raise SystemExit("trusted capture harness identity does not match collector pin")
    if args.trusted_selector_sha256 != selector_sha256:
        raise SystemExit("trusted selector identity does not match collector pin")
    if args.trusted_plugin_sha256 != plugin_sha256:
        raise SystemExit("trusted pytest plugin identity does not match collector pin")
    if args.expected_population is None or args.expected_population_sha256 is None:
        raise SystemExit("trusted expected test population identity is required")
    try:
        capture_python_sha256 = _sha256(Path(sys.executable).resolve())
    except OSError as exc:
        raise SystemExit("capture Python executable is not readable") from exc
    if (
        args.capture_python_sha256 is not None
        and args.capture_python_sha256 != capture_python_sha256
    ):
        raise SystemExit("capture Python executable identity does not match collector pin")
    try:
        population_payload = json.loads(args.expected_population.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("trusted expected test population is unreadable") from exc
    canonical_population = json.dumps(
        population_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    if hashlib.sha256(canonical_population).hexdigest() != args.expected_population_sha256:
        raise SystemExit("trusted expected test population identity does not match collector pin")
    if (
        not isinstance(population_payload, dict)
        or population_payload.get("selector_sha256") != selector_sha256
    ):
        raise SystemExit("trusted expected test population selector identity is invalid")
    population_map = cast(dict[str, object], population_payload)
    raw_expected_nodes_obj = population_map.get("node_ids")
    raw_expected_nodes_values = (
        cast(list[object], raw_expected_nodes_obj)
        if isinstance(raw_expected_nodes_obj, list)
        else None
    )
    if raw_expected_nodes_values is None:
        raise SystemExit("trusted expected node population is required")
    if not all(isinstance(value, str) for value in raw_expected_nodes_values):
        raise SystemExit("trusted expected node population is invalid")
    raw_expected_nodes = cast(list[str], raw_expected_nodes_values)
    expected_nodes: tuple[str, ...] = tuple(raw_expected_nodes)
    if not expected_nodes:
        raise SystemExit("trusted expected node population is required")
    if expected_nodes != tuple(sorted(set(expected_nodes))):
        raise SystemExit("trusted expected node population is not canonical")
    if args.cohort == "ci-shard" and args.source_shard is None:
        raise SystemExit("--source-shard is required for ci-shard")
    if args.cohort == "full-suite" and args.source_shard is not None:
        raise SystemExit("--source-shard is only valid for ci-shard")
    files = _selected_files(root, args)
    cohort = FrozenTestCohort(
        kind=args.cohort,
        source_shard=args.source_shard,
        source_shards=args.source_shards if args.cohort == "ci-shard" else None,
        split_count=args.split_count if args.cohort == "ci-shard" else None,
        split_part=args.split_part if args.cohort == "ci-shard" else None,
        test_files=files,
    )
    attempt_id = uuid.uuid4().hex
    fragment_root = Path(args.fragments_dir).resolve()
    attempt_dir = fragment_root / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=False)
    plugin_module, _plugin_loader = _trusted_plugin_loader(attempt_dir)
    env = safe_test_environment(attempt_dir, args.cache_state)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-n",
        "2",
        "--dist=loadfile",
        "--durations=25",
        "-p",
        plugin_module,
        *files,
    ]
    started = time.perf_counter()
    initial_receipt = receipt_from_fragments(
        root,
        cohort,
        [],
        attempt_id=attempt_id,
        execution_outcome="not_run",
        cache_state=args.cache_state,
    )
    write_receipt(initial_receipt, args.receipt)
    completed = subprocess.run(command, cwd=root, env=env, capture_output=True, check=False)
    wall = time.perf_counter() - started
    safe_stdout = redacted_output(completed.stdout)
    safe_stderr = redacted_output(completed.stderr)
    sys.stdout.buffer.write(safe_stdout)
    sys.stderr.buffer.write(safe_stderr)
    (attempt_dir / "pytest.stdout").write_bytes(safe_stdout)
    (attempt_dir / "pytest.stderr").write_bytes(safe_stderr)
    fragments: list[WorkerEvidence] = []
    fragment_errors: list[str] = []
    for path in sorted(attempt_dir.glob("worker-*.json")):
        try:
            fragments.append(WorkerEvidence.model_validate_json(path.read_text()))
        except (OSError, ValidationError, ValueError) as exc:
            fragment_errors.append(f"invalid worker fragment {path.name}: {type(exc).__name__}")
    receipt = receipt_from_fragments(
        root,
        cohort,
        fragments,
        attempt_id=attempt_id,
        execution_outcome="passed" if completed.returncode == 0 else "failed",
        cache_state=args.cache_state,
        fragment_errors=tuple(fragment_errors),
    )
    trusted_configuration = (
        *receipt.configuration,
        ArtifactIdentity(path="__trusted__/capture_test_ci_performance.py", sha256=harness_sha256),
        ArtifactIdentity(path="__trusted__/.github/scripts/ci_gate.py", sha256=selector_sha256),
        ArtifactIdentity(
            path="__trusted__/src/quality/pytest_performance_plugin.py", sha256=plugin_sha256
        ),
        ArtifactIdentity(path="__runtime__/capture-python", sha256=capture_python_sha256),
    )
    receipt = receipt.model_copy(
        update={
            "configuration": trusted_configuration,
            "config_sha256": config_identity(trusted_configuration),
            "command_sha256": hashlib.sha256("\0".join(command).encode()).hexdigest(),
            "output_sha256": hashlib.sha256(safe_stdout + safe_stderr).hexdigest(),
            "process_wall_seconds": wall,
            "process_peak_rss_bytes": _process_peak_rss_bytes(),
        }
    )
    actual_nodes = tuple(sorted(node_id for worker in fragments for node_id in worker.node_ids))
    if actual_nodes != expected_nodes:
        receipt = receipt.model_copy(
            update={
                "evidence_status": "invalid",
                "hold_reasons": (
                    *receipt.hold_reasons,
                    "executed node population differs from trusted expected nodes",
                ),
            }
        )
    write_receipt(receipt, args.receipt)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
