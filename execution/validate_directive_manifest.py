"""Validate the directive classification manifest without importing the app."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
DIRECTIVES = ROOT / "directives"
MANIFEST = DIRECTIVES / "directive_manifest.json"
CLASSES = frozenset({"canonical", "runbook", "draft", "history"})
BASE_FIELDS = frozenset({"class", "summary"})
DOMAIN_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")


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

    if payload.get("schema_version") != 2:
        errors.append("schema_version must be 2")
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

    owners_by_domain: dict[str, str] = {}
    runbook_owners: dict[str, list[str]] = {}
    for relative, entry_value in sorted(entries.items()):
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or pure.suffix != ".md":
            errors.append(f"unsafe or non-Markdown directive path: {relative!r}")
        if not isinstance(entry_value, dict):
            errors.append(f"{relative}: entry must be an object")
            continue
        entry = cast(dict[object, object], entry_value)
        if not all(isinstance(key, str) for key in entry):
            errors.append(f"{relative}: entry keys must be strings")
            continue
        typed_entry = cast(dict[str, object], entry)
        classification = entry.get("class")
        if classification not in CLASSES:
            errors.append(
                f"{relative}: class must be one of {sorted(CLASSES)}, got {classification!r}"
            )
        summary = entry.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            errors.append(f"{relative}: summary must be a non-empty string")

        if classification == "canonical":
            allowed_fields = BASE_FIELDS | {"authority_domains"}
            unexpected = set(typed_entry) - allowed_fields
            if unexpected:
                errors.append(
                    f"{relative}: canonical entry has unexpected fields {sorted(unexpected)}"
                )
            domains_value = typed_entry.get("authority_domains")
            if not isinstance(domains_value, list) or not domains_value:
                errors.append(f"{relative}: canonical entry needs non-empty authority_domains")
                continue
            domains = cast(list[object], domains_value)
            if len(domains) != len(set(map(repr, domains))):
                errors.append(f"{relative}: authority_domains must not repeat values")
            for domain in domains:
                if not isinstance(domain, str) or DOMAIN_PATTERN.fullmatch(domain) is None:
                    errors.append(
                        f"{relative}: invalid authority domain {domain!r}; use lower_snake_case"
                    )
                    continue
                prior_owner = owners_by_domain.get(domain)
                if prior_owner is not None:
                    errors.append(
                        f"authority domain {domain!r} has multiple canonical owners: "
                        f"{prior_owner}, {relative}"
                    )
                else:
                    owners_by_domain[domain] = relative
        elif classification == "runbook":
            allowed_fields = BASE_FIELDS | {"governed_by"}
            unexpected = set(typed_entry) - allowed_fields
            if unexpected:
                errors.append(
                    f"{relative}: runbook entry has unexpected fields {sorted(unexpected)}"
                )
            owners_value = typed_entry.get("governed_by")
            if not isinstance(owners_value, list) or not owners_value:
                errors.append(f"{relative}: runbook entry needs non-empty governed_by")
                continue
            owner_values = cast(list[object], owners_value)
            if not all(isinstance(owner, str) for owner in owner_values):
                errors.append(f"{relative}: governed_by values must be directive paths")
                continue
            owners = cast(list[str], owner_values)
            if len(owners) != len(set(owners)):
                errors.append(f"{relative}: governed_by must not repeat paths")
            runbook_owners[relative] = owners
        elif classification in {"draft", "history"}:
            unexpected = set(typed_entry) - BASE_FIELDS
            if unexpected:
                errors.append(
                    f"{relative}: {classification} entry cannot claim authority metadata "
                    f"{sorted(unexpected)}"
                )

    for runbook, owners in sorted(runbook_owners.items()):
        for owner in owners:
            owner_entry = entries.get(owner)
            if not isinstance(owner_entry, dict) or owner_entry.get("class") != "canonical":
                errors.append(
                    f"{runbook}: governed_by target must be a canonical directive: {owner!r}"
                )

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
        if "authority_domains" not in readme_text or "governed_by" not in readme_text:
            errors.append("directives/README.md must explain authority graph metadata")

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
    print(f"directive-manifest: ok ({count} Markdown directives in the authority graph)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
