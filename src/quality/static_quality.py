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
    suppressions_by_file: dict[str, dict[str, int]] = Field(default_factory=dict)


class InventoryError(RuntimeError):
    """A contract violation; CLI prints this as structured stderr."""


InventoryFailure = InventoryError


def _run(args: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # reachability: external-process
        args, cwd=root, text=True, capture_output=True, check=False
    )


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
    except (IndentationError, tokenize.TokenError) as exc:
        raise InventoryFailure(f"unable to tokenize tracked Python source: {exc}") from exc
    return counts


def _tracked(root: Path, runner: CommandRunner) -> list[str]:
    result = runner(["git", "ls-files", "--", "*.py"], root)
    if result.returncode:
        raise InventoryFailure(f"git ls-files failed ({result.returncode})")
    paths = sorted(
        {line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()}
    )
    if any(not (root / path).is_file() for path in paths):
        raise InventoryFailure("tracked Python file is missing from the working tree")
    return paths


def _version(executable: str, root: Path, runner: CommandRunner) -> str:
    result = runner([executable, "--version"], root)
    if result.returncode or not result.stdout.strip():
        raise InventoryFailure(f"required tool unavailable: {executable}")
    return result.stdout.strip().splitlines()[0]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _executable(name: str) -> str:
    candidate = Path(sys.executable).with_name(name)
    return str(candidate) if candidate.is_file() else name


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


def _portable_diagnostic_path(root: Path, value: object) -> PurePosixPath:
    normalized = str(value or "").replace("\\", "/")
    if not normalized:
        return PurePosixPath("<unknown>")
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
        diagnostic_path = _portable_diagnostic_path(root, row.get("filename", row.get("file", "")))
        by_dir[diagnostic_path.parent.as_posix()] += 1
        rule = row.get("code", row.get("rule", "unknown"))
        by_rule[str(rule)] += 1
    return DiagnosticSummary(
        tool=tool,
        command=command,
        version=version,
        exit_status=result.returncode,
        count=len(rows),
        receipt_path=receipt.relative_to(root).as_posix(),
        diagnostics_by_directory=dict(sorted(by_dir.items())),
        diagnostics_by_rule=dict(sorted(by_rule.items())),
        command_hash=_sha("\0".join(command)),
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
        config_text = pyproject_path.read_text(encoding="utf-8") if pyproject_path.is_file() else ""
        config = cast(dict[str, object], tomllib.loads(config_text)) if config_text else {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InventoryFailure(f"malformed pyproject.toml: {exc}") from exc
    source_hash = _sha("\n".join(f"{p}\0{(root / p).read_bytes().hex()}" for p in files))
    config_hash = _sha(config_text)
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
    identity = _sha(
        "\n".join(files)
        + source_hash
        + config_hash
        + "\n".join(configured_exceptions)
        + json.dumps(versions, sort_keys=True)
    )[:24]
    receipt_dir = root / ".tmp" / "static_quality" / identity
    receipt_dir.mkdir(parents=True, exist_ok=True)
    diagnostics: list[DiagnosticSummary] = []
    for tool, command, name in specs:
        version = versions[tool]
        result = runner(command, root)
        if result.returncode not in (0, 1):
            raise InventoryFailure(
                f"{tool} command failed ({result.returncode}): {' '.join(command)}"
            )
        receipt = receipt_dir / name
        receipt.write_text(
            result.stdout + ("\n" + result.stderr if result.stderr else ""), encoding="utf-8"
        )
        if name == "ruff-format.txt":
            count = len(
                re.findall(
                    r"^(?:Would reformat:|unformatted: File would be reformatted$)",
                    result.stdout,
                    re.MULTILINE,
                )
            )
            diagnostics.append(
                DiagnosticSummary(
                    tool="ruff-format",
                    command=command,
                    version=version,
                    exit_status=result.returncode,
                    count=count,
                    receipt_path=receipt.relative_to(root).as_posix(),
                    command_hash=_sha("\0".join(command)),
                    version_hash=_sha(version),
                )
            )
        else:
            diagnostics.append(_summary(tool, command, result, version, receipt, root))
    suppressions: dict[str, dict[str, int]] = {}
    for path in files:
        content = (root / path).read_text(encoding="utf-8", errors="replace")
        counts = _suppression_counts(content)
        if any(counts.values()):
            suppressions[path] = counts
    type_ignores = sum(counts["# type: ignore"] for counts in suppressions.values())
    pyright_ignores = sum(counts["# pyright: ignore"] for counts in suppressions.values())
    ignore_receipt = receipt_dir / "source-ignore-comments.txt"
    ignore_receipt.write_text(
        f"# type: ignore: {type_ignores}\n# pyright: ignore: {pyright_ignores}\n", encoding="utf-8"
    )
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
    exclusion_map["commands"] = [" ".join(command) for _, command, _ in specs]
    excluded_active = [
        p
        for p in active
        if any(
            p == e or p.startswith(e.rstrip("/") + "/") or fnmatch.fnmatch(p, e)
            for e in exclusion_map["tool.pyright.exclude"]
        )
    ]
    if excluded_active:
        violations.append("active files excluded by Pyright: " + ", ".join(excluded_active))
    # Re-read the typed include list: files outside declared roots are hidden too.
    tool_value = config.get("tool", {})
    tool_node = cast(dict[str, object], tool_value) if isinstance(tool_value, dict) else {}
    pyright_value = tool_node.get("pyright", {})
    pyright_node = cast(dict[str, object], pyright_value) if isinstance(pyright_value, dict) else {}
    includes = pyright_node.get("include", [])
    if isinstance(includes, list):
        include_values = [str(value).rstrip("/") for value in cast(list[object], includes)]
        outside = [
            p
            for p in active
            if not any(
                p == root_name or p.startswith(root_name + "/") or fnmatch.fnmatch(p, root_name)
                for root_name in include_values
            )
        ]
        if outside:
            violations.append("active files outside Pyright include roots: " + ", ".join(outside))
    return StaticQualityInventory(
        repo_root=str(root),
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


if __name__ == "__main__":
    raise SystemExit(main())
