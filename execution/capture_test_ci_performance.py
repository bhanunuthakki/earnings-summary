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
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import cast

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from runtime.python_process import managed_python_argv  # noqa: E402
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=PROJECT_ROOT, help="Revision checkout to test.")
    parser.add_argument("--cohort", choices=("full-suite", "ci-shard"), required=True)
    parser.add_argument("--source-shard", type=int)
    parser.add_argument("--source-shards", type=int, default=8)
    parser.add_argument("--split-count", type=int, default=1)
    parser.add_argument("--split-part", type=int, default=0)
    parser.add_argument("--cache-state", choices=("cold", "warm", "unknown"), required=True)
    parser.add_argument("--receipt", required=True, help="Receipt JSON output path.")
    parser.add_argument("--fragments-dir", help="Raw fragments root; defaults to runner temp.")
    parser.add_argument(
        "--expected-population", type=Path, required=True, help="Trusted population JSON."
    )
    parser.add_argument("--expected-population-sha256", required=True)
    parser.add_argument("--trusted-harness-sha256", required=True)
    parser.add_argument("--trusted-selector-sha256", required=True)
    parser.add_argument("--trusted-plugin-sha256", required=True)
    parser.add_argument("--capture-python-sha256", required=True)
    parser.add_argument(
        "--sqlite-preload",
        help="Optional absolute SQLite shared library to validate and pass to pytest.",
    )
    parser.add_argument(
        "--sqlite-preload-sha256",
        help="Expected SHA-256 for the SQLite preload library.",
    )
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


def _validated_sqlite_preload(value: str | None, expected_sha256: str | None) -> str | None:
    """Validate one explicitly supplied SQLite preload before propagation."""
    if value is None and expected_sha256 is not None:
        raise SystemExit("SQLite preload digest requires --sqlite-preload")
    if value is None:
        return None
    if expected_sha256 is None:
        raise SystemExit("--sqlite-preload-sha256 is required with --sqlite-preload")
    if len(expected_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_sha256
    ):
        raise SystemExit("SQLite preload SHA-256 must be lowercase hexadecimal")
    if not value or os.pathsep in value:
        raise SystemExit("SQLite preload must be one absolute library path")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise SystemExit("SQLite preload must be one absolute library path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise SystemExit("SQLite preload library is not readable") from exc
    if not resolved.is_absolute() or not resolved.is_file():
        raise SystemExit("SQLite preload library must be an absolute regular file")
    observed_sha256 = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if observed_sha256 != expected_sha256:
        raise SystemExit("SQLite preload library SHA-256 does not match trusted digest")
    probe_env = {"PATH": os.environ.get("PATH", ""), "LD_PRELOAD": str(resolved)}
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sqlite3; print(sqlite3.sqlite_version)",
        ],
        capture_output=True,
        check=False,
        env=probe_env,
        text=True,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "3.53.4":
        raise SystemExit("SQLite preload library does not provide SQLite 3.53.4")
    return str(resolved)


def safe_test_environment(
    attempt_dir: Path,
    cache_state: str,
    *,
    sqlite_preload: str | None = None,
    sqlite_preload_sha256: str | None = None,
) -> dict[str, str]:
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
    validated_preload = _validated_sqlite_preload(sqlite_preload, sqlite_preload_sha256)
    if validated_preload is not None:
        environment["LD_PRELOAD"] = validated_preload
    return environment


def capture_exit_code(pytest_returncode: int, evidence_status: str) -> int:
    """Fail CI when a nominally green pytest run produced invalid evidence."""
    if pytest_returncode != 0:
        return pytest_returncode
    return 1 if evidence_status == "invalid" else 0


def redacted_output(raw: bytes) -> bytes:
    """Return UTF-8 subprocess output safe for echoing and retention."""
    return redact(raw.decode("utf-8", errors="replace")).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preload_digest(path: Path) -> str:
    """Return a stable digest for post-run preload integrity checks."""
    try:
        return _sha256(path)
    except OSError:
        return hashlib.sha256(b"missing-preload").hexdigest()


def outside_tested_root(path: Path, root: Path, *, label: str) -> Path:
    """Resolve a private path and reject anything inside the tested checkout."""
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return resolved
    raise SystemExit(f"{label} must resolve outside the tested repository")


def _safe_test_path(root: Path, value: str) -> str:
    """Validate one canonical, repository-relative test path."""
    if not value or "\\" in value:
        raise SystemExit("trusted test population contains an unsafe path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
        or len(relative.parts) < 2
        or relative.parts[0] != "tests"
        or relative.name == ""
        or not relative.name.startswith("test_")
        or relative.suffix != ".py"
    ):
        raise SystemExit("trusted test population contains an unsafe path")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise SystemExit("trusted test population contains an outside path") from exc
    if not resolved.is_file():
        raise SystemExit("trusted expected test-file population is absent from revision")
    return value


def canonical_test_universe(root: Path) -> tuple[str, ...]:
    """Derive the tracked test universe from the tested revision's Git tree."""
    output = subprocess.check_output(
        ["git", "-C", str(root), "ls-tree", "-r", "--name-only", "HEAD", "--", "tests"],
        text=True,
    )
    universe = tuple(
        sorted(
            _safe_test_path(root, line.strip())
            for line in output.splitlines()
            if line.strip().startswith("tests/")
            and Path(line.strip()).name.startswith("test_")
            and line.strip().endswith(".py")
            and line.strip() != "tests/test_design_computed_canary.py"
        )
    )
    if not universe:
        raise SystemExit("trusted canonical test universe is empty")
    return universe


