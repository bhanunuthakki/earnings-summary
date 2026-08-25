"""Validate the directive classification manifest without importing the app."""

from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
DIRECTIVES = ROOT / "directives"
MANIFEST = DIRECTIVES / "directive_manifest.json"
CLASSES = frozenset({"canonical", "runbook", "draft", "history"})


def _load_manifest() -> dict[str, object]:
    try:
        payload: object = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {MANIFEST.relative_to(ROOT)}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("manifest root must be an object")
    payload_dict = cast(dict[object, object], payload)
    if not all(isinstance(key, str) for key in payload_dict):
        raise ValueError("manifest keys must be strings")
    return cast(dict[str, object], payload_dict)


def validate() -> list[str]:
    errors: list[str] = []
    try:
        payload = _load_manifest()
    except ValueError as exc:
        return [str(exc)]

    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    entries_value = payload.get("directives")
    if not isinstance(entries_value, dict):
        errors.append("directives must be an object keyed by repo-relative directive path")
        return errors
    entries_dict = cast(dict[object, object], entries_value)
    if not all(isinstance(key, str) for key in entries_dict):
        errors.append("directive paths must be strings")
        return errors
    entries = cast(dict[str, object], entries_dict)

    actual = {path.relative_to(DIRECTIVES).as_posix() for path in DIRECTIVES.rglob("*.md")}
    declared = set(entries)
    for missing in sorted(actual - declared):
        errors.append(f"unclassified directive: directives/{missing}")
    for stale in sorted(declared - actual):
        errors.append(f"manifest entry has no file: directives/{stale}")

    for relative, entry_value in sorted(entries.items()):
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or pure.suffix != ".md":
            errors.append(f"unsafe or non-Markdown directive path: {relative!r}")
        if not isinstance(entry_value, dict):
            errors.append(f"{relative}: entry must be an object")
            continue
        entry = cast(dict[object, object], entry_value)
        classification = entry.get("class")
        if classification not in CLASSES:
            errors.append(
                f"{relative}: class must be one of {sorted(CLASSES)}, got {classification!r}"
            )
        summary = entry.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            errors.append(f"{relative}: summary must be a non-empty string")

    readme = DIRECTIVES / "README.md"
    try:
        readme_text = readme.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"cannot read directives/README.md: {exc}")
    else:
        if "directive_manifest.json" not in readme_text:
            errors.append("directives/README.md must link the complete manifest")
        if "four classes" not in readme_text.lower():
            errors.append("directives/README.md must explain the four directive classes")

    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"directive-manifest: {error}", file=sys.stderr)
        return 1
    entries = _load_manifest().get("directives")
    if not isinstance(entries, dict):
        print("directive-manifest: directives disappeared after validation", file=sys.stderr)
        return 1
    count = len(cast(dict[object, object], entries))
    print(f"directive-manifest: ok ({count} Markdown directives classified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
