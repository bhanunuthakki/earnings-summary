"""Enforce exact, descending Pyright and suppression ceilings by subsystem."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from quality.changed_suppressions import ChangedSuppressionError, suppression_findings
from quality.git_env import clean_local_git_env

_NON_RETAINED_PREFIXES = (
    "alembic/versions/",
    "alembic/versions_archived/",
    # The Streamlit sandbox is an optional-dependency surface that [tool.pyright]
    # excludes, because its third-party imports are absent from the hash-pinned
    # lock this gate installs. Keeping it out of the retained population is what
    # makes the analyzed-file count and the subsystem buckets exact; its format,
    # lint, and suppression coverage still run through quality.check_changed.
    "explore-sandbox/",
    "scratch/",
)


class StaticQualityGateError(RuntimeError):
    """The gate could not establish complete, trustworthy evidence."""


class PyrightProcessError(StaticQualityGateError):
    """Retain bounded process evidence without retaining subprocess stderr."""

    def __init__(self, stage: Literal["main", "hidden"], returncode: int, stderr: str) -> None:
        super().__init__(f"Pyright failed before reporting diagnostics ({returncode})")
        self.stage = stage
        self.returncode = returncode
        markers = {
            "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory",
            "FATAL ERROR: Ineffective mark-compacts near heap limit Allocation failed - JavaScript heap out of memory",
        }
        self.reason: Literal["node_heap_exhaustion_marker", "unclassified"] = (
            "node_heap_exhaustion_marker"
            if any(line.strip() in markers for line in stderr.splitlines())
            else "unclassified"
        )


def subsystem(path: str) -> str:
    """Return the stable quality bucket for a repository-relative Python path."""
    parts = list(PurePosixPath(path).parts)
    if not parts:
        raise StaticQualityGateError("empty Python path")
    if parts[0] == "src" and len(parts) > 2:
        return "/".join(parts[:2])
    return parts[0]


def _retained_python_files(root: Path) -> list[str]:
    """Include new non-ignored source files that Pyright already analyzes locally."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.py"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            env=clean_local_git_env(),
        )
    except (OSError, UnicodeError) as exc:
        raise StaticQualityGateError("unable to inventory retained Python files") from exc
    if result.returncode:
        raise StaticQualityGateError(f"git ls-files failed ({result.returncode})")
    retained: list[str] = []
    for path in sorted({item for item in result.stdout.split("\0") if item}):
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or ".." in candidate.parts or re.match(r"^[A-Za-z]:/", path):
            raise StaticQualityGateError("retained Python path escapes the repository")
        if path.startswith(_NON_RETAINED_PREFIXES):
            continue
        target = root.joinpath(*candidate.parts)
        try:
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise StaticQualityGateError(f"retained Python file is missing: {path}") from exc
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise StaticQualityGateError(f"retained Python file escapes the repository: {path}")
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


