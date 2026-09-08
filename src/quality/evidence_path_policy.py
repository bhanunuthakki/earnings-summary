"""Canonical receipt/generator path policy (sole authority)."""

from __future__ import annotations

from pathlib import PurePosixPath

FREEZE_PATH = "docs/quality/roadmap-freeze.json"


def is_canonical_receipt_path(value: str) -> bool:
    if not value or value.startswith("/") or "\\" in value:
        return False
    try:
        parts = PurePosixPath(value).parts
    except Exception:
        return False
    if len(parts) < 3 or value != str(PurePosixPath(value)):
        return False
    if parts[0] != "docs" or parts[1] != "quality":
        return False
    if any(p in ("..", ".", "") for p in parts):
        return False
    if ".." in value.split("/"):
        return False
    return value.endswith(".json")


def is_canonical_generator_path(value: str) -> bool:
    if not value or value.startswith("/") or "\\" in value:
        return False
    try:
        parts = PurePosixPath(value).parts
    except Exception:
        return False
    if value != str(PurePosixPath(value)):
        return False
    if any(p in ("..", ".", "") for p in parts):
        return False
    if ".." in value.split("/"):
        return False
    if not value.endswith(".py"):
        return False
    return value.startswith("src/quality/") or value.startswith("execution/")


def _sanitize_key(key: str) -> str:
    return key.replace(".", "-").replace("/", "-")


def admission_path_for(kind: str, key: str) -> str:
    if kind == "block":
        return f"docs/quality/admission-block-{_sanitize_key(key)}.json"
    return f"docs/quality/admission-gate-{_sanitize_key(key)}.json"
