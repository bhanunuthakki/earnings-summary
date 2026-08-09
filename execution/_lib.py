"""Small shared substrate for deterministic ``execution`` entrypoints.

New CLIs use this module instead of growing another project-root calculation,
database-path parser, or ad-hoc JSON logger. Existing scripts can migrate
mechanically without changing their business logic.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from db_paths import resolve_db_path  # noqa: E402
from log_redact import redact  # noqa: E402


def _safe_log_value(value: object) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        return {redact(str(key)): _safe_log_value(item) for key, item in mapping.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        sequence = cast("Sequence[object]", value)
        return [_safe_log_value(item) for item in sequence]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


def log_event(event: str, **fields: object) -> None:
    """Emit one redacted structured event to stderr."""
    payload: dict[str, object] = {"event": redact(event)}
    payload.update({redact(key): _safe_log_value(value) for key, value in fields.items()})
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), file=sys.stderr)


def standard_parser(
    description: str,
    *,
    ticker: bool = False,
    force: bool = False,
    mutation_mode: bool = False,
) -> argparse.ArgumentParser:
    """Create the common typed CLI surface used by execution scripts."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--db-path", type=Path)
    if ticker:
        parser.add_argument("--ticker", type=str)
    if force:
        parser.add_argument("--force", action="store_true")
    if mutation_mode:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true")
        mode.add_argument("--dry-run", action="store_true")
    return parser


__all__ = [
    "PROJECT_ROOT",
    "SRC_ROOT",
    "log_event",
    "resolve_db_path",
    "standard_parser",
]
