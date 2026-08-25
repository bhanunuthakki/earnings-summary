"""Validate the top-level folder contract without importing the app."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "directives" / "folder_structure.md"
START = "<!-- folder-contract:start -->"
END = "<!-- folder-contract:end -->"


def _load_contract() -> dict[str, object]:
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    try:
        block = text.split(START, 1)[1].split(END, 1)[0].strip()
    except IndexError as exc:
        raise ValueError("folder contract markers are missing") from exc
    if not block.startswith("```json") or not block.endswith("```"):
        raise ValueError("folder contract must be a fenced JSON block")
    payload: object = json.loads(block.removeprefix("```json").removesuffix("```").strip())
    if not isinstance(payload, dict):
        raise ValueError("folder contract root must be an object")
    payload_dict = cast(dict[object, object], payload)
    if not all(isinstance(key, str) for key in payload_dict):
        raise ValueError("folder contract keys must be strings")
    return cast(dict[str, object], payload_dict)


def _string_set(payload: dict[str, object], key: str, errors: list[str]) -> set[str]:
    value_object = payload.get(key)
    if not isinstance(value_object, list):
        errors.append(f"{key} must be a list of non-empty strings")
        return set()
    value_list = cast(list[object], value_object)
    if not all(isinstance(item, str) and item for item in value_list):
        errors.append(f"{key} must be a list of non-empty strings")
        return set()
    value = cast(list[str], value_list)
    result = set(value)
    if len(result) != len(value):
        errors.append(f"{key} contains duplicates")
    return result


def _tracked_roots(errors: list[str]) -> set[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        errors.append(f"git ls-files failed: {result.stderr.strip()}")
        return set()
    return {line.split("/", 1)[0] for line in result.stdout.splitlines() if "/" in line}


def validate() -> list[str]:
    errors: list[str] = []
    try:
        payload = _load_contract()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [str(exc)]

    required_source = _string_set(payload, "required_source_directories", errors)
    required_state = _string_set(payload, "required_state_directories", errors)
    optional_runtime = _string_set(payload, "optional_runtime_directories", errors)
    tooling = _string_set(payload, "tooling_directories", errors)
    exceptions = _string_set(payload, "registered_exception_directories", errors)
    forbidden = _string_set(payload, "forbidden_top_level_directories", errors)
    registered = required_source | required_state | optional_runtime | tooling | exceptions

    overlap = (required_source & optional_runtime) | (required_state & optional_runtime)
    if overlap:
        errors.append(f"required and optional directory sets overlap: {sorted(overlap)}")
    for relative in sorted(required_source | required_state):
        if not (ROOT / relative).is_dir():
            errors.append(f"required directory is missing: {relative}")
    for relative in sorted(forbidden):
        if (ROOT / relative).exists():
            errors.append(f"forbidden top-level path exists: {relative}")

    unregistered = _tracked_roots(errors) - registered
    if unregistered:
        errors.append(f"tracked top-level directories are unregistered: {sorted(unregistered)}")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"folder-contract: {error}", file=sys.stderr)
        return 1
    print("folder-contract: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
