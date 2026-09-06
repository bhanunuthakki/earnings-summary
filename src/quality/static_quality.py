"""Inventory tracked Python code and reproduce the repository static-quality debt.

This is intentionally a read-only, deterministic audit.  It never changes source
files; command output is retained as an evidence receipt under ``.tmp``.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import platform
import re
import subprocess
import sys
import tokenize
import tomllib
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

MAX_EXCEPTIONS = 3
MAX_STDOUT_BYTES = 100_000
PROJECT_PYTHON_TOKEN = "<project-python>"
CommandRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


class DiagnosticSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    command: list[str]
    version: str
    exit_status: int
    count: int
    receipt_path: str
    diagnostics_by_directory: dict[str, int] = Field(default_factory=dict)
    diagnostics_by_rule: dict[str, int] = Field(default_factory=dict)
    command_hash: str
    version_hash: str


class RuntimeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    implementation: str
    python_version: str
    platform: str
    machine: str


class StaticQualityInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "bha-120.v2"
    repo_root: str
    tracked_python_files: int
    active: list[str]
    immutable_historical_migration: list[str]
    generated_declarative_exception: list[str]
    diagnostics: list[DiagnosticSummary]
    current_exclusions: dict[str, list[str]]
    retirement_candidates: list[str] = Field(default_factory=list)
    status: Literal["PASS", "HOLD"] = "PASS"
    violations: list[str] = Field(default_factory=list)
    scoped_commit: str
    source_hash: str
    config_hash: str
    receipt_identity: str
    runtime: RuntimeIdentity
    suppressions_by_file: dict[str, dict[str, int]] = Field(default_factory=dict)


class InventoryError(RuntimeError):
    """A contract violation; CLI prints this as structured stderr."""


InventoryFailure = InventoryError


def _run(args: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # reachability: external-process
            args, cwd=root, text=True, capture_output=True, check=False
        )
    except (OSError, UnicodeError) as exc:
        raise InventoryFailure(f"unable to run required tool: {Path(args[0]).name}") from exc


def _suppression_counts(content: str) -> dict[str, int]:
    """Count suppression directives only in lexical comments, never strings."""
    counts = {"# type: ignore": 0, "# pyright: ignore": 0}
    try:
        tokens = tokenize.generate_tokens(io.StringIO(content).readline)
        for token in tokens:
            if token.type != tokenize.COMMENT:
                continue
            for directive in counts:
                counts[directive] += token.string.count(directive)
    except (IndentationError, SyntaxError, tokenize.TokenError) as exc:
        raise InventoryFailure(f"unable to tokenize tracked Python source: {exc}") from exc
    return counts


def _tracked_file(root: Path, relative: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InventoryFailure(f"tracked Python file is missing or unreadable: {relative}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise InventoryFailure(f"tracked Python file escapes the repository: {relative}")
    return candidate


def _tracked(root: Path, runner: CommandRunner) -> list[str]:
    result = runner(["git", "ls-files", "-z", "--", "*.py"], root)
    if result.returncode:
        raise InventoryFailure(f"git ls-files failed ({result.returncode})")
    paths = sorted({path for path in result.stdout.split("\0") if path})
    if any(
        PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
        or re.match(r"^[A-Za-z]:/", path)
        for path in paths
    ):
        raise InventoryFailure("tracked Python path escapes the repository")
    for path in paths:
        _tracked_file(root, path)
    return paths


def _version(executable: str, root: Path, runner: CommandRunner) -> str:
    result = runner([executable, "--version"], root)
    if result.returncode or not result.stdout.strip():
        raise InventoryFailure(f"required tool unavailable: {executable}")
    return result.stdout.strip().splitlines()[0]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scanner_input_hashes(root: Path, files: Sequence[str], config_text: str) -> tuple[str, str]:
    digest = hashlib.sha256()
    for path in files:
        encoded_path = path.encode("utf-8")
        try:
            content = _tracked_file(root, path).read_bytes()
        except OSError as exc:
            raise InventoryFailure(f"unable to read tracked Python file: {path}") from exc
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest(), _sha(config_text)


def scanner_input_hashes(repo_root: Path, runner: CommandRunner = _run) -> tuple[str, str]:
    """Return the source/config hashes that identify a static-quality scan.

    This intentionally performs only the cheap input discovery and hashing
    portion of :func:`inventory`; it never invokes Ruff or Pyright.  Keep the
    calculation shared so validation cannot silently diverge from receipt
    generation (notably for historical migration Python files).
    """
    root = repo_root.resolve()
    files = _tracked(root, runner)
    try:
        config_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except FileNotFoundError:
        config_text = ""
    except (OSError, UnicodeDecodeError) as exc:
        raise InventoryFailure(f"unable to read pyproject.toml: {exc}") from exc
    return _scanner_input_hashes(root, files, config_text)


def _executable(name: str) -> str:
    candidate = Path(sys.executable).with_name(name)
    return str(candidate) if candidate.is_file() else name


def _logical_command(command: Sequence[str]) -> list[str]:
    """Return portable command identity without changing the executed argv."""
    logical: list[str] = []
    for index, value in enumerate(command):
        if value == sys.executable:
            logical.append(PROJECT_PYTHON_TOKEN)
        elif index == 0:
            logical.append(Path(value).name)
        else:
            logical.append(value)
    return logical


def _runtime_identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        implementation=platform.python_implementation(),
        python_version=platform.python_version(),
        platform=sys.platform,
        machine=platform.machine(),
    )


def _matches_config_path(path: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/").rstrip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    return bool(
        normalized
        and (
            path == normalized
            or path.startswith(normalized + "/")
            or fnmatch.fnmatch(path, normalized)
        )
    )


def _json_diagnostics(raw: str, tool: str) -> list[dict[str, object]]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InventoryFailure(f"malformed {tool} machine-readable output: {exc}") from exc
    if tool == "ruff":
        if not isinstance(payload, list):
            raise InventoryFailure("malformed ruff JSON output")
        raw_rows = cast(list[object], payload)
        if not all(isinstance(row, dict) for row in raw_rows):
            raise InventoryFailure("malformed ruff JSON output")
        return cast(list[dict[str, object]], cast(list[object], payload))
    if not isinstance(payload, dict) or not isinstance(payload.get("generalDiagnostics"), list):
        raise InventoryFailure("malformed pyright JSON output")
    rows = cast(list[object], payload["generalDiagnostics"])
    if not all(isinstance(row, dict) for row in rows):
        raise InventoryFailure("malformed pyright diagnostics")
    return cast(list[dict[str, object]], rows)


def _portable_diagnostic_path(root: Path, value: str) -> PurePosixPath:
    normalized = value
    if not normalized:
        return PurePosixPath("<unknown>")
    if re.match(r"^[A-Za-z]:[\\/]", normalized):
        normalized = normalized.replace("\\", "/")
    root_text = root.resolve().as_posix().rstrip("/")
    if normalized.startswith(root_text + "/"):
        normalized = normalized[len(root_text) + 1 :]
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or re.match(r"^[A-Za-z]:/", normalized) or ".." in candidate.parts:
        return PurePosixPath("<external>") / (candidate.name or "<unknown>")
    return candidate


def _summary(
    tool: str,
    command: list[str],
    result: subprocess.CompletedProcess[str],
    version: str,
    receipt: Path,
    root: Path,
) -> DiagnosticSummary:
    rows = _json_diagnostics(result.stdout, tool) if tool in {"ruff", "pyright"} else []
    by_dir: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    for row in rows:
        path_value = row.get("filename", row.get("file", ""))
        if not isinstance(path_value, str):
            raise InventoryFailure(f"malformed {tool} diagnostic path")
        diagnostic_path = _portable_diagnostic_path(root, path_value)
        by_dir[diagnostic_path.parent.as_posix()] += 1
        rule = row.get("code", row.get("rule", "unknown"))
        if not isinstance(rule, str):
            raise InventoryFailure(f"malformed {tool} diagnostic rule")
        by_rule[rule] += 1
    logical_command = _logical_command(command)
    return DiagnosticSummary(
        tool=tool,
        command=logical_command,
        version=version,
        exit_status=result.returncode,
        count=len(rows),
        receipt_path=receipt.relative_to(root).as_posix(),
        diagnostics_by_directory=dict(sorted(by_dir.items())),
        diagnostics_by_rule=dict(sorted(by_rule.items())),
        command_hash=_sha("\0".join(logical_command)),
        version_hash=_sha(version),
    )


def inventory(
    repo_root: Path, runner: CommandRunner = _run, exception_paths: Sequence[str] = ()
) -> StaticQualityInventory:
    root = repo_root.resolve()
    configured_exceptions = tuple(path.replace("\\", "/") for path in exception_paths)
    violations: list[str] = []
    if len(configured_exceptions) > MAX_EXCEPTIONS or len(set(configured_exceptions)) != len(
        configured_exceptions
    ):
        violations.append("generated/declarative exceptions exceed hard cap or overlap")
    files = _tracked(root, runner)
    missing_exceptions = sorted(set(configured_exceptions) - set(files))
    if missing_exceptions:
        violations.append("configured exception is not tracked: " + ", ".join(missing_exceptions))
    migrations = [
        p for p in files if p.startswith(("alembic/versions/", "alembic/versions_archived/"))
    ]
    exceptions = [p for p in files if p in configured_exceptions]
    retirement = [p for p in files if p.startswith("scratch/")]
    active = [
        p for p in files if p not in migrations and p not in exceptions and p not in retirement
    ]
    partitions = (set(active), set(migrations), set(exceptions), set(retirement))
    if set().union(*partitions) != set(files) or any(
        left & right for index, left in enumerate(partitions) for right in partitions[index + 1 :]
    ):
        violations.append("static inventory partition is incomplete or overlapping")
    pyproject_path = root / "pyproject.toml"
    try:
        config_text = pyproject_path.read_text(encoding="utf-8")
        config = cast(dict[str, object], tomllib.loads(config_text)) if config_text else {}
    except FileNotFoundError:
        config_text = ""
        config = {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise InventoryFailure(f"malformed pyproject.toml: {exc}") from exc
    source_hash, config_hash = _scanner_input_hashes(root, files, config_text)
    specs = [
        ("ruff", [_executable("ruff"), "check", "--output-format", "json", "."], "ruff-check.json"),
        ("ruff", [_executable("ruff"), "format", "--check", "."], "ruff-format.txt"),
        (
            "pyright",
            [_executable("pyright"), "--pythonpath", sys.executable, "--outputjson"],
            "pyright.json",
        ),
    ]
    versions: dict[str, str] = {}
    for tool, command, _name in specs:
        if tool not in versions:
            versions[tool] = _version(command[0], root, runner)
    runtime = _runtime_identity()
    logical_specs = [_logical_command(command) for _, command, _ in specs]
    runtime_json = json.dumps(runtime.model_dump(), sort_keys=True, separators=(",", ":"))
    identity = _sha(
        "\n".join(files)
        + source_hash
        + config_hash
        + "\n".join(configured_exceptions)
        + json.dumps(versions, sort_keys=True)
        + runtime_json
        + json.dumps(logical_specs, sort_keys=True)
    )[:24]
    receipt_dir = root / ".tmp" / "static_quality" / identity
    try:
        receipt_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InventoryFailure("unable to create static-quality receipt directory") from exc
    diagnostics: list[DiagnosticSummary] = []
    for tool, command, name in specs:
        version = versions[tool]
        result = runner(command, root)
        if result.returncode not in (0, 1):
            raise InventoryFailure(
                f"{tool} command failed ({result.returncode}): {' '.join(command)}"
            )
        receipt = receipt_dir / name
        try:
            receipt.write_text(
                result.stdout + ("\n" + result.stderr if result.stderr else ""),
                encoding="utf-8",
            )
        except OSError as exc:
            raise InventoryFailure(f"unable to write static-quality receipt: {name}") from exc
        if name == "ruff-format.txt":
            count = len(
                re.findall(
                    r"^(?:Would reformat:|unformatted: File would be reformatted$)",
                    result.stdout,
                    re.MULTILINE,
                )
            )
            logical_command = _logical_command(command)
            diagnostics.append(
                DiagnosticSummary(
                    tool="ruff-format",
                    command=logical_command,
                    version=version,
                    exit_status=result.returncode,
                    count=count,
                    receipt_path=receipt.relative_to(root).as_posix(),
                    command_hash=_sha("\0".join(logical_command)),
                    version_hash=_sha(version),
                )
            )
        else:
            diagnostics.append(_summary(tool, command, result, version, receipt, root))
    suppressions: dict[str, dict[str, int]] = {}
    for path in files:
        try:
            content = _tracked_file(root, path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise InventoryFailure(f"unable to decode tracked Python file: {path}") from exc
        counts = _suppression_counts(content)
        if any(counts.values()):
            suppressions[path] = counts
    type_ignores = sum(counts["# type: ignore"] for counts in suppressions.values())
    pyright_ignores = sum(counts["# pyright: ignore"] for counts in suppressions.values())
    ignore_receipt = receipt_dir / "source-ignore-comments.txt"
    try:
        ignore_receipt.write_text(
            f"# type: ignore: {type_ignores}\n# pyright: ignore: {pyright_ignores}\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise InventoryFailure("unable to write suppression receipt") from exc
    diagnostics.append(
        DiagnosticSummary(
            tool="source-ignore-comments",
            command=["tracked-source-scan"],
            version="n/a",
            exit_status=0,
            count=type_ignores + pyright_ignores,
            receipt_path=ignore_receipt.relative_to(root).as_posix(),
            diagnostics_by_rule={
                "# type: ignore": type_ignores,
                "# pyright: ignore": pyright_ignores,
            },
            command_hash=_sha("tracked-source-scan"),
            version_hash=_sha("n/a"),
        )
    )
    exclusion_map: dict[str, list[str]] = {}
    for section in ("tool.ruff", "tool.pyright", "tool.basedpyright"):
        node: object = config
        for part in section.split("."):
            mapping: dict[str, object] = (
                {str(key): value for key, value in cast(dict[object, object], node).items()}
                if isinstance(node, dict)
                else {}
            )
            node = mapping.get(part, {})
        excluded: list[str] = []
        included: list[str] = []
        if isinstance(node, dict):
            for key in ("exclude", "extend-exclude"):
                if isinstance(node.get(key), list):
                    excluded.extend(str(item) for item in cast(list[object], node[key]))
            if isinstance(node.get("include"), list):
                included.extend(str(item) for item in cast(list[object], node["include"]))
        exclusion_map[f"{section}.exclude"] = sorted(set(excluded))
        exclusion_map[f"{section}.include"] = sorted(set(included))
    exclusion_map["commands"] = [" ".join(command) for command in logical_specs]
    for section, label in (("tool.ruff", "Ruff"), ("tool.pyright", "Pyright")):
        excluded_active = [
            path
            for path in active
            if any(
                _matches_config_path(path, pattern)
                for pattern in exclusion_map[f"{section}.exclude"]
            )
        ]
        if excluded_active:
            violations.append(f"active files excluded by {label}: " + ", ".join(excluded_active))
    # Re-read the typed include list: files outside declared roots are hidden too.
    tool_value = config.get("tool", {})
    tool_node = cast(dict[str, object], tool_value) if isinstance(tool_value, dict) else {}
    pyright_value = tool_node.get("pyright", {})
    pyright_node = cast(dict[str, object], pyright_value) if isinstance(pyright_value, dict) else {}
    includes = pyright_node.get("include", [])
    if isinstance(includes, list) and includes:
        include_values = [str(value).rstrip("/") for value in cast(list[object], includes)]
        outside = [
            p
            for p in active
            if not any(_matches_config_path(p, root_name) for root_name in include_values)
        ]
        if outside:
            violations.append("active files outside Pyright include roots: " + ", ".join(outside))
    return StaticQualityInventory(
        repo_root=".",
        tracked_python_files=len(files),
        active=active,
        immutable_historical_migration=migrations,
        generated_declarative_exception=exceptions,
        diagnostics=diagnostics,
        current_exclusions=exclusion_map,
        retirement_candidates=retirement,
        status="HOLD" if violations else "PASS",
        violations=violations,
        scoped_commit=_git_commit(root, runner),
        source_hash=source_hash,
        config_hash=config_hash,
        receipt_identity=identity,
        runtime=runtime,
        suppressions_by_file=suppressions,
    )


def _git_commit(root: Path, runner: CommandRunner) -> str:
    result = runner(["git", "rev-parse", "HEAD"], root)
    if result.returncode or not result.stdout.strip():
        raise InventoryFailure("unable to determine scoped git commit")
    return result.stdout.strip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--exception-path", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = inventory(args.repo_root, exception_paths=args.exception_path)
        payload = result.model_dump_json(indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
            print(json.dumps({"output": str(args.output), "status": result.status}))
        elif len(payload.encode("utf-8")) > MAX_STDOUT_BYTES:
            output = (
                args.repo_root.resolve()
                / ".tmp"
                / "static_quality"
                / result.receipt_identity
                / "inventory.json"
            )
            output.write_text(payload, encoding="utf-8")
            print(json.dumps({"output": str(output), "status": result.status}))
        else:
            sys.stdout.write(payload)
        return 0 if result.status == "PASS" else 2
    except InventoryFailure as exc:
        print(
            json.dumps({"error": "static_quality_inventory_failed", "message": str(exc)}),
            file=sys.stderr,
        )
        return 2
    except OSError:
        print(
            json.dumps(
                {
                    "error": "static_quality_inventory_failed",
                    "message": "unable to write static-quality inventory output",
                }
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
