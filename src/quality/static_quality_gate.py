"""Enforce exact, descending Pyright and suppression ceilings by subsystem."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import cast

from quality.changed_suppressions import ChangedSuppressionError, suppression_findings
from quality.git_env import clean_local_git_env

_NON_RETAINED_PREFIXES = (
    "alembic/versions/",
    "alembic/versions_archived/",
    "scratch/",
)


class StaticQualityGateError(RuntimeError):
    """The gate could not establish complete, trustworthy evidence."""


def subsystem(path: str) -> str:
    """Return the stable quality bucket for a repository-relative Python path."""
    parts = list(PurePosixPath(path).parts)
    if not parts:
        raise StaticQualityGateError("empty Python path")
    if parts[0] == "src" and len(parts) > 2:
        return "/".join(parts[:2])
    return parts[0]


def _tracked_python_files(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            env=clean_local_git_env(),
        )
    except (OSError, UnicodeError) as exc:
        raise StaticQualityGateError("unable to inventory tracked Python files") from exc
    if result.returncode:
        raise StaticQualityGateError(f"git ls-files failed ({result.returncode})")
    retained: list[str] = []
    for path in sorted({item for item in result.stdout.split("\0") if item}):
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or ".." in candidate.parts or re.match(r"^[A-Za-z]:/", path):
            raise StaticQualityGateError("tracked Python path escapes the repository")
        if path.startswith(_NON_RETAINED_PREFIXES):
            continue
        target = root.joinpath(*candidate.parts)
        try:
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise StaticQualityGateError(f"tracked Python file is missing: {path}") from exc
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise StaticQualityGateError(f"tracked Python file escapes the repository: {path}")
        retained.append(path)
    return retained


def _ceiling_map(value: object, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise StaticQualityGateError(f"{label} must be an object")
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise StaticQualityGateError(f"{label} keys must be strings")
    if not all(
        isinstance(count, int) and not isinstance(count, bool) and count >= 0
        for count in mapping.values()
    ):
        raise StaticQualityGateError(f"{label} values must be non-negative integers")
    return {cast(str, key): cast(int, count) for key, count in mapping.items()}


def load_ceilings(path: Path) -> tuple[dict[str, int], dict[str, int]]:
    try:
        return _parse_ceilings(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StaticQualityGateError("unable to read static-quality ceilings") from exc


def _parse_ceilings(text: str) -> tuple[dict[str, int], dict[str, int]]:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise StaticQualityGateError("unsupported static-quality ceiling schema")
    payload_map = cast(dict[str, object], payload)
    if payload_map.get("schema_version") != "bha-105.v1":
        raise StaticQualityGateError("unsupported static-quality ceiling schema")
    return (
        _ceiling_map(payload_map.get("pyright_diagnostics"), "pyright_diagnostics"),
        _ceiling_map(payload_map.get("suppressions"), "suppressions"),
    )


def load_base_ceilings(
    root: Path, config_path: Path, base: str
) -> tuple[dict[str, int], dict[str, int]]:
    """Read the immutable merge-base limits, never the proposed replacement."""
    if not base.strip() or base.startswith("-"):
        raise StaticQualityGateError("base revision is invalid")
    try:
        relative = config_path.resolve().relative_to(root).as_posix()
        ancestor = subprocess.run(
            ["git", "merge-base", base, "HEAD"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            env=clean_local_git_env(),
        )
        if ancestor.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", ancestor.stdout.strip()):
            raise StaticQualityGateError("unable to resolve static-quality comparison base")
        result = subprocess.run(
            ["git", "show", f"{ancestor.stdout.strip()}:{relative}"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            env=clean_local_git_env(),
        )
        if result.returncode:
            raise StaticQualityGateError("comparison base has no static-quality ceilings")
        return _parse_ceilings(result.stdout)
    except (OSError, UnicodeError, ValueError) as exc:
        raise StaticQualityGateError(
            "unable to read comparison-base static-quality ceilings"
        ) from exc


def compare_descending(
    label: str, previous: Mapping[str, int], proposed: Mapping[str, int]
) -> list[str]:
    """New subsystems start at zero; existing budgets can only decrease."""
    return [
        f"{bucket}: {label} ceiling increased from {previous.get(bucket, 0)} to {count}"
        for bucket, count in sorted(proposed.items())
        if count > previous.get(bucket, 0)
    ]


def _relative_diagnostic_path(root: Path, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise StaticQualityGateError("Pyright diagnostic is missing a file path")
    candidate = Path(value)
    try:
        resolved = (
            candidate.resolve(strict=False)
            if candidate.is_absolute()
            else (root / candidate).resolve(strict=False)
        )
        return resolved.relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError) as exc:
        raise StaticQualityGateError("Pyright diagnostic escapes the repository") from exc


def parse_pyright_payload(
    root: Path,
    payload: object,
    retained: Sequence[str],
    aliases: Mapping[str, str] | None = None,
) -> dict[str, int]:
    if not isinstance(payload, dict):
        raise StaticQualityGateError("malformed Pyright JSON")
    payload_map = cast(dict[str, object], payload)
    diagnostics = payload_map.get("generalDiagnostics")
    summary = payload_map.get("summary")
    if not isinstance(diagnostics, list) or not isinstance(summary, dict):
        raise StaticQualityGateError("malformed Pyright JSON")
    diagnostic_rows = cast(list[object], diagnostics)
    summary_map = cast(dict[str, object], summary)
    files_analyzed = summary_map.get("filesAnalyzed")
    if files_analyzed != len(retained):
        raise StaticQualityGateError(
            f"Pyright analyzed {files_analyzed!r} files; expected all {len(retained)} retained files"
        )
    retained_set = set(retained)
    path_aliases = aliases or {}
    counts: Counter[str] = Counter()
    for raw in diagnostic_rows:
        if not isinstance(raw, dict):
            raise StaticQualityGateError("malformed Pyright diagnostic")
        row = cast(dict[str, object], raw)
        relative = _relative_diagnostic_path(root, row.get("file"))
        relative = path_aliases.get(relative, relative)
        if relative not in retained_set:
            raise StaticQualityGateError(f"Pyright reported a non-retained file: {relative}")
        counts[subsystem(relative)] += 1
    return dict(counts)


def compare_exact(
    retained: Sequence[str],
    expected_pyright: Mapping[str, int],
    expected_suppressions: Mapping[str, int],
    actual_pyright: Mapping[str, int],
    actual_suppressions: Mapping[str, int],
) -> list[str]:
    buckets = {subsystem(path) for path in retained}
    violations: list[str] = []
    for label, expected in (
        ("pyright", expected_pyright),
        ("suppressions", expected_suppressions),
    ):
        configured = set(expected)
        if configured != buckets:
            missing = sorted(buckets - configured)
            stale = sorted(configured - buckets)
            if missing:
                violations.append(f"{label} ceilings missing subsystem(s): {', '.join(missing)}")
            if stale:
                violations.append(
                    f"{label} ceilings contain stale subsystem(s): {', '.join(stale)}"
                )
    for label, expected, actual in (
        ("pyright diagnostics", expected_pyright, actual_pyright),
        ("suppressions", expected_suppressions, actual_suppressions),
    ):
        for bucket in sorted(buckets):
            wanted = expected.get(bucket)
            observed = actual.get(bucket, 0)
            if wanted is None:
                continue
            if observed > wanted:
                violations.append(f"{bucket}: {label} increased from {wanted} to {observed}")
            elif observed < wanted:
                violations.append(f"{bucket}: lower {label} ceiling from {wanted} to {observed}")
    return violations


def _run_pyright(
    root: Path, pythonpath: str, retained: Sequence[str]
) -> tuple[object, dict[str, str]]:
    executable = Path(sys.executable).with_name("pyright")
    base_command = [
        str(executable) if executable.is_file() else "pyright",
        "--pythonpath",
        pythonpath,
        "--outputjson",
    ]
    hidden = [
        path for path in retained if any(part.startswith(".") for part in PurePosixPath(path).parts)
    ]
    try:
        main_result = subprocess.run(
            base_command, cwd=root, text=True, capture_output=True, check=False
        )
    except (OSError, UnicodeError) as exc:
        raise StaticQualityGateError("unable to run Pyright") from exc
    if main_result.returncode not in (0, 1):
        raise StaticQualityGateError(
            f"Pyright failed before reporting diagnostics ({main_result.returncode})"
        )
    try:
        payloads: list[object] = [json.loads(main_result.stdout)]
    except json.JSONDecodeError as exc:
        raise StaticQualityGateError("Pyright produced malformed JSON") from exc
    temporary = Path(tempfile.mkdtemp(prefix="quality_pyright_", dir=root))
    aliases: dict[str, str] = {}
    copied: list[str] = []
    for path in hidden:
        copy = temporary.joinpath(*PurePosixPath(path).parts[1:])
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root.joinpath(*PurePosixPath(path).parts), copy)
        relative_copy = copy.relative_to(root).as_posix()
        aliases[relative_copy] = path
        copied.append(relative_copy)
    try:
        hidden_result = subprocess.run(
            [*base_command, *copied], cwd=root, text=True, capture_output=True, check=False
        )
    except (OSError, UnicodeError) as exc:
        raise StaticQualityGateError("unable to run Pyright") from exc
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    if hidden_result.returncode not in (0, 1):
        raise StaticQualityGateError(
            f"Pyright failed before reporting diagnostics ({hidden_result.returncode})"
        )
    try:
        payloads.append(json.loads(hidden_result.stdout))
    except json.JSONDecodeError as exc:
        raise StaticQualityGateError("Pyright produced malformed JSON") from exc
    diagnostics: list[object] = []
    files_analyzed = 0
    for payload in payloads:
        if not isinstance(payload, dict):
            raise StaticQualityGateError("malformed Pyright JSON")
        payload_map = cast(dict[str, object], payload)
        rows = payload_map.get("generalDiagnostics")
        summary = payload_map.get("summary")
        if not isinstance(rows, list) or not isinstance(summary, dict):
            raise StaticQualityGateError("malformed Pyright JSON")
        summary_map = cast(dict[str, object], summary)
        analyzed = summary_map.get("filesAnalyzed")
        if not isinstance(analyzed, int) or isinstance(analyzed, bool):
            raise StaticQualityGateError("malformed Pyright file count")
        diagnostics.extend(cast(list[object], rows))
        files_analyzed += analyzed
    return {
        "generalDiagnostics": diagnostics,
        "summary": {"filesAnalyzed": files_analyzed},
    }, aliases


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=Path("config/static_quality_ceilings.json"))
    parser.add_argument("--pythonpath", default=sys.executable)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--pyright-json", type=Path)
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    config_path = args.config if args.config.is_absolute() else root / args.config
    try:
        retained = _tracked_python_files(root)
        expected_pyright, expected_suppressions = load_ceilings(config_path)
        prior_pyright, prior_suppressions = load_base_ceilings(root, config_path, args.base)
        violations = compare_descending("pyright diagnostics", prior_pyright, expected_pyright)
        violations.extend(
            compare_descending("suppressions", prior_suppressions, expected_suppressions)
        )
        if args.pyright_json:
            payload = json.loads(args.pyright_json.read_text(encoding="utf-8"))
            aliases: dict[str, str] = {}
        else:
            payload, aliases = _run_pyright(root, args.pythonpath, retained)
        actual_pyright = parse_pyright_payload(root, payload, retained, aliases)
        findings = suppression_findings(root, retained)
        actual_suppressions = Counter(subsystem(finding.path) for finding in findings)
        violations.extend(
            compare_exact(
                retained,
                expected_pyright,
                expected_suppressions,
                actual_pyright,
                actual_suppressions,
            )
        )
    except (
        StaticQualityGateError,
        ChangedSuppressionError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        print(f"static-quality gate failed closed: {exc}", file=sys.stderr)
        return 2
    if violations:
        for violation in violations:
            print(violation, file=sys.stderr)
        return 1
    print(
        "static-quality ceilings exact: "
        f"{len(retained)} retained files, "
        f"{sum(actual_pyright.values())} Pyright diagnostics, "
        f"{sum(actual_suppressions.values())} suppressions"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