def trusted_selected_files(universe: tuple[str, ...], args: argparse.Namespace) -> tuple[str, ...]:
    """Run the trusted selector once against the canonical full universe."""
    selected = subprocess.run(
        managed_python_argv(
            PROJECT_ROOT,
            PROJECT_ROOT / ".github" / "scripts" / "ci_gate.py",
            "select-tests",
            "--source-shard",
            str(args.source_shard),
            "--source-shards",
            str(args.source_shards),
            "--split-count",
            str(args.split_count),
            "--split-part",
            str(args.split_part),
        ),
        cwd=PROJECT_ROOT,
        input=("\n".join(universe) + "\n").encode(),
        capture_output=True,
        check=False,
    )
    if selected.returncode != 0:
        raise SystemExit("canonical CI shard selection failed")
    values = tuple(line for line in selected.stdout.decode().splitlines() if line)
    if not values or any(value not in universe for value in values):
        raise SystemExit("trusted CI selector returned an unsafe population")
    return values


def trusted_plugin_loader(attempt_dir: Path, tested_root: Path) -> tuple[str, Path]:
    """Create a private pytest loader for collector-owned plugins.

    ``pytest -p`` imports by module name. A module rooted in the revision under
    test is forgeable, so the randomized loader is kept outside that checkout,
    imports the pinned collector plugin by absolute path, and then drops the
    collector ``src`` modules so tests still import the revision's source.
    """
    attempt_dir = outside_tested_root(attempt_dir, tested_root, label="private capture directory")
    attempt_dir.mkdir(parents=True, exist_ok=True)
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


def selected_files(root: Path, args: argparse.Namespace) -> tuple[str, ...]:
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
    if len(raw_files) != len(set(raw_files)):
        raise SystemExit("trusted expected test-file population is not canonical")
    raw_files = [_safe_test_path(root, value) for value in raw_files]
    files: tuple[str, ...] = tuple(raw_files)
    if files != tuple(sorted(files)):
        raise SystemExit("trusted expected test-file population is not canonical")
    try:
        observed_revision = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit("tested repository revision is unavailable") from exc
    payload_revision = payload_map.get("revision")
    if isinstance(payload_revision, str):
        if observed_revision != payload_revision:
            raise SystemExit("trusted expected population revision does not match checkout")
    else:
        payload_revisions = tuple(
            value
            for value in (
                payload_map.get("baseline_revision"),
                payload_map.get("current_revision"),
            )
            if isinstance(value, str)
        )
        if not payload_revisions or observed_revision not in payload_revisions:
            raise SystemExit("trusted expected population revision does not match checkout")
    universe = canonical_test_universe(root)
    if args.cohort == "full-suite":
        if files != universe:
            raise SystemExit("trusted full-suite population differs from canonical universe")
        return files
    expected_coordinates = {
        "source_shard": args.source_shard,
        "source_shards": args.source_shards,
        "split_count": args.split_count,
        "split_part": args.split_part,
    }
    if any(payload_map.get(name) != value for name, value in expected_coordinates.items()):
        raise SystemExit("trusted expected population shard coordinates do not match capture")
    selected = trusted_selected_files(universe, args)
    if files != selected:
        raise SystemExit("trusted expected population differs from canonical shard selection")
    return selected


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
    try:
        capture_python_sha256 = _sha256(Path(sys.executable).resolve())
    except OSError as exc:
        raise SystemExit("capture Python executable is not readable") from exc
    if args.capture_python_sha256 != capture_python_sha256:
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
    files = selected_files(root, args)
    if any(node.split("::", 1)[0] not in files for node in expected_nodes):
        raise SystemExit("trusted expected node population does not belong to selected files")
    cohort = FrozenTestCohort(
        kind=args.cohort,
        source_shard=args.source_shard,
        source_shards=args.source_shards if args.cohort == "ci-shard" else None,
        split_count=args.split_count if args.cohort == "ci-shard" else None,
        split_part=args.split_part if args.cohort == "ci-shard" else None,
        test_files=files,
    )
    attempt_id = uuid.uuid4().hex
    fragment_root = outside_tested_root(
        Path(args.fragments_dir)
        if args.fragments_dir
        else Path(tempfile.gettempdir()) / "earnings-summary-test-ci-performance",
        root,
        label="fragments directory",
    )
    attempt_dir = fragment_root / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=False)
    plugin_module, _plugin_loader = trusted_plugin_loader(attempt_dir, root)
    env = safe_test_environment(
        attempt_dir,
        args.cache_state,
        sqlite_preload=args.sqlite_preload,
        sqlite_preload_sha256=args.sqlite_preload_sha256,
    )
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
    preload_observed_sha256: str | None = None
    preload_mutated = False
    if args.sqlite_preload is not None:
        preload_observed_sha256 = preload_digest(Path(env["LD_PRELOAD"]))
        preload_mutated = preload_observed_sha256 != args.sqlite_preload_sha256
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
        execution_outcome="passed"
        if completed.returncode == 0 and not preload_mutated
        else "failed",
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
    if args.sqlite_preload is not None:
        trusted_configuration = (
            *trusted_configuration,
            ArtifactIdentity(
                path="__runtime__/sqlite-preload-expected",
                sha256=args.sqlite_preload_sha256,
            ),
            ArtifactIdentity(
                path="__runtime__/sqlite-preload-observed",
                sha256=preload_observed_sha256
                if preload_observed_sha256 is not None
                else hashlib.sha256(b"missing-preload").hexdigest(),
            ),
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
    if preload_mutated:
        receipt = receipt.model_copy(
            update={
                "evidence_status": "invalid",
                "hold_reasons": (
                    *receipt.hold_reasons,
                    "SQLite preload digest changed during capture",
                ),
            }
        )
    write_receipt(receipt, args.receipt)
    return capture_exit_code(completed.returncode, receipt.evidence_status)


if __name__ == "__main__":
    raise SystemExit(main())