def _diagnostic_position(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise StaticQualityGateError("diagnostic position unavailable")
    position = cast(dict[str, object], value)
    line, character = position.get("line"), position.get("character")
    if (
        not isinstance(line, int)
        or isinstance(line, bool)
        or line < 0
        or not isinstance(character, int)
        or isinstance(character, bool)
        or character < 0
    ):
        raise StaticQualityGateError("diagnostic position unavailable")
    return {"line": line, "character": character}


def failure_diagnostics_receipt(
    root: Path,
    payload: object,
    retained: Sequence[str],
    aliases: Mapping[str, str],
    config_path: Path,
) -> dict[str, object]:
    """Build a count-replayable receipt; never publish untrusted diagnostic text."""
    parse_pyright_payload(root, payload, retained, aliases)
    if not isinstance(payload, dict):
        raise StaticQualityGateError("diagnostic payload unavailable")
    payload_map = cast(dict[str, object], payload)
    raw_rows = payload_map["generalDiagnostics"]
    if not isinstance(raw_rows, list):
        raise StaticQualityGateError("diagnostic rows unavailable")
    rows: list[dict[str, object]] = []
    for raw in cast(list[object], raw_rows):
        if not isinstance(raw, dict):
            raise StaticQualityGateError("diagnostic row unavailable")
        row = cast(dict[str, object], raw)
        relative = _relative_diagnostic_path(root, row.get("file"))
        relative = aliases.get(relative, relative)
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or any(ord(c) < 32 for c in relative):
            raise StaticQualityGateError("diagnostic path unavailable")
        severity, rule, message = row.get("severity"), row.get("rule"), row.get("message")
        if not isinstance(severity, str) or severity not in {"error", "warning", "information"}:
            raise StaticQualityGateError("diagnostic severity unavailable")
        if rule is not None and not isinstance(rule, str):
            raise StaticQualityGateError("diagnostic rule unavailable")
        message_sha256 = row.get("message_sha256")
        if isinstance(message, str):
            message_sha256 = hashlib.sha256(message.encode("utf-8")).hexdigest()
        elif not isinstance(message_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", message_sha256
        ):
            raise StaticQualityGateError("diagnostic message unavailable")
        raw_range = row.get("range")
        safe_range: dict[str, dict[str, int]] | None = None
        if raw_range is not None:
            if not isinstance(raw_range, dict):
                raise StaticQualityGateError("diagnostic range unavailable")
            diagnostic_range = cast(dict[str, object], raw_range)
            start = _diagnostic_position(diagnostic_range.get("start"))
            end = _diagnostic_position(diagnostic_range.get("end"))
            if (end["line"], end["character"]) < (start["line"], start["character"]):
                raise StaticQualityGateError("diagnostic range unavailable")
            safe_range = {"start": start, "end": end}
        rule_sha256 = row.get("rule_sha256")
        if isinstance(rule, str):
            # A replayed receipt already has its rule identity, not the original text.
            if (
                rule != "hashed"
                or not isinstance(rule_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", rule_sha256)
            ):
                rule_sha256 = hashlib.sha256(rule.encode("utf-8")).hexdigest()
        else:
            rule_sha256 = None
        rows.append(
            {
                "file": relative,
                "severity": severity,
                "rule": "hashed" if rule is not None else None,
                "rule_sha256": rule_sha256,
                "range": safe_range,
                "message_sha256": message_sha256,
            }
        )
    source_hashes = {"ceilings": hashlib.sha256(config_path.read_bytes()).hexdigest()}
    for label, source in (("gate", Path(__file__)), ("pyproject", root / "pyproject.toml")):
        if source.is_file():
            source_hashes[label] = hashlib.sha256(source.read_bytes()).hexdigest()
    return {
        "schema_version": "static-quality-failure-diagnostics.v1",
        "summary": {"filesAnalyzed": len(retained)},
        "generalDiagnostics": rows,
        "metadata": {
            "source_sha256": source_hashes,
            "retained_population_sha256": hashlib.sha256(
                json.dumps(sorted(retained), separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        },
    }


def _emit_failure_diagnostics(
    root: Path,
    payload: object,
    retained: Sequence[str],
    aliases: Mapping[str, str],
    config_path: Path,
    failure: Exception | None = None,
) -> None:
    try:
        if isinstance(failure, PyrightProcessError):
            print(
                "static-quality process diagnostic: "
                + json.dumps(
                    {
                        "stage": failure.stage,
                        "returncode": failure.returncode,
                        "reason": failure.reason,
                    }
                ),
                file=sys.stderr,
            )
        receipt = failure_diagnostics_receipt(root, payload, retained, aliases, config_path)
        serialized = json.dumps(receipt, ensure_ascii=True, indent=2)
        print("static-quality diagnostics: BEGIN", file=sys.stderr)
        print(serialized, file=sys.stderr)
        print("static-quality diagnostics: END", file=sys.stderr)
    except Exception:
        # Reporting must neither expose exception payloads nor replace a failed gate result.
        with suppress(Exception):
            print("static-quality diagnostics unavailable", file=sys.stderr)


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
        raise PyrightProcessError("main", main_result.returncode, main_result.stderr)
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
        raise PyrightProcessError("hidden", hidden_result.returncode, hidden_result.stderr)
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
    parser.add_argument("--failure-diagnostics", action="store_true")
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    config_path = args.config if args.config.is_absolute() else root / args.config
    retained: list[str] = []
    payload: object = None
    aliases: dict[str, str] = {}
    population_validated = False
    try:
        retained = _retained_python_files(root)
        expected_pyright, expected_suppressions = load_ceilings(config_path)
        prior_pyright, prior_suppressions = load_base_ceilings(root, config_path, args.base)
        violations = compare_descending("pyright diagnostics", prior_pyright, expected_pyright)
        violations.extend(
            compare_descending("suppressions", prior_suppressions, expected_suppressions)
        )
        if args.pyright_json:
            payload = json.loads(args.pyright_json.read_text(encoding="utf-8"))
        else:
            payload, aliases = _run_pyright(root, args.pythonpath, retained)
        actual_pyright = parse_pyright_payload(root, payload, retained, aliases)
        population_validated = True
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
        if args.failure_diagnostics:
            print("static-quality gate failed closed: evidence unavailable", file=sys.stderr)
            _emit_failure_diagnostics(
                root, payload if population_validated else None, retained, aliases, config_path, exc
            )
        else:
            print(f"static-quality gate failed closed: {exc}", file=sys.stderr)
        return 2
    if violations:
        for violation in violations:
            print(violation, file=sys.stderr)
        if args.failure_diagnostics:
            _emit_failure_diagnostics(root, payload, retained, aliases, config_path)
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
